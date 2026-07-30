"""
Tests fuer audio_gate.py.

Jeder Test dokumentiert einen Fehlerfall, der im Feld tatsaechlich aufgetreten
ist. Die Namen sind entsprechend gewaehlt: sie beschreiben das Symptom, nicht
die Methode.
"""

import pytest

from audio_gate import (
    GateConfig,
    MicrophoneGate,
    contains_sequence,
    detect_repetition_loop,
    normalize_words,
)


@pytest.fixture
def gate():
    return MicrophoneGate(GateConfig())


# ─────────────────────────────────────────────────────────────
# Normalisierung
# ─────────────────────────────────────────────────────────────

def test_normalisierung_entfernt_satzzeichen_und_gross_klein():
    assert normalize_words("Guten Tag, wie geht's?") == [
        "guten", "tag", "wie", "geht", "s"]


def test_teilfolge_muss_zusammenhaengend_sein():
    haystack = ["ich", "verbinde", "sie", "gerne", "weiter"]
    assert contains_sequence(haystack, ["sie", "gerne"])
    assert not contains_sequence(haystack, ["ich", "gerne"])


# ─────────────────────────────────────────────────────────────
# Das Kernproblem: Freigabe erst nach bestaetigter Wiedergabe
# ─────────────────────────────────────────────────────────────

def test_mikrofon_bleibt_zu_solange_wiedergabe_unbestaetigt(gate):
    """Der Fehler, der die Rueckkopplung verursacht hat: Freigabe bei
    Stream-Ende statt bei bestaetigter Wiedergabe."""
    gate.hold()
    gate.register_mark("tts-1")

    decision = gate.check_release()
    assert not decision
    assert "unconfirmed_marks" in decision.reason

    gate.confirm_mark("tts-1")
    assert gate.check_release()


def test_freigabe_wartet_auf_alle_marks_nicht_nur_den_letzten(gate):
    """Mehrere Saetze in der Warteschlange erzeugen mehrere Marks. Die
    Bestaetigung des letzten sagt nichts ueber die frueheren aus."""
    gate.hold()
    for name in ("tts-1", "tts-2", "tts-3"):
        gate.register_mark(name)

    gate.confirm_mark("tts-3")
    assert not gate.check_release()

    gate.confirm_mark("tts-1")
    gate.confirm_mark("tts-2")
    assert gate.check_release()


def test_unbekanntes_mark_bricht_nichts(gate):
    """Die Plattform kann Marks doppelt melden - discard statt remove."""
    gate.hold()
    gate.register_mark("tts-1")
    gate.confirm_mark("gibt-es-nicht")
    gate.confirm_mark("tts-1")
    gate.confirm_mark("tts-1")
    assert gate.check_release()


@pytest.mark.parametrize("attribut,grund", [
    ("llm_generating", "llm_generating"),
    ("tts_streaming", "tts_streaming"),
    ("call_ending", "call_ending"),
])
def test_jede_einzelne_bedingung_verhindert_freigabe(gate, attribut, grund):
    gate.hold()
    setattr(gate.state, attribut, True)
    decision = gate.check_release()
    assert not decision
    assert decision.reason == grund


def test_wartende_saetze_verhindern_freigabe(gate):
    gate.hold()
    gate.state.speak_queue_size = 2
    assert gate.check_release().reason == "sentences_queued"


# ─────────────────────────────────────────────────────────────
# Echo-Erkennung
# ─────────────────────────────────────────────────────────────

def test_wortgleiches_echo_wird_erkannt(gate):
    gate.remember_output("Einen Moment bitte, ich schaue in den Kalender.")
    assert gate.is_probably_echo("ich schaue in den Kalender")


def test_echo_wird_auch_bei_ungenauer_transkription_erkannt(gate):
    """Die STT-Engine liefert das Echo nie wortgleich - deshalb die
    Ueberlappungsquote als zweites Kriterium."""
    gate.remember_output("Ich gebe alles an den Kollegen weiter.")
    assert gate.is_probably_echo("gebe alles den Kollegen weiter")


