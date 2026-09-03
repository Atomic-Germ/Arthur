"""Audio8 TTS engine for the guarded audiobook example (book preview).

Uses ``Audio8/Audio8-TTS-Preview-0.1b`` (transformers remote code) for a
higher-quality voice than piper, optionally zero-shot cloned from the
author's own reference recording. Output is deliberately downsampled to a
16 kHz mono WAV: this is a *preview*, and every render embeds the spoken
AI/publication disclaimer, so it can never double as a deliverable
audiobook.

Engine notes:

* The codec natively emits 44.1 kHz float audio; we polyphase-resample to
  16 kHz with scipy before writing 16-bit PCM.
* Long prose is split into sentence-ish chunks that fit the model's packed
  context; pacing pauses (paragraph/scene/chapter) are spliced as silence.
* ``speech_rate`` has no effect on this autoregressive engine (piper's
  length-scale knob does not apply); pauses still apply.
* Runs on CPU by default (RTF roughly 9× realtime on a mid-range desktop;
  fine for previews). Set ``GW_TTS_DEVICE=cuda`` to opt into GPU inference —
  some ROCm builds segfault in the mamba fallback kernels, so GPU is not
  the default.
* Voice cloning requires a saved author-voice profile (reference audio +
  matching transcript). Consent is implicit (the author uploads their own
  voice) and synthetic speech is disclosed by the embedded guardrail.
"""

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger("ghostwriter.tts.audio8")

MODEL_ID = "Audio8/Audio8-TTS-Preview-0.1b"
OUTPUT_RATE = 16000
# Sentence-ish chunks small enough for the packed context (~2048 positions).
MAX_CHUNK_CHARS = 350
# Reference-clip limits (long/noisy refs destabilize cloning).
REF_MIN_SECONDS = 1.0
REF_MAX_SECONDS = 30.0

VOICE_DIRNAME = Path("tts") / "author_voice"
HF_CACHE_DIRNAME = "hf"

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")


