"""Import manuscript text/markdown files into projects and series.

Pure parsing lives here (chapter splits, front matter, cast/place heuristics).
The LLM layer lives in ``app.api.imports`` and reuses the extraction parser
from ``app.api.extract`` — scripts should work with no model attached just as
well as they do with one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM = r"[0-9]+|[IVXLCDM]+|[ivxlcdm]+"

# "Chapter 1", "chap. XIV", "CHAPTER TWO: The Call" (word-numbers included)
_WORD_NUM = (
    r"one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|"
    r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|hundred"
)
_CH_NUM = rf"(?:{_NUM}|{_WORD_NUM}|[A-Z])"
_CHAPTER_LINE = re.compile(
    rf"^\s*(?:chapter|chap\.?|ch\.?)\s+{_CH_NUM}\b.*$",
    re.IGNORECASE,
)
_CHAPTER_HEADING_TEXT = re.compile(
    rf"^(?:chapter|chap\.?|ch\.?)\s+{_CH_NUM}\b.*$",
    re.IGNORECASE,
)
_PART_LINE = re.compile(
    r"^\s*(?:part|book|section)\s+(?:the\s+)?[IVXLCDM0-9]+\b.*$",
    re.IGNORECASE,
)
_FRONT_LINE = re.compile(
    r"^\s*(?:prologue|preface|introduction|foreword|afterword|epilogue|interlude)"
    r"(?:\b.*)?$",
    re.IGNORECASE,
)
_ATX = re.compile(r"^(#{1,6})[ \t]+(.*)$")
_SETEXT_UNDERLINE = re.compile(r"^\s*(=+|-+|_+)\s*$")
_DECO_LINE = re.compile(r"^[\s=\-_*#~.]+$")

# Built-in locative prepositions. The manuscript's own prose can widen this set
# at import time via ``expand_locative_vocabulary`` (embedding synonyms of these
# seeds that a hardcoded list would never name).
_LOCATIVE_SEEDS = (
    "in",
    "into",
    "at",
    "to",
    "from",
    "near",
    "beyond",
    "across",
    "toward",
    "towards",
    "inside",
    "outside",
    "behind",
    "beside",
    "beneath",
    "under",
    "upon",
    "over",
    "through",
)
_LOCATIVE_SEED_SET = frozenset(_LOCATIVE_SEEDS)
# A lowercase word immediately before a capitalized token: a preposition slot.
_SLOT = re.compile(r"\b([a-z]{2,16})\s+(?:the\s+)?[A-Z][\w']")
# Cosine floor for accepting a slot word as a locative synonym. Conservative —
# function words embed noisily, so false negatives are cheaper than false places.
_LOCATIVE_SIM_THRESHOLD = 0.5
_LOC_PREP = re.compile(
    r"\b(?:in|into|at|to|from|near|beyond|across|toward|towards|inside|outside|"
    r"behind|beside|beneath|under|upon|over|through)\s+"
    r"(?:the\s+)?([A-Z][\w']*(?:\s+[A-Z][\w']*){0,2})"
)
_SPEECH_VERBS = (
    "said",
    "says",
    "asked",
    "asks",
    "replied",
    "replies",
    "answered",
    "answers",
    "whispered",
    "whispers",
    "yelled",
    "shouted",
    "shouts",
    "called",
    "screamed",
    "murmured",
    "muttered",
    "growled",
    "snapped",
    "added",
    "noted",
    "observed",
    "begged",
    "pleaded",
    "commented",
    "offered",
    "agreed",
    "announced",
    "suggested",
    "continued",
    "interrupted",
    "remarked",
    "declared",
    "argued",
    "insisted",
    "prompted",
    "thought",
    "wondered",
    "mused",
    "recalled",
)
_VERB_ALT = "(?i:" + "|".join(_SPEECH_VERBS) + ")"
_SPEECH_AFTER = re.compile(
    r"(?:^|[\s\"'“”‘’.,!?;:])([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){0,2})\s+"
    + _VERB_ALT
    + r"\b"
)
_SPEECH_BEFORE = re.compile(
    r"\b"
    + _VERB_ALT
    + r"\s+([A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){0,2})[\s\"'“”‘’.,!?;:]"
)
# First-person/pronoun + common sentence-start words never to treat as names
_STOP_NAMES = {
    "you",
    "that",
    "this",
    "there",
    "here",
    "she",
    "her",
    "he",
    "him",
    "they",
    "them",
    "the",
    "and",
    "but",
    "so",
    "or",
    "then",
    "with",
    "without",
    "when",
    "what",
    "who",
    "why",
    "ours",
}


@dataclass
class ChapterSplit:
    title: str
    content: str
    word_count: int = 0


@dataclass
class ParsedDocument:
    title: str = ""
    author: str = ""
    genre: str = ""
    description: str = ""
    series: str = ""
    format: str = "plain"
    chapters: list[ChapterSplit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def decode_file(data: bytes) -> str:
    """Best-effort text decode: UTF-8 (BOM-safe) then Latin-1 fallback."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def natural_key(name: str) -> list:
    return [int(piece) if piece.isdigit() else piece.lower() for piece in re.split(r"(\d+)", name)]


