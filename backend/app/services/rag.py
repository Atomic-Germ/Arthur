"""ChromaDB-backed story memory / RAG."""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from typing import Optional

from app.config import get_settings
from app.models.schemas import Character, Location, Project
from app.services.embeddings import (
    embed_query,
    embed_texts,
    get_status as embedding_status,
    is_embedding_ready,
)

logger = logging.getLogger("ghostwriter.rag")

# Chroma is not reliably multi-threaded — serialize all client use.
_chroma_lock = threading.RLock()


def _chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = re.split(r"\n\s*\n", text)
    chunks: list[str] = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) <= chunk_size:
            if len(current) + len(para) + 1 <= chunk_size:
                current = f"{current}\n\n{para}".strip()
            else:
                flush()
                current = para
        else:
            flush()
            words = para.split()
            buf: list[str] = []
            size = 0
            for w in words:
                if size + len(w) + 1 > chunk_size and buf:
                    chunks.append(" ".join(buf))
                    overlap_words = []
                    osize = 0
                    for ow in reversed(buf):
                        if osize + len(ow) + 1 > overlap:
                            break
                        overlap_words.insert(0, ow)
                        osize += len(ow) + 1
                    buf = overlap_words + [w]
                    size = sum(len(x) + 1 for x in buf)
                else:
                    buf.append(w)
                    size += len(w) + 1
            if buf:
                chunks.append(" ".join(buf))
            current = ""
    flush()
    return chunks


