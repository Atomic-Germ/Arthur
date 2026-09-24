const BASE = "/api";

async function request(path, options = {}) {
  const { timeoutMs, ...fetchOpts } = options;
  const controller = new AbortController();
  const ms = timeoutMs ?? 30_000;
  const timer = setTimeout(() => controller.abort(), ms);

  try {
    const res = await fetch(`${BASE}${path}`, {
      headers: {
        "Content-Type": "application/json",
        ...(fetchOpts.headers || {}),
      },
      ...fetchOpts,
      signal: controller.signal,
    });
    if (res.status === 204) return null;
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (!res.ok) {
      const detail = data?.detail || data || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  } catch (err) {
    if (err?.name === "AbortError") {
      throw new Error(
        `Request timed out after ${Math.round(ms / 1000)}s. Backend may be stuck — restart \`python run.py\`.`
      );
    }
    if (err?.message === "Failed to fetch" || err?.name === "TypeError") {
      throw new Error("Cannot reach API. Is the backend running on :8000?");
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Stream assist via SSE.
 * callbacks: onMeta, onToken, onThinking, onPromoteThinking, onDone, onError
 */
async function assistStream(body, handlers = {}) {
  const {
    onMeta,
    onToken,
    onThinking,
    onPromoteThinking,
    onDone,
    onError,
    signal,
  } = handlers;

  const res = await fetch(`${BASE}/assist/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });

  if (!res.ok) {
    const text = await res.text();
    let detail = text;
    try {
      detail = JSON.parse(text)?.detail || text;
    } catch {
      /* ignore */
    }
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }

  if (!res.body) {
    throw new Error("No response body for stream");
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let full = "";
  let thinking = "";
  let sawDone = false;

  const handleEvent = (evt) => {
    if (evt.type === "meta") {
      onMeta?.(evt);
    } else if (evt.type === "token") {
      full += evt.text || "";
      onToken?.(evt.text || "", full);
    } else if (evt.type === "thinking") {
      thinking += evt.text || "";
      onThinking?.(evt.text || "", thinking);
    } else if (evt.type === "promote_thinking") {
      // Model only produced reasoning_content — use it as the answer
      if (!full && thinking) {
        full = thinking;
        onToken?.("", full);
      }
      onPromoteThinking?.(thinking);
    } else if (evt.type === "error") {
      onError?.(evt.message || "Stream error");
    } else if (evt.type === "done") {
      sawDone = true;
      if (!full && thinking) {
        full = thinking;
      }
      onDone?.(full, { thinking });
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";

    for (const part of parts) {
      for (const line of part.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const raw = line.slice(5).trim();
        if (!raw || raw === "[DONE]") continue;
        try {
          handleEvent(JSON.parse(raw));
        } catch {
          /* skip bad chunk */
        }
      }
    }
  }

  if (!sawDone) {
    if (!full && thinking) full = thinking;
    onDone?.(full, { thinking });
  }
  return full;
}

export const api = {
  health: () => request("/health", { timeoutMs: 5_000 }),

  listProjects: () => request("/projects", { timeoutMs: 10_000 }),
  getProject: (id) => request(`/projects/${id}`, { timeoutMs: 10_000 }),
  createProject: (body) =>
    request("/projects", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 10_000,
    }),
  updateProject: (id, body) =>
    request(`/projects/${id}`, {
      method: "PATCH",
      body: JSON.stringify(body),
      timeoutMs: 10_000,
    }),
  deleteProject: (id) =>
    request(`/projects/${id}`, { method: "DELETE", timeoutMs: 10_000 }),
  updateWorldNotes: (id, world_notes) =>
    request(`/projects/${id}/world-notes`, {
      method: "PUT",
      body: JSON.stringify({ world_notes }),
      timeoutMs: 15_000,
    }),
  forkProject: (id, title) =>
    request(`/projects/${id}/fork`, {
      method: "POST",
      body: JSON.stringify({ title }),
      timeoutMs: 15_000,
    }),

  listSeries: () => request("/series", { timeoutMs: 10_000 }),
  getSeriesBible: (name) =>
    request(`/series/${encodeURIComponent(name)}/bible`, { timeoutMs: 10_000 }),
  updateSeriesBible: (name, body) =>
    request(`/series/${encodeURIComponent(name)}/bible`, {
      method: "PUT",
      body: JSON.stringify(body),
      timeoutMs: 15_000,
    }),

  extractFromStory: (projectId, body) =>
    request(`/projects/${projectId}/extract`, {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 3_600_000,
    }),

  /** Import text/markdown files as manuscript(s). Multipart; no client
   *  timeout — with an LLM attached the pass can take several minutes. */
  async importManuscript(formData) {
    const res = await fetch(`${BASE}/import`, {
      method: "POST",
      body: formData,
    });
    let data = null;
    const text = await res.text();
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = text;
    }
    if (!res.ok) {
      const detail = data?.detail || data || res.statusText;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return data;
  },

  ttsStatus: (projectId) =>
    request(`/projects/${projectId}/tts/status`, { timeoutMs: 10_000 }),

  /** Read a short selection aloud — returns a WAV blob (no guardrail). */
  async ttsPreview(projectId, text, pacing = {}) {
    const params = new URLSearchParams();
    if (pacing.paragraph_pause !== undefined)
      params.set("paragraph_pause", pacing.paragraph_pause);
    if (pacing.scene_pause !== undefined)
      params.set("scene_pause", pacing.scene_pause);
    if (pacing.chapter_pause !== undefined)
      params.set("chapter_pause", pacing.chapter_pause);
    if (pacing.quote_pause !== undefined)
      params.set("quote_pause", pacing.quote_pause);
    if (pacing.comma_pause !== undefined)
      params.set("comma_pause", pacing.comma_pause);
    if (pacing.speech_rate !== undefined)
      params.set("speech_rate", pacing.speech_rate);
    const qs = params.toString() ? `?${params.toString()}` : "";
    const res = await fetch(`${BASE}/projects/${projectId}/tts/preview${qs}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch {
        /* ignore */
      }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return await res.blob();
  },

  /** Full-book audiobook example — WAV with guardrail disclaimers embedded.
   *  opts.engine: "auto" | "audio8" | "piper"; opts.clone: use saved author voice. */
  async downloadTtsExport(projectId, pacing = {}, opts = {}) {
    const params = new URLSearchParams();
    if (pacing.paragraph_pause !== undefined)
      params.set("paragraph_pause", pacing.paragraph_pause);
    if (pacing.scene_pause !== undefined)
      params.set("scene_pause", pacing.scene_pause);
    if (pacing.chapter_pause !== undefined)
      params.set("chapter_pause", pacing.chapter_pause);
    if (pacing.quote_pause !== undefined)
      params.set("quote_pause", pacing.quote_pause);
    if (pacing.comma_pause !== undefined)
      params.set("comma_pause", pacing.comma_pause);
    if (pacing.speech_rate !== undefined)
      params.set("speech_rate", pacing.speech_rate);
    if (opts.engine) params.set("engine", opts.engine);
    if (opts.clone) params.set("clone", "true");
    const qs = params.toString() ? `?${params.toString()}` : "";
    const res = await fetch(
      `${BASE}/projects/${projectId}/tts/export${qs}`,
      { headers: { Accept: "*/*" } }
    );
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch {
        /* ignore */
      }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const match = cd.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || "audiobook-example.wav";
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    return filename;
  },

  authorVoiceStatus: () =>
    request("/tts/author-voice/status", { timeoutMs: 10_000 }),

  async uploadAuthorVoice(file, transcript) {
    const form = new FormData();
    form.append("file", file);
    form.append("transcript", transcript);
    const res = await fetch(`${BASE}/tts/author-voice`, {
      method: "POST",
      body: form,
    });
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch {
        /* ignore */
      }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return res.json();
  },

  deleteAuthorVoice: () => request("/tts/author-voice", { method: "DELETE" }),

  listCharacters: (projectId) =>
    request(`/projects/${projectId}/characters`, { timeoutMs: 10_000 }),
  createCharacter: (projectId, body) =>
    request(`/projects/${projectId}/characters`, {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 10_000,
    }),
  updateCharacter: (projectId, characterId, body) =>
    request(`/projects/${projectId}/characters/${characterId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
      timeoutMs: 10_000,
    }),
  deleteCharacter: (projectId, characterId) =>
    request(`/projects/${projectId}/characters/${characterId}`, {
      method: "DELETE",
      timeoutMs: 10_000,
    }),

  listLocations: (projectId) =>
    request(`/projects/${projectId}/locations`, { timeoutMs: 10_000 }),
  createLocation: (projectId, body) =>
    request(`/projects/${projectId}/locations`, {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 10_000,
    }),
  updateLocation: (projectId, locationId, body) =>
    request(`/projects/${projectId}/locations/${locationId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
      timeoutMs: 10_000,
    }),
  deleteLocation: (projectId, locationId) =>
    request(`/projects/${projectId}/locations/${locationId}`, {
      method: "DELETE",
      timeoutMs: 10_000,
    }),

  listChapters: (projectId) =>
    request(`/projects/${projectId}/chapters`, { timeoutMs: 10_000 }),
  createChapter: (projectId, body) =>
    request(`/projects/${projectId}/chapters`, {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 10_000,
    }),
  updateChapter: (projectId, chapterId, body) =>
    request(`/projects/${projectId}/chapters/${chapterId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
      timeoutMs: 12_000,
    }),
  deleteChapter: (projectId, chapterId) =>
    request(`/projects/${projectId}/chapters/${chapterId}`, {
      method: "DELETE",
      timeoutMs: 10_000,
    }),

  assist: (body) =>
    request("/assist", {
      method: "POST",
      body: JSON.stringify(body),
      timeoutMs: 3_600_000,
    }),
  assistStream,
  index: (projectId, chapterId = null) =>
    request("/index", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId, chapter_id: chapterId }),
      timeoutMs: 15_000,
    }),

  listExportFormats: (projectId) =>
    request(`/projects/${projectId}/export/formats`, { timeoutMs: 10_000 }),

  /** Trigger browser download for a publishable export. */
  async downloadExport(projectId, format = "markdown") {
    const res = await fetch(
      `${BASE}/projects/${projectId}/export?format=${encodeURIComponent(format)}`,
      { headers: { Accept: "*/*" } }
    );
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const j = await res.json();
        detail = j.detail || detail;
      } catch {
        /* ignore */
      }
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const match = cd.match(/filename="?([^";]+)"?/i);
    const filename = match?.[1] || `manuscript.${format}`;
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    return filename;
  },
};
