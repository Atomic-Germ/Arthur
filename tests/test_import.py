"""Import + locations tests: parsing heuristics, offline/series/LLM import, CRUD."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "tests"))

from test_api import client  # noqa: E402  (reuse the app/client fixture)

FIXTURE = ROOT / "tests" / "test_data" / "superposition.txt"


def _files(*paths: str):
    return [
        (path.rsplit("/", 1)[-1], Path(path).read_bytes(), "text/plain")
        for path in paths
    ]


# ── Parser unit tests ────────────────────────────────────────

def test_split_chapters_plain_superposition():
    from app.services.importer import decode_file, split_chapters

    doc = split_chapters(decode_file(FIXTURE.read_bytes()), "superposition.txt")
    assert doc.format == "plain"
    assert doc.title == "Superposition"
    assert len(doc.chapters) == 10
    assert doc.chapters[0].title == "Chapter 1"
    assert doc.chapters[0].word_count > 50
    assert not doc.warnings


def test_split_chapters_markdown():
    from app.services.importer import split_chapters

    text = (
        "# The Long Way Home\n\n"
        "Dad never liked shortcuts.\n\n"
        "## Chapter 1\n\nThe road began.\n\n"
        "## Chapter 2\n\nIt ended near the coast.\n"
    )
    doc = split_chapters(text, "book.md")
    assert doc.format == "markdown"
    assert doc.title == "The Long Way Home"
    assert [c.title for c in doc.chapters] == ["Chapter 1", "Chapter 2"]
    assert doc.chapters[0].content.strip() == "The road began."


def test_split_chapters_no_markers():
    from app.services.importer import split_chapters

    text = "Just a blob of writing here with nothing special at all."
    doc = split_chapters(text, "nope.txt")
    assert len(doc.chapters) == 1
    assert "single chapter" in doc.warnings[0]
    assert text in doc.chapters[0].content


def test_split_chapters_setext_only():
    from app.services.importer import split_chapters

    text = (
        "Prologue\n--------\nOnce upon a time.\n\n"
        "The Rising\n==========\nThings got worse.\n\n"
        "The Fall\n========\nThings ended.\n"
    )
    doc = split_chapters(text, "x.txt")
    assert [c.title for c in doc.chapters] == [
        "Prologue",
        "The Rising",
        "The Fall",
    ]
    assert "split on section headings" in doc.warnings[0]


def test_yaml_front_matter():
    from app.services.importer import split_chapters

    text = (
        "---\ntitle: The Salt Road\nauthor: Jane Doe\ngenre: Fantasy\n"
        "---\n\n## Chapter 1\n\nA road of salt.\n"
    )
    doc = split_chapters(text, "a.md")
    assert doc.title == "The Salt Road"
    assert doc.author == "Jane Doe"
    assert doc.genre == "Fantasy"
    assert len(doc.chapters) == 1


def test_detect_cast_and_places():
    from app.services.importer import decode_file, detect_cast_and_places, split_chapters

    doc = split_chapters(decode_file(FIXTURE.read_bytes()), "superposition.txt")
    prose = "\n".join(c.content for c in doc.chapters)
    chars, places = detect_cast_and_places(prose)
    names = [c["name"] for c in chars]
    assert "Eddie" in names and "Jessop" in names
    assert all(n.lower() not in ("to himself", "aloud", "it") for n in names)
    assert any(p["name"] == "Elmont" for p in places)
    assert len(places) <= 12


def test_expand_locative_vocabulary_uses_embeddings(monkeypatch):
    """Embedding synonyms widen the preposition set; non-spatial slots are cut."""
    from app.services import importer as imp_mod
    from app import config as config_mod
    from app.services import embeddings as emb_mod

    class _Settings:
        embedding_backend = "st"

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    monkeypatch.setattr(emb_mod, "is_embedding_ready", lambda: True)

    def fake_embed(texts):
        axis = {
            "across": [1.0, 0.0, 0.0],
            "athwart": [0.99, 0.1, 0.05],
            "old": [0.1, 1.0, 0.0],
        }
        return [axis.get(t, [0.0, 0.0, 1.0]) for t in texts]

    monkeypatch.setattr(emb_mod, "embed_texts", fake_embed)

    extra = imp_mod.expand_locative_vocabulary(
        "Fintan rowed athwart the Gorge at dusk. Old Hisa said goodbye."
    )
    assert "athwart" in extra
    assert "old" not in extra


def test_expand_locative_vocabulary_skips_hash_backend(monkeypatch):
    from app.services import importer as imp_mod
    from app import config as config_mod

    class _Settings:
        embedding_backend = "hash"

    monkeypatch.setattr(config_mod, "get_settings", lambda: _Settings())
    assert imp_mod.expand_locative_vocabulary("He rowed athwart the Gorge.") == set()


def test_detect_cast_and_places_extra_prepositions():
    from app.services.importer import detect_cast_and_places

    text = "Fintan rowed athwart the Gorge. Then he rowed athwart the Gorge again."
    chars, places = detect_cast_and_places(text, extra_prepositions={"athwart"})
    assert any(p["name"] == "Gorge" for p in places)


# ── Offline import API tests ─────────────────────────────────

def test_import_offline_single(client):
    r = client.post(
        "/api/import",
        data={"mode": "single", "analyze": "true"},
        files=[("files", ("superposition.txt", FIXTURE.read_bytes(), "text/plain"))],
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "single"
    assert body["llm_used"] is False
    assert not body["projects"][0]["series"]
    assert body["projects"][0]["chapter_count"] == 10
    assert body["characters_added"] >= 2
    assert any("No LLM connected" in w for w in body["warnings"])

    pid = body["projects"][0]["id"]
    r = client.get(f"/api/projects/{pid}")
    assert r.status_code == 200
    project = r.json()
    assert any(c["name"] == "Eddie" for c in project["characters"])
    assert len(project["locations"]) == body["projects"][0]["location_count"]
    assert body["projects"][0]["word_count"] > 0

    # project list exposes location_count
    r = client.get("/api/projects")
    summary = next(p for p in r.json() if p["id"] == pid)
    assert summary["location_count"] == len(project["locations"])

    client.delete(f"/api/projects/{pid}")


def test_import_series_offline(client):
    one = (
        '## Chapter 1\n\n"Two suns," Mara said, shading her eyes.\n\n'
        '## Chapter 2\n\nThey crossed into Vell Mar. "Walls hold," Mara said.\n'
    )
    two = (
        '## Chapter 1\n\n"The battery is dead," Hugo said, shaking it.\n\n'
        '## Chapter 2\n\n"Ring it," Hugo said, pointing at the tower.\n'
    )
    r = client.post(
        "/api/import",
        data={"mode": "series", "series": "Multi Book", "analyze": "false"},
        files=[
            ("files", ("novella1.md", one.encode(), "text/markdown")),
            ("files", ("novella2.md", two.encode(), "text/markdown")),
        ],
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "series"
    assert body["series"] == "Multi Book"
    assert len(body["projects"]) == 2
    positions = [p["series_position"] for p in body["projects"]]
    assert positions == [1, 2]
    assert body["bible_updated"] is True

    for p in body["projects"]:
        assert p["character_count"] >= 1

    r = client.get("/api/series/Multi Book/bible")
    assert r.status_code == 200
    bible = r.json()
    names = {c["name"] for c in bible["characters"]}
    assert "Mara" in names or "Hugo" in names

    for p in body["projects"]:
        client.delete(f"/api/projects/{p['id']}")


def test_import_joins_existing_universe(client):
    # Pre-existing universe, with a bible already on file
    r = client.post(
        "/api/projects",
        json={"title": "Prequel", "series": "Shared Verse", "series_position": 1},
    )
    assert r.status_code == 201
    prequel_id = r.json()["id"]

    r = client.put(
        "/api/series/Shared Verse/bible",
        json={
            "world_notes": "Tonal magic runs on silence.",
            "characters": [
                {"name": "Hugo", "role": "Chronist", "backstory": "Came first."}
            ],
            "locations": [
                {"name": "Vell Mar", "type": "city", "description": "Old capital."}
            ],
        },
    )
    assert r.status_code == 200

    draft = (
        "## Chapter 1\n\nMara said: 'Hugo sings in Vell Mar.' "
        "'He is older,' Mara said, pulling her hood up.\n"
    )
    r = client.post(
        "/api/import",
        data={"mode": "series", "series": "Shared Verse", "analyze": "false"},
        files=[("files", ("newbook.md", draft.encode(), "text/markdown"))],
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["projects"]) == 1
    pid = body["projects"][0]["id"]
    assert body["projects"][0]["series_position"] == 2  # joined after the prequel

    project = client.get(f"/api/projects/{pid}").json()
    names = {c["name"] for c in project["characters"]}
    assert "Hugo" in names  # shared universe seeded into the book
    assert "Mara" in names  # the book's own cast detected
    # Story toujours referenced the universe even when the book joins
    assert any(l["name"] == "Vell Mar" for l in project["locations"])

    # Bible got the book's additions but no Hugo duplicate
    bible = client.get("/api/series/Shared Verse/bible").json()
    bib_names = [c["name"] for c in bible["characters"]]
    assert bib_names.count("Hugo") == 1
    assert "Mara" in bib_names

    client.delete(f"/api/projects/{prequel_id}")
    client.delete(f"/api/projects/{pid}")


def test_import_single_merge_two_files(client):
    a = "## Chapter 1\n\nFile A chapter one.\n"
    b = "## Chapter 1\n\nFile B chapter one.\n"
    r = client.post(
        "/api/import",
        data={"mode": "single", "analyze": "false"},
        files=[
            ("files", ("partA.md", a.encode(), "text/markdown")),
            ("files", ("partB.md", b.encode(), "text/markdown")),
        ],
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "single"
    assert len(body["projects"]) == 1
    assert body["projects"][0]["chapter_count"] == 2
    client.delete(f"/api/projects/{body['projects'][0]['id']}")


def test_import_with_llm(client, monkeypatch):
    from app.api import imports as imports_mod

    class _ImportLLM:
        async def check_available(self, force: bool = False):
            return True

        async def complete(self, **kwargs):
            user = kwargs.get("user_message", "")
            if "Summarize this chapter" in user:
                return "Eddie and Jessop argue, then the guards advance."
            return (
                '{"characters": [{"name": "Dukkat Blane", "role": "Bureaucrat"}, '
                '{"name": "Eloise", "role": "Teacher"}], '
                '"locations": [{"name": "Synergy Cab HQ", "type": "building"}], '
                '"world_facts": ["Synergy Cab is the transit service.", '
                '"The city runs on tubes."]}'
            )

    monkeypatch.setattr(imports_mod, "get_llm", lambda: _ImportLLM())

    draft = (
        "## Chapter 1\n\nDukkat Blane waited for the tube car. Eloise called him Ducky.\n\n"
        "## Chapter 2\n\nEloise waited at Synergy Cab HQ. Dukkat used the tubes.\n"
    )
    r = client.post(
        "/api/import",
        data={"mode": "single", "analyze": "true"},
        files=[("files", ("novel.md", draft.encode(), "text/markdown"))],
        timeout=60,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "single"
    assert body["llm_used"] is True
    assert body["summaries_generated"] == 2
    assert body["world_facts_added"] >= 1

    pid = body["projects"][0]["id"]
    project = client.get(f"/api/projects/{pid}").json()
    assert any(c["name"] == "Dukkat Blane" for c in project["characters"])
    assert all((c["summary"] or "").strip() for c in project["chapters"])

    client.delete(f"/api/projects/{pid}")


def test_import_validation(client):
    # no files → FastAPI rejects the missing required field
    r = client.post("/api/import", data={"mode": "single"})
    assert r.status_code == 422

    # unsupported extension
    r = client.post(
        "/api/import",
        data={"mode": "single"},
        files=[("files", ("book.pdf", b"not a pdf", "application/pdf"))],
    )
    assert r.status_code == 400

    # invalid mode
    r = client.post(
        "/api/import",
        data={"mode": "bogus"},
        files=[("files", ("a.txt", b"hello", "text/plain"))],
    )
    assert r.status_code == 400


# ── Locations CRUD ───────────────────────────────────────────

def test_locations_crud(client):
    r = client.post("/api/projects", json={"title": "Places"})
    assert r.status_code == 201
    pid = r.json()["id"]

    r = client.post(
        f"/api/projects/{pid}/locations",
        json={"name": "The Docks", "type": "district", "description": "Wet and dark."},
    )
    assert r.status_code == 201
    loc = r.json()
    assert loc["name"] == "The Docks"

    r = client.get(f"/api/projects/{pid}/locations")
    assert r.status_code == 200
    assert len(r.json()) == 1

    r = client.get(f"/api/projects/{pid}/locations/{loc['id']}")
    assert r.status_code == 200

    r = client.patch(
        f"/api/projects/{pid}/locations/{loc['id']}",
        json={"name": "Rot Dock", "description": "Rises at low tide."},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "Rot Dock"
    assert r.json()["description"] == "Rises at low tide."

    r = client.delete(f"/api/projects/{pid}/locations/{loc['id']}")
    assert r.status_code == 204

    r = client.get(f"/api/projects/{pid}/locations")
    assert r.json() == []

    r = client.get("/api/projects/does-not-exist/locations")
    assert r.status_code == 404

    client.delete(f"/api/projects/{pid}")