import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.db.storage import get_store
from app.services import audio8 as audio8_svc
from app.services import tts as tts_svc

logger = logging.getLogger("ghostwriter.tts.api")

router = APIRouter(prefix="/projects", tags=["tts"])
voice_router = APIRouter(prefix="/tts", tags=["tts"])

def _tts():
    return tts_svc.get_tts()


def _audio8():
    return audio8_svc.get_audio8()


def _project(project_id: str):
    try:
        return get_store().get_project(project_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


def _data_dir():
    from app.config import get_settings

    return get_settings().data_dir


@router.get("/{project_id}/tts/status")
def tts_status(project_id: str):
    _project(project_id)
    tts = _tts()
    a8 = _audio8()
    return {
        "available": tts.available(),
        "voice": "en_US-lessac-medium",
        "guardrail": tts.guardrail_text(),
        "guardrail_interval_seconds": tts.guardrail_interval_seconds(),
        "pacing_defaults": tts_svc.Pacing().as_dict(),
        "audio8": {
            "model_id": audio8_svc.MODEL_ID,
            "available": a8.available(),
            "output_rate": audio8_svc.OUTPUT_RATE,
        },
        "author_voice": audio8_svc.author_voice_status(_data_dir()),
    }


def _pacing(
    paragraph_pause,
    scene_pause,
    chapter_pause,
    quote_pause,
    comma_pause,
    speech_rate,
) -> "tts_svc.Pacing":
    return tts_svc.Pacing(
        paragraph_pause=paragraph_pause,
        scene_pause=scene_pause,
        chapter_pause=chapter_pause,
        quote_pause=quote_pause,
        comma_pause=comma_pause,
        speech_rate=speech_rate,
    )


@router.post("/{project_id}/tts/preview")
def tts_preview(
    project_id: str,
    payload: dict | None = None,
    text: str = Query("", alias="text"),
    paragraph_pause: float = Query(None, alias="paragraph_pause"),
    scene_pause: float = Query(None, alias="scene_pause"),
    chapter_pause: float = Query(None, alias="chapter_pause"),
    quote_pause: float = Query(None, alias="quote_pause"),
    comma_pause: float = Query(None, alias="comma_pause"),
    speech_rate: float = Query(None, alias="speech_rate"),
):
    """Quick low-fidelity clip of a short selection (no guardrail)."""
    _project(project_id)
    text = (payload or {}).get("text", "") if payload else text
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="Selection too long (max 5000 chars)")

    tts = _tts()
    if not tts.available():
        raise HTTPException(
            status_code=501,
            detail=(
                "TTS voice not downloaded yet. Run "
                "`python -m app.services.tts download` in the backend and retry."
            ),
        )
    try:
        pacing = _pacing(
            paragraph_pause, scene_pause, chapter_pause, quote_pause, comma_pause, speech_rate
        )
        wav = tts.preview_wav(text, pacing)
    except RuntimeError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e

    return Response(
        content=wav,
        media_type="audio/wav",
        headers={"Content-Disposition": 'inline; filename="preview.wav"'},
    )


@router.get("/{project_id}/tts/export")
def tts_export(
    project_id: str,
    paragraph_pause: float = Query(None, alias="paragraph_pause"),
    scene_pause: float = Query(None, alias="scene_pause"),
    chapter_pause: float = Query(None, alias="chapter_pause"),
    quote_pause: float = Query(None, alias="quote_pause"),
    comma_pause: float = Query(None, alias="comma_pause"),
    speech_rate: float = Query(None, alias="speech_rate"),
    engine: str = Query(None, alias="engine"),
    clone: bool = Query(False, alias="clone"),
):
    """Full-book audiobook example — WAV with guardrail disclaimers embedded.

    engine: "auto" (default; audio8 when downloaded, else piper), "audio8"
    (16 kHz, optional voice clone) or "piper" (legacy 22.05 kHz).
    """
    project = _project(project_id)
    chapters = sorted(project.chapters, key=lambda c: c.order)
    if not chapters:
        raise HTTPException(status_code=400, detail="No chapters to synthesize.")

    pacing = _pacing(
        paragraph_pause, scene_pause, chapter_pause, quote_pause, comma_pause, speech_rate
    )

    use_engine = _resolve_engine(engine)
    reference = None
    if clone:
        if use_engine != "audio8":
            raise HTTPException(
                status_code=400,
                detail="Voice cloning requires the Audio8 engine.",
            )
        reference = audio8_svc.load_author_voice(_data_dir())
        if reference is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "No author voice saved yet. Upload a reference recording "
                    "and transcript first."
                ),
            )
    if use_engine == "audio8":
        tts_impl = _audio8()
        if not tts_impl.available():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Audio8 model not downloaded. Run "
                    "`python -m app.services.audio8 download` in the backend "
                    "or export with engine=piper."
                ),
            )
    else:
        tts_impl = _tts()
        if not tts_impl.available():
            raise HTTPException(
                status_code=501,
                detail=(
                    "TTS voice not downloaded yet. Run "
                    "`python -m app.services.tts download` in the backend and retry."
                ),
            )

    tmp = Path(tempfile.mkstemp(suffix=".wav", prefix="ghostwriter-audio-")[1])
    try:
        try:
            if use_engine == "audio8":
                meta = tts_impl.export_book(chapters, tmp, pacing, reference)
            else:
                meta = tts_impl.export_book(chapters, tmp, pacing)
        except RuntimeError as e:
            raise HTTPException(status_code=501, detail=str(e)) from e
        slug = (project.title or "manuscript").lower().replace(" ", "-")
        filename = f"{slug}-audiobook-example.wav"
        rate = meta.get("rate", 22050)
        return Response(
            content=tmp.read_bytes(),
            media_type="audio/wav",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Audio-Meta": (
                    f"duration={meta['duration_seconds']:.0f}s;"
                    f"chapters={meta['chapters']};"
                    f"disclaimers={meta['disclaimers']};"
                    f"engine={use_engine};rate={rate}"
                ),
            },
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def _resolve_engine(requested: str | None) -> str:
    """Resolve the requested engine against settings and availability.

    "auto" (explicit or via GW_TTS_ENGINE=auto) prefers audio8 when its model
    is present on disk and falls back to piper otherwise.
    """
    choice = (requested or "").strip().lower()
    if not choice or choice == "auto":
        from app.config import get_settings

        settings_engine = (get_settings().tts_engine or "auto").strip().lower()
        if settings_engine in ("audio8", "piper"):
            choice = settings_engine
    if not choice or choice == "auto":
        return "audio8" if _audio8().available() else "piper"
    if choice in ("audio8", "piper"):
        return choice
    raise HTTPException(
        status_code=400, detail="engine must be one of: auto, audio8, piper"
    )


@voice_router.get("/author-voice/status")
def author_voice_status():
    return audio8_svc.author_voice_status(_data_dir())


@voice_router.post("/author-voice")
async def author_voice_upload(
    file: UploadFile = File(...),
    transcript: str = Form(...),
):
    """Save the author's own voice (reference recording + verbatim transcript)."""
    raw = await file.read()
    try:
        result = audio8_svc.save_author_voice(_data_dir(), raw, transcript)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"saved": True, **result}


@voice_router.delete("/author-voice")
def author_voice_delete():
    removed = audio8_svc.clear_author_voice(_data_dir())
    return {"removed": removed}
