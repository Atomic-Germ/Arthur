# Arthur

Intelligent writing companion for authors. Arthur's AI is Arthur, and Arthur is never the author. Arthur does not try to be the author. It reads what you write and keeps story bible of your characters, chapters, and world lore, then uses RAG with an LLM of your choice to help you brainstorm, continue prose, check consistency, and catch plot holes. Most features don't actually require a running LLM -- this is not a story generating bot.

## Features (MVP)

- **Distraction-free editor** — projects, chapters, autosave, word counts
- **Character dossiers** — traits, motivations, speech patterns, relationships
- **Places** — named locations with type/description/notes, indexed into story memory and carried through exports
- **Manuscript import** — drag in `.txt`/`.md` manuscripts: chapters split automatically (headings, `Chapter N`, form feeds), front matter becomes metadata, and cast/places/world facts are populated from the prose before you even open a panel. Locative synonyms are discovered with embeddings when sentence-transformers is loaded, and cast/place extraction plus per-chapter summaries kick in when an LLM is connected — nothing blocks when there is no model
- **World & lore notes** — freeform world-building stored with the manuscript
- **Story memory (RAG)** — chapters/characters/world notes chunked into ChromaDB
- **AI assist modes** — Brainstorm · Continue · Consistency Check · Lore · Plot · Influence Check
- **Influence Analyzer** — maps literary/thematic resonances with cited evidence (craft awareness, not judgment)
- **Story map** — tension pulse, chapter mass, cast presence grid, arc lanes, story circle, co-presence links -- This is the most useful feature for most authors. It offloads mental overhead that isn't the storyline.
- **Export** — Markdown, plain text, HTML, DOCX, EPUB, no-publish watermarked WAV, or full JSON backup
- **Audiobook example** — hear your manuscript read aloud: a higher-quality Audio8 voice (optionally zero-shot cloned from your own reference recording) at 16 kHz, with spoken AI/publication disclaimers embedded so it can never be published; piper remains the lightweight in-editor "Listen" voice and the fallback
- **Local-first LLM** — any OpenAI-compatible API (llama.cpp server, Ollama, OpenAI, FastFlowLM, etc)
- **Offline fallbacks** — useful checklists when no model is running

## Stack

| Layer | Tech |
|--------|------|
| Frontend | React, Vite, Tailwind CSS |
| Backend | Python, FastAPI |
| Memory | ChromaDB + sentence-transformers |
| LLM | OpenAI-compatible HTTP API |

## Quick start

### 1. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

API: http://127.0.0.1:8000  
Docs: http://127.0.0.1:8000/docs

Optional env (see `backend/.env.example`):

```bash
export GW_LLM_BASE_URL=http://localhost:11434/v1   # Ollama
export GW_LLM_MODEL=llama3.2
```

### 2. Frontend

```bash
cd frontend
npm install
npm run dev
```

App: http://127.0.0.1:5173

### 3. LLM (optional but recommended)

**llama.cpp server**

```bash
llama-server -m /path/to/model.gguf --port 8080
# default GW_LLM_BASE_URL=http://localhost:8080/v1
```

**Ollama**

```bash
ollama serve
ollama pull llama3.2
export GW_LLM_BASE_URL=http://localhost:11434/v1
export GW_LLM_MODEL=llama3.2
```

Without a model, the app still runs; assist endpoints return offline guidance.

### 4. Audiobook example (optional)

The guarded audiobook preview defaults to the Audio8 TTS engine. Download its
model once (≈1.7 GB into `data/hf`):

```bash
cd backend && python -m app.services.audio8 download
```

Runs on CPU by default (~9× realtime; fine for a preview). Set
`GW_TTS_DEVICE=cuda` to opt into GPU inference where your torch build is
stable (some ROCm builds segfault in mamba kernels). To save your own voice
for cloning, use "Export → add your voice sample" in the app: upload a short
recording plus its verbatim transcript. Every render embeds spoken
disclaimers and stays un-publishable.


## Project layout

```
Arthur/
├── backend/
│   ├── app/
│   │   ├── api/          # FastAPI routers
│   │   ├── db/           # JSON project storage
│   │   ├── models/       # Pydantic schemas
│   │   ├── services/     # RAG, embeddings, LLM
│   │   ├── config.py
│   │   └── main.py
│   ├── requirements.txt
│   └── run.py
├── frontend/
│   └── src/              # React UI
├── data/                 # projects + chroma (runtime)
└── tests/
```

## Tests

```bash
cd backend && source .venv/bin/activate
pip install pytest httpx
cd ..
pytest tests/ -q
```

## How assist works

1. You write chapters and fill character/world panels.
2. Content is chunked and embedded into a per-project Chroma collection.
3. On assist, Arthur retrieves relevant story fragments + full character dossiers.
4. Context is sent to the LLM with a mode-specific system prompt.
5. Sources used for retrieval are shown in the AI panel.

## Roadmap

- **Phase 2** — deeper plot-hole detection, automated consistency scoring, subplot tracker
- **Phase 3** — collaboration, export (DOCX/EPUB), richer world graph

## License

MIT
