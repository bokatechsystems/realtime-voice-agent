# State flows

Three flows that together cover the entire microphone state space. Time runs downward.

---

## 1. Normal turn-taking

The agent answers with two sentences. Two marks, so two receipts — and the microphone opens only after the second one.

```
 app.py            audio_gate           Twilio             Caller
   │                   │                  │                  │
   │  hold()           │                  │                  │
   ├──────────────────►│ speaking = true  │                  │
   │                   │                  │                  │
   │  sentence 1 media │                  │                  │
   ├──────────────────────────────────────►│                  │
   │  mark "tts-a"     │                  │                  │
   ├──────────────────────────────────────►│ ── playback ────►│
   │  register("tts-a")│                  │                  │
   ├──────────────────►│ marks = {a}      │                  │
   │                   │                  │                  │
   │  sentence 2 media │                  │                  │
   ├──────────────────────────────────────►│                  │
   │  mark "tts-b"     │                  │                  │
   ├──────────────────────────────────────►│                  │
   ├──────────────────►│ marks = {a, b}   │                  │
   │                   │                  │                  │
   │                   │      mark "tts-a" acknowledged       │
   │◄──────────────────────────────────────┤                  │
   ├──────────────────►│ marks = {b}      │                  │
   │  check_release()  │                  │                  │
   │◄──────────────────┤ false: marks     │ ◄─ still hears ──►│
   │                   │                  │                  │
   │                   │      mark "tts-b" acknowledged       │
   │◄──────────────────────────────────────┤                  │
   ├──────────────────►│ marks = {}       │                  │
   │  check_release()  │                  │                  │
   │◄──────────────────┤ true             │                  │
   │                                                          │
   │  … cooldown, discard transcripts …                       │
   │  release()        │                  │                  │
   ├──────────────────►│ speaking = false │                  │
   │                   │                  │   mic open        │
```

**The critical point** sits between the two `check_release()` calls. At the first one the TTS stream finished long ago and the speech queue is empty — a naive implementation would open here. At that moment the caller is still hearing sentence 2.

---

## 2. Echo gets discarded

The caller says nothing. What comes in is the agent's own voice, returning over the line.

```
 STT callback        audio_gate
     │                   │
     │  "let me check the calendar"
     │                   │
     │  should_barge_in()│
     ├──────────────────►│  speaking?            → yes
     │                   │  ≥ 3 words?           → yes
     │                   │  subsequence of own?  → YES
     │◄──────────────────┤  false
     │                   │
     │  discarded, never reaches the LLM
```

Without the third check this would trigger a barge-in: the agent falls silent mid-sentence without anyone having interrupted it.

The check uses two criteria, because STT practically never returns echo word-for-word:

| Criterion | Catches |
|---|---|
| Contiguous subsequence | clean feedback |
| Word overlap ≥ threshold | noisy echo, dropped words, partial transcript |

The comparison base is a rolling window over roughly the last 120 words the agent spoke. The window **must** be bounded: unbounded, it grows across the call until eventually every caller utterance counts as echo and the agent goes deaf.

---

## 3. A real interruption

```
 STT callback        audio_gate         app.py            Twilio
     │                   │                │                 │
     │  "no wait that's not right"        │                 │
     │  should_barge_in()│                │                 │
     ├──────────────────►│ speaking?  yes │                 │
     │                   │ ≥ 3 words? yes │                 │
     │                   │ echo?      no  │                 │
     │◄──────────────────┤ TRUE           │                 │
     │                   │                │                 │
     │  barge_in()       │                │                 │
     ├───────────────────┼───────────────►│                 │
     │                   │  cancel_generation = true        │
     │                   │                │  clear          │
     │                   │                ├────────────────►│  buffer dropped
     │                   │                │  cancel TTS task│
     │                   │                │  drain queue    │
     │                   │  gate.barge_in()                 │
     │                   │◄───────────────┤                 │
     │                   │  marks   = {}  │                 │
     │                   │  queue   = 0   │                 │
     │                   │  speaking= false                 │
     │                                                      │
     │  mic open immediately — no waiting for receipts       │
```

### Why flushing the marks is not optional

After the `clear` frame, Twilio discards the buffered audio. Playback never reaches the marks that were set — **so the receipts never arrive**.

If `pending_marks` isn't flushed on barge-in, `check_release()` waits for the rest of the call on confirmations that do not exist. The microphone stays shut; the agent never hears the caller again. The connection is up, the conversation is dead.

This bug is particularly nasty because the symptom sits far from the cause: it surfaces seconds later and looks like an STT outage.

Covered by `test_barge_in_leert_offene_marks`.

### The generation flag

`cancel_generation` is set **before** the queue is drained. The other way round creates a race condition: the still-running LLM stream drops new sentences into the queue that was just emptied, and the agent "wakes up" again after the interruption — while the caller is speaking.

---

*Deutsche Fassung: [barge-in.de.md](barge-in.de.md)*

