"""Manuscript import endpoint: text/markdown files become projects + series bible.

Everything needed for a usable import runs without an LLM: chapter splitting,
front-matter metadata, and prose heuristics for cast/places. When a model is
connected the same pass also runs universe extraction and chapter summaries.

Join behavior: importing into an existing series (any mode) merges the new
book's cast, locations, and world facts into the shared bible add-only, and
seeds the bible's cast/locations into the new project so the book sees the
whole universe.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.api.extract import run_extraction
from app.db.storage import get_store
from app.models.schemas import (
    CharacterCreate,
    ChapterCreate,
    ImportedProject,
    ImportReport,
    LocationCreate,
    ProjectCreate,
    SeriesBibleUpdate,
)
from app.services.importer import (
    decode_file,
    detect_cast_and_places,
    expand_locative_vocabulary,
    natural_key,
    split_chapters,
)
from app.services.indexer import schedule_reindex
from app.services.llm import MODE_SYSTEM_PROMPTS, get_llm

logger = logging.getLogger("ghostwriter.import")

router = APIRouter(prefix="/import", tags=["import"])

_MAX_FILE_BYTES = 25 * 1024 * 1024
_ALLOWED_EXTS = {".txt", ".text", ".md", ".markdown", ".mdown", ".mkd", ".rst"}
_CAST_NOTE = "Auto-detected from prose on import — verify and fill in details."
_PLACE_NOTE = "Auto-detected from prose on import — verify and fill in details."
_SUMMARY_CHARS = 6000


def _fact_bullets(bible_notes: str, facts: list[str]) -> str:
    """Append facts as '- fact' bullets, skipping duplicates/substrings."""
    if not facts:
        return bible_notes
    current = (bible_notes or "").split("\n")
    seen = {ln.strip().lower() for ln in current if ln.strip()}
    to_add: list[str] = []
    for fact in facts:
        s = (fact or "").strip()
        if not s:
            continue
        if s.lower() in seen or any(s.lower() in l for l in seen):
            continue
        seen.add(s.lower())
        to_add.append(f"- {s}")
    if not to_add:
        return bible_notes
    joined = "\n".join(current).strip()
    part = "\n\n".join(to_add)
    return (joined + "\n\n" + part).strip() if joined else part


def _merge_into_bible(store, series_name: str, project, facts: list[str]) -> None:
    """Add-only merge of a project's cast/locations/facts into the series bible."""
    bible = store.get_series_bible(series_name)
    known_chars = {
        c.name.strip().lower() for c in bible.characters if c.name.strip()
    }
    for c in project.characters:
        key = (c.name or "").strip().lower()
        if key and key not in known_chars:
            known_chars.add(key)
            bible.characters.append(c)
    known_locs = {
        l.name.strip().lower() for l in bible.locations if l.name.strip()
    }
    for loc in project.locations:
        key = (loc.name or "").strip().lower()
        if key and key not in known_locs:
            known_locs.add(key)
            bible.locations.append(loc)
    store.update_series_bible(
        series_name,
        SeriesBibleUpdate(
            world_notes=_fact_bullets(bible.world_notes, facts),
            characters=bible.characters,
            locations=bible.locations,
        ),
    )


def _next_position(store, series_name: str) -> int:
    positions = [
        p.series_position for p in store.projects_in_series(series_name)
    ]
    return (max(positions, default=0) or 0) + 1


