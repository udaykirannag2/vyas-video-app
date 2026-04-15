"use client";
import { useEffect, useState } from "react";
import {
  api,
  EpisodeSummary,
  EpisodeDetail,
  EpisodeIdea,
  SceneAudio,
  ScriptResponse,
  Quote,
} from "../lib/api";

type View =
  | { kind: "list" }
  | { kind: "new" }
  | { kind: "episode"; episodeId: string }
  | { kind: "idea"; episodeId: string; rank: number };

export default function Home() {
  const [view, setView] = useState<View>({ kind: "list" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

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

  return (
    <main style={{ maxWidth: 960, margin: "0 auto", padding: 32 }}>
      <header style={{ marginBottom: 24, display: "flex", alignItems: "baseline", gap: 16 }}>
        <h1
          style={{ margin: 0, fontSize: 28, cursor: "pointer" }}
          onClick={() => setView({ kind: "list" })}
        >
          Vyas-Video
        </h1>
        <span style={{ color: "var(--muted)" }}>
          Bhagavad Gita podcast → reels for 15–35.
        </span>
      </header>

      {err && <div style={errStyle}>Error: {err}</div>}

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
          onCreated={(id) => setView({ kind: "episode", episodeId: id })}
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
    </main>
  );
}

// ---------- Shared styles ----------

const panelStyle: React.CSSProperties = {
  background: "var(--panel)",
  border: "1px solid var(--border)",
  borderRadius: 12,
  padding: 20,
};
const errStyle: React.CSSProperties = {
  background: "#3a1d1d",
  border: "1px solid #6b2b2b",
  color: "#ff9999",
  padding: 12,
  borderRadius: 8,
  marginBottom: 16,
};
const btn: React.CSSProperties = {
  background: "var(--accent)",
  color: "#1a1a1a",
  border: 0,
  padding: "10px 18px",
  borderRadius: 8,
  fontWeight: 700,
  textDecoration: "none",
  display: "inline-block",
};
const btnSecondary: React.CSSProperties = {
  background: "transparent",
  color: "var(--text)",
  border: "1px solid var(--border)",
  padding: "10px 18px",
  borderRadius: 8,
  textDecoration: "none",
  display: "inline-block",
};
const linkBtn: React.CSSProperties = {
  background: "transparent",
  color: "var(--accent)",
  border: 0,
  padding: 0,
  cursor: "pointer",
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
    <div style={panelStyle}>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <h2 style={{ margin: 0 }}>Episodes</h2>
        <button style={btn} onClick={onNew} disabled={busy}>
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
    <div style={panelStyle}>
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
        <button style={btn} disabled={!canSubmit} onClick={start}>
          {working ? "Working…" : "Upload & generate ideas"}
        </button>
        <button style={btnSecondary} onClick={onCancel} disabled={working}>
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

  if (!ep) return <div style={panelStyle}>Loading…</div>;

  return (
    <div style={panelStyle}>
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
      {idea.quotes?.length > 0 && (
        <div style={{ margin: "8px 0 12px" }}>
          <div style={{ color: "var(--muted)", fontSize: 12, marginBottom: 4 }}>
            Verbatim quotes this reel will use:
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            {idea.quotes.map((q, i) => (
              <QuoteChip key={i} quote={q} />
            ))}
          </div>
        </div>
      )}
      <button style={btn} onClick={onOpen}>
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
  const [reviseInput, setReviseInput] = useState("");
  const [renderStatus, setRenderStatus] = useState<string | null>(null);
  const [mp4Url, setMp4Url] = useState<string | null>(null);

  // Load existing script (if any) on mount
  useEffect(() => {
    (async () => {
      const ep = await run(() => api.getEpisode(episodeId));
      if (!ep) return;
      const idea = ep.ideas.find((i) => i.rank === rank);
      if (!idea) return;
      setRenderStatus(idea.render_status);
      setScriptVersion(idea.script_version);
      if (idea.has_script) {
        const s = await run(() => api.getScript(episodeId, rank));
        if (s && (s as ScriptResponse).scenes) setScript(s as ScriptResponse);
      }
      if (idea.render_status === "READY" && idea.render_mp4_key) {
        const u = await run(() => api.assetUrl(idea.render_mp4_key!));
        if (u) setMp4Url(u.url);
      } else if (idea.render_status === "RENDERING") {
        pollRender();
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [episodeId, rank]);

  const audioForScene = (i: number): SceneAudio | undefined =>
    script?.scene_audio?.find((a) => a.index === i);

  const pollRender = async () => {
    const tick = async () => {
      const s = await api.renderStatus(episodeId, rank).catch(() => null);
      if (!s) return;
      setRenderStatus(s.status);
      if (s.status === "READY" && s.mp4_key) {
        const u = await api.assetUrl(s.mp4_key);
        setMp4Url(u.url);
        return;
      }
      if (s.status === "RENDERING") setTimeout(tick, 5000);
    };
    tick();
  };

  const pollScriptUntilReady = async () => {
    while (true) {
      const s = await api.scriptStatus(episodeId, rank);
      if (s.status === "READY") break;
      if (s.status === "SCRIPT_FAILED") {
        throw new Error("Script generation failed: " + (s.failure_reason ?? "unknown"));
      }
      await new Promise((r) => setTimeout(r, 5000));
    }
    const fresh = await api.getScript(episodeId, rank);
    if ((fresh as ScriptResponse).scenes) {
      setScript(fresh as ScriptResponse);
    }
  };

  const gen = async () => {
    const ok = await run(async () => {
      await api.generateScript(episodeId, rank);
      await pollScriptUntilReady();
      return true;
    });
    if (!ok) setScript(null);
  };

  const doRevise = async () => {
    if (!reviseInput.trim()) return;
    const ok = await run(async () => {
      await api.revise(episodeId, rank, reviseInput);
      await pollScriptUntilReady();
      return true;
    });
    if (ok) setReviseInput("");
  };

  const doRender = async () => {
    const r = await run(() => api.render(episodeId, rank));
    if (r) {
      setRenderStatus(r.status);
      pollRender();
    }
  };

  return (
    <div style={panelStyle}>
      <button onClick={onBack} style={linkBtn}>
        ← Back to episode
      </button>
      <h2 style={{ margin: "8px 0 4px" }}>Idea #{rank}</h2>

      {!script && (
        <div style={{ marginTop: 12 }}>
          <p style={{ color: "var(--muted)" }}>
            No script yet for this idea.
          </p>
          <button style={btn} onClick={gen} disabled={busy}>
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
          <div style={{ display: "grid", gap: 10 }}>
            {script.scenes.map((s, idx) => {
              const aud = audioForScene(idx);
              const isBug = aud?.source === "polly-bug";
              return (
                <div
                  key={idx}
                  style={{
                    border: "1px solid var(--border)",
                    borderRadius: 6,
                    padding: 10,
                    background: "#0e1118",
                  }}
                >
                  <div
                    style={{
                      color: "var(--muted)",
                      fontSize: 12,
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                    }}
                  >
                    <span>scene {idx + 1} · {s.start}–{s.end}s</span>
                    {s.source_start != null && s.source_end != null && (
                      <span style={{ color: "var(--accent)" }}>
                        source {s.source_start.toFixed(1)}–{s.source_end.toFixed(1)}s
                      </span>
                    )}
                    {isBug && (
                      <span style={{ color: "#ff9999" }}>⚠ synthesized (no timestamps)</span>
                    )}
                  </div>
                  <div style={{ margin: "6px 0" }}>
                    <b>VO:</b> {s.voiceover}
                  </div>
                  <div style={{ color: "var(--muted)", marginBottom: 8 }}>
                    <b>Text:</b> {s.on_screen_text} · <b>Visual:</b> {s.visual}
                  </div>
                  {aud?.audio_url && (
                    <audio
                      controls
                      preload="none"
                      src={aud.audio_url}
                      style={{ width: "100%", height: 32 }}
                    />
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
                style={btnSecondary}
                disabled={busy || !reviseInput.trim()}
                onClick={doRevise}
              >
                {busy ? "Revising…" : "Apply revision"}
              </button>
              <button style={btnSecondary} disabled={busy} onClick={gen}>
                Regenerate from scratch
              </button>
              <button
                style={btn}
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
          Rendering in progress — TTS → b-roll → Remotion → packaging (~1–3 min).
        </p>
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
            <a href={mp4Url} download="reel.mp4" style={btn}>
              Download MP4
            </a>
            <button style={btnSecondary} onClick={() => copy(caption)}>
              Copy caption
            </button>
            <button style={btnSecondary} onClick={() => copy(tags)}>
              Copy hashtags
            </button>
            <a
              style={btnSecondary}
              href="https://studio.youtube.com/"
              target="_blank"
              rel="noreferrer"
            >
              YouTube Studio
            </a>
            <a
              style={btnSecondary}
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