def looks_like_markdown(text: str, filename: str = "") -> bool:
    if filename.lower().endswith((".md", ".markdown", ".mdown", ".mkd")):
        return True
    return bool(re.search(r"^#{1,6}[ \t]", text, re.MULTILINE))


def _clean_title(stem: str) -> str:
    stem = re.sub(
        r"^\s*(?:\[?\d+\]?[\s.\-_]+|(?:book|part|ch(?:apter)?)\.?\s*\d+\s*[-_.]*)",
        "",
        stem,
        flags=re.IGNORECASE,
    )
    stem = stem.replace("_", " ").strip()
    return stem or "Imported manuscript"


def title_from_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return _clean_title(stem)


def _parse_yaml_front_matter(text: str) -> tuple[dict, str]:
    """Return (metadata, remainder). Only handles a leading ---...--- block."""
    lines = text.split("\n")
    if not lines or not re.match(r"^\s*---\s*$", lines[0]):
        return {}, text
    meta: dict = {}
    for i in range(1, len(lines)):
        if re.match(r"^\s*(---|\.\.\.)\s*$", lines[i]):
            return meta, "\n".join(lines[i + 1 :])
        m = re.match(r"^\s*([A-Za-z_][\w ]*?)\s*:\s*(.*)$", lines[i])
        if m:
            key = m.group(1).strip().lower().replace(" ", "_")
            val = m.group(2).strip().strip("\"'")
            meta[key] = val
    return {}, text  # no closing fence — treat as body, not front matter


def _classify(text: str) -> str:
    """chapter | part | front | other for a heading-ish title line."""
    t = text.strip()
    if _CHAPTER_HEADING_TEXT.match(t):
        return "chapter"
    if _PART_LINE.match(t):
        return "part"
    if _FRONT_LINE.match(t):
        return "front"
    return "other"


def _split_single_chapter_front(lines: list[str]) -> tuple[str, str]:
    """Return (front_matter_block, rest) when a document has no chapter markers.

    Only strips a title block when real content follows — a lone short blob is
    treated as story text, not as a title.
    """
    i = 0
    n = len(lines)
    while i < n and (not lines[i].strip() or _DECO_LINE.match(lines[i].strip())):
        i += 1
    if i >= n:
        return "", "\n".join(lines)
    first = lines[i].strip()
    m = _ATX.match(first)
    if m and _classify(m.group(2).strip()) == "other":
        rest = "\n".join(lines[i + 1 :])
        return (first, rest) if rest.strip() else ("", "\n".join(lines))
    if _classify(first) != "other" or len(first) > 140:
        return "", "\n".join(lines)
    head = [lines[i]]
    j = i + 1
    while (
        j < n
        and lines[j].strip()
        and not _DECO_LINE.match(lines[j].strip())
        and len(lines[j].strip()) <= 140
        and _classify(lines[j].strip()) == "other"
        and not _ATX.match(lines[j].strip())
    ):
        head.append(lines[j])
        j += 1
    rest = "\n".join(lines[j:])
    if not rest.strip():
        return "", "\n".join(lines)
    return "\n".join(head), rest


