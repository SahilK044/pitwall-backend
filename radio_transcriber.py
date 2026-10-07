"""
Team radio transcription.

Each new clip from the F1 live timing feed is transcribed once, with Whisper large-v3, and the
text is served with the clip to every app. Whisper is primed with the session's driver names and
the words engineers use, which is what gets names and jargon right on noisy radio. Silence and
Whisper's stock hallucinations ("Thank you.") are dropped rather than shown.

Off unless a key is set: GROQ_API_KEY (preferred: whisper-large-v3, fast) or OPENAI_API_KEY.
"""
import logging
import os
import queue
import re
import threading
from dataclasses import dataclass
from typing import Callable, Dict, Optional

import httpx

logger = logging.getLogger("radio")


@dataclass
class Provider:
    name: str
    url: str
    key: str
    model: str


def provider_from_env() -> Optional[Provider]:
    groq = os.environ.get("GROQ_API_KEY", "").strip()
    if groq:
        return Provider("groq", "https://api.groq.com/openai/v1/audio/transcriptions", groq, "whisper-large-v3")
    openai = os.environ.get("OPENAI_API_KEY", "").strip()
    if openai:
        return Provider("openai", "https://api.openai.com/v1/audio/transcriptions", openai, "whisper-1")
    return None


# Words an engineer and driver actually say; Whisper spells what it has been shown.
JARGON = (
    "Box box, box this lap, stay out, push now, DRS, delta, undercut, overcut, lift and coast, "
    "safety car, VSC, red flag, yellow in sector two, softs, mediums, hards, inters, wets, "
    "tyre deg, graining, blistering, P1, plus one, gap ahead, track limits, strat mode, brake bias, diff, "
    "push lap, cool-down lap, in-lap, out-lap, traffic, blue flags, pit entry, pit exit, "
    "copy, understood, good job, mate, chequered flag."
)


def _name(full: str) -> str:
    return " ".join(w if not (len(w) > 1 and w.isupper()) else w.capitalize() for w in full.split())


def build_prompt(drivers: Dict[str, Dict], number: Optional[str]) -> str:
    """The clip's driver first, then the field and the jargon, kept short enough for Whisper's window."""
    me = drivers.get(str(number) or "", {})
    first = f"{_name(me.get('FullName') or '')}, {me.get('TeamName') or ''}. ".strip(", ") if me else ""
    surnames = [_name(d.get("FullName") or "").split(" ")[-1] for k, d in drivers.items() if k != str(number) and d.get("FullName")]
    field = ", ".join(s for s in surnames if s)
    prompt = f"{first} Formula 1 team radio. Drivers: {field}. {JARGON}".strip()
    return prompt[:880]


HALLUCINATIONS = {
    "thank you", "thank you.", "thanks for watching", "thank you for watching", "bye", "bye.", "you",
    "subtitles by the amara.org community", "please subscribe", ".", "",
}


def clean(text: Optional[str]) -> Optional[str]:
    t = re.sub(r"\s+", " ", (text or "")).strip()
    if t.lower().rstrip(".!") in {h.rstrip(".") for h in HALLUCINATIONS} or len(t) < 2:
        return None
    return t[0].upper() + t[1:]


def text_from_response(data: Dict) -> Optional[str]:
    segments = data.get("segments") or []
    if segments:
        # Keep only what Whisper is confident is speech.
        spoken = [s.get("text", "") for s in segments if (s.get("no_speech_prob") or 0) < 0.6]
        return clean(" ".join(spoken))
    return clean(data.get("text"))


def whisper_call(provider: Provider) -> Callable[[str, str], Optional[str]]:
    def run(url: str, prompt: str) -> Optional[str]:
        with httpx.Client(timeout=30.0) as http:
            audio = http.get(url)
            audio.raise_for_status()
            r = http.post(
                provider.url,
                headers={"Authorization": f"Bearer {provider.key}"},
                files={"file": ("radio.mp3", audio.content, "audio/mpeg")},
                data={"model": provider.model, "language": "en", "temperature": "0",
                      "response_format": "verbose_json", "prompt": prompt},
            )
            r.raise_for_status()
            return text_from_response(r.json())
    return run


class RadioTranscriber:
    """A single worker that transcribes each clip once, in arrival order, and remembers the text."""

    def __init__(self, provider: Optional[Provider] = None, transcribe: Optional[Callable[[str, str], Optional[str]]] = None):
        self.provider = provider
        self._transcribe = transcribe or (whisper_call(provider) if provider else None)
        self._texts: Dict[str, Optional[str]] = {}
        self._queued = set()
        self._q: "queue.Queue[Optional[tuple]]" = queue.Queue()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def enabled(self) -> bool:
        return self.provider is not None and self._transcribe is not None

    def start(self):
        if self.enabled and not hasattr(self, '_threads'):
            self._threads = []
            for i in range(3):
                t = threading.Thread(target=self._run, daemon=True, name=f"Radio-Transcriber-{i}")
                t.start()
                self._threads.append(t)
            logger.info(f"Team radio transcription on ({self.provider.name}, {self.provider.model}).")

    def stop(self):
        if hasattr(self, '_threads'):
            for _ in self._threads:
                self._q.put(None)

    def submit(self, url: str, prompt: str):
        if not self.enabled:
            return
        with self._lock:
            if url in self._queued:
                return
            self._queued.add(url)
        self._q.put((url, prompt))

    def get(self, url: str) -> Optional[str]:
        with self._lock:
            return self._texts.get(url)

    def done(self, url: str) -> bool:
        """True once a clip has been through the worker (its text may be None: no speech)."""
        with self._lock:
            return url in self._texts

    def _run(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            url, prompt = item
            text = None
            for attempt in range(2):
                try:
                    text = self._transcribe(url, prompt)
                    break
                except Exception as e:  # network or provider error: one retry, then leave it untranscribed
                    logger.warning(f"Radio transcription failed ({attempt + 1}/2): {e}")
            with self._lock:
                self._texts[url] = text
                if len(self._texts) > 600:
                    for k in list(self._texts)[:200]:
                        self._texts.pop(k, None)
                        self._queued.discard(k)
