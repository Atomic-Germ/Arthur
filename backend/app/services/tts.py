"""Local neural text-to-speech for reading the author's prose aloud.

Two output paths mirror the feature's two use-cases:

* ``preview_text`` — a fast, lightweight clip of a short selection (the "does
  this sound right out loud?" case). Intentionally small and quick, no
  guardrail. Always piper (this module).

* ``export_book`` — a full-length audiobook-style WAV of the whole manuscript.
  This is explicitly an *example*, never a deliverable: a spoken disclaimer
  ("This audiobook is not for publication and has been generated with AI") is
  embedded at every chapter start and again roughly every 5 minutes of audio,
  so the file can never be mistaken for a publishable recording. The book
  preview defaults to the higher-quality Audio8 engine at 16 kHz
  (``app.services.audio8``), optionally cloned from the author's own voice,
  with piper as the fallback.

Intent: help an author hear what their writing sounds like out loud. It never
writes for them; it only reads their own words back.
"""

import logging
import tempfile
import time
import wave
from pathlib import Path

logger = logging.getLogger("ghostwriter.tts")

VOICE_NAME = "en_US-lessac-medium"

DISCLAIMER = (
    "This audiobook is not for publication, and has been generated with AI."
)
GUARDRAIL_INTERVAL_SEC = 300  # ~5 minutes
# Spoken before the body of each chapter, e.g. "Chapter 3."
CHAPTER_INTRO = "Chapter {n}."

# Scene-break markers common in manuscripts (a paragraph by itself, like * * *).
_SCENE_BREAK_MARKERS = {
    "* * *",
    "***",
    "*** ",
    "—",
    "--",
    "···",
    "· · ·",
}


def _seconds(rate: int, n_samples: int) -> float:
    return n_samples / rate if rate > 0 else 0.0


def _coerce(value, default: float, lo: float, hi: float) -> float:
    """Resolve an optional value, clamping into [lo, hi]."""
    if value is None:
        return default
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


def _syn_config(speech_rate: float):
    """Piper synthesis config. rate >1 = faster (shorter phonemes), <1 = slower."""
    from piper import SynthesisConfig

    return SynthesisConfig(length_scale=1.0 / float(speech_rate))


def _silence(rate: int, seconds: float) -> tuple:
    """A silence chunk of the given duration (mono 16-bit)."""
    n = max(0, int(rate * seconds))
    return (b"\x00\x00" * n, rate)


def _is_scene_break(paragraph: str) -> bool:
    return paragraph.strip() in _SCENE_BREAK_MARKERS


def _split_paragraphs(text: str) -> list[str]:
    """Split prose into paragraph units, keeping blank-line breaks."""
    text = (text or "").strip()
    if not text:
        return []
    return [p.strip() for p in text.split("\n\n") if p.strip()]


_OPEN_QUOTES = ('"', "“", "‘", "«", "„")
_CLOSE_QUOTES = ('"', "”", "’", "»", "“")  # "“" double duty: some books close with "
_QUOTE_CHARS = set(_OPEN_QUOTES) | set(_CLOSE_QUOTES)


def _split_paragraph_units(
    paragraph: str, *, split_quotes: bool, split_commas: bool
) -> list[tuple[str, str]]:
    """Split a paragraph into (text | 'quote' | 'comma', segment) units.

    Returns a list of tagged units so the synthesizer can insert pauses at the
    right boundaries: 'quote' marks the character(s) that OPEN a quotation,
    'comma' marks a comma (pause goes AFTER it). Text units are contiguous
    prose. Paragraphs that contain neither quotes nor commas pass through as a
    single text unit.
    """
    if not (split_quotes or split_commas):
        return [("text", paragraph)]

    units: list[tuple[str, str]] = []
    buf = []
    i = 0
    n = len(paragraph)
    while i < n:
        ch = paragraph[i]
        if ch in _QUOTE_CHARS and split_quotes:
            # flush pending text
            if buf:
                units.append(("text", "".join(buf)))
                buf = []
            # decide open vs close: a quote that ends a word is closing
            prev = paragraph[i - 1] if i > 0 else " "
            next_ = paragraph[i + 1] if i + 1 < n else " "
            prev_ws = prev.isspace() or prev in "([{\u2014\u2013:;,!"
            next_ws = next_.isspace() or next_ in ".,;:!?)]}"
            if prev_ws and not next_ws:
                units.append(("quote", ch))
            # closing quotes: just drop the marker (piper pauses on its own)
            i += 1
            continue
        if ch == "," and split_commas:
            if buf:
                units.append(("text", "".join(buf)))
                buf = []
            units.append(("comma", ","))
            i += 1
            continue
        buf.append(ch)
        i += 1
    if buf:
        units.append(("text", "".join(buf)))
    return units