def _stable_id(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


class StoryMemory:
    def __init__(self):
        settings = get_settings()
        self.settings = settings
        self._client = None
        self._client_error: Optional[str] = None

    @property
    def client(self):
        if self._client is not None:
            return self._client
        if self._client_error is not None:
            return None
        try:
            import chromadb
            from chromadb.config import Settings as ChromaSettings

            self._client = chromadb.PersistentClient(
                path=str(self.settings.chroma_dir),
                settings=ChromaSettings(anonymized_telemetry=False),
            )
            return self._client
        except Exception as exc:  # noqa: BLE001
            self._client_error = str(exc)
            logger.warning("Chroma unavailable: %s", exc)
            return None

    def _collection(self, project_id: str):
        client = self.client
        if client is None:
            return None
        return client.get_or_create_collection(
            name=f"project_{project_id.replace('-', '_')}",
            metadata={"hnsw:space": "cosine"},
        )

    def index_project(self, project: Project, chapter_id: Optional[str] = None) -> int:
        if not is_embedding_ready():
            return 0

        with _chroma_lock:
            collection = self._collection(project.id)
            if collection is None:
                return 0

            # Drop superseded vectors before re-adding, or the collection grows
            # stale forever (chunk ids hash content, so any edit renames them).
            # Scoped re-index only touches the affected chapter; full re-index
            # clears the whole project so deleted chapters/characters/notes go.
            if chapter_id:
                try:
                    collection.delete(where={"chapter_id": chapter_id})
                except Exception:  # noqa: BLE001
                    pass
            else:
                try:
                    collection.delete(where={"project_id": project.id})
                except Exception:  # noqa: BLE001
                    pass

            ids: list[str] = []
            documents: list[str] = []
            metadatas: list[dict] = []

            for char in project.characters:
                doc = self._character_doc(char)
                cid = _stable_id(project.id, "character", char.id)
                ids.append(cid)
                documents.append(doc)
                metadatas.append(
                    {
                        "type": "character",
                        "project_id": project.id,
                        "character_id": char.id,
                        "name": char.name,
                        "title": char.name,
                    }
                )

            for loc in project.locations:
                doc = self._location_doc(loc)
                cid = _stable_id(project.id, "location", loc.id)
                ids.append(cid)
                documents.append(doc)
                metadatas.append(
                    {
                        "type": "location",
                        "project_id": project.id,
                        "location_id": loc.id,
                        "name": loc.name,
                        "title": loc.name,
                    }
                )

            if project.world_notes.strip():
                for i, chunk in enumerate(
                    _chunk_text(
                        project.world_notes,
                        self.settings.chunk_size,
                        self.settings.chunk_overlap,
                    )
                ):
                    cid = _stable_id(project.id, "world", str(i), chunk[:40])
                    ids.append(cid)
                    documents.append(chunk)
                    metadatas.append(
                        {
                            "type": "world",
                            "project_id": project.id,
                            "title": "World Notes",
                        }
                    )

            chapters = project.chapters
            if chapter_id:
                chapters = [c for c in chapters if c.id == chapter_id]

            for chapter in chapters:
                if not chapter.content.strip():
                    continue
                if chapter.summary.strip():
                    cid = _stable_id(project.id, "summary", chapter.id)
                    ids.append(cid)
                    documents.append(
                        f"Chapter summary — {chapter.title}: {chapter.summary}"
                    )
                    metadatas.append(
                        {
                            "type": "summary",
                            "project_id": project.id,
                            "chapter_id": chapter.id,
                            "title": chapter.title,
                        }
                    )
                for i, chunk in enumerate(
                    _chunk_text(
                        chapter.content,
                        self.settings.chunk_size,
                        self.settings.chunk_overlap,
                    )
                ):
                    cid = _stable_id(
                        project.id, "chapter", chapter.id, str(i), chunk[:40]
                    )
                    ids.append(cid)
                    documents.append(f"[{chapter.title}]\n{chunk}")
                    metadatas.append(
                        {
                            "type": "chapter",
                            "project_id": project.id,
                            "chapter_id": chapter.id,
                            "title": chapter.title,
                            "chunk_index": i,
                        }
                    )

            if not ids:
                return 0

            batch = 32
            total = 0
            for start in range(0, len(ids), batch):
                end = start + batch
                batch_docs = documents[start:end]
                batch_ids = ids[start:end]
                batch_meta = metadatas[start:end]
                embeddings = embed_texts(batch_docs)
                try:
                    collection.delete(ids=batch_ids)
                except Exception:  # noqa: BLE001
                    pass
                collection.add(
                    ids=batch_ids,
                    documents=batch_docs,
                    metadatas=batch_meta,
                    embeddings=embeddings,
                )
                total += len(batch_ids)
            return total

    def _character_doc(self, char: Character) -> str:
        parts = [f"Character: {char.name}"]
        if char.role:
            parts.append(f"Role: {char.role}")
        if char.physical_traits:
            parts.append(f"Physical traits: {char.physical_traits}")
        if char.personality:
            parts.append(f"Personality: {char.personality}")
        if char.motivations:
            parts.append(f"Motivations: {char.motivations}")
        if char.speech_patterns:
            parts.append(f"Speech patterns: {char.speech_patterns}")
        if char.backstory:
            parts.append(f"Backstory: {char.backstory}")
        if char.relationships:
            parts.append(f"Relationships: {char.relationships}")
        if char.notes:
            parts.append(f"Notes: {char.notes}")
        return "\n".join(parts)

    def _location_doc(self, loc: Location) -> str:
        parts = [f"Location: {loc.name}"]
        if loc.type:
            parts.append(f"Type: {loc.type}")
        if loc.description:
            parts.append(f"Description: {loc.description}")
        if loc.notes:
            parts.append(f"Notes: {loc.notes}")
        return "\n".join(parts)

    def query(
        self,
        project_id: str,
        query_text: str,
        top_k: Optional[int] = None,
        filter_type: Optional[str] = None,
    ) -> list[dict]:
        # Never block assist on a cold/loading embedder
        if embedding_status() != "ready" or not query_text.strip():
            return []

        if not _chroma_lock.acquire(timeout=2000.0):
            logger.warning("Skipping vector query — chroma busy")
            return []
        try:
            collection = self._collection(project_id)
            if collection is None or collection.count() == 0:
                return []

            k = top_k or self.settings.rag_top_k
            where = {"type": filter_type} if filter_type else None
            emb = embed_query(query_text)

            kwargs = {
                "query_embeddings": [emb],
                "n_results": min(k, collection.count()),
                "include": ["documents", "metadatas", "distances"],
            }
            if where:
                kwargs["where"] = where

            results = collection.query(**kwargs)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Vector query failed: %s", exc)
            return []
        finally:
            _chroma_lock.release()

        out: list[dict] = []
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0]
        for doc, meta, dist in zip(docs, metas, dists):
            out.append(
                {
                    "text": doc,
                    "metadata": meta or {},
                    "score": 1.0 - float(dist) if dist is not None else 0.0,
                }
            )
        return out

    def build_context_block(
        self,
        project: Project,
        query_text: str,
        extra_text: str = "",
        use_vector: bool = True,
    ) -> tuple[str, list[str]]:
        """Build LLM context from project data (+ optional vector hits)."""
        sources: list[str] = []
        sections: list[str] = []

        meta_bits = [f"Title: {project.title}"]
        if project.genre:
            meta_bits.append(f"Genre: {project.genre}")
        if project.premise:
            meta_bits.append(f"Premise: {project.premise}")
        if project.description:
            meta_bits.append(f"Description: {project.description}")
        sections.append("## Project\n" + "\n".join(meta_bits))

        if project.characters:
            char_blocks = [self._character_doc(c) for c in project.characters]
            sections.append("## Characters\n" + "\n\n".join(char_blocks))
            sources.extend([f"Character: {c.name}" for c in project.characters])

        if project.locations:
            loc_blocks = [self._location_doc(l) for l in project.locations]
            sections.append("## Locations\n" + "\n\n".join(loc_blocks))
            sources.extend([f"Location: {l.name}" for l in project.locations])

        # Shared series bible — worldbuilding/cast that spans every book
        if project.series.strip():
            bible_section, bible_sources = self.build_series_bible_section(project)
            if bible_section:
                sections.append(bible_section)
                sources.extend(bible_sources)

        # Full world notes — primary source for lore questions
        if project.world_notes.strip():
            notes = project.world_notes.strip()
            if len(notes) > 12000:
                notes = notes[:12000] + "\n…[truncated]"
            sections.append(f"## World Notes\n{notes}")
            sources.append("World Notes")

        # Chapter bodies — every chapter is represented (no silent middle dropouts)
        chapters = sorted(project.chapters, key=lambda c: c.order)
        if chapters:
            ms_block, ms_sources = self._pack_manuscript(
                chapters,
                query_text=query_text,
                extra_text=extra_text,
                budget=int(self.settings.manuscript_context_chars),
            )
            if ms_block:
                sections.append(ms_block)
                sources.extend(ms_sources)

        if use_vector:
            search_q = " ".join(filter(None, [query_text, extra_text[:500]]))
            hits = self.query(project.id, search_q)
            if hits:
                hit_texts = []
                for h in hits:
                    title = (h.get("metadata") or {}).get("title", "story")
                    htype = (h.get("metadata") or {}).get("type", "chunk")
                    label = f"{htype}: {title}"
                    if label not in sources:
                        sources.append(label)
                    hit_texts.append(h["text"])
                sections.append(
                    "## Retrieved Story Memory\n" + "\n---\n".join(hit_texts)
                )

        return "\n\n".join(sections), sources

    def build_series_bible_section(
        self, project: Project
    ) -> tuple[str, list[str]]:
        """Shared series bible (worldbuilding doc + cross-book cast and locations)."""
        from app.db.storage import get_store

        bible = get_store().get_series_bible(project.series.strip())
        sections: list[str] = []
        sources: list[str] = []

        if bible.world_notes.strip():
            notes = bible.world_notes.strip()
            if len(notes) > 12000:
                notes = notes[:12000] + "\n…[truncated]"
            sections.append(
                f"## Series Bible — {project.series.strip()}\n{notes}"
            )
            sources.append(f"Series Bible ({project.series.strip()})")

        if bible.characters:
            char_blocks = [self._character_doc(c) for c in bible.characters]
            sections.append(
                f"## Series Cast\n" + "\n\n".join(char_blocks)
            )
            sources.extend(
                [f"Series Character: {c.name}" for c in bible.characters]
            )

        if bible.locations:
            loc_blocks = [self._location_doc(l) for l in bible.locations]
            sections.append(
                f"## Series Locations\n" + "\n\n".join(loc_blocks)
            )
            sources.extend(
                [f"Series Location: {l.name}" for l in bible.locations]
            )

        return "\n\n".join(sections), sources

    def build_series_context(
        self, project: Project, query_text: str
    ) -> tuple[str, list[str]]:
        """Cross-book context for the 'series' mode — worldbuilding + cast only.

        Deliberately excludes chapter/plot content: this mode answers lore and
        relationship questions across the whole series, not about any single
        book's plot.
        """
        from app.db.storage import get_store

        store = get_store()
        sources: list[str] = []
        sections: list[str] = []

        meta_bits = [f"Series: {project.series.strip()}"]
        if project.genre:
            meta_bits.append(f"Genre: {project.genre}")
        sections.append("## Series\n" + "\n".join(meta_bits))

        bible_section, bible_sources = self.build_series_bible_section(project)
        if bible_section:
            sections.append(bible_section)
            sources.extend(bible_sources)

        books = store.projects_in_series(project.series.strip())
        for book in books:
            bits = [f"Book: {book.title}"]
            if book.series_position:
                bits.append(f"Series position: {book.series_position}")
            if book.genre:
                bits.append(f"Genre: {book.genre}")
            if book.premise:
                bits.append(f"Premise: {book.premise}")
            if book.description:
                bits.append(f"Description: {book.description}")
            sections.append("## " + " | ".join(bits))
            sources.append(f"Book: {book.title}")

            if book.characters:
                char_blocks = [self._character_doc(c) for c in book.characters]
                sections.append(
                    f"### Characters ({book.title})\n" + "\n\n".join(char_blocks)
                )
                sources.extend(
                    [f"Character: {c.name} ({book.title})" for c in book.characters]
                )

            if book.locations:
                loc_blocks = [self._location_doc(l) for l in book.locations]
                sections.append(
                    f"### Locations ({book.title})\n" + "\n\n".join(loc_blocks)
                )
                sources.extend(
                    [f"Location: {l.name} ({book.title})" for l in book.locations]
                )

            if book.world_notes.strip():
                notes = book.world_notes.strip()
                if len(notes) > 8000:
                    notes = notes[:8000] + "\n…[truncated]"
                sections.append(
                    f"### World Notes ({book.title})\n{notes}"
                )
                sources.append(f"World Notes ({book.title})")

        return "\n\n".join(sections), sources

    def build_canon_context(
        self, project: Project
    ) -> tuple[str, list[str]]:
        """Context for the 'canon' mode — the universe (bible + every other book's
        cast/world) plus THIS book's full draft, so the model can judge whether the
        manuscript belongs in the canon before the author commits it.
        """
        from app.db.storage import get_store

        store = get_store()
        sources: list[str] = []
        sections: list[str] = []

        universe_section, universe_sources = self.build_series_context(
            project, "canon review"
        )
        if universe_section:
            sections.append(universe_section)
            sources.extend(universe_sources)

        chapters = sorted(project.chapters, key=lambda c: c.order)
        if chapters:
            blocks = []
            for idx, ch in enumerate(chapters, 1):
                body = self._clip_chapter_body(ch.content or "", 9000)
                if body:
                    blocks.append(f"### Chapter {idx}: {ch.title}\n{body}")
            if blocks:
                sections.append(
                    "## This Book's Draft\n" + "\n\n".join(blocks)
                )
                sources.append("This Book's Draft")

        return "\n\n".join(sections), sources

    @staticmethod
    def _clip_chapter_body(body: str, limit: int) -> str:
        """Fit body into limit chars, preferring head+tail when truncated."""
        body = (body or "").strip()
        if not body or limit <= 0:
            return ""
        if len(body) <= limit:
            return body
        if limit < 80:
            return body[:limit] + "…"
        # head / tail split with ellipsis
        head = max(40, int(limit * 0.55))
        tail = max(20, limit - head - 5)
        return body[:head].rstrip() + "\n…\n" + body[-tail:].lstrip()

    def _pack_manuscript(
        self,
        chapters: list,
        *,
        query_text: str = "",
        extra_text: str = "",
        budget: int = 28000,
    ) -> tuple[str, list[str]]:
        """
        Pack all chapters into a context budget without skipping middles.

        Always emits a full chapter index, then bodies with fair per-chapter
        shares (active/recent chapters get a modest bonus).
        """
        n = len(chapters)
        if n == 0:
            return "", []

        # Detect which chapter the user is sitting in (from draft excerpt)
        active_idx = -1
        extra = (extra_text or "").strip()
        if extra:
            best = 0
            needle = extra[:120]
            for i, ch in enumerate(chapters):
                body = ch.content or ""
                if needle and needle in body:
                    active_idx = i
                    break
                # fallback: overlap score on last 400 chars
                tail = body[-400:]
                if tail and extra[-200:] in body:
                    active_idx = i
                    break
                overlap = len(set(tail.split()) & set(extra.split()))
                if overlap > best and overlap > 8:
                    best = overlap
                    active_idx = i

        # --- Index (always complete) ---
        index_lines = [
            f"{i + 1}. {ch.title or f'Chapter {i + 1}'} "
            f"({len((ch.content or '').split())} words"
            f"{', has summary' if (ch.summary or '').strip() else ''})"
            for i, ch in enumerate(chapters)
        ]
        index_block = (
            "## Manuscript index (complete)\n"
            + "\n".join(index_lines)
            + "\n\n_All chapters exist. If a body below is shortened, it is "
            "marked — never treat a missing body as a missing chapter._"
        )

        # --- Fair body budget ---
        # Reserve room for index + wrappers
        body_budget = max(4000, budget - len(index_block) - 64 * n)
        # Weight: base 1.0, active 1.75, neighbors 1.25, last chapter 1.15
        weights = [1.0] * n
        for i in range(n):
            if i == active_idx:
                weights[i] = 1.75
            elif active_idx >= 0 and abs(i - active_idx) == 1:
                weights[i] = max(weights[i], 1.25)
            if i == n - 1:
                weights[i] = max(weights[i], 1.15)
            # slight boost for query-term hits
            q = (query_text or "").lower()
            if q and len(q) > 3:
                body_l = (chapters[i].content or "").lower()
                hits = sum(1 for tok in q.split() if len(tok) > 3 and tok in body_l)
                if hits:
                    weights[i] += min(0.5, hits * 0.08)

        wsum = sum(weights) or 1.0
        # Minimum floor so no chapter is zeroed out when non-empty
        floor = min(500, max(200, body_budget // (n * 3)))
        shares = [max(floor, int(body_budget * (w / wsum))) for w in weights]
        # Renormalize if floors blew the budget
        total_shares = sum(shares)
        if total_shares > body_budget:
            scale = body_budget / total_shares
            shares = [max(120, int(s * scale)) for s in shares]

        sources: list[str] = []
        parts: list[str] = [index_block]
        for i, ch in enumerate(chapters):
            title = ch.title or f"Chapter {i + 1}"
            body = (ch.content or "").strip()
            summary = (ch.summary or "").strip()
            share = shares[i]

            header = f"### {i + 1}. {title}"
            if i == active_idx:
                header += "  _(current)_"

            chunks: list[str] = [header]
            meta_used = 0
            if summary:
                s = summary if len(summary) <= 400 else summary[:400] + "…"
                chunks.append(f"Summary: {s}")
                meta_used += len(s) + 10

            allow = max(0, share - meta_used)
            if not body:
                chunks.append("_(empty chapter)_")
                flag = "empty"
            elif len(body) <= allow:
                chunks.append(body)
                flag = "full"
            else:
                clipped = self._clip_chapter_body(body, allow)
                chunks.append(clipped)
                chunks.append(
                    f"_[truncated — {len(body)} chars total; "
                    f"showing ~{len(clipped)} for context budget]_"
                )
                flag = "truncated"

            parts.append("\n".join(chunks))
            sources.append(f"chapter: {title} ({flag})")

        return "## Manuscript\n" + "\n\n".join(parts), sources

    def delete_project_collection(self, project_id: str) -> None:
        if not _chroma_lock.acquire(timeout=5.0):
            return
        try:
            client = self.client
            if client is None:
                return
            name = f"project_{project_id.replace('-', '_')}"
            try:
                client.delete_collection(name)
            except Exception:  # noqa: BLE001
                pass
        finally:
            _chroma_lock.release()


_memory: Optional[StoryMemory] = None


def get_memory() -> StoryMemory:
    global _memory
    if _memory is None:
        _memory = StoryMemory()
    return _memory
