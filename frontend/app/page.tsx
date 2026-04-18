"use client";
import { useEffect, useState } from "react";
import {
  api,
  EpisodeSummary,
  EpisodeDetail,
  EpisodeIdea,
  SceneAudio,
  ScriptResponse,
  BeatData,
  Quote,
} from "../lib/api";
import { AuthGate } from "./auth-gate";
import { signOut } from "../lib/auth";

type View =
  | { kind: "list" }
  | { kind: "new" }
  | { kind: "episode"; episodeId: string }
  | { kind: "idea"; episodeId: string; rank: number };

export default function Home() {
  const [user, setUser] = useState<string | null>(null);
  return (
    <AuthGate onUser={setUser}>
      <App user={user} />
    </AuthGate>
  );
}

function App({ user }: { user: string | null }) {
  const [view, setView] = useState<View>({ kind: "list" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [episodes, setEpisodes] = useState<EpisodeSummary[]>([]);

  const run = async <T,>(fn: () => Promise<T>): Promise<T | null> => {
    setBusy(true);
    setErr(null);
    try {
      return await fn();
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e));
      return null;
    } finally {
      setBusy(false);
    }
  };

  // Load episodes for sidebar on mount.
  useEffect(() => {
    api.listEpisodes().then((r) => setEpisodes(r.episodes)).catch(() => {});
  }, []);

  const refreshEpisodes = () => {
    api.listEpisodes().then((r) => setEpisodes(r.episodes)).catch(() => {});
  };

  const breadcrumb = (() => {
    if (view.kind === "new") return "New Episode";
    if (view.kind === "episode") return `Episode ${view.episodeId}`;
    if (view.kind === "idea") return `Episode ${view.episodeId} › Idea #${view.rank}`;
    return "Episodes";
  })();

  return (
    <div className="app-shell">
      {/* Sidebar */}
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-header">
          <div className="logo">V</div>
          <h1>Vyas</h1>
        </div>
        <nav className="sidebar-nav">
          <div className="sidebar-section">
            <div className="sidebar-section-label">Navigation</div>
            <button
              className={`sidebar-item ${view.kind === "list" ? "active" : ""}`}
              onClick={() => { setView({ kind: "list" }); setSidebarOpen(false); }}
            >
              <span className="icon">📋</span> All Episodes
            </button>
            <button
              className={`sidebar-item ${view.kind === "new" ? "active" : ""}`}
              onClick={() => { setView({ kind: "new" }); setSidebarOpen(false); }}
            >
              <span className="icon">➕</span> New Episode
            </button>
          </div>
          {episodes.length > 0 && (
            <div className="sidebar-section">
              <div className="sidebar-section-label">Recent Episodes</div>
              {episodes.slice(0, 10).map((e) => (
                <button
                  key={e.episode_id}
                  className={`sidebar-item ${
                    (view.kind === "episode" || view.kind === "idea") &&
                    view.episodeId === e.episode_id
                      ? "active"
                      : ""
                  }`}
                  onClick={() => {
                    setView({ kind: "episode", episodeId: e.episode_id });
                    setSidebarOpen(false);
                  }}
                >
                  <span className="icon">🎙</span>
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {e.name}
                  </span>
                </button>
              ))}
            </div>
          )}
        </nav>
        <div className="sidebar-footer">
          {user && (
            <>
              <div style={{ color: "var(--text)", fontWeight: 600, marginBottom: 4, overflow: "hidden", textOverflow: "ellipsis" }}>
                {user}
              </div>
              <button
                onClick={() => signOut().then(() => window.location.reload())}
                style={{ background: "none", border: "none", color: "var(--muted)", padding: 0, fontSize: 11, cursor: "pointer" }}
              >
                Sign out
              </button>
            </>
          )}
        </div>
      </aside>

      {/* Overlay for mobile sidebar */}
      {sidebarOpen && (
        <div
          style={{
            position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)",
            zIndex: 99,
          }}
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Main content */}
      <div className="main-content">
        <div className="top-bar">
          <button className="burger" onClick={() => setSidebarOpen(!sidebarOpen)}>
            ☰
          </button>
          <div className="breadcrumb">
            <span>{breadcrumb}</span>
          </div>
          {busy && (
            <span style={{ marginLeft: "auto", color: "var(--accent)", fontSize: 12 }}>
              Processing…
            </span>
          )}
        </div>

        <div className="content-area">
          {err && <div className="error-banner">Error: {err}</div>}

          {view.kind === "list" && (
            <EpisodeList
              onOpen={(id) => setView({ kind: "episode", episodeId: id })}
              onNew={() => setView({ kind: "new" })}
              run={run}
              busy={busy}
            />
          )}
          {view.kind === "new" && (
            <NewEpisode
              run={run}
              busy={busy}
              onCreated={(id) => {
                refreshEpisodes();
                setView({ kind: "episode", episodeId: id });
              }}
              onCancel={() => setView({ kind: "list" })}
            />
          )}
          {view.kind === "episode" && (
            <EpisodeView
              episodeId={view.episodeId}
              run={run}
              busy={busy}
              onBack={() => setView({ kind: "list" })}
              onOpenIdea={(rank) =>
                setView({ kind: "idea", episodeId: view.episodeId, rank })
              }
            />
          )}
          {view.kind === "idea" && (
            <IdeaView
              episodeId={view.episodeId}
              rank={view.rank}
              run={run}
              busy={busy}
              onBack={() => setView({ kind: "episode", episodeId: view.episodeId })}
            />
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- Shared styles ----------

// Now using CSS classes from globals.css — these are kept as thin wrappers.
const panelStyle: React.CSSProperties = {};  // → className="card"
const btn: React.CSSProperties = {};         // → className="btn btn-primary"
const btnSecondary: React.CSSProperties = {}; // → className="btn btn-secondary"
const linkBtn: React.CSSProperties = {
  background: "transparent", color: "var(--accent)", border: 0, padding: 0, cursor: "pointer",
};

// ---------- Episode list ----------

function EpisodeList({
  onOpen,
  onNew,
  run,
  busy,
}: {
  onOpen: (id: string) => void;
  onNew: () => void;
  run: <T,>(fn: () => Promise<T>) => Promise<T | null>;
  busy: boolean;
}) {
  const [episodes, setEpisodes] = useState<EpisodeSummary[] | null>(null);

  useEffect(() => {
    (async () => {
      const r = await run(() => api.listEpisodes());
      if (r) setEpisodes(r.episodes);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="card">
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <h2 style={{ margin: 0 }}>Episodes</h2>
        <button className="btn btn-primary" onClick={onNew} disabled={busy}>
          + New episode
        </button>
      </div>

      {episodes === null && <p style={{ color: "var(--muted)" }}>Loading…</p>}
      {episodes && episodes.length === 0 && (
        <p style={{ color: "var(--muted)" }}>
          No episodes yet. Create one to get started.
        </p>
      )}
      {episodes && episodes.length > 0 && (
        <div style={{ display: "grid", gap: 8 }}>
          {episodes.map((e) => (
            <button
              key={e.episode_id}
              onClick={() => onOpen(e.episode_id)}
              style={{
                textAlign: "left",
                background: "#0e1118",
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: "12px 14px",
                color: "var(--text)",
                cursor: "pointer",
              }}
            >
              <div style={{ fontWeight: 700 }}>{e.name}</div>
              <div style={{ color: "var(--muted)", fontSize: 12 }}>
                {new Date(e.created_at).toLocaleString()}
              </div>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------- New episode ----------

function NewEpisode({
  run,
  busy,
  onCreated,
  onCancel,
}: {
  run: <T,>(fn: () => Promise<T>) => Promise<T | null>;
  busy: boolean;
  onCreated: (id: string) => void;
  onCancel: () => void;
}) {
  const [episodeNumber, setEpisodeNumber] = useState<string>("");
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [phase, setPhase] = useState<
    "idle" | "uploading" | "transcribing" | "ideating" | "done" | "error"
  >("idle");
  const [progress, setProgress] = useState<string>("");

  const num = parseInt(episodeNumber, 10);
  const canSubmit =
    Number.isInteger(num) && num >= 1 && file !== null && phase === "idle";

  const start = async () => {
    if (!canSubmit || !file) return;
    try {
      // 1. Get presigned PUT URL
      setPhase("uploading");
      setProgress(`Uploading ${file.name} (${Math.round(file.size / 1024 / 1024)} MB)…`);
      const up = await api.uploadUrl(num, file.name);
      await api.uploadFile(up.url, up.content_type, file);

      // 2. Register episode + kick off Transcribe
      setPhase("transcribing");
      setProgress("Starting AWS Transcribe…");
      const created = await api.createEpisode(num, title.trim(), up.audio_key);

      // 3. Poll until transcription completes
      const epId = created.episode_id;
      while (true) {
        const s = await api.episodeStatus(epId);
        if (s.status === "TRANSCRIBED") break;
        if (s.status === "TRANSCRIBE_FAILED") {
          throw new Error("Transcription failed: " + (s.failure_reason || "unknown"));
        }
        setProgress(`AWS Transcribe: ${s.status.toLowerCase()}…`);
        await new Promise((r) => setTimeout(r, 8000));
      }

      // 4. Kick off ideation (async — runs past the API Gateway 30s limit)
      setPhase("ideating");
      setProgress("Starting Opus 4.6 ideation…");
      const ideateResp = await api.ideate(epId);

      // If the server already had ideas cached it returns status READY here;
      // otherwise it kicks off a background worker and returns IDEATING.
      if (ideateResp.status !== "READY") {
        while (true) {
          const s = await api.episodeStatus(epId);
          if (s.status === "READY") break;
          if (s.status === "IDEATE_FAILED") {
            throw new Error(
              "Ideation failed: " + (s.failure_reason || "unknown"),
            );
          }
          setProgress(`Opus 4.6 ideation: ${s.status.toLowerCase()}…`);
          await new Promise((r) => setTimeout(r, 6000));
        }
      }

      setPhase("done");
      onCreated(epId);
    } catch (e: unknown) {
      setPhase("error");
      setProgress(e instanceof Error ? e.message : String(e));
    }
  };

  const working = phase !== "idle" && phase !== "error" && phase !== "done";

  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>New episode</h2>
      <div style={{ display: "grid", gridTemplateColumns: "140px 1fr", gap: 12, marginBottom: 12 }}>
        <div>
          <label style={{ color: "var(--muted)", fontSize: 13 }}>Episode # *</label>
          <input
            type="number"
            min={1}
            step={1}
            value={episodeNumber}
            onChange={(e) => setEpisodeNumber(e.target.value)}
            placeholder="1"
            disabled={working}
            style={{
              width: "100%",
              background: "#0b0d12",
              color: "var(--text)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 10,
            }}
          />
        </div>
        <div>
          <label style={{ color: "var(--muted)", fontSize: 13 }}>Title (optional)</label>
          <input
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="e.g. Karma Yoga"
            disabled={working}
            style={{
              width: "100%",
              background: "#0b0d12",
              color: "var(--text)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 10,
            }}
          />
        </div>
      </div>
      <label style={{ color: "var(--muted)", fontSize: 13 }}>Podcast audio (MP3/M4A/WAV)</label>
      <input
        type="file"
        accept="audio/*,.mp3,.m4a,.wav,.aac,.ogg,.flac"
        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        disabled={working}
        style={{
          display: "block",
          marginTop: 6,
          marginBottom: 12,
          color: "var(--text)",
        }}
      />
      {file && (
        <p style={{ color: "var(--muted)", margin: "0 0 12px", fontSize: 13 }}>
          Selected: <b>{file.name}</b> · {(file.size / 1024 / 1024).toFixed(1)} MB
        </p>
      )}
      {progress && (
        <div
          style={{
            background: phase === "error" ? "#3a1d1d" : "#0e1118",
            border: "1px solid var(--border)",
            borderRadius: 8,
            padding: 10,
            marginBottom: 12,
            fontSize: 13,
            color: phase === "error" ? "#ff9999" : "var(--muted)",
          }}
        >
          {progress}
        </div>
      )}
      <div style={{ display: "flex", gap: 10, marginTop: 12 }}>
        <button className="btn btn-primary" disabled={!canSubmit} onClick={start}>
          {working ? "Working…" : "Upload & generate ideas"}
        </button>
        <button className="btn btn-secondary" onClick={onCancel} disabled={working}>
          Cancel
        </button>
      </div>
    </div>
  );
}

// ---------- Episode view ----------

function EpisodeView({
  episodeId,
  run,
  busy,
  onBack,
  onOpenIdea,
}: {
  episodeId: string;
  run: <T,>(fn: () => Promise<T>) => Promise<T | null>;
  busy: boolean;
  onBack: () => void;
  onOpenIdea: (rank: number) => void;
}) {
  const [ep, setEp] = useState<EpisodeDetail | null>(null);
  const reload = async () => {
    const r = await run(() => api.getEpisode(episodeId));
    if (r) setEp(r);
  };
  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId]);

  if (!ep) return <div className="card">Loading…</div>;

  return (
    <div className="card">
      <button onClick={onBack} style={linkBtn}>
        ← All episodes
      </button>
      <h2 style={{ margin: "8px 0 4px" }}>{ep.name}</h2>
      <div style={{ color: "var(--muted)", fontSize: 13, marginBottom: 20 }}>
        Created {new Date(ep.created_at).toLocaleString()} · {ep.ideas.length} ideas
      </div>

      <div style={{ display: "grid", gap: 12 }}>
        {ep.ideas.map((i) => (
          <IdeaRow key={i.rank} idea={i} onOpen={() => onOpenIdea(i.rank)} />
        ))}
      </div>
    </div>
  );
}

function IdeaRow({ idea, onOpen }: { idea: EpisodeIdea; onOpen: () => void }) {
  const statusChip = (() => {
    if (idea.render_status === "READY")
      return <Chip color="#2d7a2d">video ready</Chip>;
    if (idea.render_status === "RENDERING")
      return <Chip color="#7a5a2d">rendering…</Chip>;
    if (idea.render_status === "RENDER_FAILED")
      return <Chip color="#c0392b">render failed</Chip>;
    if (idea.script_status === "GENERATING")
      return <Chip color="#2980b9">writing…</Chip>;
    if (idea.script_status === "SCRIPT_FAILED")
      return <Chip color="#c0392b">script failed</Chip>;
    if (idea.has_script) return <Chip color="#2d5a7a">script ready</Chip>;
    return <Chip color="#444">not started</Chip>;
  })();

  return (
    <div
      style={{
        border: "1px solid var(--border)",
        borderRadius: 8,
        padding: 14,
        background: "#0e1118",
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        <span style={{ color: "var(--accent)", fontWeight: 800 }}>#{idea.rank}</span>
        <h3 style={{ margin: 0, flex: 1 }}>{idea.title}</h3>
        {statusChip}
      </div>
      <p style={{ margin: "6px 0 4px" }}>
        <b>Hook:</b> “{idea.hook}”
      </p>
      <p style={{ margin: "4px 0" }}>{idea.summary}</p>
      <p style={{ color: "var(--muted)", margin: "4px 0 8px", fontSize: 13 }}>
        {idea.verse_ref} · {idea.target_length_sec}s · {idea.why_it_works}
      </p>
      {idea.window_start > 0 && idea.window_end > 0 && (
        <div style={{ margin: "8px 0 12px" }}>
          <div style={{ color: "var(--muted)", fontSize: 12, marginBottom: 4 }}>
            Continuous audio window:
          </div>
          <div
            style={{
              border: "1px solid var(--border)",
              borderRadius: 6,
              padding: "8px 12px",
              background: "#161b26",
              fontSize: 13,
            }}
          >
            <div style={{ color: "var(--accent)", fontSize: 12, marginBottom: 4 }}>
              {idea.window_start.toFixed(1)}s – {idea.window_end.toFixed(1)}s
              ({(idea.window_end - idea.window_start).toFixed(0)}s clip)
            </div>
            <div style={{ color: "var(--text)", lineHeight: 1.5 }}>
              "{idea.window_text.length > 250
                ? idea.window_text.slice(0, 250) + "…"
                : idea.window_text}"
            </div>
          </div>
        </div>
      )}
      {!idea.window_start && idea.quotes?.length > 0 && (
        <div style={{ margin: "8px 0 12px" }}>
          <div style={{ color: "var(--muted)", fontSize: 12, marginBottom: 4 }}>
            Verbatim quotes (legacy):
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {idea.quotes.map((q, i) => (
              <QuoteChip key={i} quote={q} />
            ))}
          </div>
        </div>
      )}
      <button className="btn btn-primary" onClick={onOpen}>
        Work on this idea
      </button>
    </div>
  );
}

function QuoteChip({ quote }: { quote: Quote }) {
  const dur = (quote.end_sec - quote.start_sec).toFixed(1);
  const truncated =
    quote.text.length > 140 ? quote.text.slice(0, 140) + "…" : quote.text;
  return (
    <div
      style={{
        border: "1px solid var(--border)",
        borderRadius: 6,
        padding: "6px 10px",
        background: "#161b26",
        fontSize: 13,
      }}
    >
      <span style={{ color: "var(--accent)", fontSize: 11, marginRight: 8 }}>
        {quote.start_sec.toFixed(1)}–{quote.end_sec.toFixed(1)}s · {dur}s
      </span>
      <span style={{ color: "var(--text)" }}>“{truncated}”</span>
    </div>
  );
}

function Chip({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span
      style={{
        background: color,
        color: "white",
        fontSize: 11,
        padding: "3px 8px",
        borderRadius: 999,
        textTransform: "uppercase",
        letterSpacing: 0.5,
        fontWeight: 700,
      }}
    >
      {children}
    </span>
  );
}

// ---------- Idea view (script + render + publish) ----------

function IdeaView({
  episodeId,
  rank,
  run,
  busy,
  onBack,
}: {
  episodeId: string;
  rank: number;
  run: <T,>(fn: () => Promise<T>) => Promise<T | null>;
  busy: boolean;
  onBack: () => void;
}) {
  const [script, setScript] = useState<ScriptResponse | null>(null);
  const [scriptVersion, setScriptVersion] = useState<string | null>(null);
  const [scriptError, setScriptError] = useState<string | null>(null);
  const [reviseInput, setReviseInput] = useState("");
  const [renderStatus, setRenderStatus] = useState<string | null>(null);
  const [mp4Url, setMp4Url] = useState<string | null>(null);

  // Load existing script on mount. If a script is GENERATING (e.g. user
  // navigated away and came back), resume polling automatically.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const ep = await run(() => api.getEpisode(episodeId));
      if (!ep || cancelled) return;
      const idea = ep.ideas.find((i) => i.rank === rank);
      if (!idea) return;
      setRenderStatus(idea.render_status);
      setScriptVersion(idea.script_version);

      // Check script state: READY, GENERATING, SCRIPT_FAILED, or NONE.
      const scriptStat = idea.script_status;
      if (scriptStat === "GENERATING") {
        await run(async () => {
          try {
            await pollScriptUntilReady();
          } catch (e: unknown) {
            setScriptError(e instanceof Error ? e.message : String(e));
          }
          return true;
        });
      } else if (scriptStat === "SCRIPT_FAILED") {
        // Fetch the failure reason from script-status endpoint.
        const ss = await api.scriptStatus(episodeId, rank).catch(() => null);
        setScriptError(ss?.failure_reason ?? "Unknown error");
      } else if (idea.has_script) {
        const s = await run(() => api.getScript(episodeId, rank));
        if (s && !cancelled && isReadyScript(s)) setScript(s);
      }

      if (idea.render_status === "READY" && idea.render_mp4_key) {
        const u = await run(() => api.assetUrl(idea.render_mp4_key!));
        if (u && !cancelled) setMp4Url(u.url);
      } else if (idea.render_status === "RENDERING") {
        pollRender();
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId, rank]);

  const audioForBeat = (i: number): SceneAudio | undefined =>
    script?.scene_audio?.find((a) => a.index === i);

  const [renderError, setRenderError] = useState<string | null>(null);

  const pollRender = async () => {
    const tick = async () => {
      const s = await api.renderStatus(episodeId, rank).catch(() => null);
      if (!s) return;
      setRenderStatus(s.status);
      if (s.status === "READY" && s.mp4_key) {
        const u = await api.assetUrl(s.mp4_key);
        setMp4Url(u.url);
        setRenderError(null);
        return;
      }
      if (s.status === "RENDER_FAILED") {
        setRenderError(s.failure_reason ?? "Unknown error");
        return;
      }
      if (s.status === "RENDERING") setTimeout(tick, 5000);
    };
    tick();
  };

  const isReadyScript = (d: any): d is ScriptResponse =>
    d && (Array.isArray(d.beats) && d.beats.length > 0);

  const pollScriptUntilReady = async () => {
    while (true) {
      const s = await api.scriptStatus(episodeId, rank);
      if (s.status === "READY") break;
      if (s.status === "SCRIPT_FAILED") {
        throw new Error("Script generation failed: " + (s.failure_reason ?? "unknown"));
      }
      await new Promise((r) => setTimeout(r, 6000));
    }
    const fresh = await api.getScript(episodeId, rank);
    if (isReadyScript(fresh)) {
      setScript(fresh);
    }
  };

  const gen = async () => {
    setScriptError(null);
    const ok = await run(async () => {
      await api.generateScript(episodeId, rank);
      try {
        await pollScriptUntilReady();
      } catch (e: unknown) {
        setScriptError(e instanceof Error ? e.message : String(e));
        throw e;
      }
      return true;
    });
    if (!ok) setScript(null);
  };

  const doRevise = async () => {
    if (!reviseInput.trim()) return;
    setScriptError(null);
    const ok = await run(async () => {
      await api.revise(episodeId, rank, reviseInput);
      try {
        await pollScriptUntilReady();
      } catch (e: unknown) {
        setScriptError(e instanceof Error ? e.message : String(e));
        throw e;
      }
      return true;
    });
    if (ok) setReviseInput("");
  };

  const doRender = async () => {
    setRenderError(null);
    setMp4Url(null);
    const r = await run(() => api.render(episodeId, rank));
    if (r) {
      setRenderStatus(r.status);
      pollRender();
    }
  };

  return (
    <div className="card">
      <button onClick={onBack} style={linkBtn}>
        ← Back to episode
      </button>
      <h2 style={{ margin: "8px 0 4px" }}>Idea #{rank}</h2>

      {!script && scriptError && (
        <div
          style={{
            marginTop: 12,
            padding: 14,
            border: "1px solid rgba(255,71,87,0.3)",
            background: "rgba(255,71,87,0.08)",
            borderRadius: 8,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <span className="chip" style={{ background: "var(--danger)" }}>FAILED</span>
            <b style={{ color: "#ff8591" }}>Script generation failed</b>
          </div>
          <div
            style={{
              color: "var(--muted)",
              fontSize: 13,
              marginBottom: 12,
              maxHeight: 120,
              overflow: "auto",
              fontFamily: "ui-monospace, monospace",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
            }}
          >
            {scriptError}
          </div>
          <button className="btn btn-primary" onClick={gen} disabled={busy}>
            {busy ? "Retrying…" : "Retry"}
          </button>
        </div>
      )}

      {!script && !scriptError && (
        <div style={{ marginTop: 12 }}>
          <p style={{ color: "var(--muted)" }}>
            No script yet for this idea.
          </p>
          <button className="btn btn-primary" onClick={gen} disabled={busy}>
            {busy ? "Writing…" : "Generate script"}
          </button>
        </div>
      )}

      {script && (
        <>
          <h3 style={{ margin: "16px 0 2px" }}>{script.title}</h3>
          <p style={{ color: "var(--muted)", margin: "0 0 12px" }}>
            {script.duration_sec}s · {script.aspect}
            {scriptVersion && ` · v${scriptVersion}`}
          </p>
          <div style={{ display: "grid", gap: 12 }}>
            {(script.beats || []).map((beat, bi) => {
              const aud = audioForBeat(bi);
              return (
                <div
                  key={bi}
                  style={{
                    border: "1px solid var(--border)",
                    borderRadius: 8,
                    padding: 12,
                    background: "#0e1118",
                  }}
                >
                  <div style={{ color: "var(--muted)", fontSize: 12, display: "flex", gap: 8, marginBottom: 6 }}>
                    <span className={`chip chip-${beat.purpose || "build"}`}>{beat.purpose}</span>
                    <span>beat {bi + 1} · {beat.start.toFixed(0)}–{beat.end.toFixed(0)}s</span>
                    {typeof beat.source_start === "number" && typeof beat.source_end === "number" && (
                      <span style={{ color: "var(--accent)" }}>
                        src {beat.source_start.toFixed(1)}–{beat.source_end.toFixed(1)}s
                      </span>
                    )}
                  </div>
                  <div style={{ margin: "4px 0" }}>
                    <b>VO:</b> {beat.voiceover.length > 150 ? beat.voiceover.slice(0, 150) + "…" : beat.voiceover}
                  </div>
                  <div style={{ color: "var(--muted)", marginBottom: 6 }}>
                    <b>Text:</b> {beat.on_screen_text}
                  </div>
                  {aud?.audio_url && (
                    <audio controls preload="none" src={aud.audio_url} style={{ width: "100%", height: 32, marginBottom: 8 }} />
                  )}
                  {beat.shots?.length > 0 && (
                    <div style={{ display: "grid", gap: 6, marginTop: 6 }}>
                      <div style={{ color: "var(--muted)", fontSize: 11 }}>{beat.shots.length} shot(s):</div>
                      {beat.shots.map((shot, si) => (
                        <div key={si} style={{ borderLeft: "2px solid var(--border)", paddingLeft: 10, fontSize: 13 }}>
                          <span style={{ color: "var(--accent)", fontSize: 11 }}>
                            #{shot.shot_number} · {shot.shot_duration_sec}s · {shot.framing} · {shot.visual_mode}
                          </span>
                          <div style={{ color: "var(--text)", marginTop: 2 }}>
                            {shot.visual.length > 120 ? shot.visual.slice(0, 120) + "…" : shot.visual}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              );
            })}
          </div>

          <div style={{ marginTop: 16 }}>
            <textarea
              placeholder='Revision ("make scene 2 punchier"…)'
              value={reviseInput}
              onChange={(e) => setReviseInput(e.target.value)}
              rows={3}
              style={{
                width: "100%",
                background: "#0b0d12",
                color: "var(--text)",
                border: "1px solid var(--border)",
                borderRadius: 8,
                padding: 10,
              }}
            />
            <div style={{ display: "flex", gap: 10, marginTop: 10, flexWrap: "wrap" }}>
              <button
                className="btn btn-secondary"
                disabled={busy || !reviseInput.trim()}
                onClick={doRevise}
              >
                {busy ? "Revising…" : "Apply revision"}
              </button>
              <button className="btn btn-secondary" disabled={busy} onClick={gen}>
                Regenerate from scratch
              </button>
              <button
                className="btn btn-primary"
                disabled={busy || renderStatus === "RENDERING"}
                onClick={doRender}
              >
                {renderStatus === "RENDERING" ? "Rendering…" : "Render video"}
              </button>
            </div>
          </div>
        </>
      )}

      {renderStatus === "RENDERING" && (
        <p style={{ marginTop: 16, color: "var(--muted)" }}>
          Rendering in progress — audio slice → b-roll → Remotion → packaging (~5–7 min).
        </p>
      )}

      {renderStatus === "RENDER_FAILED" && (
        <div
          style={{
            marginTop: 16,
            padding: 14,
            border: "1px solid rgba(255,71,87,0.3)",
            background: "rgba(255,71,87,0.08)",
            borderRadius: 8,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
            <span className="chip" style={{ background: "var(--danger)" }}>FAILED</span>
            <b style={{ color: "#ff8591" }}>Render failed</b>
          </div>
          {renderError && (
            <div
              style={{
                color: "var(--muted)",
                fontSize: 13,
                marginBottom: 12,
                maxHeight: 120,
                overflow: "auto",
                fontFamily: "ui-monospace, monospace",
                whiteSpace: "pre-wrap",
                wordBreak: "break-word",
              }}
            >
              {renderError}
            </div>
          )}
          <button className="btn btn-primary" onClick={doRender} disabled={busy}>
            {busy ? "…" : "Retry render"}
          </button>
        </div>
      )}

      {renderStatus === "READY" && mp4Url && script && (
        <Publish script={script} mp4Url={mp4Url} />
      )}
    </div>
  );
}

function Publish({ script, mp4Url }: { script: ScriptResponse; mp4Url: string }) {
  const [caption, setCaption] = useState(script.caption);
  const [tags, setTags] = useState(script.hashtags.join(" "));
  const copy = (t: string) => navigator.clipboard.writeText(t);

  return (
    <div style={{ marginTop: 20, borderTop: "1px solid var(--border)", paddingTop: 20 }}>
      <h3 style={{ marginTop: 0 }}>Publish</h3>
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "280px 1fr",
          gap: 20,
          alignItems: "start",
        }}
      >
        <video
          src={mp4Url}
          controls
          style={{
            width: 280,
            aspectRatio: "9 / 16",
            background: "black",
            borderRadius: 8,
          }}
        />
        <div>
          <label style={{ color: "var(--muted)", fontSize: 13 }}>Caption</label>
          <textarea
            value={caption}
            onChange={(e) => setCaption(e.target.value)}
            rows={4}
            style={{
              width: "100%",
              background: "#0b0d12",
              color: "var(--text)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 10,
              marginBottom: 10,
            }}
          />
          <label style={{ color: "var(--muted)", fontSize: 13 }}>Hashtags</label>
          <textarea
            value={tags}
            onChange={(e) => setTags(e.target.value)}
            rows={2}
            style={{
              width: "100%",
              background: "#0b0d12",
              color: "var(--text)",
              border: "1px solid var(--border)",
              borderRadius: 8,
              padding: 10,
              marginBottom: 10,
            }}
          />
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            <a href={mp4Url} download="reel.mp4" className="btn btn-primary">
              Download MP4
            </a>
            <button className="btn btn-secondary" onClick={() => copy(caption)}>
              Copy caption
            </button>
            <button className="btn btn-secondary" onClick={() => copy(tags)}>
              Copy hashtags
            </button>
            <a
              className="btn btn-secondary"
              href="https://studio.youtube.com/"
              target="_blank"
              rel="noreferrer"
            >
              YouTube Studio
            </a>
            <a
              className="btn btn-secondary"
              href="https://www.instagram.com/"
              target="_blank"
              rel="noreferrer"
            >
              Instagram
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