@router.post("", response_model=ImportReport)
async def import_manuscripts(
    files: list[UploadFile] = File(...),
    mode: str = Form("auto"),
    series: str = Form(""),
    title: str = Form(""),
    genre: str = Form(""),
    premise: str = Form(""),
    description: str = Form(""),
    author: str = Form(""),
    analyze: bool = Form(True),
):
    store = get_store()
    warnings: list[str] = []

    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "single", "series"):
        raise HTTPException(
            status_code=400,
            detail="mode must be one of: auto, single, series",
        )

    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded.")

    records: list[tuple[str, bytes]] = []
    for f in files:
        name = (f.filename or "").strip()
        if not name:
            continue
        ext = Path(name).suffix.lower()
        if ext not in _ALLOWED_EXTS:
            raise HTTPException(
                status_code=400,
                detail="Unsupported file type for "
                f"'{name}'. Allowed: {', '.join(sorted(_ALLOWED_EXTS))}",
            )
        raw = await f.read()
        if len(raw) == 0:
            raise HTTPException(status_code=400, detail=f"'{name}' is empty.")
        if len(raw) > _MAX_FILE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"'{name}' exceeds the 25 MB import limit.",
            )
        records.append((name, raw))
    if not records:
        raise HTTPException(status_code=400, detail="No readable files uploaded.")

    records.sort(key=lambda r: natural_key(r[0]))
    effective = mode if mode != "auto" else (
        "single" if len(records) == 1 else "series"
    )

    parsed_docs = []
    for name, raw in records:
        text = decode_file(raw)
        try:
            doc = split_chapters(text, name)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{name}: couldn't parse ({exc}) — skipped.")
            continue
        if not doc.chapters:
            warnings.append(f"{name}: no story content found — skipped.")
            continue
        parsed_docs.append((name, doc))
    if not parsed_docs:
        raise HTTPException(
            status_code=400,
            detail="None of the uploaded files contained readable story content.",
        )

    series_name = (series or "").strip()
    bible = store.get_series_bible(series_name) if series_name else None
    joining_universe = bool(
        bible
        and (bible.world_notes.strip() or bible.characters or bible.locations)
    )

    report = ImportReport(
        mode=effective,
        series=series_name,
        llm_used=False,
        warnings=warnings,
    )

    llm = get_llm()
    llm_available = False
    if analyze:
        try:
            llm_available = await llm.check_available()
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"LLM check failed ({exc}) — using prose heuristics only.")
            llm_available = False
    report.warnings = warnings
    if analyze and not llm_available:
        warnings.append(
            "No LLM connected — cast/places detected by prose heuristics only"
            " (no AI summaries or extraction)."
        )
        report.warnings = warnings

    # ── single: merge every document into one book ──
    if effective == "single":
        merged_title = (title or "").strip() or parsed_docs[0][1].title
        chapters: list[tuple[str, str]] = []
        docs_author = parsed_docs[0][1].author
        docs_genre = parsed_docs[0][1].genre
        docs_desc = " · ".join(
            d.description for _, d in parsed_docs if d.description
        )[:300]
        for name, doc in parsed_docs:
            for ch in doc.chapters:
                chapters.append((ch.title, ch.content))
            warnings.extend(f"{name}: {w}" for w in doc.warnings)
        report.warnings = warnings

        project = None
        try:
            project = store.create_project(
                ProjectCreate(
                    title=merged_title[:200] or "Imported manuscript",
                    description=(description or "").strip() or docs_desc,
                    genre=(genre or "").strip() or docs_genre,
                    premise=(premise or "").strip(),
                    author=(author or "").strip() or docs_author,
                    series=series_name,
                    series_position=_next_position(store, series_name)
                    if series_name
                    else 0,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Import: project creation failed")
            raise HTTPException(
                status_code=500, detail=f"Failed to create project: {exc}"
            ) from exc

        stored = await _convert_project(
            store,
            project.id,
            merged_title,
            effective,
            series_name,
            joining_universe,
            bible,
            chapters,
            analyze,
            llm_available,
            llm,
            report,
        )
        report.projects.append(stored)

    # ── series: every document becomes its own book ──
    else:
        for name, doc in parsed_docs:
            book_title = (title or "").strip() or doc.title
            project = None
            position = _next_position(store, series_name) if series_name else 0
            try:
                project = store.create_project(
                    ProjectCreate(
                        title=book_title[:200] or "Imported manuscript",
                        description=(description or "").strip() or doc.description,
                        genre=(genre or "").strip() or doc.genre,
                        premise=(premise or "").strip(),
                        author=(author or "").strip() or doc.author,
                        series=series_name,
                        series_position=position,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{name}: project creation failed ({exc}) — skipped.")
                report.warnings = warnings
                continue

            chapters = [(ch.title, ch.content) for ch in doc.chapters]
            try:
                stored = await _convert_project(
                    store,
                    project.id,
                    book_title,
                    effective,
                    series_name,
                    joining_universe,
                    bible,
                    chapters,
                    analyze,
                    llm_available,
                    llm,
                    report,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Import: convert failed project=%s", project.id)
                warnings.append(f"{name}: failed to finish importing ({exc}) — rolled back.")
                try:
                    store.delete_project(project.id)
                except Exception:  # noqa: BLE001
                    pass
                project = None
                report.warnings = warnings
                continue
            warnings.extend(f"{name}: {w}" for w in doc.warnings)
            report.warnings = warnings
            report.projects.append(stored)

    if not report.projects:
        raise HTTPException(
            status_code=500,
            detail="Import finished but no projects could be created: "
            + "; ".join(report.warnings),
        )

    report.bible_updated = report.bible_updated and bool(series_name)
    return report


async def _convert_project(
    store,
    project_id: str,
    book_title: str,
    effective: str,
    series_name: str,
    joining_universe: bool,
    bible,
    chapters: list[tuple[str, str]],
    analyze: bool,
    llm_available: bool,
    llm,
    report: ImportReport,
) -> ImportedProject:
    """Add chapters, run heuristics + LLM, merge series, schedule indexing."""
    added_chapters = store.add_chapters(
        project_id,
        [
            ChapterCreate(title=title, content=content, order=i)
            for i, (title, content) in enumerate(chapters)
        ],
    )
    word_count = sum(ch.word_count for ch in added_chapters)

    project = store.get_project(project_id)

    # Seed the shared universe into a book joining an existing series
    if joining_universe and bible:
        store.add_characters(
            project_id,
            [CharacterCreate(**c.model_dump()) for c in bible.characters],
        )
        store.add_locations(
            project_id,
            [LocationCreate(**l.model_dump()) for l in bible.locations],
        )
        project = store.get_project(project_id)

    # Deterministic prose heuristics — always run, LLM or not
    prose = "\n".join(ch.content for ch in project.chapters)
    extra_preps = expand_locative_vocabulary(prose)
    cast, places = detect_cast_and_places(prose, extra_prepositions=extra_preps)
    heur_chars = store.add_characters(
        project_id,
        [CharacterCreate(name=c["name"], notes=c.get("notes", _CAST_NOTE)) for c in cast],
    )
    heur_locs = store.add_locations(
        project_id,
        [LocationCreate(name=p["name"], notes=p.get("notes", _PLACE_NOTE)) for p in places],
    )
    report.characters_added += len(heur_chars)
    report.locations_added += len(heur_locs)

    # Optional LLM pass (only when a model is connected and analyze is on)
    world_facts: list[str] = []
    if analyze and llm_available:
        report.llm_used = True
        project = store.get_project(project_id)
        try:
            extracted = await run_extraction(project, llm)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Import: LLM extraction failed for %s", project_id)
            report.warnings.append(
                f"{book_title}: LLM extraction failed ({exc})."
            )
            extracted = None

        if extracted:
            added_chars = store.add_characters(
                project_id,
                [
                    CharacterCreate(**c.model_dump())
                    for c in extracted.characters
                ],
            )
            added_locs = store.add_locations(
                project_id,
                [LocationCreate(**l.model_dump()) for l in extracted.locations],
            )
            report.characters_added += len(added_chars)
            report.locations_added += len(added_locs)
            world_facts = [f for f in extracted.world_facts if isinstance(f, str)]
            if world_facts:
                store.append_world_notes_lines(project_id, world_facts)
                report.world_facts_added += len(world_facts)

        project = store.get_project(project_id)
        summaries: dict[str, str] = {}
        for ch in sorted(project.chapters, key=lambda c: c.order):
            body = (ch.content or "").strip()
            if not body or (ch.summary or "").strip() or len(body) < 30:
                continue
            try:
                text = await llm.complete(
                    user_message=f"Summarize this chapter:\n\n{body[:_SUMMARY_CHARS]}",
                    system_prompt=MODE_SYSTEM_PROMPTS["summarize"],
                    temperature=0.3,
                    max_tokens=300,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Import: chapter summary failed for %s", ch.id)
                continue
            if text and text.strip():
                summaries[ch.id] = text.strip()
        if summaries:
            store.update_chapter_summaries(project_id, summaries)
            report.summaries_generated += len(summaries)

    # Merge into the shared series bible (any mode once a series is set)
    if series_name:
        project = store.get_project(project_id)
        try:
            _merge_into_bible(store, series_name, project, world_facts)
            report.bible_updated = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Import: bible merge failed for %s", project_id)
            report.warnings.append(
                f"{book_title}: series bible merge failed ({exc})."
            )

    schedule_reindex(project_id)

    project = store.get_project(project_id)
    chapter_count = (
        len(project.chapters)
        if effective == "single"
        else len(added_chapters)
    )
    return ImportedProject(
        id=project.id,
        title=project.title,
        series=project.series,
        series_position=project.series_position,
        chapter_count=chapter_count,
        word_count=word_count,
        character_count=len(project.characters),
        location_count=len(project.locations),
    )