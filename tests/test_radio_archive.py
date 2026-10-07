import radio_archive
import radio_transcriber


class FakeT:
    def __init__(self):
        self.texts, self.subs = {}, []
    def done(self, url): return url in self.texts
    def get(self, url): return self.texts.get(url)
    def submit(self, url, prompt): self.subs.append((url, prompt))


def test_known_come_back_and_new_ones_are_queued_with_the_drivers_name(monkeypatch):
    monkeypatch.setattr(radio_archive, "_db_get", lambda urls: {"https://livetiming.formula1.com/static/a/HAM_44_20261002_122628.mp3": "Box box"})
    saved = []
    monkeypatch.setattr(radio_archive, "_db_put", lambda u, t: saved.append((u, t)))
    t = FakeT()
    done = "https://livetiming.formula1.com/static/a/RUS_63_20261002_124306.mp3"
    t.texts[done] = None  # through the worker, no speech
    new = "https://livetiming.formula1.com/static/a/PIA_81_20261002_122628.mp3"
    r = radio_archive.lookup(
        ["https://livetiming.formula1.com/static/a/HAM_44_20261002_122628.mp3", done, new],
        {"81": {"FullName": "Oscar PIASTRI", "TeamName": "McLaren"}}, t)
    assert r["transcripts"]["https://livetiming.formula1.com/static/a/HAM_44_20261002_122628.mp3"] == "Box box"
    assert r["transcripts"][done] is None and saved == [(done, None)]
    assert r["pending"] == [new]
    assert t.subs[0][0] == new and t.subs[0][1].startswith("Oscar Piastri, McLaren")


def test_only_f1_audio_is_accepted():
    assert not "https://evil.example/x.mp3".startswith(radio_archive.ALLOWED_PREFIX)
