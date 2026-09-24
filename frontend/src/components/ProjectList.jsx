import { useState } from "react";

export default function ProjectList({
  projects,
  seriesList = [],
  loading,
  onOpen,
  onCreate,
  onDelete,
  onFork,
  onImport,
}) {
  const [form, setForm] = useState({
    title: "",
    genre: "",
    description: "",
    premise: "",
    series: "",
  });
  const [showForm, setShowForm] = useState(false);
  const [creating, setCreating] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [importFiles, setImportFiles] = useState([]);
  const [importOpts, setImportOpts] = useState({
    mode: "auto",
    series: "",
    title: "",
    analyze: true,
  });
  const [importing, setImporting] = useState(false);
  const [importResult, setImportResult] = useState(null);
  const [importError, setImportError] = useState("");

  function resetImport() {
    setImportFiles([]);
    setImportOpts({ mode: "auto", series: "", title: "", analyze: true });
    setImportResult(null);
    setImportError("");
  }

  function toggleImport() {
    setShowImport((v) => {
      if (v) {
        setShowForm(false);
        resetImport();
      }
      return !v;
    });
  }

  async function handleImport(e) {
    e.preventDefault();
    if (!importFiles.length) return;
    setImporting(true);
    setImportError("");
    try {
      const report = await onImport(importFiles, importOpts);
      setImportResult(report);
    } catch (err) {
      setImportError(err.message || "Import failed");
    } finally {
      setImporting(false);
    }
  }

  return (
    <div className="mx-auto flex min-h-full max-w-4xl flex-col px-6 py-12">
      <header className="mb-10">
        <div className="mb-2 flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl border border-accent/30 bg-accent/10 font-serif text-lg text-accent">
            G
          </div>
          <div>
            <h1 className="font-serif text-3xl font-semibold tracking-tight text-ink-50">
              Arthur
            </h1>
            <p className="text-sm text-ink-400">Story-aware writing companion</p>
          </div>
        </div>
      </header>

      <div className="mb-6 flex items-center justify-between">
        <h2 className="panel-title">Your manuscripts</h2>
        <div className="flex gap-2">
          <button
            type="button"
            className="btn-ghost"
            onClick={() => {
              setShowForm(false);
              if (showImport) resetImport();
              setShowImport(!showImport);
            }}
          >
            {showImport ? "Cancel" : "Import"}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => {
              setShowImport(false);
              if (showForm) resetImport();
              setShowForm((v) => !v);
            }}
          >
            {showForm ? "Cancel" : "New project"}
          </button>
        </div>
      </div>

      {showImport && (
        <form onSubmit={handleImport} className="card mb-8 grid gap-4 p-5">
          <div className="sm:col-span-2">
            <label className="label">Manuscript files</label>
            <input
              className="input file:mr-3 file:rounded-md file:border-0 file:bg-accent/15 file:px-3 file:py-1 file:text-xs file:font-medium file:text-accent"
              type="file"
              multiple
              accept=".txt,.text,.md,.markdown,.mdown,.mkd,.rst"
              onChange={(e) => setImportFiles([...e.target.files])}
            />
            <p className="mt-1 text-[11px] text-ink-500">
              Text or Markdown (≤25 MB each). One file = one book; several files
              can be merged into a single project or become a series.
            </p>
          </div>
          <div>
            <label className="label">Mode</label>
            <select
              className="input"
              value={importOpts.mode}
              onChange={(e) => setImportOpts({ ...importOpts, mode: e.target.value })}
            >
              <option value="auto">Auto (best guess)</option>
              <option value="single">Single book — merge all files</option>
              <option value="series">Series — one book per file</option>
            </select>
          </div>
          <div>
            <label className="label">
              Series <span className="font-normal text-ink-500">(optional)</span>
            </label>
            <input
              className="input"
              list="pl-import-series-list"
              value={importOpts.series}
              onChange={(e) => setImportOpts({ ...importOpts, series: e.target.value })}
              placeholder={
                seriesList.length
                  ? "Pick an existing series or type a new one"
                  : "e.g. The Drowned Chronicles"
              }
            />
            <datalist id="pl-import-series-list">
              {seriesList.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </div>
          <div className="sm:col-span-2">
            <label className="label">
              Title <span className="font-normal text-ink-500">(optional)</span>
            </label>
            <input
              className="input"
              value={importOpts.title}
              onChange={(e) => setImportOpts({ ...importOpts, title: e.target.value })}
              placeholder="Overrides the one detected from front matter"
            />
          </div>
          <div className="flex items-center gap-3 sm:col-span-2">
            <label className="flex items-center gap-2 text-sm text-ink-300">
              <input
                type="checkbox"
                className="accent-accent"
                checked={importOpts.analyze}
                onChange={(e) =>
                  setImportOpts({ ...importOpts, analyze: e.target.checked })
                }
              />
              Analyze with the LLM (extraction + summaries when available)
            </label>
          </div>
          <div className="sm:col-span-2 flex justify-end gap-2">
            {importResult && (
              <p className="mr-auto self-center text-xs text-emerald-300">
                {importResult.projects.length}{" "}
                {importResult.projects.length === 1 ? "book" : "books"} imported
                {importResult.series ? ` into “${importResult.series}”` : ""}
                {importResult.bible_updated ? " · bible updated" : ""}
              </p>
            )}
            <button type="submit" className="btn-primary" disabled={importing || !importFiles.length}>
              {importing ? "Importing…" : "Import"}
            </button>
          </div>
          {importError && (
            <p className="sm:col-span-2 rounded-md bg-red-950/30 px-3 py-2 text-xs text-red-200">
              {importError}
            </p>
          )}
          {importResult && (importResult.warnings || []).length > 0 && (
            <ul className="sm:col-span-2 space-y-1 rounded-md bg-ink-900/40 px-3 py-2 text-[11px] text-ink-400">
              {(importResult.warnings || []).map((w, i) => (
                <li key={i}>• {w}</li>
              ))}
            </ul>
          )}
          {importResult && importResult.projects.length > 0 && (
            <div className="sm:col-span-2 overflow-hidden rounded-md border border-panel-border">
              <table className="w-full text-left text-xs">
                <tbody>
                  {importResult.projects.map((p) => (
                    <tr key={p.id} className="border-b border-panel-border last:border-0">
                      <td className="px-3 py-1.5 font-medium text-ink-200">{p.title}</td>
                      <td className="px-3 py-1.5 font-mono text-[11px] text-ink-500">
                        {p.series ? `#${p.series_position}` : ""}
                      </td>
                      <td className="px-3 py-1.5 font-mono text-[11px] text-ink-500">
                        {p.chapter_count} ch
                      </td>
                      <td className="px-3 py-1.5 font-mono text-[11px] text-ink-500">
                        {p.character_count} cast
                      </td>
                      <td className="px-3 py-1.5 font-mono text-[11px] text-ink-500">
                        {p.location_count} places
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {importResult && importResult.llm_used && (
            <p className="sm:col-span-2 text-[11px] text-ink-500">
              +{importResult.characters_added} cast · +{importResult.locations_added}{" "}
              places · +{importResult.world_facts_added} world facts · +
              {importResult.summaries_generated} chapter summaries
            </p>
          )}
        </form>
      )}

      {showForm && (
        <form onSubmit={handleCreate} className="card mb-8 grid gap-4 p-5 sm:grid-cols-2">
          <div className="sm:col-span-2">
            <label className="label">Title</label>
            <input
              className="input"
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              placeholder="The Last Cartographer"
              autoFocus
              required
            />
          </div>
          <div>
            <label className="label">Genre</label>
            <input
              className="input"
              value={form.genre}
              onChange={(e) => setForm({ ...form, genre: e.target.value })}
              placeholder="Literary fantasy"
            />
          </div>
          <div>
            <label className="label">Premise</label>
            <input
              className="input"
              value={form.premise}
              onChange={(e) => setForm({ ...form, premise: e.target.value })}
              placeholder="One-line hook"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="label">Description</label>
            <textarea
              className="input min-h-[80px] resize-y"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
              placeholder="What is this book about?"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="label">
              Series <span className="font-normal text-ink-500">(optional)</span>
            </label>
            <input
              className="input"
              list="ghostwriter-series-list"
              value={form.series}
              onChange={(e) => setForm({ ...form, series: e.target.value })}
              placeholder={
                seriesList.length
                  ? "Pick an existing series or type a new one"
                  : "e.g. The Drowned Chronicles"
              }
            />
            <datalist id="ghostwriter-series-list">
              {seriesList.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
            {form.series.trim() && seriesList.includes(form.series.trim()) && (
              <p className="mt-1 text-[11px] text-accent/80">
                New book in the “{form.series.trim()}” universe — you can import
                its cast after opening.
              </p>
            )}
          </div>
          <div className="sm:col-span-2 flex justify-end">
            <button type="submit" className="btn-primary" disabled={creating}>
              {creating ? "Creating…" : "Create project"}
            </button>
          </div>
        </form>
      )}

      {loading ? (
        <p className="text-sm text-ink-400">Loading projects…</p>
      ) : projects.length === 0 ? (
        <div className="card flex flex-col items-center gap-3 px-8 py-16 text-center">
          <p className="font-serif text-xl text-ink-200">No manuscripts yet</p>
          <p className="max-w-sm text-sm text-ink-500">
            Create a project to start writing with character dossiers, chapter
            memory, and AI assistance grounded in your story.
          </p>
          <button type="button" className="btn-primary mt-2" onClick={() => setShowForm(true)}>
            Start writing
          </button>
        </div>
      ) : (
        (() => {
          const standalone = [];
          const groups = new Map();
          for (const p of projects) {
            const series = (p.series || "").trim();
            if (series) {
              if (!groups.has(series)) groups.set(series, []);
              groups.get(series).push(p);
            } else {
              standalone.push(p);
            }
          }
          const renderCard = (p) => (
            <li key={p.id}>
              <div className="card group flex items-stretch overflow-hidden transition hover:border-accent/30">
                <button
                  type="button"
                  onClick={() => onOpen(p.id)}
                  className="flex flex-1 flex-col items-start gap-1 px-5 py-4 text-left"
                >
                  <span className="font-serif text-lg text-ink-50 group-hover:text-accent-glow">
                    {p.title}
                  </span>
                  <span className="line-clamp-2 text-sm text-ink-400">
                    {p.description || p.premise || "No description"}
                  </span>
                  <span className="mt-2 flex flex-wrap gap-3 font-mono text-[11px] text-ink-500">
                    {p.genre && <span>{p.genre}</span>}
                    <span>{p.chapter_count} chapters</span>
                    <span>{p.character_count} characters</span>
                    <span>{p.word_count.toLocaleString()} words</span>
                  </span>
                </button>
                <button
                  type="button"
                  className="btn-ghost border-l border-panel-border px-4 text-ink-500 hover:text-accent"
                  title="Fork this draft"
                  onClick={(e) => {
                    e.stopPropagation();
                    onFork?.(p.id, p.title);
                  }}
                >
                  Fork
                </button>
                <button
                  type="button"
                  className="btn-ghost border-l border-panel-border px-4 text-ink-500 hover:text-red-300"
                  title="Delete project"
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirm(`Delete "${p.title}"? This cannot be undone.`)) {
                      onDelete(p.id);
                    }
                  }}
                >
                  Delete
                </button>
              </div>
            </li>
          );
          return (
            <div className="space-y-8">
              {[...groups.entries()].map(([series, books]) => (
                <section key={series}>
                  <div className="mb-2 flex items-center gap-2">
                    <h3 className="panel-title !text-sm !text-accent">{series}</h3>
                    <span className="font-mono text-[11px] text-ink-500">
                      {books.length} {books.length === 1 ? "book" : "books"}
                    </span>
                    <div className="h-px flex-1 bg-panel-border" />
                  </div>
                  <ul className="grid gap-3">{books.map(renderCard)}</ul>
                </section>
              ))}
              {standalone.length > 0 && (
                <section>
                  <div className="mb-2 flex items-center gap-2">
                    <h3 className="panel-title !text-sm text-ink-300">
                      Standalone
                    </h3>
                    <div className="h-px flex-1 bg-panel-border" />
                  </div>
                  <ul className="grid gap-3">{standalone.map(renderCard)}</ul>
                </section>
              )}
            </div>
          );
        })()
      )}
    </div>
  );
}
