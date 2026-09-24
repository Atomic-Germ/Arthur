"""JSON file-based project storage with atomic writes + lock."""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.models.schemas import (
    Chapter,
    ChapterCreate,
    ChapterUpdate,
    Character,
    CharacterCreate,
    CharacterUpdate,
    Location,
    LocationCreate,
    LocationUpdate,
    Project,
    ProjectCreate,
    ProjectSummary,
    ProjectUpdate,
    SeriesBible,
    SeriesBibleUpdate,
    SeriesInfo,
    new_id,
)

_file_lock = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _word_count(text: str) -> int:
    return len(text.split()) if text.strip() else 0


class ProjectStore:
    def __init__(
        self,
        base_dir: Optional[Path] = None,
        series_dir: Optional[Path] = None,
    ):
        settings = get_settings()
        self.base_dir = base_dir or settings.projects_dir
        self.series_dir = series_dir or settings.series_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.series_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, project_id: str) -> Path:
        return self.base_dir / f"{project_id}.json"

    def _load(self, project_id: str) -> Project:
        path = self._path(project_id)
        if not path.exists():
            raise FileNotFoundError(f"Project {project_id} not found")
        raw = path.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # Recover from trailing garbage left by a raced write
            data, _end = json.JSONDecoder().raw_decode(raw)
            path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        project = Project.model_validate(data)
        if self._renumber_chapters(project):
            self._save(project)
        return project

    @staticmethod
    def _renumber_chapters(project: Project) -> bool:
        """Keep chapter.order == list position (0..n-1).

        The chapters list is always kept in display order (append on add,
        filter on delete), so renumbering by list position repairs the
        drift that otherwise accumulates when chapters are deleted — which
        previously made later chapters sort into earlier positions.
        Returns True if any order was changed.
        """
        changed = False
        for i, ch in enumerate(project.chapters):
            if ch.order != i:
                ch.order = i
                changed = True
        return changed

    def _save(self, project: Project) -> Project:
        project.updated_at = _now()
        path = self._path(project.id)
        payload = project.model_dump_json(indent=2)
        # Unique tmp name avoids races when two writers share *.json.tmp
        tmp = path.with_name(f".{path.stem}.{os.getpid()}.{new_id()[:8]}.tmp")
        try:
            tmp.write_text(payload + "\n", encoding="utf-8")
            os.replace(tmp, path)
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
        return project

    def list_projects(self) -> list[ProjectSummary]:
        with _file_lock:
            summaries: list[ProjectSummary] = []
            for path in sorted(self.base_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    chapters = data.get("chapters", [])
                    characters = data.get("characters", [])
                    word_count = sum(
                        _word_count(c.get("content", "")) for c in chapters
                    )
                    summaries.append(
                        ProjectSummary(
                            id=data["id"],
                            title=data.get("title", "Untitled"),
                            description=data.get("description", ""),
                            genre=data.get("genre", ""),
                            fork_of=data.get("fork_of"),
                            series=data.get("series", ""),
                            chapter_count=len(chapters),
                            character_count=len(characters),
                            location_count=len(data.get("locations", [])),
                            word_count=word_count,
                            updated_at=data.get("updated_at", ""),
                        )
                    )
                except (json.JSONDecodeError, KeyError, OSError):
                    continue
            summaries.sort(key=lambda s: s.updated_at, reverse=True)
            return summaries

    def get_project(self, project_id: str) -> Project:
        with _file_lock:
            return self._load(project_id)

    def create_project(self, payload: ProjectCreate) -> Project:
        with _file_lock:
            project = Project(
                id=new_id(),
                title=payload.title,
                description=payload.description,
                genre=payload.genre,
                premise=payload.premise,
                author=payload.author,
                publisher=payload.publisher,
                copyright=payload.copyright,
                isbn=payload.isbn,
                series=payload.series,
                series_position=payload.series_position,
                language=payload.language,
                characters=[],
                chapters=[],
                locations=[],
                world_notes="",
                created_at=_now(),
                updated_at=_now(),
            )
            return self._save(project)

    def update_project(self, project_id: str, payload: ProjectUpdate) -> Project:
        with _file_lock:
            project = self._load(project_id)
            data = payload.model_dump(exclude_unset=True)
            for key, value in data.items():
                setattr(project, key, value)
            return self._save(project)

    def delete_project(self, project_id: str) -> None:
        with _file_lock:
            path = self._path(project_id)
            if not path.exists():
                raise FileNotFoundError(f"Project {project_id} not found")
            path.unlink()

    def update_world_notes(self, project_id: str, notes: str) -> Project:
        with _file_lock:
            project = self._load(project_id)
            project.world_notes = notes
            return self._save(project)

    def fork_project(self, project_id: str, title_override: str | None = None) -> Project:
        with _file_lock:
            original = self._load(project_id)
            new_title = title_override or f"{original.title} (fork)"
            now = _now()
            id_map: dict[str, str] = {}

            def remap_id(obj_id: str) -> str:
                if obj_id not in id_map:
                    id_map[obj_id] = new_id()
                return id_map[obj_id]

            new_chars = [
                Character(
                    id=remap_id(c.id),
                    name=c.name,
                    role=c.role,
                    physical_traits=c.physical_traits,
                    personality=c.personality,
                    motivations=c.motivations,
                    speech_patterns=c.speech_patterns,
                    backstory=c.backstory,
                    notes=c.notes,
                    relationships=c.relationships,
                    created_at=now,
                    updated_at=now,
                )
                for c in original.characters
            ]

            new_chapters = [
                Chapter(
                    id=remap_id(ch.id),
                    title=ch.title,
                    content=ch.content,
                    order=ch.order,
                    summary=ch.summary,
                    word_count=ch.word_count,
                    created_at=now,
                    updated_at=now,
                )
                for ch in original.chapters
            ]

            new_locations = [
                Location(
                    id=remap_id(loc.id),
                    name=loc.name,
                    type=loc.type,
                    description=loc.description,
                    notes=loc.notes,
                    created_at=now,
                    updated_at=now,
                )
                for loc in original.locations
            ]

            forked = Project(
                id=new_id(),
                title=new_title,
                description=original.description,
                genre=original.genre,
                premise=original.premise,
                fork_of=original.id,
                author=original.author,
                publisher=original.publisher,
                copyright=original.copyright,
                isbn=original.isbn,
                series=original.series,
                series_position=original.series_position,
                language=original.language,
                characters=new_chars,
                chapters=new_chapters,
                locations=new_locations,
                world_notes=original.world_notes,
                created_at=now,
                updated_at=now,
            )
            return self._save(forked)

    def list_characters(self, project_id: str) -> list[Character]:
        return self.get_project(project_id).characters

    def get_character(self, project_id: str, character_id: str) -> Character:
        project = self.get_project(project_id)
        for c in project.characters:
            if c.id == character_id:
                return c
        raise FileNotFoundError(f"Character {character_id} not found")

    def add_character(self, project_id: str, payload: CharacterCreate) -> Character:
        with _file_lock:
            project = self._load(project_id)
            character = Character(
                id=new_id(),
                **payload.model_dump(),
                created_at=_now(),
                updated_at=_now(),
            )
            project.characters.append(character)
            self._save(project)
            return character

    def update_character(
        self, project_id: str, character_id: str, payload: CharacterUpdate
    ) -> Character:
        with _file_lock:
            project = self._load(project_id)
            for i, c in enumerate(project.characters):
                if c.id == character_id:
                    data = c.model_dump()
                    data.update(payload.model_dump(exclude_unset=True))
                    data["updated_at"] = _now()
                    updated = Character.model_validate(data)
                    project.characters[i] = updated
                    self._save(project)
                    return updated
            raise FileNotFoundError(f"Character {character_id} not found")

    def delete_character(self, project_id: str, character_id: str) -> None:
        with _file_lock:
            project = self._load(project_id)
            before = len(project.characters)
            project.characters = [c for c in project.characters if c.id != character_id]
            if len(project.characters) == before:
                raise FileNotFoundError(f"Character {character_id} not found")
            self._save(project)

    def list_chapters(self, project_id: str) -> list[Chapter]:
        chapters = self.get_project(project_id).chapters
        return sorted(chapters, key=lambda c: c.order)

    def get_chapter(self, project_id: str, chapter_id: str) -> Chapter:
        project = self.get_project(project_id)
        for c in project.chapters:
            if c.id == chapter_id:
                return c
        raise FileNotFoundError(f"Chapter {chapter_id} not found")

    def add_chapter(self, project_id: str, payload: ChapterCreate) -> Chapter:
        with _file_lock:
            project = self._load(project_id)
            order = payload.order if payload.order else len(project.chapters)
            chapter = Chapter(
                id=new_id(),
                title=payload.title,
                content=payload.content,
                order=order,
                summary=payload.summary,
                word_count=_word_count(payload.content),
                created_at=_now(),
                updated_at=_now(),
            )
            project.chapters.append(chapter)
            self._renumber_chapters(project)
            self._save(project)
            return chapter

    def update_chapter(
        self, project_id: str, chapter_id: str, payload: ChapterUpdate
    ) -> Chapter:
        with _file_lock:
            project = self._load(project_id)
            for i, c in enumerate(project.chapters):
                if c.id == chapter_id:
                    data = c.model_dump()
                    updates = payload.model_dump(exclude_unset=True)
                    data.update(updates)
                    if "content" in updates:
                        data["word_count"] = _word_count(data["content"])
                    data["updated_at"] = _now()
                    updated = Chapter.model_validate(data)
                    project.chapters[i] = updated
                    self._save(project)
                    return updated
            raise FileNotFoundError(f"Chapter {chapter_id} not found")

    def delete_chapter(self, project_id: str, chapter_id: str) -> None:
        with _file_lock:
            project = self._load(project_id)
            before = len(project.chapters)
            project.chapters = [c for c in project.chapters if c.id != chapter_id]
            if len(project.chapters) == before:
                raise FileNotFoundError(f"Chapter {chapter_id} not found")
            self._renumber_chapters(project)
            self._save(project)

    # ── Locations ────────────────────────────────────────

    def list_locations(self, project_id: str) -> list[Location]:
        return self.get_project(project_id).locations

    def get_location(self, project_id: str, location_id: str) -> Location:
        project = self.get_project(project_id)
        for loc in project.locations:
            if loc.id == location_id:
                return loc
        raise FileNotFoundError(f"Location {location_id} not found")

    def add_location(self, project_id: str, payload: LocationCreate) -> Location:
        with _file_lock:
            project = self._load(project_id)
            location = Location(
                id=new_id(),
                **payload.model_dump(),
                created_at=_now(),
                updated_at=_now(),
            )
            project.locations.append(location)
            self._save(project)
            return location

    def update_location(
        self, project_id: str, location_id: str, payload: LocationUpdate
    ) -> Location:
        with _file_lock:
            project = self._load(project_id)
            for i, loc in enumerate(project.locations):
                if loc.id == location_id:
                    data = loc.model_dump()
                    data.update(payload.model_dump(exclude_unset=True))
                    data["updated_at"] = _now()
                    updated = Location.model_validate(data)
                    project.locations[i] = updated
                    self._save(project)
                    return updated
            raise FileNotFoundError(f"Location {location_id} not found")

    def delete_location(self, project_id: str, location_id: str) -> None:
        with _file_lock:
            project = self._load(project_id)
            before = len(project.locations)
            project.locations = [
                loc for loc in project.locations if loc.id != location_id
            ]
            if len(project.locations) == before:
                raise FileNotFoundError(f"Location {location_id} not found")
            self._save(project)

    # ── Bulk import helpers (single write per batch) ─────

    def add_chapters(
        self, project_id: str, payloads: list[ChapterCreate]
    ) -> list[Chapter]:
        with _file_lock:
            project = self._load(project_id)
            now = _now()
            added: list[Chapter] = []
            for i, payload in enumerate(payloads):
                chapter = Chapter(
                    id=new_id(),
                    title=payload.title,
                    content=payload.content,
                    order=payload.order if payload.order else len(project.chapters),
                    summary=payload.summary,
                    word_count=_word_count(payload.content),
                    created_at=now,
                    updated_at=now,
                )
                if not chapter.content.strip():
                    continue
                project.chapters.append(chapter)
                added.append(chapter)
            self._renumber_chapters(project)
            if added:
                self._save(project)
            return added

    def add_characters(
        self, project_id: str, payloads: list[CharacterCreate]
    ) -> list[Character]:
        with _file_lock:
            project = self._load(project_id)
            now = _now()
            existing = {
                c.name.strip().lower() for c in project.characters if c.name.strip()
            }
            added: list[Character] = []
            for payload in payloads:
                key = (payload.name or "").strip().lower()
                if not key or key in existing:
                    continue
                existing.add(key)
                character = Character(
                    id=new_id(),
                    **payload.model_dump(),
                    created_at=now,
                    updated_at=now,
                )
                project.characters.append(character)
                added.append(character)
            if added:
                self._save(project)
            return added

    def add_locations(
        self, project_id: str, payloads: list[LocationCreate]
    ) -> list[Location]:
        with _file_lock:
            project = self._load(project_id)
            now = _now()
            existing = {
                loc.name.strip().lower()
                for loc in project.locations
                if loc.name.strip()
            }
            added: list[Location] = []
            for payload in payloads:
                key = (payload.name or "").strip().lower()
                if not key or key in existing:
                    continue
                existing.add(key)
                location = Location(
                    id=new_id(),
                    **payload.model_dump(),
                    created_at=now,
                    updated_at=now,
                )
                project.locations.append(location)
                added.append(location)
            if added:
                self._save(project)
            return added

    def update_chapter_summaries(
        self, project_id: str, summaries: dict[str, str]
    ) -> Project:
        """Set summary by chapter id (add-only: never overwrite a summary)."""
        with _file_lock:
            project = self._load(project_id)
            changed = False
            for ch in project.chapters:
                if ch.id in summaries and not (ch.summary or "").strip():
                    ch.summary = (summaries.get(ch.id) or "").strip()
                    ch.updated_at = _now()
                    changed = True
            if changed:
                self._save(project)
            return project

    def append_world_notes_lines(self, project_id: str, lines: list[str]) -> Project:
        """Append facts as '- fact' bullets, skipping ones already present."""
        with _file_lock:
            project = self._load(project_id)
            current = (project.world_notes or "").split("\n")
            seen = {ln.strip().lower() for ln in current if ln.strip()}
            to_add: list[str] = []
            for fact in lines:
                s = (fact or "").strip()
                if not s:
                    continue
                if s.lower() in seen:
                    continue
                if any(s.lower() in l for l in seen):
                    continue
                seen.add(s.lower())
                to_add.append(f"- {s}")
            if to_add:
                project.world_notes = ("\n".join(current).strip() + "\n\n" + "\n".join(to_add)).strip() if current else "\n".join(to_add)
                self._save(project)
            return project

    # ── Series ─────────────────────────────────────────────

    def _series_path(self, name: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).lower()
        safe = safe.strip("-") or "untitled-series"
        return self.series_dir / f"{safe}.json"

    def list_series(self) -> list[SeriesInfo]:
        """Group projects by their series name; returns summaries + bible info."""
        with _file_lock:
            summaries = self.list_projects()
            grouped: dict[str, list[ProjectSummary]] = {}
            for s in summaries:
                if s.series.strip():
                    grouped.setdefault(s.series.strip(), []).append(s)
            out: list[SeriesInfo] = []
            for name, books in sorted(grouped.items(), key=lambda kv: kv[0].lower()):
                bible = self._load_series_bible(name)
                out.append(
                    SeriesInfo(
                        name=name,
                        books=sorted(books, key=lambda b: b.title.lower()),
                        world_notes=bible.world_notes if bible else "",
                        character_count=len(bible.characters) if bible else 0,
                    )
                )
            return out

    def _load_series_bible(self, name: str) -> Optional[SeriesBible]:
        path = self._series_path(name)
        if not path.exists():
            return None
        try:
            return SeriesBible.model_validate_json(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    def get_series_bible(self, name: str) -> SeriesBible:
        bible = self._load_series_bible(name)
        if bible is None:
            return SeriesBible(name=name)
        return bible

    def update_series_bible(self, name: str, payload: SeriesBibleUpdate) -> SeriesBible:
        with _file_lock:
            bible = self._load_series_bible(name) or SeriesBible(name=name)
            if payload.world_notes is not None:
                bible.world_notes = payload.world_notes
            if payload.characters is not None:
                bible.characters = payload.characters
            if payload.locations is not None:
                bible.locations = payload.locations
            bible.updated_at = _now()
            path = self._series_path(name)
            tmp = path.with_name(f".{path.stem}.{os.getpid()}.{new_id()[:8]}.tmp")
            try:
                tmp.write_text(bible.model_dump_json(indent=2) + "\n", encoding="utf-8")
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    try:
                        tmp.unlink()
                    except OSError:
                        pass
            return bible

    def projects_in_series(self, name: str) -> list[Project]:
        """Full project objects for every book sharing this series name."""
        with _file_lock:
            projects: list[Project] = []
            for path in sorted(self.base_dir.glob("*.json")):
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if (data.get("series") or "").strip() == name:
                    try:
                        projects.append(Project.model_validate(data))
                    except (KeyError, ValueError):
                        continue
            projects.sort(key=lambda p: (p.series_position, p.title.lower()))
            return projects


_store: Optional[ProjectStore] = None


def get_store() -> ProjectStore:
    global _store
    if _store is None:
        _store = ProjectStore()
    return _store
