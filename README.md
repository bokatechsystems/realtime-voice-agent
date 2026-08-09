# Realtime Voice Agent

A bidirectional voice loop for phone calls: **Twilio Media Streams → STT → LLM → TTS → back**, at a latency that carries a natural conversation.

The signal path is the easy part — a few hours of work. This repository is about the problem waiting behind it, the one most prototypes trip over: **when is it safe to open the microphone again?**

---

https://github.com/bokatechsystems/realtime-voice-agent/raw/main/demo.mp4

## What this is, and what it isn't

A **reference implementation**, extracted from a phone assistant running in production and reduced to the technically interesting core. The business logic of the production system — appointment booking, CRM integration, escalation handling, conversation management — is deliberately **not** included.

What is here works. What isn't here isn't claimed.

---

## The problem

A phone line is a single channel. Your own output comes back as input — through the caller's speaker, through network echo, through the conference bridge on the other end.

With no countermeasure, this happens:

```
Agent:  "Hello, how can I help you?"
STT:    "hello how can i help you"          ← its own voice
LLM:    "Sure! What can I help you with?"
STT:    "sure what can i help you with"     ← and again
```

The call spirals into a loop without the caller having said a word.

### The obvious fix, and why it doesn't hold

The first instinct is: close the microphone while the agent speaks, reopen it when the TTS stream ends.

This fails because the end of the TTS stream is **not** the moment the caller hears the sentence. Between the last audio chunk sent and playback at the caller's ear sit the telephony platform's buffers and network latency — anywhere from a few hundred milliseconds to well over a second, depending on the connection.

Release on stream end and the microphone is open **while the caller is still hearing your output**. That is exactly where the feedback loop starts. The bug is especially nasty because it never shows up in local testing: latency there is too small to open the window.

### The fix: playback receipts as the anchor

Twilio supports a `mark` frame. Place one behind an audio block and Twilio reports it back **as soon as playback reaches that point**. That is the only reliable proof the caller actually heard the sentence.

So the microphone opens only when **no unacknowledged mark is outstanding** — and even then, only if nothing else argues against it:

| Condition | If ignored |
|---|---|
| All marks acknowledged | Feedback — the caller is still hearing the agent |
| Speech queue empty | Opens in the gap between two sentences |
| No TTS stream active | Race condition when more audio is pushed |
| LLM not generating | Opens while the next sentence is already forming |
| Not tearing down the call | The closing sentence gets cut off |

Every row in that table was a bug in the field once. That's why `check_release()` returns the **reason** alongside the decision rather than a bare `True`/`False` — it makes field logs readable.

### Barge-in: the same logic, inverted

An assistant you can't interrupt is unusable. But treat every incoming transcript as an interruption and you get an agent that never finishes a sentence — because some of those transcripts are its own voice.

Three hurdles, in this order:

1. **The agent must be speaking** — otherwise there is nothing to interrupt.
2. **Minimum length.** "yes", "mhm", "right" are backchannel signals, not interruptions.
3. **Not echo.** Two criteria, because STT never returns echo word-for-word: a contiguous subsequence of the agent's own recent output, *or* word overlap above a threshold (default 0.75), measured over a rolling window of roughly the last 120 spoken words.

One detail that was expensive to learn: on barge-in, outstanding marks **must** be discarded. They refer to audio Twilio will never play after the `clear` frame — so the receipts never arrive. Without flushing, the gate waits for them and the microphone stays shut for the rest of the call.

---

## Architecture

```
                    ┌──────────────────────────────────┐
    Caller ────────►│ Twilio Media Streams  (mulaw 8k) │
        ◄───────────└────────────────┬─────────────────┘
                                     │  WebSocket
                          ┌──────────▼──────────┐
                          │      app.py         │
                          │  FastAPI / asyncio  │
                          └──┬───────────────┬──┘
                media (in)   │               │   media + mark (out)
                        ┌────▼────┐     ┌────▼────┐
                        │   STT   │     │   TTS   │
                        │streaming│     │streaming│
                        └────┬────┘     └────▲────┘
                             │               │  per sentence
                        ┌────▼───────────────┴────┐
                        │   LLM  (streaming)      │
                        └─────────────────────────┘

              Every state decision: audio_gate.py
```

### Why output goes out sentence by sentence

Wait for the complete LLM response and every reply opens with a pause as long as the entire generation. Instead, the token stream is cut at sentence boundaries and each finished sentence goes straight into the speech queue. Output starts as soon as the **first** sentence is ready.

This produces several marks per response — and with it the requirement to wait for *all* of them, not just the last.

### Further safeguards

- **Loop detection.** An n-gram detector catches immediate repetition of word sequences, the classic signature of an LLM loop. Calibrated so that natural doubling in spoken language ("very, very happy to") doesn't trigger a false positive.
- **First-chunk watchdog.** If the first TTS chunk never arrives, the caller sits in complete silence — worse for them than any error message. A timeout triggers handling.
- **Sentence dedup** within a single response round.
- **Cooldown** after the last receipt, against line reverb. Transcripts accumulated during that window are discarded: whatever came in while the agent was speaking was echo.

---

## Files

| File | Contents |
|---|---|
| `audio_gate.py` | Microphone state gate, echo detection, barge-in, loop detection. No external dependencies, no network I/O — the entire decision logic is pure, and therefore testable. |
| `app.py` | FastAPI application: TwiML webhook, WebSocket loop, provider wiring, sentence-wise output. |
| `tests/test_audio_gate.py` | 24 tests. Each documents a failure that actually occurred in the field — the test names describe the symptom, not the method. |
| `docs/barge-in.md` | Sequence diagrams: normal turn-taking, echo rejection, barge-in. |
| `config.example.json` | Every threshold in one place. Configuration-driven: no code change for different parameters. |

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json    # adjust thresholds and prompt

export PUBLIC_HOST=your-domain.example.com
export DEEPGRAM_API_KEY=...
export OPENAI_API_KEY=...
export ELEVENLABS_API_KEY=...

uvicorn app:app --host 0.0.0.0 --port 8000
```

In the Twilio phone number configuration, point the voice webhook at `https://$PUBLIC_HOST/voice` (POST).

Credentials come exclusively from the environment. `config.json` holds no secrets and is excluded via `.gitignore`.

> The sample config runs the assistant in German — that is the language of the system this was extracted from. Change `language`, `greeting` and `system_prompt` to run it in any other language; nothing in the gate logic is language-specific.

### Tests

```bash
pytest -q
```

The gate is fully testable without a phone line, without API keys and without network access. That was the reason to lift the decision logic out of the I/O layer: a state bug reproducible only on a real call costs several minutes and a phone connection per iteration.

---

## Stack

| Layer | Used |
|---|---|
| Telephony | Twilio Media Streams (WebSocket, mulaw 8 kHz) |
| STT | Deepgram Nova-3, streaming, with VAD events |
| LLM | OpenAI GPT-4o, streaming |
| TTS | ElevenLabs Flash v2.5, streaming, mulaw 8 kHz |
| Runtime | Python 3.11+, FastAPI, Uvicorn, asyncio |

Provider calls are deliberately thin. The gate logic is provider-agnostic — it assumes only that the telephony platform emits a playback receipt.

---

## License

MIT — see [LICENSE](LICENSE).

---

*German version: [README.de.md](README.de.md)*