def _detect_title_from_front(front: str, fmt: str) -> str:
    if fmt == "markdown" and front:
        m = re.search(r"^#{1,6}[ \t]+(.*)$", front, re.MULTILINE)
        if m and _classify(m.group(1).strip()) == "other":
            return m.group(1).strip()
    for line in (front or "").split("\n"):
        s = line.strip()
        if not s or _DECO_LINE.match(s) or _ATX.match(s):
            continue
        if len(s) > 120 or _classify(s) != "other":
            continue
        return s
    return ""


def _detect_description_from_front(front: str, fmt: str, title: str) -> str:
    """Collect short non-decorative lines after the title."""
    bits: list[str] = []
    seen_title = False
    for line in (front or "").split("\n"):
        s = line.strip()
        if not s or _DECO_LINE.match(s) or _ATX.match(s):
            continue
        if not seen_title:
            if s == title:
                seen_title = True
            continue
        if len(s) > 300 or _classify(s) != "other":
            break
        bits.append(s)
        if len(bits) >= 3:
            break
    return " · ".join(bits)[:300]


def _formfeed_title(lines: list[str], start: int, end: int) -> str:
    for line in lines[start + 1 : min(start + 4, end)]:
        s = line.strip()
        if not s or _DECO_LINE.match(s):
            continue
        kind = _classify(s)
        if kind in ("chapter", "part", "front"):
            return s
        break
    return ""


