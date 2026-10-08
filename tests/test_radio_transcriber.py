"""Team radio transcription: provider choice, the vocabulary prompt, filtering and the queue."""
import time

import radio_transcriber as rt


def test_off_without_a_key(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert rt.provider_from_env() is None


def test_groq_preferred_then_openai(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("GROQ_API_KEY", "g")
    p = rt.provider_from_env()
    assert p.name == "groq" and p.model == "whisper-large-v3" and p.key == "g"
    monkeypatch.delenv("GROQ_API_KEY")
    p = rt.provider_from_env()
    assert p.name == "openai" and p.key == "o"


def test_prompt_names_the_driver_and_the_field():
    drivers = {"63": {"FullName": "George RUSSELL", "TeamName": "Mercedes"}, "12": {"FullName": "Andrea Kimi ANTONELLI", "TeamName": "Mercedes"}}
    p = rt.build_prompt(drivers, "63")
    assert p.startswith("George Russell, Mercedes")
    assert "Antonelli" in p and "box" in p.lower() and "DRS" in p
    assert len(p) < 900  # Whisper only reads the last ~224 tokens of a prompt


def test_clean_drops_silence_and_hallucinations():
    assert rt.clean(" Thank you. ") is None
    assert rt.clean("Bye.") is None
    assert rt.clean("you") is None
    assert rt.clean("") is None
    assert rt.clean("  box box,  box box.  ") == "Box box, box box."
    assert rt.clean("okay george, p3 now, push push") == "Okay george, p3 now, push push"


def test_segments_with_no_speech_are_dropped():
    verbose = {"text": "Thank you for watching", "segments": [{"text": "Thank you for watching", "no_speech_prob": 0.92}]}
    assert rt.text_from_response(verbose) is None
    spoken = {"text": "Copy, box this lap.", "segments": [{"text": "Copy, box this lap.", "no_speech_prob": 0.02}]}
    assert rt.text_from_response(spoken) == "Copy, box this lap."


def test_each_clip_is_transcribed_once():
    calls = []

    def fake(url, prompt):
        calls.append((url, prompt))
        return "Box box."

    t = rt.RadioTranscriber(provider=rt.Provider("test", "u", "k", "m"), transcribe=fake)
    t.start()
    try:
        t.submit("https://x/clip1.mp3", "prompt")
        t.submit("https://x/clip1.mp3", "prompt")
        deadline = time.time() + 3
        while t.get("https://x/clip1.mp3") is None and time.time() < deadline:
            time.sleep(0.02)
        assert t.get("https://x/clip1.mp3") == "Box box."
        assert len(calls) == 1
    finally:
        t.stop()


def test_disabled_transcriber_ignores_clips():
    t = rt.RadioTranscriber(provider=None)
    t.submit("https://x/clip.mp3", "p")
    assert t.get("https://x/clip.mp3") is None
    assert t.enabled is False


def test_engine_sends_live_clips_not_the_snapshot_backlog():
    import live_engine

    class Spy:
        enabled = True
        provider = rt.Provider("spy", "", "", "")

        def __init__(self):
            self.sent = []

        def submit(self, url, prompt):
            self.sent.append(url)

        def get(self, url):
            return "Box box." if url in self.sent else None

    e = live_engine.LiveF1Engine()
    e._transcriber = Spy()
    e._session_info = {"Path": "2026/x/race/"}
    e._apply("TeamRadio", {"Captures": [{"Utc": "a", "RacingNumber": "63", "Path": "TeamRadio/old.mp3"}]}, snapshot=True)
    e._apply("TeamRadio", {"Captures": [{"Utc": "b", "RacingNumber": "63", "Path": "TeamRadio/new.mp3"}]}, snapshot=False)
    assert e._transcriber.sent == ["https://livetiming.formula1.com/static/2026/x/race/TeamRadio/new.mp3"]
    import asyncio
    radio = asyncio.run(e.get_live_timing())["team_radio"]
    assert [r["transcript"] for r in radio] == [None, "Box box."]


def test_noisy_but_confident_speech_is_kept():
    noisy = {"text": "Box box, box this lap.", "segments": [{"text": "Box box, box this lap.", "no_speech_prob": 0.7, "avg_logprob": -0.3}]}
    assert rt.text_from_response(noisy) == "Box box, box this lap."


def test_a_failed_clip_is_retried_later_not_saved_as_silence(monkeypatch):
    monkeypatch.setattr(rt.time, "sleep", lambda s: None)
    attempts = []

    def flaky(url, prompt):
        attempts.append(url)
        raise RuntimeError("provider down")

    t = rt.RadioTranscriber(provider=rt.Provider("test", "u", "k", "m"), transcribe=flaky)
    t.start()
    try:
        t.submit("https://x/c.mp3", "p")
        deadline = time.time() + 3
        while (t.is_queued("https://x/c.mp3") or len(attempts) < 4) and time.time() < deadline:
            time.sleep(0.02)
        assert not t.done("https://x/c.mp3")          # not recorded as "no speech"
        t.submit("https://x/c.mp3", "p")              # and can be queued again

        deadline = time.time() + 3
        while len(attempts) < 5 and time.time() < deadline:
            time.sleep(0.02)
        assert len(attempts) >= 5
    finally:
        t.stop()