class Pacing:
    """Timing signals used during synthesis (book-wide, adjustable on the fly)."""

    def __init__(
        self,
        paragraph_pause: float = 0.35,
        scene_pause: float = 1.0,
        chapter_pause: float = 1.4,
        quote_pause: float = 0.0,
        comma_pause: float = 0.0,
        speech_rate: float = 1.0,
    ):
        self.paragraph_pause = _coerce(paragraph_pause, 0.5, lo=0.0, hi=5.0)
        self.scene_pause = _coerce(scene_pause, 1.0, lo=0.0, hi=8.0)
        self.chapter_pause = _coerce(chapter_pause, 1.4, lo=0.0, hi=8.0)
        # Pause before an opening quote (e.g. before dialogue) and after a comma.
        self.quote_pause = _coerce(quote_pause, 0.0, lo=0.0, hi=2.0)
        self.comma_pause = _coerce(comma_pause, 0.0, lo=0.0, hi=1.0)
        # >1 faster, <1 slower (clamped to piper's usable range)
        self.speech_rate = _coerce(speech_rate, 1.0, lo=0.5, hi=1.9)

    def as_dict(self) -> dict:
        return {
            "paragraph_pause": self.paragraph_pause,
            "scene_pause": self.scene_pause,
            "chapter_pause": self.chapter_pause,
            "quote_pause": self.quote_pause,
            "comma_pause": self.comma_pause,
            "speech_rate": self.speech_rate,
        }


