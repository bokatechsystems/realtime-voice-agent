# Realtime Voice Agent — Referenzimplementierung

Bidirektionale Sprachschleife für Telefonanrufe: **Twilio Media Streams → STT → LLM → TTS → zurück**, mit einer Latenz, die ein natürliches Gespräch trägt.

Der Schwerpunkt dieses Repositories liegt nicht auf dem Signalweg — der ist in wenigen Stunden gebaut — sondern auf dem Problem, das dahinter wartet und über das die meisten Prototypen stolpern: **wann darf das Mikrofon wieder öffnen?**

---

## Was das ist, und was nicht

Dies ist eine **Referenzimplementierung**, aus einem produktiv laufenden Telefonassistenten herausgelöst und auf den technisch interessanten Kern reduziert. Die Geschäftslogik des Produktivsystems — Terminbuchung, CRM-Anbindung, Notfallprotokoll, Mehrsprachigkeit, Gesprächsführung — ist bewusst **nicht** enthalten.

Was enthalten ist, funktioniert. Was nicht enthalten ist, wird hier auch nicht behauptet.

---

## Das Problem

Eine Telefonleitung ist ein einziger Kanal. Die eigene Ausgabe kommt als Eingabe zurück — über den Lautsprecher des Anrufers, über Netz-Echo, über die Konferenzbrücke der Gegenseite.

Ohne Gegenmaßnahme passiert Folgendes:

```
Agent:   "Guten Tag, wie kann ich Ihnen helfen?"
STT:     "guten tag wie kann ich ihnen helfen"     ← die eigene Stimme
LLM:     "Gerne! Womit können Sie mir helfen?"
STT:     "gerne womit können sie mir helfen"       ← und wieder
```

Das Gespräch läuft in eine Endlosschleife, ohne dass der Anrufer ein Wort gesagt hat.

### Der naheliegende Ansatz — und warum er nicht trägt

Der erste Reflex ist: Mikrofon zu, während der Agent spricht, und beim Ende des TTS-Streams wieder auf.

Das schlägt fehl, weil das Ende des TTS-Streams **nicht** der Moment ist, in dem der Anrufer den Satz hört. Zwischen dem letzten gesendeten Audio-Chunk und der Wiedergabe am Ohr des Anrufers liegen die Puffer und die Netzlaufzeit der Telefonieplattform — je nach Verbindung einige hundert Millisekunden bis über eine Sekunde.

Gibt man bei Stream-Ende frei, ist das Mikrofon offen, **während der Anrufer die eigene Ausgabe noch hört**. Die Rückkopplung beginnt genau dort. Der Fehler ist besonders unangenehm, weil er im lokalen Test nie auftritt: dort ist die Latenz zu klein, um das Fenster zu öffnen.

### Die Lösung: die Wiedergabe-Quittung als Anker

Twilio kennt einen `mark`-Frame. Setzt man ihn hinter einen Audio-Block, meldet Twilio ihn zurück, **sobald die Wiedergabe diesen Punkt erreicht hat**. Das ist der einzige verlässliche Beweis, dass der Anrufer den Satz tatsächlich gehört hat.

Das Mikrofon öffnet daher erst, wenn **kein unquittierter Mark mehr offen ist** — und auch dann nur, wenn keine weitere Bedingung dagegen spricht:

| Bedingung | Wenn ignoriert |
|---|---|
| Alle Marks quittiert | Rückkopplung — der Anrufer hört den Agent noch |
| Sprechwarteschlange leer | Öffnen in der Pause zwischen zwei Sätzen |
| Kein TTS-Stream aktiv | Race Condition beim Nachschieben von Audio |
| LLM generiert nicht | Öffnen, während schon der nächste Satz entsteht |
| Kein Verbindungsabbau | Der Abschiedssatz wird abgeschnitten |

Jede Zeile dieser Tabelle war einmal ein Fehler im Feld. Deshalb liefert `check_release()` nicht `True`/`False`, sondern den **Grund** mit — was Feldprotokolle lesbar macht.

### Barge-in: dieselbe Logik, umgekehrt

Ein Assistent, den man nicht unterbrechen kann, ist unbenutzbar. Wer aber jedes eingehende Transkript als Unterbrechung wertet, bekommt einen Agent, der nie einen Satz beendet — denn ein Teil dieser Transkripte ist die eigene Stimme.

Drei Hürden, in dieser Reihenfolge:

1. **Der Agent muss sprechen** — sonst gibt es nichts zu unterbrechen.
2. **Mindestlänge.** „ja", „mhm", „genau" sind Zuhörsignale, keine Unterbrechungen.
3. **Kein Echo.** Zwei Kriterien, weil die STT das Echo nie wortgleich liefert: eine zusammenhängende Teilfolge der eigenen letzten Ausgabe, *oder* eine Wort-Überlappung über dem Schwellwert (Standard 0,75), gemessen über ein rollierendes Fenster der letzten ~120 gesprochenen Wörter.

