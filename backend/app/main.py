import logging
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import assist, chapters, characters, export, extract, imports, locations, projects, series, tts
from app.config import get_settings
from app.models.schemas import HealthResponse
from app.services.embeddings import get_status as embedding_status, is_embedding_ready
from app.services.indexer import get_indexer
from app.services.llm import get_llm

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("ghostwriter")


def _warmup_background() -> None:
    try:
        from app.services.embeddings import warm_embeddings

        ok = warm_embeddings()
        logger.info("Embedding warmup %s (%s)", "ok" if ok else "failed", embedding_status())
    except Exception:  # noqa: BLE001
        logger.exception("Embedding warmup crashed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    get_settings().ensure_dirs()
    get_indexer().start()
    # Hash embedder is instant; ST is heavy — only warm when not skipped
    if os.environ.get("GW_SKIP_EMBED_WARMUP", "").lower() not in ("1", "true", "yes"):
        threading.Thread(
            target=_warmup_background,
            name="ghostwriter-embed-warmup",
            daemon=True,
        ).start()
    yield
    get_indexer().stop()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        description="Intelligent writing companion with story-aware RAG",
        version="0.1.1",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(projects.router, prefix="/api")
    app.include_router(characters.router, prefix="/api")
    app.include_router(chapters.router, prefix="/api")
    app.include_router(export.router, prefix="/api")
    app.include_router(assist.router, prefix="/api")
    app.include_router(series.router, prefix="/api")
    app.include_router(extract.router, prefix="/api")
    app.include_router(imports.router, prefix="/api")
    app.include_router(locations.router, prefix="/api")
    app.include_router(tts.router, prefix="/api")
    app.include_router(tts.voice_router, prefix="/api")

    @app.get("/api/health", response_model=HealthResponse)
    async def health():
        # Keep this tiny — never touch embeddings/torch here
        llm_ok = await get_llm().check_available()
        return HealthResponse(
            status="ok",
            llm_available=llm_ok,
            embedding_ready=is_embedding_ready(),
        )

    @app.get("/api/health/detail")
    async def health_detail():
        llm = get_llm()
        llm_ok = await llm.check_available()
        from app.services import embeddings as emb
        from app.services.indexer import get_indexer

        return {
            "status": "ok",
            "llm_available": llm_ok,
            "llm_base_url": llm.base_url,
            "llm_model": llm._model_name(),
            "embedding_ready": is_embedding_ready(),
            "embedding_status": embedding_status(),
            "embedding_backend": get_settings().embedding_backend,
            "embedding_error": emb.get_load_error(),
            "index_error": get_indexer().last_error,
        }

    @app.get("/")
    def root():
        return {
            "name": settings.app_name,
            "docs": "/docs",
            "health": "/api/health",
        }

    return app


app = create_app()
