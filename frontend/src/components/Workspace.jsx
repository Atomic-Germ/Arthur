import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { useDebouncedCallback } from "../hooks/useDebouncedCallback";
import AssistPanel from "./AssistPanel";
import AuthorPanel from "./AuthorPanel";
import ChapterSidebar from "./ChapterSidebar";
import CharacterPanel from "./CharacterPanel";
import DraftCompare from "./DraftCompare";
import Editor from "./Editor";
import SeriesPanel from "./SeriesPanel";
import StatusPill from "./StatusPill";
import StoryMap from "./StoryMap";
import WorldNotes from "./WorldNotes";

function countWords(text) {
  return text.trim() ? text.trim().split(/\s+/).length : 0;
}

function TabButton({ id, label, active, onSelect }) {
  return (
    <button
      type="button"
      onClick={() => onSelect(id)}
      className={`flex-1 py-2.5 text-xs font-medium transition ${
        active === id
          ? "border-b-2 border-accent text-accent-glow"
          : "text-ink-500 hover:text-ink-200"
      }`}
    >
      {label}
    </button>
  );
}

export default function Workspace({
  projectId,
  seriesList = [],
  health,
  onBack,
  onOpenProject,
}) {
  const [project, setProject] = useState(null);
  const [chapters, setChapters] = useState([]);
  const [characters, setCharacters] = useState([]);
  const [activeChapterId, setActiveChapterId] = useState(null);
  const [rightTab, setRightTab] = useState("ai");
  const [saving, setSaving] = useState(false);
  const [indexing, setIndexing] = useState(false);
  const [error, setError] = useState("");
  const [loadState, setLoadState] = useState("loading");
  const [compareOpen, setCompareOpen] = useState(false);
  const [ttsPacing, setTtsPacing] = useState({
    paragraph_pause: 0.35,
    scene_pause: 1.0,
    chapter_pause: 1.4,
    quote_pause: 0.0,
    comma_pause: 0.0,
    speech_rate: 1.0,
  });
  const saveGen = useRef(0);
  const latestContent = useRef({});
  const [contentSync, setContentSync] = useState(0);

  const activeChapter = useMemo(
    () => chapters.find((c) => c.id === activeChapterId) || null,
    [chapters, activeChapterId]
  );

  const tabs = useMemo(() => {
    const base = [
      ["ai", "Arthur"],
      ["map", "Map"],
      ["characters", "Cast"],
      ["world", "World"],
      ["author", "Author"],
      ["drafts", "Drafts"],
    ];
    if (project?.series?.trim()) {
      return [...base.slice(0, 4), ["series", "Series"], ...base.slice(4)];
    }
    return base;
  }, [project]);

  useEffect(() => {
    if (rightTab === "series" && !project?.series?.trim()) {
      setRightTab("world");
    }
  }, [project, rightTab]);

  const load = useCallback(async () => {
    setLoadState("loading");
    setError("");
    try {
      const p = await api.getProject(projectId);
      setProject(p);
      const sorted = [...(p.chapters || [])].sort((a, b) => a.order - b.order);
      setChapters(sorted);
      setCharacters(p.characters || []);
      sorted.forEach((c) => {
        latestContent.current[c.id] = c.content || "";
      });
      setActiveChapterId((prev) => {
        if (prev && sorted.some((c) => c.id === prev)) return prev;
        return sorted[0]?.id || null;
      });
      setLoadState("ready");
    } catch (err) {
      setError(err.message);
      setLoadState("error");
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  const saveChapter = useDebouncedCallback(async (chapterId, patch) => {
    const gen = ++saveGen.current;
    setSaving(true);
    try {
      const updated = await api.updateChapter(projectId, chapterId, patch);
      // Only merge metadata — never clobber newer local text mid-type
      setChapters((prev) =>
        prev.map((c) => {
          if (c.id !== updated.id) return c;
          const local = latestContent.current[c.id];
          const keepLocal =
            local !== undefined && local !== updated.content && patch.content !== undefined;
          return {
            ...updated,
            content: keepLocal ? local : updated.content,
            word_count: keepLocal ? countWords(local) : updated.word_count,
            title: patch.title !== undefined ? c.title : updated.title,
          };
        })
      );
    } catch (err) {
      setError(err.message);
    } finally {
      if (gen === saveGen.current) setSaving(false);
    }
  }, 500);

  async function handleAddChapter() {
    const n = chapters.length + 1;
    const ch = await api.createChapter(projectId, {
      title: `Chapter ${n}`,
      content: "",
      order: chapters.length,
    });
    latestContent.current[ch.id] = "";
    setChapters((prev) => [...prev, ch]);
    setActiveChapterId(ch.id);
  }

  async function handleDeleteChapter(id) {
    await api.deleteChapter(projectId, id);
    delete latestContent.current[id];
    setChapters((prev) => {
      const next = prev.filter((c) => c.id !== id);
      if (activeChapterId === id) {
        setActiveChapterId(next[0]?.id || null);
      }
      return next;
    });
  }

  async function handleCreateCharacter(body) {
    const c = await api.createCharacter(projectId, body);
    setCharacters((prev) => [...prev, c]);
    return c;
  }

  async function handleUpdateCharacter(id, body) {
    const c = await api.updateCharacter(projectId, id, body);
    setCharacters((prev) => prev.map((x) => (x.id === id ? c : x)));
    return c;
  }

  async function handleDeleteCharacter(id) {
    await api.deleteCharacter(projectId, id);
    setCharacters((prev) => prev.filter((c) => c.id !== id));
  }

  async function handleImportSeriesCast() {
    const series = project?.series?.trim();
    if (!series) return 0;
    const bible = await api.getSeriesBible(series);
    const existing = new Set(characters.map((c) => c.name.trim().toLowerCase()));
    const additions = (bible.characters || []).filter(
      (c) => !existing.has((c.name || "").trim().toLowerCase())
    );
    let count = 0;
    for (const c of additions) {
      const created = await api.createCharacter(projectId, {
        name: c.name,
        role: c.role || "",
        physical_traits: c.physical_traits || "",
        personality: c.personality || "",
        motivations: c.motivations || "",
        speech_patterns: c.speech_patterns || "",
        backstory: c.backstory || "",
        relationships: c.relationships || "",
        notes: c.notes || "",
      });
      setCharacters((prev) => [...prev, created]);
      count += 1;
    }
    return count;
  }

  async function handleSaveWorld(notes) {
    const p = await api.updateWorldNotes(projectId, notes);
    setProject(p);
  }

  async function handleSaveAuthor(patch) {
    const p = await api.updateProject(projectId, patch);
    setProject(p);
  }

  async function handleChangeSeries(series) {
    const p = await api.updateProject(projectId, {
      series: (series || "").trim(),
      series_position: series ? project?.series_position : undefined,
    });
    setProject(p);
    return p;
  }

  async function handleAssistStream({ mode, prompt }, handlers) {
    const content =
      latestContent.current[activeChapterId] ?? activeChapter?.content ?? "";
    return api.assistStream(
      {
        project_id: projectId,
        chapter_id: activeChapterId,
        mode,
        prompt,
        context_text: content.slice(-2000),
      },
      handlers
    );
  }

  function handleInsert(text) {
    if (!activeChapter) return;
    if (text.includes("**Offline")) return;
    const base =
      latestContent.current[activeChapter.id] ?? activeChapter.content ?? "";
    const next = base
      ? `${base.replace(/\s+$/, "")}\n\n${text.trim()}\n`
      : `${text.trim()}\n`;
    latestContent.current[activeChapter.id] = next;
    setChapters((prev) =>
      prev.map((c) =>
        c.id === activeChapter.id
          ? { ...c, content: next, word_count: countWords(next) }
          : c
      )
    );
    saveChapter(activeChapter.id, { content: next });
    setContentSync((s) => s + 1);
  }

  async function handleIndex() {
    setIndexing(true);
    try {
      await api.index(projectId);
    } catch (err) {
      setError(err.message);
    } finally {
      setIndexing(false);
    }
  }

  async function handleFork() {
    try {
      const forked = await api.forkProject(projectId);
      setError("");
      if (onOpenProject && forked?.id) {
        onOpenProject(forked.id);
      }
      return forked;
    } catch (err) {
      setError(err.message || "Fork failed");
      throw err;
    }
  }

  async function handleExport(format, opts = {}) {
    try {
      if (format === "audiobook-example") {
        await api.downloadTtsExport(projectId, ttsPacing, opts);
        return;
      }
      await api.downloadExport(projectId, format);
    } catch (err) {
      setError(err.message || "Export failed");
      throw err;
    }
  }

  if (loadState === "loading") {
    return (
      <div className="flex h-full items-center justify-center text-sm text-ink-400">
        Opening manuscript…
      </div>
    );
  }

  if (loadState === "error") {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3">
        <p className="text-sm text-red-300">{error}</p>
        <button type="button" className="btn-ghost" onClick={onBack}>
          Back to projects
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-panel-border bg-panel/40 px-4 py-2">
        <div className="flex items-center gap-3">
          <span className="font-serif text-sm text-accent">👻 GhostWriter</span>
          {project?.genre && (
            <span className="rounded-full border border-panel-border px-2 py-0.5 font-mono text-[10px] text-ink-500">
              {project.genre}
            </span>
          )}
          {project?.fork_of && (
            <span className="rounded-full border border-accent/30 px-2 py-0.5 font-mono text-[10px] text-accent">
              fork
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="btn-ghost px-2 py-1 text-xs"
            onClick={async () => {
              try {
                const forked = await handleFork();
                if (forked) {
                  setError("");
                }
              } catch {
                /* error already set */
              }
            }}
            title="Fork this draft to try a new version"
          >
            📋 Fork
          </button>
          <button
            type="button"
            className="btn-ghost px-2 py-1 text-xs"
            onClick={() => setCompareOpen((v) => !v)}
            title="Compare drafts side by side"
          >
            🔍 Compare
          </button>
          <StatusPill label="API" ok title="Backend reachable" />
          <StatusPill
            label="LLM"
            ok={!!health?.llm_available}
            title={
              health?.llm_available
                ? "Language model connected"
                : "No LLM — offline helpers active"
            }
          />
          <StatusPill
            label="Memory"
            ok={!!health?.embedding_ready}
            title={
              health?.embedding_ready
                ? "Embeddings ready for RAG"
                : "Embeddings loading or unavailable"
            }
          />
        </div>
      </header>

      {error && (
        <div className="border-b border-red-900/40 bg-red-950/30 px-4 py-1.5 text-xs text-red-200">
          {error}
          <button type="button" className="ml-3 underline" onClick={() => setError("")}>
            dismiss
          </button>
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <ChapterSidebar
          project={project}
          chapters={chapters}
          activeChapterId={activeChapterId}
          onSelect={setActiveChapterId}
          onAdd={handleAddChapter}
          onDelete={handleDeleteChapter}
          onBack={onBack}
          onExport={handleExport}
          onFork={handleFork}
        />

        <Editor
          chapter={activeChapter}
          saving={saving}
          projectId={projectId}
          contentSync={contentSync}
          pacing={ttsPacing}
          onPacingChange={setTtsPacing}
          onChangeTitle={(title) => {
            if (!activeChapter) return;
            setChapters((prev) =>
              prev.map((c) => (c.id === activeChapter.id ? { ...c, title } : c))
            );
            saveChapter(activeChapter.id, { title });
          }}
          onChangeContent={(content) => {
            if (!activeChapter) return;
            latestContent.current[activeChapter.id] = content;
            const word_count = countWords(content);
            setChapters((prev) =>
              prev.map((c) =>
                c.id === activeChapter.id ? { ...c, content, word_count } : c
              )
            );
            saveChapter(activeChapter.id, { content });
          }}
        />

        <aside className="flex w-[400px] shrink-0 flex-col border-l border-panel-border bg-panel/60">
          <div className="flex border-b border-panel-border">
            {tabs.map(([id, label]) => (
              <TabButton key={id} id={id} label={label} active={rightTab} onSelect={setRightTab} />
            ))}
          </div>
          <div className="min-h-0 flex-1">
            {rightTab === "ai" && (
              <AssistPanel
                onAssistStream={handleAssistStream}
                onInsert={handleInsert}
                onIndex={handleIndex}
                llmAvailable={!!health?.llm_available}
                indexing={indexing}
              />
            )}
            {rightTab === "map" && (
              <StoryMap
                chapters={chapters}
                characters={characters}
                activeChapterId={activeChapterId}
                onSelectChapter={setActiveChapterId}
              />
            )}
            {rightTab === "characters" && (
              <CharacterPanel
                characters={characters}
                onCreate={handleCreateCharacter}
                onUpdate={handleUpdateCharacter}
                onDelete={handleDeleteCharacter}
                seriesName={project?.series?.trim() || ""}
                onImportFromSeries={project?.series?.trim() ? handleImportSeriesCast : undefined}
              />
            )}
            {rightTab === "world" && (
              <WorldNotes
                notes={project?.world_notes || ""}
                series={project?.series || ""}
                seriesList={seriesList}
                onSave={handleSaveWorld}
                onChangeSeries={handleChangeSeries}
              />
            )}
            {rightTab === "series" && project?.series?.trim() && (
              <SeriesPanel
                seriesName={project.series.trim()}
                projectId={projectId}
                chapters={chapters}
                activeChapterId={activeChapterId}
                onOpenProject={onOpenProject}
              />
            )}
            {rightTab === "author" && (
              <AuthorPanel project={project} onSave={handleSaveAuthor} />
            )}
            {rightTab === "drafts" && (
              <DraftCompare
                projectId={projectId}
                project={project}
              />
            )}
          </div>
        </aside>
      </div>
    </div>
  );
}