def split_chapters(
    text: str, filename: str = ""
) -> ParsedDocument:
    """Split manuscript text into chapters + extract front matter metadata."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\f+", "\n\f\n", text)  # give form feeds their own line

    meta, body = _parse_yaml_front_matter(text)
    fmt = "markdown" if looks_like_markdown(body, filename) else "plain"
    lines = body.split("\n")

    candidates: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        stripped = raw.strip()
        if not stripped:
            i += 1
            continue
        if stripped == "\f":
            candidates.append({"idx": i, "title": None, "kind": "formfeed"})
            i += 1
            continue
        m = _ATX.match(stripped)
        if m:
            text_title = m.group(2).strip()
            candidates.append(
                {"idx": i, "title": text_title, "kind": _classify(text_title)}
            )
            i += 1
            continue
        if i + 1 < n and _SETEXT_UNDERLINE.match(lines[i + 1]):
            underline = lines[i + 1].strip()
            if len(underline) >= 3 and underline[0] in "=-_":
                kind = _classify(stripped)
                if kind == "other":
                    # Reserve generic setext headings as a fallback split source
                    candidates.append(
                        {
                            "idx": i,
                            "title": stripped,
                            "kind": "setext",
                            "level": 1 if underline[0] == "=" else 2,
                            "skip": 2,
                        }
                    )
                else:
                    candidates.append(
                        {
                            "idx": i,
                            "title": stripped,
                            "kind": kind,
                            "skip": 2,
                        }
                    )
                i += 2
                continue
        kind = _classify(stripped)
        if kind != "other":
            candidates.append({"idx": i, "title": stripped, "kind": kind, "skip": 1})
        i += 1

    chapters_selected = [c for c in candidates if c["kind"] == "chapter"]
    fronts = [c for c in candidates if c["kind"] == "front"]
    parts = [c for c in candidates if c["kind"] == "part"]
    setexts = [c for c in candidates if c["kind"] == "setext"]
    formfeeds = [c for c in candidates if c["kind"] == "formfeed"]
    used_setext = False

    if chapters_selected:
        keep: list[dict] = chapters_selected + fronts
    elif len(setexts) >= 2:
        keep = setexts + fronts
        used_setext = True
    elif len(parts) >= 2:
        keep = parts + fronts
    elif fronts:
        keep = fronts
    elif formfeeds:
        keep = formfeeds
    else:
        keep = []
    keep.sort(key=lambda c: c["idx"])

    doc = ParsedDocument(format=fmt)
    if used_setext:
        doc.warnings.append(
            "No numbered chapters found — split on section headings instead."
        )
    doc.title = str(meta.get("title", "") or "").strip()
    doc.author = str(meta.get("author", "") or "").strip()
    doc.genre = str(meta.get("genre", "") or "").strip()
    doc.description = str(meta.get("description", "") or "").strip()
    doc.series = str(meta.get("series", "") or "").strip()

    if not keep:
        doc.warnings.append(
            "No chapter markers found — imported the file as a single chapter."
        )
        front, content = _split_single_chapter_front(lines)
        title_hint = _detect_title_from_front(front, fmt)
        if not doc.title and title_hint:
            doc.title = title_hint
        if not doc.title:
            doc.title = title_from_filename(filename)
        extra = _detect_description_from_front(front, fmt, title_hint or doc.title)
        if extra and not doc.description:
            doc.description = extra
        content = content.strip()
        doc.chapters = [
            ChapterSplit(
                title=doc.title or "Manuscript",
                content=content,
                word_count=len(content.split()),
            )
        ]
        return doc

    starts = [c["idx"] for c in keep]
    front = "\n".join(lines[: starts[0]])
    title_hint = _detect_title_from_front(front, fmt)
    if not doc.title and title_hint:
        doc.title = title_hint
    if not doc.title:
        doc.title = title_from_filename(filename)
    extra = _detect_description_from_front(front, fmt, title_hint or doc.title)
    if extra and not doc.description:
        doc.description = extra

    for k, c in enumerate(keep):
        start = c["idx"]
        end = keep[k + 1]["idx"] if k + 1 < len(keep) else len(lines)
        skip = c.get("skip", 1 if c["kind"] != "plain" else 1)
        title = c["title"]
        if c["kind"] == "formfeed":
            title = _formfeed_title(lines, start, end)
        body_lines = lines[start + skip : end]
        body = "\n".join(body_lines)
        if title and body.strip().split("\n", 1)[0].strip() == title.strip():
            body = "\n".join(body.split("\n", 1)[1:])
        body = body.strip()
        doc.chapters.append(
            ChapterSplit(
                title=title or f"Section {k + 1}",
                content=body,
                word_count=len(body.split()),
            )
        )

    return doc


def _loc_prep_pattern(extra_prepositions: "set[str] | None" = None):
    """Compile the locative-preposition regex, seeded plus any extras."""
    words = set(_LOCATIVE_SEEDS)
    if extra_prepositions:
        words |= {w.lower() for w in extra_prepositions if w}
    if words == _LOCATIVE_SEED_SET:
        return _LOC_PREP
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(
        rf"\b(?:{alts})\s+(?:the\s+)?([A-Z][\w']*(?:\s+[A-Z][\w']*){{0,2}})"
    )


def expand_locative_vocabulary(prose: str) -> set[str]:
    """Embedding-derived synonyms of the built-in locative prepositions.

    The manuscript's own words supply the candidate vocabulary (every lowercase
    word sitting immediately before a capitalized token), so the only hardcoded
    part is the seed concept. The embedder is whatever the user configured in
    ``backend/.env`` (``GW_EMBEDDING_BACKEND``) — the same engine that powers
    RAG. Best-effort: the default ``hash`` backend carries no semantics (and
    never blocks an import), and anything that isn't sentence-transformers is
    skipped; the built-in list then stands alone. When the user opts into ``st``
    the semantic synonyms are actually discovered.
    """
    try:
        from app.config import get_settings
        from app.services.embeddings import embed_texts, is_embedding_ready
    except Exception:  # noqa: BLE001
        return set()
    backend = (getattr(get_settings(), "embedding_backend", "") or "").lower()
    if backend not in ("st", "sentence-transformers", "torch"):
        return set()
    if not is_embedding_ready():
        return set()

    candidates = {m.group(1) for m in _SLOT.finditer(prose)} - _LOCATIVE_SEED_SET
    if not candidates:
        return set()

    ordered = sorted(candidates)
    try:
        vecs = embed_texts([*_LOCATIVE_SEEDS, *ordered])
    except Exception:  # noqa: BLE001
        return set()

    import numpy as np

    n = len(_LOCATIVE_SEEDS)
    seeds = np.asarray(vecs[:n], dtype=np.float32)
    cand = np.asarray(vecs[n:], dtype=np.float32)
    seeds /= np.clip(np.linalg.norm(seeds, axis=1, keepdims=True), 1e-9, None)
    cand /= np.clip(np.linalg.norm(cand, axis=1, keepdims=True), 1e-9, None)
    best = (cand @ seeds.T).max(axis=1)
    return {
        word
        for word, score in zip(ordered, best)
        if float(score) >= _LOCATIVE_SIM_THRESHOLD
    }


def detect_cast_and_places(
    text: str,
    extra_prepositions: "set[str] | None" = None,
) -> tuple[list[dict], list[dict]]:
    """Parse-prose-only character + place candidates.

    Returns (characters, places), each a list of dicts with `name` (+ `notes`).
    Conservative: only names that appear in dialogue attribution, and places
    that follow locative prepositions repeatedly. LLM extraction (when a model
    is available) supersedes these.

    ``extra_prepositions`` are additional locative words (e.g. embedding-derived
    synonyms from ``expand_locative_vocabulary``) merged with the built-ins.
    """
    normalized = re.sub(r"\s+", " ", text)
    loc_re = _loc_prep_pattern(extra_prepositions)
    counts: dict[str, int] = {}

    def bump(raw: str) -> None:
        name = re.sub(r"\s+", " ", raw).strip().strip(".,;:!?'\"“”‘’")
        if len(name) < 2:
            return
        words = name.split()
        if any(w.lower() in _STOP_NAMES for w in words):
            return
        counts[name] = counts.get(name, 0) + 1

    for m in _SPEECH_AFTER.finditer(normalized):
        bump(m.group(1))
    for m in _SPEECH_BEFORE.finditer(normalized):
        bump(m.group(1))

    # Collapse "Eddie Rapton" vs "Eddie": keep the more frequent form, longer on tie.
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0])))
    final: dict[str, int] = {}
    for name, cnt in ranked:
        overlap = [k for k in final if k in name or name in k]
        if not overlap:
            final[name] = cnt
            continue
        prev = overlap[0]
        if cnt > counts[prev]:
            final.pop(prev, None)
            final[name] = cnt
    characters = [
        {"name": name, "notes": "Auto-detected from prose on import"}
        for name, cnt in sorted(final.items(), key=lambda kv: -kv[1])[:20]
        if cnt >= 2
    ]

    char_tokens: set[str] = set()
    for c in characters:
        for tok in c["name"].replace("-", " ").split():
            char_tokens.add(tok.lower())

    place_counts: dict[str, int] = {}
    for m in loc_re.finditer(normalized):
        place = m.group(1).strip()
        lower = place.lower()
        if not place or lower in char_tokens or lower in _STOP_NAMES:
            continue
        place_counts[place] = place_counts.get(place, 0) + 1

    places = [
        {
            "name": place,
            "notes": "Auto-detected from prose on import",
        }
        for place, cnt in sorted(place_counts.items(), key=lambda kv: -kv[1])[:12]
        if cnt >= 2
    ]
    return characters, places