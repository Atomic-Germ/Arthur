from pathlib import Path
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_BACKEND_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data"
_ENV_FILES = (
    _BACKEND_DIR / ".env",
    Path.cwd() / ".env",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GW_",
        env_file=[str(p) for p in _ENV_FILES if p.exists()] or ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Arthur"
    debug: bool = True

    data_dir: Path = Field(default=_DEFAULT_DATA)
    projects_dir: Path | None = None
    series_dir: Path | None = None
    chroma_dir: Path | None = None

    # LLM: OpenAI-compatible endpoint (llama.cpp server, Ollama, OpenAI, etc.)
    llm_base_url: str = "http://127.0.0.1:8080/v1"
    llm_api_key: str = "not-needed"
    llm_model: str = "local-model"
    llm_max_tokens: int = 320000
    llm_temperature: float = 0.3
    # Max seconds to wait for the LLM (large contexts on local models take
    # minutes; applies to streamed and non-streamed calls alike)
    llm_request_timeout: float = 18000.0

    # Embeddings: "hash" (default, fast, no torch) or "st" (sentence-transformers)
    embedding_backend: str = "st"
    embedding_model: str = "all-MiniLM-L6-v2"
    rag_top_k: int = 5
    chunk_size: int = 500
    chunk_overlap: int = 80
    # How much manuscript prose to pack into assist context (chars).
    # All chapters are always listed; bodies are budgeted fairly across them.
    manuscript_context_chars: int = 128000
    # Auto-index on save (safe with hash backend)
    auto_index: bool = True

    # TTS: engine for the guarded audiobook example ("auto" prefers audio8,
    # falling back to piper when its model is absent; the in-editor Listen
    # clip always uses piper). tts_device runs the Audio8 engine: "cpu" by
    # default (some ROCm builds segfault in mamba kernels); set "cuda" to
    # opt in to GPU inference where the stack supports it.
    tts_engine: str = "auto"
    tts_device: str = "cpu"

    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
    ]

    @model_validator(mode="after")
    def _paths(self) -> "Settings":
        self.projects_dir = self.data_dir / "projects"
        self.series_dir = self.data_dir / "series"
        self.chroma_dir = self.data_dir / "chroma"
        return self

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.series_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