def test_echte_anrufereingabe_ist_kein_echo(gate):
    gate.remember_output("Wie kann ich Ihnen helfen?")
    assert not gate.is_probably_echo(
        "Ich braeuchte einen Termin fuer naechste Woche Dienstag")


def test_leeres_transkript_gilt_als_echo(gate):
    """Sicherer Zustand: was leer ist, geht nicht ans LLM."""
    assert gate.is_probably_echo("   ")


def test_ohne_eigene_ausgabe_kein_echo(gate):
    """Vor dem ersten Satz gibt es kein Vergleichsmaterial."""
    assert not gate.is_probably_echo("Hallo, sind Sie da?")


def test_fenster_verwirft_alte_ausgabe():
    """Das rollierende Fenster darf nicht unbegrenzt wachsen, sonst gilt nach
    zehn Minuten Gespraech jede Anrufereingabe als Echo."""
    gate = MicrophoneGate(GateConfig(recent_tts_max_words=5))
    gate.remember_output("eins zwei drei vier fuenf sechs sieben")
    assert gate.state.recent_tts_words == [
        "drei", "vier", "fuenf", "sechs", "sieben"]


# ─────────────────────────────────────────────────────────────
# Barge-in
# ─────────────────────────────────────────────────────────────

def test_anrufer_kann_unterbrechen(gate):
    gate.hold()
    gate.remember_output("Ich kann Ihnen folgende Termine anbieten, und zwar")
    assert gate.should_barge_in("nein warten Sie das passt nicht")


def test_kurze_bestaetigung_unterbricht_nicht(gate):
    """"ja", "mhm", "genau" sind Zuhoersignale. Wer darauf abbricht, baut
    einen Agent, der nie einen Satz beendet."""
    gate.hold()
    gate.remember_output("Ich schaue kurz nach.")
    assert not gate.should_barge_in("ja genau")


def test_eigenes_echo_unterbricht_nicht(gate):
    """Der Fehler, der den Agent mitten im Satz verstummen liess."""
    gate.hold()
    gate.remember_output("Ich gebe alles an den Kollegen weiter, er meldet sich.")
    assert not gate.should_barge_in("gebe alles an den Kollegen weiter")


def test_kein_barge_in_wenn_agent_schweigt(gate):
    assert not gate.should_barge_in("das ist ein langer echter Satz")


def test_barge_in_leert_offene_marks(gate):
    """Nach dem `clear` spielt die Plattform das verworfene Audio nie ab - die
    Bestaetigungen kommen also nie. Ohne Leeren bleibt das Mikrofon fuer den
    Rest des Anrufs geschlossen."""
    gate.hold()
    gate.register_mark("tts-1")
    gate.register_mark("tts-2")
    gate.state.speak_queue_size = 3

    gate.barge_in()

    assert gate.state.pending_marks == set()
    assert gate.state.speak_queue_size == 0
    assert not gate.state.assistant_speaking


def test_barge_in_abschaltbar():
    gate = MicrophoneGate(GateConfig(barge_in_enabled=False))
    gate.hold()
    assert not gate.should_barge_in("nein das passt so ueberhaupt nicht")


# ─────────────────────────────────────────────────────────────
# Schleifenerkennung
# ─────────────────────────────────────────────────────────────

def test_llm_schleife_wird_erkannt():
    assert detect_repetition_loop(
        "Ich helfe Ihnen gerne Ich helfe Ihnen gerne Ich helfe Ihnen gerne")


def test_natuerliche_doppelung_ist_keine_schleife():
    """Gesprochenes Deutsch wiederholt sich - das darf nicht abschneiden."""
    assert not detect_repetition_loop(
        "Das mache ich sehr, sehr gerne fuer Sie.")


def test_normaler_satz_ist_keine_schleife():
    assert not detect_repetition_loop(
        "Ich habe am Dienstag um zehn Uhr einen Termin frei.")
