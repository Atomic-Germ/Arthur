import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import ProjectList from "./components/ProjectList";
import Workspace from "./components/Workspace";

export default function App() {
  const [projects, setProjects] = useState([]);
  const [seriesList, setSeriesList] = useState([]);
  const [loading, setLoading] = useState(true);
  const [activeProjectId, setActiveProjectId] = useState(null);
  const [health, setHealth] = useState(null);
  const [bootError, setBootError] = useState("");

  const refreshProjects = useCallback(async () => {
    const list = await api.listProjects();
    setProjects(list);
    setSeriesList(await api.listSeries().then((s) => s.map((x) => x.name)));
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [h] = await Promise.all([api.health(), refreshProjects()]);
        if (!cancelled) {
          setHealth(h);
          setBootError("");
        }
      } catch (err) {
        if (!cancelled) {
          setBootError(
            err.message ||
              "Cannot reach the Arthur API. Is the backend running on :8000?"
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    const t = setInterval(async () => {
      try {
        const h = await api.health();
        if (!cancelled) setHealth(h);
      } catch {
        /* ignore poll errors */
      }
    }, 15000);

    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [refreshProjects]);

  async function handleCreate(form) {
    const p = await api.createProject(form);
    await refreshProjects();
    setActiveProjectId(p.id);
  }

  async function handleDelete(id) {
    await api.deleteProject(id);
    if (activeProjectId === id) setActiveProjectId(null);
    await refreshProjects();
  }

  async function handleFork(id, title) {
    const forked = await api.forkProject(id, title);
    await refreshProjects();
    setActiveProjectId(forked.id);
    return forked;
  }

  async function handleImport(files, opts) {
    const form = new FormData();
    files.forEach((f) => form.append("files", f));
    form.append("mode", opts.mode || "auto");
    form.append("series", opts.series || "");
    form.append("title", opts.title || "");
    form.append("analyze", opts.analyze ? "true" : "false");
    const report = await api.importManuscript(form);
    await refreshProjects();
    return report;
  }

  if (bootError && !activeProjectId) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="card max-w-md p-8 text-center">
          <h1 className="mb-2 font-serif text-2xl text-ink-50">Arthur</h1>
          <p className="mb-4 text-sm text-red-300">{bootError}</p>
          <pre className="mb-4 rounded-lg bg-ink-950 p-3 text-left font-mono text-[11px] text-ink-400">
            {`cd backend && python run.py\ncd frontend && npm run dev`}
          </pre>
          <button
            type="button"
            className="btn-primary"
            onClick={() => window.location.reload()}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  if (activeProjectId) {
    return (
      <div className="h-full">
        <Workspace
          projectId={activeProjectId}
          seriesList={seriesList}
          health={health}
          onBack={() => {
            setActiveProjectId(null);
            refreshProjects();
          }}
          onOpenProject={setActiveProjectId}
        />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto">
      <ProjectList
        projects={projects}
        seriesList={seriesList}
        loading={loading}
        onOpen={setActiveProjectId}
        onCreate={handleCreate}
        onDelete={handleDelete}
        onFork={handleFork}
        onImport={handleImport}
      />
    </div>
  );
}