def render_guarded_book(
    synth, chapters: list, out_path: Path, pacing: Pacing, rate: int
) -> dict:
    """Render a guarded audiobook example using any synthesis backend.

    ``synth(text) -> list[(pcm_int16_bytes, sample_rate)]`` turns text into
    mono 16-bit chunks at a single fixed rate. Guardrail: a spoken disclaimer
    is emitted at the start of every chapter and again whenever ~5 minutes of
    audio have accumulated since the last one. Returns metadata.
    """
    if not chapters:
        raise ValueError("No chapters to synthesize.")

    disclaimer_chunks = synth(DISCLAIMER)

    running_sec = 0.0
    last_guard = 0.0
    disclaimer_count = 0

    def emit(chunks: list, tail_pause: float = 0.0) -> None:
        nonlocal running_sec
        for pcm, chunk_rate in chunks:
            wr.writeframes(pcm)
            running_sec += _seconds(chunk_rate, len(pcm) // 2)
        if tail_pause > 0:
            wr.writeframes(_silence(rate, tail_pause)[0])
            running_sec += tail_pause

    def emit_disclaimer() -> None:
        nonlocal last_guard, disclaimer_count
        emit(disclaimer_chunks)
        last_guard = running_sec
        disclaimer_count += 1

    with wave.open(str(out_path), "wb") as wr:
        wr.setnchannels(1)
        wr.setsampwidth(2)
        wr.setframerate(rate)

        for idx, chapter in enumerate(chapters, 1):
            # Guardrail at each chapter start.
            emit_disclaimer()

            # Spoken heading so the listener knows where they are.
            intro = CHAPTER_INTRO.format(n=idx)
            if (chapter.title or "").strip():
                intro = f"{intro} {chapter.title.strip()}"
            emit(synth(intro), pacing.chapter_pause)

            body = (chapter.content or "").strip()
            if body:
                paragraphs = _split_paragraphs(body)
                for pi, para in enumerate(paragraphs):
                    if _is_scene_break(para):
                        if pi > 0:
                            wr.writeframes(
                                _silence(rate, pacing.scene_pause)[0]
                            )
                            running_sec += pacing.scene_pause
                        continue
                    emit(
                        _synth_paragraph_with(synth, para, pacing, rate),
                        pacing.paragraph_pause,
                    )

            # Periodic re-guardrail if long enough since the last one.
            if running_sec - last_guard >= GUARDRAIL_INTERVAL_SEC:
                emit_disclaimer()

    return {
        "chapters": len(chapters),
        "duration_seconds": running_sec,
        "disclaimers": disclaimer_count,
    }


def _synth_paragraph_with(synth, text: str, pacing: Pacing, rate: int) -> list:
    """Splice quote/comma pauses around synth() calls for one paragraph."""
    split_quotes = pacing.quote_pause > 0
    split_commas = pacing.comma_pause > 0
    if not (split_quotes or split_commas):
        return synth(text)
    pieces: list = []
    for kind, seg in _split_paragraph_units(
        text, split_quotes=split_quotes, split_commas=split_commas
    ):
        if kind == "text":
            pieces.extend(synth(seg))
        elif kind == "quote":
            pieces.append(_silence(rate, pacing.quote_pause))
        elif kind == "comma":
            pieces.append(_silence(rate, pacing.comma_pause))
    return pieces


class TTSService:
    def __init__(self, model_dir: Path):
        self._model_dir = model_dir
        self._voice = None

    @property
    def model_path(self) -> Path:
        return self._model_dir / f"{VOICE_NAME}.onnx"

    @property
    def config_path(self) -> Path:
        return self._model_dir / f"{VOICE_NAME}.onnx.json"

    # ── voice loading ──────────────────────────────────────────

    def available(self) -> bool:
        return self.model_path.exists() and self.config_path.exists()

    def ensure_voice(self):
        if self._voice is not None:
            return self._voice
        if not self.available():
            raise RuntimeError(
                "Piper voice not downloaded. Run `python -m app.services.tts "
                "download` or fetch en_US-lessac-medium onnx+json into "
                f"{self._model_dir}"
            )
        from piper import PiperVoice

        t0 = time.time()
        self._voice = PiperVoice.load(str(self.model_path), str(self.config_path))
        logger.info("Loaded TTS voice %s in %.2fs", VOICE_NAME, time.time() - t0)
        return self._voice

    # ── synthesis core ─────────────────────────────────────────

    def synth(self, text: str, pacing: Pacing | None = None) -> list:
        """Synthesize text. Returns list of (pcm_int16_bytes, sample_rate)."""
        voice = self.ensure_voice()
        out: list = []
        rate = float((pacing.speech_rate if pacing else None) or 1.0)
        syn_config = _syn_config(rate) if rate != 1.0 else None
        if syn_config is not None:
            for chunk in voice.synthesize(text, syn_config=syn_config):
                out.append((bytes(chunk.audio_int16_bytes), chunk.sample_rate))
        else:
            for chunk in voice.synthesize(text):
                out.append((bytes(chunk.audio_int16_bytes), chunk.sample_rate))
        return out

    # ── preview clip ───────────────────────────────────────────

    def _synth_paragraph(
        self, text: str, pacing: Pacing, rate: int
    ) -> list:
        """Synthesize one paragraph, splicing quote/comma pauses at boundaries."""
        return _synth_paragraph_with(
            lambda t: self.synth(t, pacing), text, pacing, rate
        )

    def preview_wav(self, text: str, pacing: Pacing | None = None) -> bytes:
        """Render a short selection to a monophonic 16-bit WAV (no guardrail)."""
        text = (text or "").strip()
        if not text:
            return b""
        pacing = pacing or Pacing()
        pieces: list = []
        paragraphs = _split_paragraphs(text)
        for i, para in enumerate(paragraphs):
            if _is_scene_break(para):
                if pieces:
                    pieces.append(_silence(22050, pacing.scene_pause))
                continue
            pieces.extend(self._synth_paragraph(para, pacing, 22050))
            if i < len(paragraphs) - 1:
                pieces.append(_silence(22050, pacing.paragraph_pause))
        return _wav_bytes_from_chunks(pieces)

    # ── full export (guarded audiobook example) ────────────────

    def guardrail_text(self) -> str:
        return DISCLAIMER

    def guardrail_interval_seconds(self) -> int:
        return GUARDRAIL_INTERVAL_SEC

    def export_book(
        self,
        chapters: list,
        out_path: Path,
        pacing: Pacing | None = None,
    ) -> dict:
        """Synthesize all chapters into a single WAV at ``out_path``.

        Guardrail: a spoken disclaimer is emitted at the start of every chapter
        and again whenever ~5 minutes of audio have accumulated since the last
        one. Returns metadata (chapter count, duration, disclaimer count).
        """
        pacing = pacing or Pacing()
        return render_guarded_book(
            lambda t: self.synth(t, pacing), chapters, out_path, pacing, 22050
        )


def _wav_bytes_from_chunks(chunks: list) -> bytes:
    """Concatenate (pcm, rate) chunks into a WAV in memory (small clips only)."""
    if not chunks:
        return b""
    rate = chunks[0][1]
    frames = b"".join(pcm for pcm, _ in chunks)
    buf = tempfile.SpooledTemporaryFile(max_size=1_000_000)
    try:
        with wave.open(buf, "wb") as wr:
            wr.setnchannels(1)
            wr.setsampwidth(2)
            wr.setframerate(rate)
            wr.writeframes(frames)
        buf.seek(0)
        return buf.read()
    finally:
        buf.close()


_tts: TTSService | None = None


def get_tts() -> TTSService:
    global _tts
    if _tts is None:
        from app.config import get_settings

        model_dir = get_settings().data_dir / "tts"
        _tts = TTSService(model_dir)
    return _tts


if __name__ == "__main__":
    import argparse

    from app.config import get_settings

    parser = argparse.ArgumentParser(description="Download the piper TTS voice.")
    parser.add_argument(
        "action",
        nargs="?",
        default="download",
        choices=["download", "info"],
        help="download the voice model, or print model info",
    )
    args = parser.parse_args()

    svc = TTSService(get_settings().data_dir / "tts")
    if args.action == "download":
        if svc.available():
            print("Voice already present:", svc.model_path)
        else:
            import urllib.request

            base = (
                "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                f"en/en_US/lessac/medium/{VOICE_NAME}"
            )
            svc._model_dir.mkdir(parents=True, exist_ok=True)
            for suffix in (".onnx", ".onnx.json"):
                dest = svc._model_dir / f"{VOICE_NAME}{suffix}"
                url = base + suffix
                print(f"Downloading {url}")
                urllib.request.urlretrieve(url, dest)
                print("  ->", dest, dest.stat().st_size, "bytes")
    else:
        print("model:", svc.model_path, "exists:", svc.model_path.exists())
        print("config:", svc.config_path, "exists:", svc.config_path.exists())