Ein Detail, das in der Praxis teuer war: Beim Barge-in **müssen** die offenen Marks verworfen werden. Sie beziehen sich auf Audio, das Twilio nach dem `clear`-Frame nie mehr abspielt — die Quittungen kommen also niemals. Ohne Leeren wartet das Gate auf sie und das Mikrofon bleibt bis zum Ende des Anrufs geschlossen.

---

## Architektur

```
                    ┌──────────────────────────────────┐
   Anrufer ────────►│ Twilio Media Streams  (mulaw 8k) │
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
                             │               │  satzweise
                        ┌────▼───────────────┴────┐
                        │   LLM  (streaming)      │
                        └─────────────────────────┘

              Jede Zustandsentscheidung: audio_gate.py
```

### Warum satzweise Ausgabe

Wartet man die vollständige LLM-Antwort ab, entsteht am Anfang jeder Antwort eine Pause in der Länge der gesamten Generierung. Stattdessen wird der Token-Stream an Satzgrenzen abgegriffen und jeder fertige Satz sofort in die Sprechwarteschlange gelegt. Die Ausgabe beginnt, sobald der **erste** Satz steht.

Das erzeugt mehrere Marks pro Antwort — und damit die Anforderung, auf *alle* zu warten, nicht nur auf den letzten.

### Weitere Schutzmechanismen

- **Schleifenerkennung.** Ein n-Gramm-Detektor erkennt unmittelbare Wortfolgen-Wiederholung, das typische Zeichen einer LLM-Schleife. Kalibriert so, dass natürliche Doppelung im gesprochenen Deutsch („sehr, sehr gerne") keinen Fehlalarm auslöst.
- **Erster-Chunk-Watchdog.** Bleibt der erste TTS-Chunk aus, hängt der Anrufer in vollständiger Stille — für ihn schlimmer als jede Fehlermeldung. Nach einem Timeout greift die Behandlung.
- **Satz-Dedup** innerhalb einer Antwortrunde.
- **Cooldown** nach der letzten Quittung, gegen den Nachhall auf der Leitung. Angesammelte Transkripte werden dabei verworfen: was während der eigenen Ausgabe hereinkam, war Echo.

---

## Dateien

| Datei | Inhalt |
|---|---|
| `audio_gate.py` | Mikrofon-Zustandsgatter, Echo-Erkennung, Barge-in, Schleifenerkennung. Ohne externe Abhängigkeiten, ohne Netzwerk-I/O — die gesamte Entscheidungslogik ist rein und damit testbar. |
| `app.py` | FastAPI-Anwendung: TwiML-Webhook, WebSocket-Schleife, Provider-Anbindung, satzweise Ausgabe. |
| `tests/test_audio_gate.py` | 24 Tests. Jeder dokumentiert einen Fehlerfall, der im Feld aufgetreten ist — die Testnamen beschreiben das Symptom, nicht die Methode. |
| `docs/barge-in.md` | Ablaufdiagramme: normaler Redewechsel, Echo-Verwerfung, Barge-in. |
| `config.example.json` | Alle Schwellwerte an einer Stelle. Konfigurationsgetrieben: keine Code-Änderung für andere Parameter. |

---

## Einrichtung

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.json config.json    # Schwellwerte und Prompt anpassen

export PUBLIC_HOST=ihre-domain.example.com
export DEEPGRAM_API_KEY=...
export OPENAI_API_KEY=...
export ELEVENLABS_API_KEY=...

uvicorn app:app --host 0.0.0.0 --port 8000
```

In der Twilio-Rufnummernkonfiguration den Voice-Webhook auf `https://$PUBLIC_HOST/voice` setzen (POST).

Zugangsdaten kommen ausschließlich aus der Umgebung. `config.json` enthält keine Geheimnisse und ist über `.gitignore` ausgenommen.

### Tests

```bash
pytest -q
```

Das Gatter ist ohne Telefonleitung, ohne API-Schlüssel und ohne Netzwerkzugang vollständig testbar. Das war der Grund, die Entscheidungslogik aus der I/O-Schicht herauszulösen: ein Zustandsfehler, der nur im echten Anruf reproduzierbar ist, kostet pro Iteration mehrere Minuten und eine Telefonverbindung.

---

## Technologie

| Schicht | Verwendet |
|---|---|
| Telefonie | Twilio Media Streams (WebSocket, mulaw 8 kHz) |
| STT | Deepgram Nova-3, streaming, mit VAD-Events |
| LLM | OpenAI GPT-4o, streaming |
| TTS | ElevenLabs Flash v2.5, streaming, mulaw 8 kHz |
| Laufzeit | Python 3.11+, FastAPI, Uvicorn, asyncio |

Die Provider-Aufrufe sind absichtlich dünn gehalten. Die Gatter-Logik ist providerunabhängig — sie setzt lediglich voraus, dass die Telefonieplattform eine Wiedergabe-Quittung liefert.

---

## Lizenz

MIT — siehe [LICENSE](LICENSE).
*English version: [README.md](README.md)*

