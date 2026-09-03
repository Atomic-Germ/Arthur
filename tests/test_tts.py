"""TTS service + API tests. Uses a fake voice (no network, no real model)."""

import math
import sys
import wave
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("GW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("GW_SKIP_EMBED_WARMUP", "1")

    from app.config import get_settings
    from app.db import storage as storage_mod
    from app.services import embeddings as emb_mod
    from app.services import indexer as indexer_mod
    from app.services import llm as llm_mod
    from app.services import rag as rag_mod

    get_settings.cache_clear()
    storage_mod._store = None
    llm_mod._llm = None
    rag_mod._memory = None
    indexer_mod._worker = None
    emb_mod._model = None
    emb_mod._load_error = "skipped in tests"
    emb_mod._status = "error"
    monkeypatch.setattr(emb_mod, "is_embedding_ready", lambda: False)
    monkeypatch.setattr(emb_mod, "warm_embeddings", lambda: False)

    class _FakeLLM:
        async def check_available(self, force=False):
            return False

        async def assist(self, **kwargs):
            return ("offline test reply", False)

        async def complete(self, **kwargs):
            return '{"characters": [], "world_facts": []}'

    monkeypatch.setattr(llm_mod, "get_llm", lambda: _FakeLLM())

    # Drop the piper voice model into the temp data dir so available() is True,
    # then fake the actual inference to keep tests hermetic and instant.
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir(exist_ok=True)
    (tts_dir / "en_US-lessac-medium.onnx").write_bytes(b"fake-model")
    (tts_dir / "en_US-lessac-medium.onnx.json").write_text("{}", encoding="utf-8")

    from app.services import tts as tts_mod

    class _FakeVoice:
        def synthesize(self, text):
            # One chunk per text, ~100ms of silent 22050 Hz mono int16.
            samples = [0] * (2205)
            import struct

            pcm = struct.pack("<%dh" % len(samples), *samples)
            yield type(
                "Chunk",
                (),
                {"audio_int16_bytes": pcm, "sample_rate": 22050},
            )()

    class _FakeTTS(tts_mod.TTSService):
        def __init__(self, model_dir):
            super().__init__(model_dir)
            self._voice = _FakeVoice()

        def ensure_voice(self):
            return self._voice

    monkeypatch.setattr(tts_mod, "get_tts", lambda: _FakeTTS(tts_dir))

    from app.services import audio8 as audio8_mod

    class _FakeAudio8(audio8_mod.Audio8TTS):
        """Hermetic stand-in: emits silent 16 kHz chunks, availability opt-in."""

        def __init__(self, available=True):
            super().__init__(tmp_path)
            self._available = available
            self.references = []

        def available(self):
            return self._available

        def synth(self, text, reference=None):
            if reference is not None:
                self.references.append(reference)
            return [(b"\x00\x00" * 1600, 16000)]  # 0.1s @ 16 kHz

        def export_book(self, chapters, out_path, pacing=None, reference=None):
            meta = tts_mod.render_guarded_book(
                lambda t: self.synth(t, reference),
                chapters,
                out_path,
                pacing or tts_mod.Pacing(),
                audio8_mod.OUTPUT_RATE,
            )
            meta["engine"] = "audio8"
            meta["rate"] = audio8_mod.OUTPUT_RATE
            return meta

    fake_audio8 = _FakeAudio8(available=False)
    monkeypatch.setattr(audio8_mod, "get_audio8", lambda: fake_audio8)

    from app.main import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

    get_settings.cache_clear()
    storage_mod._store = None


def _wav_ok(data: bytes) -> bool:
    try:
        with wave.open(BytesIO(data), "rb") as w:
            return w.getnchannels() == 1 and w.getframerate() == 22050
    except Exception:  # noqa: BLE001
        return False


def test_tts_status(client):
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    r = client.get(f"/api/projects/{p['id']}/tts/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert "not for publication" in body["guardrail"]
    assert body["guardrail_interval_seconds"] == 300


def test_tts_preview_returns_wav(client):
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    r = client.post(
        f"/api/projects/{p['id']}/tts/preview",
        json={"text": "The cartographer traced the salt road."},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/wav")
    assert _wav_ok(r.content)


def test_tts_preview_requires_text(client):
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    r = client.post(f"/api/projects/{p['id']}/tts/preview", json={"text": "   "})
    assert r.status_code == 400


def test_tts_export_embeds_guardrails(client):
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    client.post(
        f"/api/projects/{p['id']}/chapters",
        json={"title": "Chapter 1", "content": "Some prose to read."},
    )
    client.post(
        f"/api/projects/{p['id']}/chapters",
        json={"title": "Chapter 2", "content": "More prose to read aloud."},
    )
    r = client.get(f"/api/projects/{p['id']}/tts/export")
    assert r.status_code == 200
    assert _wav_ok(r.content)
    meta = r.headers.get("x-audio-meta", "")
    assert "chapters=2" in meta
    assert "disclaimers=2" in meta


def test_tts_export_requires_chapters(client):
    p = client.post("/api/projects", json={"title": "Empty"}).json()
    r = client.get(f"/api/projects/{p['id']}/tts/export")
    assert r.status_code == 400


def test_pacing_inserts_silence_between_paragraphs(tmp_path, monkeypatch):
    """Pauses between paragraphs/scenes add real silence to the WAV."""
    from app.services import tts as tts_mod

    tts_dir = tmp_path / "tts"
    tts_dir.mkdir(exist_ok=True)
    (tts_dir / "en_US-lessac-medium.onnx").write_bytes(b"fake-model")
    (tts_dir / "en_US-lessac-medium.onnx.json").write_text("{}", encoding="utf-8")

    import struct

    class _Voice:
        def synthesize(self, text):
            n = 2205  # 0.1s at 22050 Hz
            yield type(
                "Chunk",
                (),
                {
                    "audio_int16_bytes": struct.pack("<%dh" % n, *([0] * n)),
                    "sample_rate": 22050,
                },
            )()

    class _FakeTTS(tts_mod.TTSService):
        def __init__(self):
            super().__init__(tts_dir)
            self._voice = _Voice()

        def ensure_voice(self):
            return self._voice

    svc = _FakeTTS()
    text = "First paragraph.\n\nSecond paragraph.\n\n* * *\n\nThird paragraph."
    pacing = tts_mod.Pacing(
        paragraph_pause=0.5, scene_pause=1.5, chapter_pause=2.0, speech_rate=1.0
    )
    wav = svc.preview_wav(text, pacing)
    # 4 synth chunks * 0.1s = 0.4s + 2 paragraph pauses (0.5 each) + 1 scene
    # pause (1.5) = ~2.9s of audio.
    with wave.open(BytesIO(wav), "rb") as w:
        frames = w.getnframes()
        duration = frames / w.getframerate()
    assert duration > 2.0
    assert duration < 4.0

    # Default pacing is much lighter.
    wav_default = svc.preview_wav(text, tts_mod.Pacing())
    with wave.open(BytesIO(wav_default), "rb") as w:
        d0 = w.getnframes() / w.getframerate()
    assert d0 < duration


def test_pacing_rate_changes_synth_config_call(tmp_path, monkeypatch):
    """speech_rate != 1.0 passes a length-scaled synthesis config to piper."""
    from app.services import tts as tts_mod

    tts_dir = tmp_path / "tts"
    tts_dir.mkdir(exist_ok=True)
    (tts_dir / "en_US-lessac-medium.onnx").write_bytes(b"fake-model")
    (tts_dir / "en_US-lessac-medium.onnx.json").write_text("{}", encoding="utf-8")

    import struct

    calls = []

    class _Voice:
        def synthesize(self, text, syn_config=None):
            calls.append(syn_config)
            n = 2205
            yield type(
                "Chunk",
                (),
                {
                    "audio_int16_bytes": struct.pack("<%dh" % n, *([0] * n)),
                    "sample_rate": 22050,
                },
            )()

    class _FakeTTS(tts_mod.TTSService):
        def __init__(self):
            super().__init__(tts_dir)
            self._voice = _Voice()

        def ensure_voice(self):
            return self._voice

    svc = _FakeTTS()
    svc.preview_wav("Some words.", tts_mod.Pacing(speech_rate=1.2))
    assert len(calls) == 1 and calls[0] is not None
    assert abs(calls[0].length_scale - (1.0 / 1.2)) < 1e-6


def test_punctuation_split_tags_quotes_and_commas():
    """Tokenizer tags opening quotes and commas so pauses can be spliced."""
    from app.services.tts import _split_paragraph_units

    para = '"Come here," she said.'
    units = _split_paragraph_units(para, split_quotes=True, split_commas=True)
    kinds = [k for k, _ in units]
    assert kinds[0] == "quote"  # opening quote first
    assert "comma" in kinds
    assert kinds[-1] == "text"

    # Closing quote (preceded by a word) is dropped, not tagged as opening.
    para2 = 'She said "hi" to me.'
    units2 = _split_paragraph_units(para2, split_quotes=True, split_commas=False)
    kinds2 = [k for k, _ in units2]
    assert kinds2.count("quote") == 1

    # No quotes/commas → single text unit, no split.
    units3 = _split_paragraph_units("Plain words only", split_quotes=True, split_commas=True)
    assert units3 == [("text", "Plain words only")]


def test_quote_and_comma_pauses_add_silence(tmp_path, monkeypatch):
    """quote_pause + comma_pause splice silence into the clip."""
    from app.services import tts as tts_mod

    tts_dir = tmp_path / "tts"
    tts_dir.mkdir(exist_ok=True)
    (tts_dir / "en_US-lessac-medium.onnx").write_bytes(b"fake-model")
    (tts_dir / "en_US-lessac-medium.onnx.json").write_text("{}", encoding="utf-8")

    import struct

    class _Voice:
        def synthesize(self, text):
            n = 2205  # 0.1s
            yield type(
                "Chunk",
                (),
                {
                    "audio_int16_bytes": struct.pack("<%dh" % n, *([0] * n)),
                    "sample_rate": 22050,
                },
            )()

    class _FakeTTS(tts_mod.TTSService):
        def __init__(self):
            super().__init__(tts_dir)
            self._voice = _Voice()

        def ensure_voice(self):
            return self._voice

    svc = _FakeTTS()
    text = '"Hello," she said, "how are you?"'
    p_on = tts_mod.Pacing(quote_pause=0.5, comma_pause=0.5)
    p_off = tts_mod.Pacing(quote_pause=0.0, comma_pause=0.0)

    w_on = svc.preview_wav(text, p_on)
    w_off = svc.preview_wav(text, p_off)
    with wave.open(BytesIO(w_on), "rb") as w:
        d_on = w.getnframes() / w.getframerate()
    with wave.open(BytesIO(w_off), "rb") as w:
        d_off = w.getnframes() / w.getframerate()
    # 2 quotes * 0.5 + 2 commas * 0.5 = 2.0s extra vs the pauses-off clip
    assert d_on - d_off > 1.5


def test_periodic_guardrail_reinserts_after_interval(tmp_path, monkeypatch):
    """A very long chapter triggers the ~5-min re-guardrail, not just the start."""
    from app.services import tts as tts_mod

    tts_dir = tmp_path / "tts"
    tts_dir.mkdir(exist_ok=True)
    (tts_dir / "en_US-lessac-medium.onnx").write_bytes(b"fake-model")
    (tts_dir / "en_US-lessac-medium.onnx.json").write_text("{}", encoding="utf-8")

    import struct

    class _LongVoice:
        def synthesize(self, text):
            # Emit 10s of silence per call regardless of text length.
            n = 22050 * 10
            yield type(
                "Chunk",
                (),
                {
                    "audio_int16_bytes": struct.pack("<%dh" % n, *([0] * n)),
                    "sample_rate": 22050,
                },
            )()

    class _FakeTTS(tts_mod.TTSService):
        def __init__(self):
            super().__init__(tts_dir)
            self._voice = _LongVoice()

        def ensure_voice(self):
            return self._voice

    svc = _FakeTTS()
    monkeypatch.setattr(tts_mod, "GUARDRAIL_INTERVAL_SEC", 25)  # tighten for test

    class Ch:
        def __init__(self, title, content):
            self.title = title
            self.content = content

    # 4 chapters x ~40s each (heading+body) = ~320s >> 25s interval
    chapters = [Ch("C%d" % i, "word " * 40) for i in range(4)]
    out = tmp_path / "book.wav"
    meta = svc.export_book(chapters, out)
    # Guardrail fires at every chapter start (4) plus multiple times mid-chapters.
    assert meta["disclaimers"] >= 4
    assert meta["duration_seconds"] > 100
    with wave.open(str(out), "rb") as w:
        assert w.getnframes() > 0


# ── audio8 engine (book preview) ───────────────────────────────


def _audio8_fake(client):
    from app.services import audio8 as audio8_mod

    fake = audio8_mod.get_audio8()
    return audio8_mod, fake


def test_tts_status_reports_audio8_and_author_voice(client):
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    body = client.get(f"/api/projects/{p['id']}/tts/status").json()
    assert body["audio8"]["available"] is False
    assert body["audio8"]["output_rate"] == 16000
    assert "0.1b" in body["audio8"]["model_id"]
    assert body["author_voice"]["exists"] is False


def test_tts_export_auto_falls_back_to_piper(client):
    """With the Audio8 model absent, auto keeps serving the piper voice."""
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    client.post(
        f"/api/projects/{p['id']}/chapters",
        json={"title": "Chapter 1", "content": "Prose."},
    )
    r = client.get(f"/api/projects/{p['id']}/tts/export")
    assert r.status_code == 200
    with wave.open(BytesIO(r.content), "rb") as w:
        assert w.getframerate() == 22050
    assert "engine=piper" in r.headers.get("x-audio-meta", "")


def test_tts_export_engine_auto_prefers_audio8_at_16k(client):
    _, fake = _audio8_fake(client)
    fake._available = True
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    client.post(
        f"/api/projects/{p['id']}/chapters",
        json={"title": "Chapter 1", "content": "Prose."},
    )
    r = client.get(f"/api/projects/{p['id']}/tts/export?engine=auto")
    assert r.status_code == 200
    with wave.open(BytesIO(r.content), "rb") as w:
        assert w.getframerate() == 16000
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
    meta = r.headers.get("x-audio-meta", "")
    assert "engine=audio8" in meta
    assert "rate=16000" in meta


def test_tts_export_explicit_audio8_unavailable_is_503(client):
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    client.post(
        f"/api/projects/{p['id']}/chapters",
        json={"title": "Chapter 1", "content": "Prose."},
    )
    r = client.get(f"/api/projects/{p['id']}/tts/export?engine=audio8")
    assert r.status_code == 503
    assert "audio8 download" in r.json()["detail"]


def test_tts_export_rejects_unknown_engine(client):
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    r = client.get(f"/api/projects/{p['id']}/tts/export?engine=elevenlabs")
    assert r.status_code == 400


def test_tts_export_clone_requires_saved_profile(client):
    _, fake = _audio8_fake(client)
    fake._available = True
    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    client.post(
        f"/api/projects/{p['id']}/chapters",
        json={"title": "Chapter 1", "content": "Prose."},
    )
    r = client.get(f"/api/projects/{p['id']}/tts/export?clone=true")
    assert r.status_code == 400
    assert "author voice" in r.json()["detail"].lower()


def test_tts_export_clone_with_profile_passes_reference(client, tmp_path, monkeypatch):
    import struct

    _, fake = _audio8_fake(client)
    fake._available = True

    sr = 16000
    samples = [int(8000 * math.sin(2 * math.pi * 220 * i / sr)) for i in range(sr)]
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    up = client.post(
        "/api/tts/author-voice",
        files={"file": ("me.wav", buf.getvalue(), "audio/wav")},
        data={"transcript": "A spoken reference sentence."},
    )
    assert up.status_code == 200
    assert up.json()["saved"] is True

    p = client.post("/api/projects", json={"title": "Audio Book"}).json()
    client.post(
        f"/api/projects/{p['id']}/chapters",
        json={"title": "Chapter 1", "content": "Prose."},
    )
    r = client.get(f"/api/projects/{p['id']}/tts/export?engine=audio8&clone=true")
    assert r.status_code == 200
    waveform, rate, transcript = fake.references[0]
    assert rate == sr and len(waveform) == sr
    assert transcript == "A spoken reference sentence."


def test_author_voice_upload_validation(client):
    r = client.post(
        "/api/tts/author-voice",
        files={"file": ("bad.wav", b"not audio", "audio/wav")},
        data={"transcript": "words"},
    )
    assert r.status_code == 400

    good = _wav_bytes(1.0)
    r2 = client.post(
        "/api/tts/author-voice",
        files={"file": ("me.wav", good, "audio/wav")},
        data={"transcript": "   "},
    )
    assert r2.status_code == 400


def test_author_voice_status_and_delete(client):
    st = client.post(
        "/api/tts/author-voice",
        files={"file": ("me.wav", _wav_bytes(1.0), "audio/wav")},
        data={"transcript": "Reference line."},
    )
    assert st.status_code == 200
    status = client.get("/api/tts/author-voice/status").json()
    assert status["exists"] is True
    assert status["seconds"] >= 1.0
    assert "Reference line." in status["transcript"]

    deleted = client.delete("/api/tts/author-voice").json()
    assert deleted["removed"] is True
    assert client.get("/api/tts/author-voice/status").json()["exists"] is False


def _wav_bytes(seconds: float) -> bytes:
    import math
    import struct

    sr = 16000
    n = int(sr * seconds)
    samples = [int(8000 * math.sin(2 * math.pi * 220 * i / sr)) for i in range(n)]
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return buf.getvalue()


def test_resample_to_scales_length():
    import numpy as np

    from app.services.audio8 import OUTPUT_RATE, resample_to

    x = np.zeros(44100, dtype=np.float32)  # 1 s at native codec rate
    y = resample_to(x, 44100, OUTPUT_RATE)
    assert len(y) == OUTPUT_RATE
    assert y.dtype == np.float32
    # Identity case returns the input untouched.
    same = resample_to(x, 44100, 44100)
    assert len(same) == 44100


def test_chunk_text_respects_limit_and_sentences():
    from app.services.audio8 import MAX_CHUNK_CHARS, _chunk_text

    text = " ".join(f"Sentence number {i} is here." for i in range(60))
    chunks = _chunk_text(text)
    assert all(len(c) <= MAX_CHUNK_CHARS for c in chunks)
    assert " ".join(chunks).split() == text.split()

    monster = "word " * 500 + "end."  # one unbroken run, no sentence breaks
    hard = _chunk_text(monster, limit=300)
    assert all(len(c) <= 300 for c in hard)

    assert _chunk_text("   ") == []

