import { useState } from "react";
import { api } from "../api";

const EXPORTS = [
  { id: "markdown", label: "Markdown (.md)" },
  { id: "docx", label: "Word (.docx)" },
  { id: "manuscript-docx", label: "Manuscript (.docx, prose only)" },
  { id: "epub", label: "EPUB ebook" },
  { id: "manuscript-epub", label: "Manuscript (.epub, prose only)" },
  { id: "html", label: "HTML (print)" },
  { id: "cover-jpg", label: "Cover image (.jpg)" },
  { id: "cover-tiff", label: "Cover image (.tiff)" },
  { id: "txt", label: "Plain text" },
  { id: "audiobook-example", label: "Audiobook example (.wav, guarded)" },
  { id: "json", label: "Full backup (.json)" },
];

export default function ChapterSidebar({
  project,
  chapters,
  activeChapterId,
  onSelect,
  onAdd,
  onDelete,
  onBack,
  onExport,
  onFork,
}) {
  const totalWords = chapters.reduce((n, c) => n + (c.word_count || 0), 0);
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(null);
  const [forking, setForking] = useState(false);
  const [voice, setVoice] = useState(null);
  const [clone, setClone] = useState(false);
  const [voiceFile, setVoiceFile] = useState(null);
  const [transcript, setTranscript] = useState("");
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [voiceError, setVoiceError] = useState("");

  async function refreshVoice() {
    try {
      const status = await api.authorVoiceStatus();
      setVoice(status);
      if (!status.exists) setClone(false);
    } catch {
      /* voice panel is optional */
    }
  }

  async function handleExportOpen() {
    const next = !exportOpen;
    setExportOpen(next);
    if (next) await refreshVoice();
  }

  async function handleSaveVoice() {
    if (!voiceFile || voiceBusy) return;
    setVoiceBusy(true);
    setVoiceError("");
    try {
      await api.uploadAuthorVoice(voiceFile, transcript.trim());
      setVoiceFile(null);
      setTranscript("");
      await refreshVoice();
    } catch (err) {
      setVoiceError(err.message);
    } finally {
      setVoiceBusy(false);
    }
  }

  async function handleDeleteVoice() {
    setVoiceBusy(true);
    setVoiceError("");
    try {
      await api.deleteAuthorVoice();
      await refreshVoice();
    } catch (err) {
      setVoiceError(err.message);
    } finally {
      setVoiceBusy(false);
    }
  }

  async function handleExport(fmt) {
    if (!onExport || exporting) return;
    setExporting(fmt);
    try {
      await onExport(fmt, fmt === "audiobook-example" ? { clone } : {});
      setExportOpen(false);
    } finally {
      setExporting(null);
    }
  }

  return (
    <aside className="flex h-full w-60 shrink-0 flex-col border-r border-panel-border bg-panel/60">
      <div className="border-b border-panel-border px-3 py-3">
        <button type="button" className="btn-ghost mb-2 -ml-1 px-2 py-1 text-xs" onClick={onBack}>
          ← Projects
        </button>
        <h2 className="truncate font-serif text-base text-ink-50" title={project?.title}>
          {project?.title}
        </h2>
        <p className="mt-1 font-mono text-[11px] text-ink-500">
          {totalWords.toLocaleString()} words · {chapters.length} ch.
        </p>
      </div>

      <div className="flex items-center justify-between px-3 py-2">
        <span className="panel-title">Chapters</span>
        <button type="button" className="btn-ghost px-2 py-1 text-xs" onClick={onAdd}>
          + Add
        </button>
      </div>

      <ul className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {chapters.length === 0 && (
          <li className="px-2 py-6 text-center text-xs text-ink-500">
            No chapters yet. Add one to begin.
          </li>
        )}
        {chapters.map((ch, i) => {
          const active = ch.id === activeChapterId;
          return (
            <li key={ch.id} className="group mb-0.5 flex items-stretch">
              <button
                type="button"
                onClick={() => onSelect(ch.id)}
                className={`flex min-w-0 flex-1 flex-col rounded-lg px-2.5 py-2 text-left transition ${
                  active
                    ? "bg-accent/15 text-accent-glow"
                    : "text-ink-300 hover:bg-panel-raised hover:text-ink-100"
                }`}
              >
                <span className="truncate text-sm font-medium">
                  <span className="mr-1.5 font-mono text-[10px] opacity-60">{i + 1}</span>
                  {ch.title}
                </span>
                <span className="font-mono text-[10px] opacity-50">
                  {(ch.word_count || 0).toLocaleString()} w
                </span>
              </button>
              <button
                type="button"
                className="invisible rounded px-1.5 text-ink-600 hover:text-red-300 group-hover:visible"
                title="Delete chapter"
                onClick={() => {
                  if (confirm(`Delete “${ch.title}”?`)) onDelete(ch.id);
                }}
              >
                ×
              </button>
            </li>
          );
        })}
      </ul>

      <div className="relative border-t border-panel-border p-2">
        <button
          type="button"
          className="btn-ghost w-full justify-between border border-panel-border px-2.5 py-2 text-xs"
          onClick={handleExportOpen}
          disabled={!project?.id}
        >
          <span>Export</span>
          <span className="font-mono text-[10px] text-ink-500">{exportOpen ? "▴" : "▾"}</span>
        </button>
        {exportOpen && (
          <div className="mt-1 overflow-hidden rounded-lg border border-panel-border bg-panel-raised shadow-soft">
            <ul>
              {EXPORTS.map((f) => (
                <li key={f.id}>
                  <button
                    type="button"
                    className="w-full px-3 py-2 text-left text-xs text-ink-200 transition hover:bg-accent/15 hover:text-accent-glow disabled:opacity-50"
                    disabled={!!exporting}
                    onClick={() => handleExport(f.id)}
                  >
                    {exporting === f.id ? "Exporting…" : f.label}
                  </button>
                </li>
              ))}
            </ul>

            <div className="border-t border-panel-border px-3 py-2">
              <label className="flex cursor-pointer items-center gap-2 text-[11px] text-ink-200">
                <input
                  type="checkbox"
                  className="accent-accent"
                  checked={clone && voice?.exists}
                  disabled={!voice?.exists || !!exporting}
                  onChange={(e) => setClone(e.target.checked)}
                />
                Read in my voice (cloned)
              </label>
              {voice?.exists ? (
                <div className="mt-1 flex items-center justify-between gap-2">
                  <span className="font-mono text-[10px] text-ink-500">
                    voice ready{voice.seconds ? ` · ${voice.seconds}s` : ""}
                  </span>
                  <button
                    type="button"
                    className="text-[10px] text-ink-500 underline hover:text-red-300 disabled:opacity-50"
                    disabled={voiceBusy}
                    onClick={handleDeleteVoice}
                  >
                    remove
                  </button>
                </div>
              ) : (
                <details className="mt-1">
                  <summary className="cursor-pointer font-mono text-[10px] text-ink-500 hover:text-ink-300">
                    + add your voice sample
                  </summary>
                  <input
                    type="file"
                    accept="audio/*,.wav,.mp3,.flac,.ogg"
                    className="mt-2 block w-full text-[10px] text-ink-400 file:mr-2 file:rounded file:border-0 file:bg-panel file:px-2 file:py-1 file:text-[10px] file:text-ink-200"
                    onChange={(e) => setVoiceFile(e.target.files?.[0] || null)}
                  />
                  <textarea
                    value={transcript}
                    onChange={(e) => setTranscript(e.target.value)}
                    placeholder="Type exactly what the recording says…"
                    rows={2}
                    className="mt-2 w-full rounded border border-panel-border bg-panel px-2 py-1 text-[11px] text-ink-100 placeholder:text-ink-600 focus:border-accent focus:outline-none"
                  />
                  <button
                    type="button"
                    className="btn-ghost mt-1.5 w-full justify-center border border-panel-border px-2 py-1 text-[10px]"
                    disabled={!voiceFile || !transcript.trim() || voiceBusy}
                    onClick={handleSaveVoice}
                  >
                    {voiceBusy ? "Saving…" : "Save voice"}
                  </button>
                </details>
              )}
              {voiceError && (
                <p className="mt-1 text-[10px] text-red-300">{voiceError}</p>
              )}
            </div>

            <p className="border-t border-panel-border px-3 py-2 text-[10px] leading-snug text-ink-500">
              The audiobook example renders at 16 kHz with spoken guardrails — a
              preview for hearing your work aloud (GPU recommended), never a
              deliverable.
            </p>
          </div>
        )}
      </div>
      {onFork && (
        <div className="border-t border-panel-border p-2">
          <button
            type="button"
            className="btn-ghost w-full justify-between border border-panel-border px-2.5 py-2 text-xs"
            onClick={async () => {
              if (!project?.id || forking) return;
              setForking(true);
              try {
                await onFork(project.id);
              } finally {
                setForking(false);
              }
            }}
            disabled={forking}
          >
            <span>Fork draft</span>
            <span className="font-mono text-[10px] text-ink-500">
              {forking ? "…" : "📋"}
            </span>
          </button>
        </div>
      )}
    </aside>
  );
}