def _chunk_text(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split prose into <=limit-char chunks on sentence, then word, bounds."""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    parts = [p for p in (s.strip() for s in _SENTENCE_SPLIT.split(text)) if p]
    chunks: list[str] = []
    buf = ""
    for part in parts:
        while len(part) > limit:
            cut = part.rfind(" ", 0, limit + 1)
            if cut < limit // 2:
                cut = limit
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(part[:cut].strip())
            part = part[cut:].strip()
        if not part:
            continue
        if len(buf) + len(part) + 1 <= limit:
            buf = f"{buf} {part}".strip()
        else:
            if buf:
                chunks.append(buf)
            buf = part
    if buf:
        chunks.append(buf)
    return chunks or [text]


def resample_to(audio, src_rate: int, dst_rate: int):
    """Mono float32 resample of ``audio`` (1-D array) from src to dst rate."""
    import numpy as np
    from math import gcd

    from scipy.signal import resample_poly

    x = np.asarray(audio, dtype=np.float32).reshape(-1)
    if src_rate == dst_rate:
        return x.astype(np.float32, copy=False)
    g = gcd(int(src_rate), int(dst_rate))
    y = resample_poly(x.astype(np.float64), dst_rate // g, src_rate // g)
    return np.clip(y, -1.0, 1.0).astype(np.float32)


def _to_int16(audio) -> bytes:
    import numpy as np

    x = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return (x * 32767.0).astype("<i2").tobytes()


class Audio8TTS:
    """Lazy-loading wrapper around the Audio8 preview checkpoint."""

    def __init__(self, data_dir: Path, device: str = "auto"):
        self._data_dir = Path(data_dir)
        self._device_pref = device
        self._model = None
        self._processor = None
        self._device = None
        self._native_rate = None

    # ── availability / loading ─────────────────────────────────

    @property
    def cache_dir(self) -> Path:
        return self._data_dir / HF_CACHE_DIRNAME

    @property
    def snapshot_glob(self) -> str:
        return str(
            self.cache_dir
            / ("models--" + MODEL_ID.replace("/", "--"))
            / "snapshots"
            / "*"
        )

    def available(self) -> bool:
        root = Path(self.snapshot_glob)
        if not root.parent.exists():
            return False
        snaps = [
            p
            for p in root.parent.iterdir()
            if p.is_dir() and (p / "model.safetensors").exists()
        ]
        return any(
            (s / "codec.pth").exists() and (s / "model.safetensors").exists()
            for s in snaps
        )

    def ensure_model(self):
        if self._model is not None:
            return self._model
        if not self.available():
            raise RuntimeError(
                "Audio8 model not downloaded. Run `python -m app.services.audio8 "
                "download` in the backend and retry."
            )
        import torch
        from transformers import AutoModel, AutoProcessor

        t0 = time.time()
        device = self._resolve_device(torch)
        dtype = torch.bfloat16 if device == "cuda" else torch.float32
        self._processor = AutoProcessor.from_pretrained(
            MODEL_ID, trust_remote_code=True, cache_dir=str(self.cache_dir)
        )
        self._model = AutoModel.from_pretrained(
            MODEL_ID, trust_remote_code=True, dtype=dtype, cache_dir=str(self.cache_dir)
        ).eval().to(device)
        self._device = device
        self._native_rate = int(getattr(self._model.config, "codec_sample_rate", 44100))
        logger.info(
            "Loaded %s (%s/%s) in %.2fs", MODEL_ID, device, dtype, time.time() - t0
        )
        return self._model

    def _resolve_device(self, torch) -> str:
        pref = (self._device_pref or "auto").lower()
        if pref in ("cpu", "cuda"):
            return pref
        return "cuda" if torch.cuda.is_available() else "cpu"

    # ── synthesis ──────────────────────────────────────────────

    def synth(self, text: str, reference: tuple | None = None) -> list:
        """Synthesize one prosodic unit into [(pcm_int16_bytes, OUTPUT_RATE)].

        ``reference`` is an optional (waveform_f32_mono, sample_rate,
        transcript) triple enabling zero-shot voice cloning.
        """
        model = self.ensure_model()
        out: list = []
        for chunk in _chunk_text(text):
            audio = self._generate_chunk(model, chunk, reference)
            pcm = _to_int16(resample_to(audio, self._native_rate, OUTPUT_RATE))
            if pcm:
                out.append((pcm, OUTPUT_RATE))
        return out

    def _generate_chunk(self, model, chunk: str, reference: tuple | None):
        import torch

        proc_kwargs = {}
        if reference is not None:
            waveform, src_rate, transcript = reference
            target = int(self._processor.audio_sampling_rate)
            proc_kwargs = {
                "reference_audio": [
                    {
                        "array": resample_to(waveform, int(src_rate), target),
                        "sampling_rate": target,
                    }
                ],
                "reference_text": [transcript],
            }
        inputs = self._processor(text=[chunk], **proc_kwargs)
        inputs = {k: v.to(self._device) for k, v in inputs.items()}
        # ~13 chars/sec speech at ~21.5 codec frames/sec, with margin.
        max_new_tokens = min(1500, int(len(chunk) * 1.8) + 64)
        with torch.inference_mode():
            output = model.generate(
                **inputs,
                max_new_tokens=max_new_tokens,
                temperature=0.7,
                top_p=0.9,
                top_k=50,
                do_sample=True,
                return_dict_in_generate=True,
            )
            waveforms, lengths = model.decode_audio(output.codes)
        return waveforms[0, : int(lengths[0])].float().cpu().numpy()

    def export_book(
        self,
        chapters: list,
        out_path: Path,
        pacing=None,
        reference: tuple | None = None,
    ) -> dict:
        """Render the guarded audiobook example at 16 kHz (see tts.render_guarded_book)."""
        from app.services.tts import Pacing, render_guarded_book

        pacing = pacing or Pacing()
        meta = render_guarded_book(
            lambda t: self.synth(t, reference),
            chapters,
            out_path,
            pacing,
            OUTPUT_RATE,
        )
        meta["engine"] = "audio8"
        meta["rate"] = OUTPUT_RATE
        return meta


# ── author-voice profile (global) ──────────────────────────────


def author_voice_dir(data_dir: Path) -> Path:
    return Path(data_dir) / VOICE_DIRNAME


def save_author_voice(data_dir: Path, raw: bytes, transcript: str) -> dict:
    """Decode an uploaded recording and store it as the author-voice profile."""
    import numpy as np
    import soundfile as sf

    transcript = (transcript or "").strip()
    if not transcript:
        raise ValueError("A verbatim transcript of the recording is required.")
    if not raw:
        raise ValueError("Reference audio file is empty.")

    import io

    try:
        array, rate = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
    except Exception as e:  # noqa: BLE001
        raise ValueError(
            "Could not decode the audio file. Upload WAV, FLAC, OGG or MP3."
        ) from e
    if array.size == 0:
        raise ValueError("Reference audio contains no samples.")
    mono = array.mean(axis=1)
    seconds = len(mono) / float(rate)
    if seconds < REF_MIN_SECONDS:
        raise ValueError("Recording too short (need at least 1 second).")
    max_samples = int(REF_MAX_SECONDS * rate)
    trimmed = mono[:max_samples]

    vdir = author_voice_dir(data_dir)
    vdir.mkdir(parents=True, exist_ok=True)
    sf.write(str(vdir / "reference.wav"), trimmed, rate, subtype="PCM_16")
    (vdir / "transcript.txt").write_text(transcript, encoding="utf-8")
    (vdir / "meta.json").write_text(
        json.dumps({"rate": int(rate), "seconds": round(seconds, 2)}),
        encoding="utf-8",
    )
    logger.info("Saved author voice profile (%.1fs @ %dHz)", seconds, rate)
    return {"seconds": round(seconds, 2), "rate": int(rate)}


def load_author_voice(data_dir: Path) -> tuple | None:
    """Return (waveform_f32_mono, sample_rate, transcript) or None."""
    import soundfile as sf

    vdir = author_voice_dir(data_dir)
    ref = vdir / "reference.wav"
    txt = vdir / "transcript.txt"
    if not (ref.exists() and txt.exists()):
        return None
    try:
        array, rate = sf.read(str(ref), dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001
        logger.warning("Author voice reference unreadable; ignoring profile.")
        return None
    transcript = txt.read_text(encoding="utf-8").strip()
    if not transcript:
        return None
    return (array.mean(axis=1), int(rate), transcript)


def has_author_voice(data_dir: Path) -> bool:
    return load_author_voice(data_dir) is not None


def clear_author_voice(data_dir: Path) -> bool:
    import shutil

    vdir = author_voice_dir(data_dir)
    if vdir.exists():
        shutil.rmtree(vdir, ignore_errors=True)
        return True
    return False


def author_voice_status(data_dir: Path) -> dict:
    vdir = author_voice_dir(data_dir)
    meta_path = vdir / "meta.json"
    exists = has_author_voice(data_dir)
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            meta = {}
    transcript = ""
    if exists:
        transcript = (vdir / "transcript.txt").read_text(encoding="utf-8")
    return {
        "exists": exists,
        "seconds": meta.get("seconds"),
        "rate": meta.get("rate"),
        "transcript": transcript,
    }


_audio8: Audio8TTS | None = None


def get_audio8() -> Audio8TTS:
    global _audio8
    if _audio8 is None:
        from app.config import get_settings

        settings = get_settings()
        _audio8 = Audio8TTS(settings.data_dir, device=settings.tts_device)
    return _audio8


if __name__ == "__main__":
    import argparse

    from app.config import get_settings

    parser = argparse.ArgumentParser(description="Download the Audio8 TTS model.")
    parser.add_argument(
        "action",
        nargs="?",
        default="download",
        choices=["download", "info"],
        help="download the model snapshot, or print model info",
    )
    args = parser.parse_args()

    svc = Audio8TTS(get_settings().data_dir, device=get_settings().tts_device)
    if args.action == "info":
        print("model:", MODEL_ID)
        print("cache:", svc.cache_dir)
        print("available:", svc.available())
    else:
        if svc.available():
            print("Model already present:", svc.snapshot_glob)
        else:
            from huggingface_hub import snapshot_download

            path = snapshot_download(
                repo_id=MODEL_ID,
                cache_dir=str(svc.cache_dir),
                allow_patterns=[
                    "*.json",
                    "*.py",
                    "*.jinja",
                    "model.safetensors",
                    "codec.pth",
                ],
            )
            print("Downloaded to", path)
