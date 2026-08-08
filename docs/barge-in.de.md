# Zustandsabläufe

Drei Abläufe, die zusammen den gesamten Mikrofon-Zustandsraum abdecken. Zeit läuft nach unten.

---

## 1. Normaler Redewechsel

Der Agent antwortet zweisätzig. Zwei Marks, also zwei Quittungen — und das Mikrofon öffnet erst nach der zweiten.

```
 app.py            audio_gate           Twilio            Anrufer
   │                   │                  │                  │
   │  hold()           │                  │                  │
   ├──────────────────►│ speaking = true  │                  │
   │                   │                  │                  │
   │  Satz 1: media    │                  │                  │
   ├──────────────────────────────────────►│                  │
   │  mark "tts-a"     │                  │                  │
   ├──────────────────────────────────────►│ ── Wiedergabe ──►│
   │  register("tts-a")│                  │                  │
   ├──────────────────►│ marks = {a}      │                  │
   │                   │                  │                  │
   │  Satz 2: media    │                  │                  │
   ├──────────────────────────────────────►│                  │
   │  mark "tts-b"     │                  │                  │
   ├──────────────────────────────────────►│                  │
   ├──────────────────►│ marks = {a, b}   │                  │
   │                   │                  │                  │
   │                   │       mark "tts-a" quittiert         │
   │◄──────────────────────────────────────┤                  │
   ├──────────────────►│ marks = {b}      │                  │
   │  check_release()  │                  │                  │
   │◄──────────────────┤ false: marks     │  ◄── hört noch ──►│
   │                   │                  │                  │
   │                   │       mark "tts-b" quittiert         │
   │◄──────────────────────────────────────┤                  │
   ├──────────────────►│ marks = {}       │                  │
   │  check_release()  │                  │                  │
   │◄──────────────────┤ true             │                  │
   │                                                          │
   │  … Cooldown, Transkripte verwerfen …                     │
   │  release()        │                  │                  │
   ├──────────────────►│ speaking = false │                  │
   │                   │                  │   Mikrofon offen  │
```

**Der kritische Punkt** liegt zwischen den beiden `check_release()`-Aufrufen. Beim ersten ist der TTS-Stream längst beendet und die Sprechwarteschlange leer — ein naiver Ansatz würde hier öffnen. Der Anrufer hört zu diesem Zeitpunkt noch Satz 2.

---

## 2. Echo wird verworfen

Der Anrufer sagt nichts. Was hereinkommt, ist die eigene Stimme über die Leitung zurück.

```
 STT-Callback        audio_gate
     │                   │
     │  "ich schaue in den kalender"
     │                   │
     │  should_barge_in()│
     ├──────────────────►│  speaking?              → ja
     │                   │  ≥ 3 Wörter?            → ja
     │                   │  Teilfolge der Ausgabe? → JA
     │◄──────────────────┤  false
     │                   │
     │  verworfen, geht nie ans LLM
```

Ohne die dritte Prüfung würde hier ein Barge-in ausgelöst: der Agent verstummt mitten im Satz, ohne dass jemand ihn unterbrochen hat.

Die Prüfung hat zwei Kriterien, weil die STT das Echo praktisch nie wortgleich liefert:

| Kriterium | Greift bei |
|---|---|
| Zusammenhängende Teilfolge | saubere Rückkopplung |
| Wort-Überlappung ≥ Schwellwert | verrauschtes Echo, Wortausfall, Teiltranskript |

Vergleichsbasis ist ein rollierendes Fenster über die letzten ~120 selbst gesprochenen Wörter. Das Fenster **muss** begrenzt sein: unbegrenzt wächst es über das Gespräch so weit, dass irgendwann jede Anrufereingabe als Echo gilt und der Agent taub wird.

---

## 3. Echte Unterbrechung

```
 STT-Callback        audio_gate         app.py            Twilio
     │                   │                │                 │
     │  "nein warten Sie das passt nicht" │                 │
     │  should_barge_in()│                │                 │
     ├──────────────────►│ speaking?  ja  │                 │
     │                   │ ≥ 3 Wörter? ja │                 │
     │                   │ Echo?      nein│                 │
     │◄──────────────────┤ TRUE           │                 │
     │                   │                │                 │
     │  barge_in()       │                │                 │
     ├───────────────────┼───────────────►│                 │
     │                   │  cancel_generation = true        │
     │                   │                │  clear          │
     │                   │                ├────────────────►│  Puffer weg
     │                   │                │  TTS-Task cancel│
     │                   │                │  Queue leeren   │
     │                   │  gate.barge_in()                 │
     │                   │◄───────────────┤                 │
     │                   │  marks   = {}  │                 │
     │                   │  queue   = 0   │                 │
     │                   │  speaking= false                 │
     │                                                      │
     │  Mikrofon sofort offen — kein Warten auf Quittungen   │
```

### Warum das Leeren der Marks unverzichtbar ist

Nach dem `clear`-Frame verwirft Twilio den gepufferten Audio-Inhalt. Die Wiedergabe erreicht die gesetzten Marks nie — **die Quittungen kommen also niemals**.

Wird `pending_marks` beim Barge-in nicht geleert, wartet `check_release()` für den Rest des Anrufs auf Bestätigungen, die nicht existieren. Das Mikrofon bleibt geschlossen; der Agent hört den Anrufer nie wieder. Die Verbindung steht, das Gespräch ist tot.

Dieser Fehler ist besonders unangenehm, weil das Symptom weit von der Ursache liegt: Die Auffälligkeit erscheint Sekunden später und wirkt wie ein STT-Ausfall.

Abgedeckt durch `test_barge_in_leert_offene_marks`.

### Das Generierungs-Flag

`cancel_generation` wird gesetzt, **bevor** die Warteschlange geleert wird. Umgekehrt entsteht eine Race Condition: der noch laufende LLM-Stream legt neue Sätze in die eben geleerte Warteschlange, und der Agent „erwacht" nach der Unterbrechung wieder — obwohl der Anrufer gerade spricht.

---

*English version: [barge-in.md](barge-in.md)*

