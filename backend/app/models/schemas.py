from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
import uuid


def new_id() -> str:
    return str(uuid.uuid4())


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Characters ──────────────────────────────────────────────

class CharacterBase(BaseModel):
    name: str
    role: str = ""
    physical_traits: str = ""
    personality: str = ""
    motivations: str = ""
    speech_patterns: str = ""
    backstory: str = ""
    notes: str = ""
    relationships: str = ""


class CharacterCreate(CharacterBase):
    pass


class CharacterUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    physical_traits: Optional[str] = None
    personality: Optional[str] = None
    motivations: Optional[str] = None
    speech_patterns: Optional[str] = None
    backstory: Optional[str] = None
    notes: Optional[str] = None
    relationships: Optional[str] = None


class Character(CharacterBase):
    id: str = Field(default_factory=new_id)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# ── Locations ──────────────────────────────────────────────

class LocationBase(BaseModel):
    name: str
    type: str = ""
    description: str = ""
    notes: str = ""


class LocationCreate(LocationBase):
    pass


class LocationUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[str] = None


class Location(LocationBase):
    id: str = Field(default_factory=new_id)
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# ── Chapters ────────────────────────────────────────────────

class ChapterBase(BaseModel):
    title: str
    content: str = ""
    order: int = 0
    summary: str = ""


class ChapterCreate(ChapterBase):
    pass


class ChapterUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    order: Optional[int] = None
    summary: Optional[str] = None


class Chapter(ChapterBase):
    id: str = Field(default_factory=new_id)
    word_count: int = 0
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# ── Projects ────────────────────────────────────────────────

class ProjectBase(BaseModel):
    title: str
    description: str = ""
    genre: str = ""
    premise: str = ""
    fork_of: Optional[str] = None
    author: str = ""
    publisher: str = ""
    copyright: str = ""
    isbn: str = ""
    series: str = ""
    series_position: int = 0
    language: str = "en"


class ProjectCreate(ProjectBase):
    pass


class ProjectUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    genre: Optional[str] = None
    premise: Optional[str] = None
    fork_of: Optional[str] = None
    author: Optional[str] = None
    publisher: Optional[str] = None
    copyright: Optional[str] = None
    isbn: Optional[str] = None
    series: Optional[str] = None
    series_position: Optional[int] = None
    language: Optional[str] = None


class Project(ProjectBase):
    id: str = Field(default_factory=new_id)
    characters: list[Character] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    world_notes: str = ""
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


class ProjectSummary(BaseModel):
    id: str
    title: str
    description: str = ""
    genre: str = ""
    fork_of: Optional[str] = None
    series: str = ""
    chapter_count: int = 0
    character_count: int = 0
    location_count: int = 0
    word_count: int = 0
    updated_at: str = ""


# ── Series ─────────────────────────────────────────────────

class SeriesBible(BaseModel):
    """Shared worldbuilding doc + cross-book cast and locations for a series name."""
    name: str
    world_notes: str = ""
    characters: list[Character] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    updated_at: str = Field(default_factory=_now)


class SeriesBibleUpdate(BaseModel):
    world_notes: Optional[str] = None
    characters: Optional[list[Character]] = None
    locations: Optional[list[Location]] = None


class SeriesInfo(BaseModel):
    name: str
    books: list[ProjectSummary] = Field(default_factory=list)
    world_notes: str = ""
    character_count: int = 0


# ── Extraction ─────────────────────────────────────────────

class ExtractedCharacter(BaseModel):
    """A character proposal parsed from story prose (no id yet)."""
    name: str
    role: str = ""
    physical_traits: str = ""
    personality: str = ""
    motivations: str = ""
    speech_patterns: str = ""
    backstory: str = ""
    relationships: str = ""
    notes: str = ""


class ExtractedLocation(BaseModel):
    """A location proposal parsed from story prose (no id yet)."""
    name: str
    type: str = ""
    description: str = ""
    notes: str = ""


class ExtractRequest(BaseModel):
    project_id: str
    chapter_id: Optional[str] = None


class ExtractResponse(BaseModel):
    characters: list[ExtractedCharacter] = Field(default_factory=list)
    locations: list[ExtractedLocation] = Field(default_factory=list)
    world_facts: list[str] = Field(default_factory=list)
    raw: str = ""


# ── AI / Assist ─────────────────────────────────────────────

class AssistRequest(BaseModel):
    project_id: str
    chapter_id: Optional[str] = None
    prompt: str
    mode: str = "brainstorm"  # brainstorm | continue | consistency | lore | plot
    context_text: str = ""


class AssistResponse(BaseModel):
    response: str
    sources: list[str] = Field(default_factory=list)
    mode: str = "brainstorm"
    llm_available: bool = True


class IndexRequest(BaseModel):
    project_id: str
    chapter_id: Optional[str] = None


class IndexResponse(BaseModel):
    indexed_chunks: int
    message: str


class HealthResponse(BaseModel):
    status: str
    llm_available: bool
    embedding_ready: bool


# ── Import ────────────────────────────────────────────────

class ImportedProject(BaseModel):
    """A single book created from an imported manuscript file."""
    id: str
    title: str
    series: str = ""
    series_position: int = 0
    chapter_count: int = 0
    word_count: int = 0
    character_count: int = 0
    location_count: int = 0


class ImportReport(BaseModel):
    """Outcome of a manuscript import: created books + what got populated."""
    mode: str = "single"  # single | series | merge
    series: str = ""
    projects: list[ImportedProject] = Field(default_factory=list)
    llm_used: bool = False
    characters_added: int = 0
    locations_added: int = 0
    world_facts_added: int = 0
    summaries_generated: int = 0
    bible_updated: bool = False
    warnings: list[str] = Field(default_factory=list)
