"""
audio_gate.py - Mikrofon-Gate fuer bidirektionale Echtzeit-Telefonie.

Loest das Kernproblem eines Voice-Agents auf einer analogen Telefonleitung:
Die eigene Ausgabe (TTS) kommt ueber dieselbe Leitung als Eingabe zurueck.
Ohne Gate transkribiert die STT-Engine die eigene Stimme des Agents, das LLM
antwortet darauf, und das Gespraech laeuft in eine Endlosschleife.

Dieses Modul ist absichtlich frei von externen Abhaengigkeiten und von
Netzwerk-I/O: die gesamte Entscheidungslogik ist rein und damit testbar.
Siehe tests/test_audio_gate.py und docs/barge-in.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────
# Textnormalisierung
# ─────────────────────────────────────────────────────────────

def normalize_words(text: str) -> list[str]:
    """Kleingeschriebene Wortliste ohne Satzzeichen - Basis aller Vergleiche."""
    return re.findall(r"\w+", text.lower())


def contains_sequence(haystack: list[str], needle: list[str]) -> bool:
    """True, wenn needle als zusammenhaengende Teilfolge in haystack vorkommt."""
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    return any(haystack[i:i + n] == needle
               for i in range(len(haystack) - n + 1))


def detect_repetition_loop(text: str, ngram_min: int = 3,
                           ngram_max: int = 10) -> bool:
    """True, wenn eine Wortfolge UNMITTELBAR mehrfach hintereinander auftritt -
    das typische Zeichen einer LLM-Schleife.

    Bei kurzen Phrasen (3 Woerter) sind drei Wiederholungen erforderlich, bei
    laengeren (4+) genuegen zwei. So loest natuerliche Doppelung im
    gesprochenen Deutsch ("sehr, sehr gerne") keinen Fehlalarm aus.
    """
    words = normalize_words(text)
    total = len(words)
    for size in range(ngram_min, ngram_max + 1):
        needed = 3 if size == ngram_min else 2
        if total < size * needed:
            continue
        for start in range(total - size * needed + 1):
            block = words[start:start + size]
            if all(words[start + k * size:start + (k + 1) * size] == block
                   for k in range(1, needed)):
                return True
    return False


# ─────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateConfig:
    """Alle Schwellwerte an einer Stelle - im Produktivsystem aus JSON geladen."""

    echo_similarity_threshold: float = 0.75
    """Anteil der Transkript-Woerter, die in der eigenen letzten Ausgabe
    vorkommen muessen, damit das Transkript als Echo gilt."""

    recent_tts_max_words: int = 120
    """Groesse des rollierenden Fensters ueber die eigene Ausgabe."""

    barge_in_min_words: int = 3
    """Kuerzere Einwuerfe werden ignoriert - "ja", "mhm", "okay" sind
    Bestaetigungen, keine Unterbrechungen."""

    barge_in_enabled: bool = True


# ─────────────────────────────────────────────────────────────
# Zustand
# ─────────────────────────────────────────────────────────────

@dataclass
class GateState:
    """Zustand eines einzelnen Anrufs."""

    assistant_speaking: bool = False
    pending_marks: set[str] = field(default_factory=set)
    recent_tts_words: list[str] = field(default_factory=list)
    speak_queue_size: int = 0
    tts_streaming: bool = False
    llm_generating: bool = False
    call_ending: bool = False


class ReleaseDecision:
    """Ergebnis der Freigabepruefung - warum das Mikrofon (nicht) oeffnet."""

    def __init__(self, release: bool, reason: str):
        self.release = release
        self.reason = reason

    def __bool__(self) -> bool:
        return self.release

    def __repr__(self) -> str:
        return f"ReleaseDecision(release={self.release}, reason={self.reason!r})"


# ─────────────────────────────────────────────────────────────
# Das Gate
# ─────────────────────────────────────────────────────────────

class MicrophoneGate:
    """Entscheidet, ob ein eingehendes Transkript vom Anrufer stammt oder die
    zurueckgekoppelte eigene Stimme ist - und wann das Mikrofon wieder oeffnet.

    Der entscheidende Punkt: das Mikrofon wird NICHT freigegeben, wenn der
    TTS-Stream endet. Zwischen dem letzten gesendeten Audio-Chunk und dem
    Moment, in dem der Anrufer diesen Chunk tatsaechlich hoert, liegt die
    Puffer- und Netzlaufzeit der Telefonieplattform. Gibt man bei Stream-Ende
    frei, ist das Mikrofon offen, waehrend der Anrufer die eigene Ausgabe noch
    hoert - und die Rueckkopplung beginnt genau dort.

    Der korrekte Anker ist die Wiedergabebestaetigung der Plattform: Twilio
    liefert nach jedem gesetzten `mark` ein `mark`-Event zurueck, sobald die
    Wiedergabe diesen Punkt erreicht hat. Erst wenn KEIN unbestaetigter Mark
    mehr offen ist, hat der Anrufer alles gehoert.
    """

    def __init__(self, config: GateConfig | None = None):
        self.config = config or GateConfig()
        self.state = GateState()

    # ── Ausgabeseite ─────────────────────────────────────────

    def hold(self) -> None:
        """Der Agent beginnt zu sprechen: Mikrofon geschlossen."""
        self.state.assistant_speaking = True

    def remember_output(self, text: str) -> None:
        """Jeden ausgesprochenen Satz im rollierenden Fenster mitschreiben -
        Grundlage des Echo-Vergleichs."""
        words = self.state.recent_tts_words
        words.extend(normalize_words(text))
        overflow = len(words) - self.config.recent_tts_max_words
        if overflow > 0:
            del words[:overflow]

    def register_mark(self, name: str) -> None:
        """Mark gesetzt - Wiedergabe noch nicht bestaetigt."""
        self.state.pending_marks.add(name)

    def confirm_mark(self, name: str) -> None:
        """Die Plattform meldet: dieser Punkt der Wiedergabe ist erreicht."""
        self.state.pending_marks.discard(name)

    # ── Freigabepruefung ─────────────────────────────────────

    def check_release(self) -> ReleaseDecision:
        """Das Mikrofon oeffnet nur, wenn ALLE Bedingungen erfuellt sind.

        Jede einzelne Bedingung hat in der Praxis schon einmal eine
        Rueckkopplung verursacht - deshalb liefert die Methode den Grund mit
        zurueck, statt nur True/False. Das macht Feldprotokolle lesbar.
        """
        s = self.state
        if s.call_ending:
            return ReleaseDecision(False, "call_ending")
        if not s.assistant_speaking:
            return ReleaseDecision(False, "already_open")
        if s.pending_marks:
            return ReleaseDecision(
                False, f"unconfirmed_marks={len(s.pending_marks)}")
        if s.speak_queue_size > 0:
            return ReleaseDecision(False, "sentences_queued")
        if s.tts_streaming:
            return ReleaseDecision(False, "tts_streaming")
        if s.llm_generating:
            return ReleaseDecision(False, "llm_generating")
        return ReleaseDecision(True, "playback_confirmed")

    def release(self) -> None:
        """Mikrofon oeffnen. Der Aufrufer wartet vorher den Cooldown ab und
        verwirft angesammelte Transkripte - siehe app.py."""
        self.state.assistant_speaking = False

    # ── Eingabeseite ─────────────────────────────────────────

    def is_probably_echo(self, transcript: str) -> bool:
        """True, wenn das Transkript sehr wahrscheinlich die eigene Stimme ist.

        Zwei Kriterien, weil die STT-Engine das Echo nie wortgleich liefert:
        eine zusammenhaengende Teilfolge der eigenen Ausgabe, ODER ein zu
        hoher Anteil gemeinsamer Woerter.
        """
        words = normalize_words(transcript)
        if not words:
            return True
        recent = self.state.recent_tts_words
        if not recent:
            return False
        if contains_sequence(recent, words):
            return True
        recent_set = set(recent)
        overlap = sum(1 for w in words if w in recent_set) / len(words)
        return overlap >= self.config.echo_similarity_threshold

    def should_barge_in(self, transcript: str) -> bool:
        """True, wenn der Anrufer den Agent wirklich unterbricht.

        Drei Huerden, in dieser Reihenfolge: Der Agent muss sprechen, der
        Einwurf muss lang genug sein, und er darf kein Echo sein.
        """
        if not self.config.barge_in_enabled:
            return False
        if not self.state.assistant_speaking:
            return False
        if len(normalize_words(transcript)) < self.config.barge_in_min_words:
            return False
        return not self.is_probably_echo(transcript)

    def barge_in(self) -> None:
        """Unterbrechung: alles Ausstehende verwerfen.

        Die offenen Marks MUESSEN geleert werden - sie beziehen sich auf Audio,
        das die Plattform nach dem `clear` nie mehr abspielt. Ohne das Leeren
        wartet das Gate auf Bestaetigungen, die niemals kommen, und das
        Mikrofon bleibt bis zum Ende des Anrufs geschlossen.
        """
        s = self.state
        s.pending_marks.clear()
        s.speak_queue_size = 0
        s.tts_streaming = False
        s.assistant_speaking = False
