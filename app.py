"""
app.py - Referenzimplementierung einer bidirektionalen Sprachschleife
         ueber Twilio Media Streams.

Signalweg:

    Anrufer -> Twilio -> WebSocket (mulaw 8 kHz)
            -> STT (Deepgram, streaming)
            -> LLM (OpenAI, streaming, satzweise abgegriffen)
            -> TTS (ElevenLabs, streaming, mulaw 8 kHz)
            -> WebSocket -> Twilio -> Anrufer

Die eigentliche Schwierigkeit liegt nicht im Signalweg, sondern in der
Frage, WANN das Mikrofon geoeffnet werden darf. Diese Logik steckt
vollstaendig in audio_gate.py und ist dort isoliert getestet.

Die Provider-Aufrufe hier sind absichtlich duenn gehalten und gegen die
jeweiligen SDKs austauschbar.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import uuid

from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents
from elevenlabs import VoiceSettings
from elevenlabs.client import AsyncElevenLabs
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from openai import AsyncOpenAI

from audio_gate import GateConfig, MicrophoneGate, detect_repetition_loop

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("voice-agent")

with open(os.getenv("CONFIG_PATH", "config.json"), encoding="utf-8") as fh:
    CFG = json.load(fh)

PUBLIC_HOST = os.environ["PUBLIC_HOST"]          # z. B. agent.example.com
LANGUAGE = CFG["language"]                        # z. B. "de"
SYSTEM_PROMPT = CFG["system_prompt"]
GREETING = CFG["greeting"]
COOLDOWN_S = CFG["gate"]["echo_cooldown_seconds"]
TTS_FIRST_CHUNK_TIMEOUT_S = CFG["timeouts"]["tts_first_chunk_seconds"]

dg = DeepgramClient(os.environ["DEEPGRAM_API_KEY"])
llm = AsyncOpenAI(api_key=os.environ["OPENAI_API_KEY"])
tts = AsyncElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])

app = FastAPI()

SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/voice")
async def voice(request: Request) -> Response:
    """Twilio-Webhook: uebergibt den Anruf an den WebSocket."""
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect>"
        f'<Stream url="wss://{PUBLIC_HOST}/stream" />'
        "</Connect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


# ─────────────────────────────────────────────────────────────
# Ausgabeseite: TTS -> Twilio, mit Wiedergabe-Quittung
# ─────────────────────────────────────────────────────────────

async def speak(sentence: str, ws: WebSocket, stream_sid: str,
                gate: MicrophoneGate) -> None:
    """Einen Satz sprechen und einen Mark setzen.

    Der Mark ist der Kern des Ganzen: Twilio meldet ihn zurueck, sobald die
    Wiedergabe diesen Punkt erreicht hat. Erst diese Quittung - nicht das Ende
    des TTS-Streams - beweist, dass der Anrufer den Satz gehoert hat.
    """
    gate.state.tts_streaming = True
    try:
        stream = tts.text_to_speech.stream(
            voice_id=CFG["tts"]["voice_id"],
            model_id=CFG["tts"]["model_id"],
            output_format="ulaw_8000",
            text=sentence,
            voice_settings=VoiceSettings(**CFG["tts"]["voice_settings"]),
        )
        chunks = stream.__aiter__()

        # Watchdog: bleibt der erste Chunk aus, haengt der Anrufer in
        # vollstaendiger Stille - schlimmer als jede Fehlermeldung.
        try:
            first = await asyncio.wait_for(
                chunks.__anext__(), timeout=TTS_FIRST_CHUNK_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.error("TTS: erster Chunk ausgeblieben (%.1fs)",
                      TTS_FIRST_CHUNK_TIMEOUT_S)
            return
        except StopAsyncIteration:
            return

        async def send(chunk: bytes) -> None:
            if chunk:
                await ws.send_text(json.dumps({
                    "event": "media",
                    "streamSid": stream_sid,
                    "media": {"payload": base64.b64encode(chunk).decode()},
                }))

        await send(first)
        async for chunk in chunks:
            await send(chunk)

        mark = f"tts-{uuid.uuid4().hex[:8]}"
        gate.register_mark(mark)
        try:
            await ws.send_text(json.dumps({
                "event": "mark",
                "streamSid": stream_sid,
                "mark": {"name": mark},
            }))
        except Exception:
            gate.confirm_mark(mark)   # nie gesendet - sonst wartet das Gate ewig
            raise
    finally:
        gate.state.tts_streaming = False


async def speaker_worker(queue: asyncio.Queue, ws: WebSocket,
                         session: dict) -> None:
    """Serialisiert die Ausgabe: immer genau ein Satz zur Zeit."""
    gate: MicrophoneGate = session["gate"]
    while True:
        sentence = await queue.get()
        gate.state.speak_queue_size = queue.qsize()
        gate.hold()
        gate.remember_output(sentence)
        task = asyncio.create_task(
            speak(sentence, ws, session["stream_sid"], gate))
        session["tts_task"] = task
        try:
            await task
        except asyncio.CancelledError:
            log.info("Ausgabe unterbrochen (barge-in).")
        except Exception as exc:
            log.error("Ausgabefehler: %s", exc)
        finally:
            session["tts_task"] = None
            gate.state.speak_queue_size = queue.qsize()
            queue.task_done()
            await try_release(session)


async def try_release(session: dict) -> None:
    """Mikrofon oeffnen, sobald das Gate es erlaubt - nach kurzem Cooldown.

    Der Cooldown faengt den Nachhall ab, der nach der letzten Quittung noch
    auf der Leitung liegt. Angesammelte Transkripte werden dabei verworfen:
    was waehrend der eigenen Ausgabe hereinkam, war Echo.
    """
    gate: MicrophoneGate = session["gate"]
    decision = gate.check_release()
    if not decision:
        log.debug("Mikrofon bleibt zu: %s", decision.reason)
        return

    old = session.get("release_task")
    if old and not old.done():
        old.cancel()

    async def _release() -> None:
        try:
            await asyncio.sleep(COOLDOWN_S)
            if not gate.check_release():
                return
            session["pending_transcript"] = ""
            gate.release()
            log.info("Mikrofon offen.")
        except asyncio.CancelledError:
            pass

    session["release_task"] = asyncio.create_task(_release())


async def barge_in(ws: WebSocket, queue: asyncio.Queue, session: dict) -> None:
    """Der Anrufer unterbricht: sofort verstummen."""
    gate: MicrophoneGate = session["gate"]
    log.info("Barge-in.")
    session["cancel_generation"] = True
    try:
        await ws.send_text(json.dumps(
            {"event": "clear", "streamSid": session["stream_sid"]}))
    except Exception as exc:
        log.error("clear fehlgeschlagen: %s", exc)

    task = session.get("tts_task")
    if task and not task.done():
        task.cancel()
    while not queue.empty():
        queue.get_nowait()
        queue.task_done()
    gate.barge_in()


# ─────────────────────────────────────────────────────────────
# LLM-Runde: satzweise in die Sprechwarteschlange
# ─────────────────────────────────────────────────────────────

async def llm_turn(text: str, queue: asyncio.Queue, session: dict) -> None:
    """Antwort streamen und satzweise abgeben, statt auf das Ende zu warten.

    Ohne satzweise Abgabe entsteht am Anfang jeder Antwort eine Pause in der
    Laenge der gesamten Generierung. Mit ihr beginnt die Ausgabe, sobald der
    erste Satz fertig ist.
    """
    gate: MicrophoneGate = session["gate"]
    history = session["history"]
    history.append({"role": "user", "content": text})
    session["cancel_generation"] = False
    gate.state.llm_generating = True
    buffer, spoken = "", []

    try:
        stream = await llm.chat.completions.create(
            model=CFG["llm"]["model"],
            temperature=CFG["llm"]["temperature"],
            max_tokens=CFG["llm"]["max_reply_tokens"],
            messages=[{"role": "system", "content": SYSTEM_PROMPT}, *history],
            stream=True,
        )
        async for event in stream:
            if session["cancel_generation"]:
                break
            delta = event.choices[0].delta.content or ""
            if not delta:
                continue
            buffer += delta
            parts = SENTENCE_END.split(buffer)
            buffer = parts.pop()
            for sentence in parts:
                sentence = sentence.strip()
                if not sentence or sentence in spoken:
                    continue          # Satz-Dedup gegen LLM-Schleifen
                if detect_repetition_loop(sentence):
                    log.warning("Schleife erkannt, Runde abgebrochen.")
                    session["cancel_generation"] = True
                    break
                spoken.append(sentence)
                await queue.put(sentence)
                gate.state.speak_queue_size = queue.qsize()

        tail = buffer.strip()
        if tail and not session["cancel_generation"] and tail not in spoken:
            spoken.append(tail)
            await queue.put(tail)
            gate.state.speak_queue_size = queue.qsize()
    except Exception as exc:
        log.error("LLM-Fehler: %s", exc)
    finally:
        gate.state.llm_generating = False

    if spoken:
        history.append({"role": "assistant", "content": " ".join(spoken)})
    await try_release(session)


# ─────────────────────────────────────────────────────────────
# WebSocket: Twilio <-> STT
# ─────────────────────────────────────────────────────────────

@app.websocket("/stream")
async def stream(ws: WebSocket) -> None:
    await ws.accept()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    session = {
        "gate": MicrophoneGate(GateConfig(**CFG["gate"]["thresholds"])),
        "stream_sid": None,
        "history": [],
        "pending_transcript": "",
        "tts_task": None,
        "release_task": None,
        "cancel_generation": False,
    }
    gate: MicrophoneGate = session["gate"]
    speaker = asyncio.create_task(speaker_worker(queue, ws, session))
    dg_conn = None

    def on_transcript(_self, result, **_kw) -> None:
        """Laeuft im Deepgram-Thread - jede Koroutine muss ueber
        run_coroutine_threadsafe in die Event-Loop zurueck."""
        try:
            text = result.channel.alternatives[0].transcript
        except Exception:
            return
        if not text:
            return

        if gate.state.assistant_speaking:
            if gate.should_barge_in(text):
                asyncio.run_coroutine_threadsafe(
                    barge_in(ws, queue, session), loop)
            else:
                log.info("Echo verworfen: %s", text)
            return

        if not getattr(result, "is_final", False):
            return
        session["pending_transcript"] = (
            f"{session['pending_transcript']} {text}".strip())
        if getattr(result, "speech_final", False):
            sentence = session["pending_transcript"]
            session["pending_transcript"] = ""
            log.info("Anrufer: %s", sentence)
            asyncio.run_coroutine_threadsafe(
                llm_turn(sentence, queue, session), loop)

    try:
        while True:
            packet = json.loads(await ws.receive_text())
            event = packet.get("event")

            if event == "start":
                session["stream_sid"] = packet["start"]["streamSid"]
                dg_conn = dg.listen.live.v("1")
                dg_conn.on(LiveTranscriptionEvents.Transcript, on_transcript)
                dg_conn.start(LiveOptions(
                    model=CFG["stt"]["model"],
                    language=LANGUAGE,
                    encoding="mulaw",
                    sample_rate=8000,
                    smart_format=True,
                    interim_results=True,
                    vad_events=True,
                    endpointing=CFG["stt"]["endpointing_ms"],
                    utterance_end_ms=CFG["stt"]["utterance_end_ms"],
                ))
                await queue.put(GREETING)

            elif event == "media" and dg_conn:
                dg_conn.send(base64.b64decode(packet["media"]["payload"]))

            elif event == "mark":
                # Die Wiedergabe-Quittung. Der einzige verlaessliche Beweis,
                # dass der Anrufer den Satz tatsaechlich gehoert hat.
                gate.confirm_mark(packet["mark"]["name"])
                await try_release(session)

            elif event == "stop":
                break
    except Exception as exc:
        log.info("Verbindung beendet: %s", exc)
    finally:
        gate.state.call_ending = True
        speaker.cancel()
        if dg_conn:
            dg_conn.finish()
