"use client";

import { useState, useRef, useCallback, useEffect } from "react";
import Link from "next/link";
import { api, BeatEdit, ScreenplayData } from "@/lib/api";
import { AuthGate } from "@/app/auth-gate";

type ShortStatus =
  | "idle"
  | "uploading"
  | "TRANSCRIBING"
  | "TRANSCRIBED"
  | "GENERATING"
  | "RENDERING"
  | "READY"
  | "TRANSCRIBE_FAILED"
  | "GENERATE_FAILED"
  | "RENDER_FAILED";

type ActiveTab = "publish" | "quality" | "scenes";

interface Evaluation {
  overall_score: number;
  visual_quality_score: number;
  alignment_score: number;
  house_style_score: number;
  strengths: string[];
  improvements: string[];
  verdict: "STRONG" | "GOOD" | "NEEDS_WORK";
}

interface ShortState {
  shortId: string;
  title: string;
  status: ShortStatus;
  mp4Key?: string;
  videoUrl?: string;
  failureReason?: string;
  caption?: string;
  hashtags?: string[];
  evaluation?: Evaluation;
}

const STATUS_LABEL: Record<ShortStatus, string> = {
  idle: "Ready",
  uploading: "Uploading…",
  TRANSCRIBING: "Transcribing audio…",
  TRANSCRIBED: "Transcribed — starting generation…",
  GENERATING: "Writing screenplay & visuals…",
  RENDERING: "Rendering video…",
  READY: "Ready",
  TRANSCRIBE_FAILED: "Transcription failed",
  GENERATE_FAILED: "Generation failed",
  RENDER_FAILED: "Render failed",
};

const FAILED_STATUSES: ShortStatus[] = [
  "TRANSCRIBE_FAILED",
  "GENERATE_FAILED",
  "RENDER_FAILED",
];

const PIPELINE_STEPS = [
  { statuses: ["uploading"], label: "Upload" },
  { statuses: ["TRANSCRIBING", "TRANSCRIBED"], label: "Transcribe" },
  { statuses: ["GENERATING"], label: "Generate" },
  { statuses: ["RENDERING"], label: "Render" },
];

interface HistoryItem {
  short_id: string;
  title: string;
  status: string;
  created_at: string;
}

export default function ShortsPage() {
  return (
    <AuthGate onUser={() => {}}>
      <ShortsApp />
    </AuthGate>
  );
}

function ShortsApp() {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [short, setShort] = useState<ShortState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const [activeTab, setActiveTab] = useState<ActiveTab>("publish");

  // Caption editing
  const [editingCaption, setEditingCaption] = useState(false);
  const [captionDraft, setCaptionDraft] = useState("");
  const [savingCaption, setSavingCaption] = useState(false);

  // History
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingShortId, setLoadingShortId] = useState<string | null>(null);

  // Scene editor
  const [screenplay, setScreenplay] = useState<ScreenplayData | null>(null);
  const [editedBeats, setEditedBeats] = useState<BeatEdit[] | null>(null);
  const [loadingScreenplay, setLoadingScreenplay] = useState(false);
  const [screenplayError, setScreenplayError] = useState<string | null>(null);
  const [savingBeats, setSavingBeats] = useState(false);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  // Auto-load screenplay when Scenes tab is opened
  useEffect(() => {
    if (
      activeTab === "scenes" &&
      short?.status === "READY" &&
      short.shortId &&
      !screenplay &&
      !loadingScreenplay &&
      !screenplayError
    ) {
      setLoadingScreenplay(true);
      setScreenplayError(null);
      api
        .getShortScreenplay(short.shortId)
        .then(({ screenplay: sp }) => {
          setScreenplay(sp);
          setEditedBeats(JSON.parse(JSON.stringify(sp.beats)));
        })
        .catch((e) => {
          const msg = e instanceof Error ? e.message : String(e);
          setScreenplayError(msg);
        })
        .finally(() => setLoadingScreenplay(false));
    }
  }, [activeTab, short?.shortId, short?.status, screenplay, loadingScreenplay, screenplayError]);

  const refreshHistory = useCallback(async () => {
    try {
      const { shorts } = await api.listShorts();
      setHistory(shorts);
    } catch {
      /* non-critical */
    }
  }, []);

  useEffect(() => {
    setLoadingHistory(true);
    refreshHistory().finally(() => setLoadingHistory(false));
  }, [refreshHistory]);

  const loadFromHistory = useCallback(async (item: HistoryItem) => {
    setLoadingShortId(item.short_id);
    setError(null);
    setScreenplay(null);
    setEditedBeats(null);
    try {
      const s = await api.shortStatus(item.short_id);
      let videoUrl: string | undefined;
      if (s.status === "READY" && s.mp4_key) {
        const { url } = await api.assetUrl(s.mp4_key);
        videoUrl = url;
      }
      setShort({
        shortId: s.short_id,
        title: s.title,
        status: s.status as ShortStatus,
        mp4Key: s.mp4_key,
        videoUrl,
        caption: s.caption ?? undefined,
        hashtags: s.hashtags ?? undefined,
        evaluation: s.evaluation ?? undefined,
      });
      setActiveTab("publish");
    } catch (e) {
      setError(
        `Could not load short: ${e instanceof Error ? e.message : String(e)}`
      );
    } finally {
      setLoadingShortId(null);
    }
  }, []);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const startPolling = useCallback(
    (shortId: string) => {
      stopPolling();
      pollRef.current = setInterval(async () => {
        try {
          const s = await api.shortStatus(shortId);
          setShort((prev) =>
            prev
              ? {
                  ...prev,
                  status: s.status as ShortStatus,
                  mp4Key: s.mp4_key,
                  failureReason: s.failure_reason,
                }
              : prev
          );

          if (s.status === "READY" && s.mp4_key) {
            stopPolling();
            const { url } = await api.assetUrl(s.mp4_key);
            setShort((prev) =>
              prev
                ? {
                    ...prev,
                    videoUrl: url,
                    caption: s.caption,
                    hashtags: s.hashtags,
                    evaluation: s.evaluation,
                  }
                : prev
            );
            setScreenplay(null);
            setEditedBeats(null);
            setActiveTab("publish");
            refreshHistory();
          }

          if (FAILED_STATUSES.includes(s.status as ShortStatus)) {
            stopPolling();
          }

          if (s.status === "TRANSCRIBED") {
            stopPolling();
            try {
              await api.generateShort(shortId);
              setShort((prev) =>
                prev ? { ...prev, status: "GENERATING" } : prev
              );
              startPolling(shortId);
            } catch (e) {
              setError(
                `Generate failed: ${e instanceof Error ? e.message : String(e)}`
              );
            }
          }
        } catch (e) {
          console.error("poll error", e);
        }
      }, 7000);
    },
    [refreshHistory]
  );

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setIsDragging(false);
    const dropped = e.dataTransfer.files[0];
    if (dropped && dropped.type.startsWith("video/")) {
      setFile(dropped);
      setError(null);
    } else {
      setError("Please drop a video file (.mp4, .mov, etc.)");
    }
  }, []);

  const handleUpload = async () => {
    if (!file) return;
    setError(null);
    try {
      setShort({ shortId: "", title: title || file.name, status: "uploading" });
      const { url, video_key, short_id, content_type } =
        await api.shortUploadUrl(file.name);
      await api.uploadFile(url, content_type, file);
      await api.createShort(title || file.name, video_key);
      setShort({
        shortId: short_id,
        title: title || file.name,
        status: "TRANSCRIBING",
      });
      startPolling(short_id);
    } catch (e) {
      setError(
        `Upload failed: ${e instanceof Error ? e.message : String(e)}`
      );
      setShort(null);
    }
  };

  const handleRetry = async () => {
    if (!short?.shortId) return;
    setError(null);
    try {
      await api.generateShort(short.shortId);
      setShort((prev) =>
        prev ? { ...prev, status: "GENERATING", failureReason: undefined } : prev
      );
      startPolling(short.shortId);
    } catch (e) {
      setError(`Retry failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const saveAndRerender = useCallback(
    async (shortId: string) => {
      if (!editedBeats) return;
      setSavingBeats(true);
      try {
        await api.updateShortBeats(shortId, editedBeats);
        await api.rerenderShort(shortId);
        setShort((prev) =>
          prev
            ? { ...prev, status: "RENDERING", videoUrl: undefined, failureReason: undefined }
            : prev
        );
        setScreenplay(null);
        setEditedBeats(null);
        setScreenplayError(null);
        setActiveTab("publish");
        startPolling(shortId);
      } catch (e) {
        setError(
          `Re-render failed: ${e instanceof Error ? e.message : String(e)}`
        );
      } finally {
        setSavingBeats(false);
      }
    },
    [editedBeats, startPolling]
  );

  const resetForm = () => {
    stopPolling();
    setFile(null);
    setTitle("");
    setShort(null);
    setError(null);
    setScreenplay(null);
    setEditedBeats(null);
    setScreenplayError(null);
    setActiveTab("publish");
    setEditingCaption(false);
  };

  const isProcessing =
    short && !["idle", "READY", ...FAILED_STATUSES].includes(short.status);

  const currentStep = PIPELINE_STEPS.findIndex((s) =>
    s.statuses.includes(short?.status ?? "")
  );

  const fmtTime = (s: number) =>
    `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

  const purposeColors: Record<string, string> = {
    hook: "text-amber-400 bg-amber-500/10 border-amber-500/25",
    setup: "text-sky-400 bg-sky-500/10 border-sky-500/25",
    build: "text-violet-400 bg-violet-500/10 border-violet-500/25",
    twist: "text-orange-400 bg-orange-500/10 border-orange-500/25",
    payoff: "text-emerald-400 bg-emerald-500/10 border-emerald-500/25",
  };

  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100">
      {/* ── Header ── */}
      <header className="border-b border-neutral-800/60 px-6 py-3">
        <div className="max-w-3xl mx-auto flex items-center gap-1">
          {/* Brand */}
          <span className="font-semibold text-sm text-neutral-100 mr-3">
            Vyas Video
          </span>

          {/* Nav links */}
          <Link
            href="/"
            className="px-3 py-1.5 rounded-md text-xs font-medium text-neutral-400 hover:text-neutral-100 hover:bg-neutral-800 transition-colors"
          >
            Home
          </Link>

          <button
            onClick={short ? resetForm : undefined}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${
              !short
                ? "text-neutral-100 bg-neutral-800"
                : "text-neutral-400 hover:text-neutral-100 hover:bg-neutral-800"
            }`}
          >
            Shorts
          </button>

          {/* Breadcrumb when a short is active */}
          {short && (
            <>
              <span className="text-neutral-700 text-xs mx-0.5">/</span>
              <span className="px-3 py-1.5 rounded-md text-xs font-medium text-neutral-100 bg-neutral-800 truncate max-w-[180px]">
                {short.title}
              </span>
            </>
          )}
        </div>
      </header>

      <div className="max-w-3xl mx-auto px-6 py-8 space-y-10">
        {/* ── UPLOAD STATE ── */}
        {!short && (
          <div className="space-y-5 max-w-lg mx-auto">
            <div>
              <h1 className="text-2xl font-bold tracking-tight">
                Create a Short
              </h1>
              <p className="text-neutral-500 mt-1.5 text-sm">
                Your voice stays. Visuals are regenerated by AI.
              </p>
            </div>

            <div
              onDragOver={(e) => {
                e.preventDefault();
                setIsDragging(true);
              }}
              onDragLeave={() => setIsDragging(false)}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`relative border-2 border-dashed rounded-2xl p-12 text-center cursor-pointer transition-all ${
                isDragging
                  ? "border-amber-400 bg-amber-400/5 scale-[1.01]"
                  : file
                  ? "border-green-500/50 bg-green-500/5"
                  : "border-neutral-800 hover:border-neutral-700 hover:bg-neutral-900/40"
              }`}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept="video/mp4,video/quicktime,video/x-m4v,video/webm,.mp4,.mov,.m4v,.webm"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) {
                    setFile(f);
                    setError(null);
                  }
                }}
              />
              {file ? (
                <div className="space-y-2">
                  <div className="w-11 h-11 rounded-full bg-green-500/15 flex items-center justify-center mx-auto">
                    <span className="text-green-400 text-xl">✓</span>
                  </div>
                  <p className="font-semibold text-green-400 text-sm">
                    {file.name}
                  </p>
                  <p className="text-xs text-neutral-600">
                    {(file.size / 1024 / 1024).toFixed(1)} MB · click to change
                  </p>
                </div>
              ) : (
                <div className="space-y-3">
                  <div className="w-12 h-12 rounded-full bg-neutral-800/80 flex items-center justify-center mx-auto text-neutral-500 text-2xl">
                    ↑
                  </div>
                  <div>
                    <p className="font-medium text-neutral-300 text-sm">
                      Drop your video here
                    </p>
                    <p className="text-sm text-neutral-600 mt-0.5">
                      or click to browse
                    </p>
                  </div>
                  <p className="text-xs text-neutral-700">
                    .mp4 · .mov · .m4v · .webm
                  </p>
                </div>
              )}
            </div>

            <input
              type="text"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="Video title (optional)"
              className="w-full bg-neutral-900 border border-neutral-800 rounded-xl px-4 py-3 text-sm placeholder-neutral-600 focus:outline-none focus:border-amber-500/50 transition-colors"
            />

            {error && (
              <div className="bg-red-950/40 border border-red-800/40 rounded-xl px-4 py-3 text-sm text-red-400">
                {error}
              </div>
            )}

            <button
              onClick={handleUpload}
              disabled={!file}
              className="w-full bg-amber-500 hover:bg-amber-400 active:bg-amber-600 disabled:opacity-30 disabled:cursor-not-allowed text-black font-bold py-3.5 rounded-xl transition-colors text-sm tracking-wide"
            >
              Upload & Generate →
            </button>
          </div>
        )}

        {/* ── PROCESSING STATE ── */}
        {short && isProcessing && (
          <div className="space-y-7 max-w-lg mx-auto">
            <div>
              <h2 className="font-semibold text-neutral-100 text-lg">
                {short.title}
              </h2>
              <p className="text-sm text-amber-400 mt-0.5">
                {STATUS_LABEL[short.status]}
              </p>
            </div>

            {/* Step indicator */}
            <div className="flex items-start">
              {PIPELINE_STEPS.map((step, i) => {
                const done = i < currentStep;
                const active = i === currentStep;
                return (
                  <div key={step.label} className="flex items-start flex-1">
                    <div className="flex flex-col items-center flex-1">
                      <div
                        className={`w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold transition-all ${
                          done
                            ? "bg-amber-500 text-black"
                            : active
                            ? "bg-neutral-900 border-2 border-amber-500 text-amber-400"
                            : "bg-neutral-900 border border-neutral-800 text-neutral-700"
                        }`}
                      >
                        {done ? "✓" : i + 1}
                      </div>
                      <p
                        className={`text-[10px] mt-2 font-medium text-center ${
                          active
                            ? "text-amber-400"
                            : done
                            ? "text-neutral-500"
                            : "text-neutral-700"
                        }`}
                      >
                        {step.label}
                      </p>
                    </div>
                    {i < PIPELINE_STEPS.length - 1 && (
                      <div
                        className={`h-px w-full mt-3.5 transition-colors ${
                          done ? "bg-amber-500" : "bg-neutral-800"
                        }`}
                      />
                    )}
                  </div>
                );
              })}
            </div>

            <div className="h-px bg-neutral-800 rounded-full overflow-hidden">
              <div className="h-full bg-amber-500 rounded-full animate-pulse w-full" />
            </div>
          </div>
        )}

        {/* ── FAILED STATE ── */}
        {short && FAILED_STATUSES.includes(short.status) && (
          <div className="max-w-lg mx-auto space-y-4">
            <div className="flex items-start justify-between">
              <div>
                <h2 className="font-semibold text-neutral-100">{short.title}</h2>
                <p className="text-sm text-red-400 mt-0.5">
                  {STATUS_LABEL[short.status]}
                </p>
              </div>
              <button
                onClick={resetForm}
                className="text-xs text-neutral-500 hover:text-neutral-300 border border-neutral-800 px-3 py-1.5 rounded-lg transition-colors"
              >
                Start over
              </button>
            </div>

            {short.failureReason && (
              <div className="bg-neutral-900 border border-neutral-800 rounded-xl p-4">
                <p className="text-xs text-neutral-500 font-mono leading-relaxed">
                  {short.failureReason}
                </p>
              </div>
            )}

            {error && (
              <div className="bg-red-950/40 border border-red-800/40 rounded-xl px-4 py-3 text-sm text-red-400">
                {error}
              </div>
            )}

            <button
              onClick={handleRetry}
              className="w-full border border-amber-500/40 text-amber-400 hover:bg-amber-500/10 font-semibold py-3 rounded-xl transition-colors text-sm"
            >
              Retry Generation
            </button>
          </div>
        )}

        {/* ── READY STATE ── */}
        {short && short.status === "READY" && short.videoUrl && (
          <div className="space-y-6">
            {/* Title row */}
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0">
                <h2 className="text-xl font-bold tracking-tight truncate">
                  {short.title}
                </h2>
                <div className="flex items-center gap-2 mt-1">
                  <span className="w-1.5 h-1.5 rounded-full bg-green-400 shrink-0" />
                  <span className="text-sm text-green-400 font-medium">
                    Ready
                  </span>
                </div>
              </div>
              <button
                onClick={resetForm}
                className="shrink-0 text-xs text-neutral-500 hover:text-neutral-200 border border-neutral-800 hover:border-neutral-700 px-3 py-1.5 rounded-lg transition-colors whitespace-nowrap"
              >
                + New Short
              </button>
            </div>

            {/* 2-col layout */}
            <div className="flex flex-col md:flex-row gap-6 items-start">
              {/* ── Left: Video Player ── */}
              <div className="md:w-[200px] shrink-0 space-y-3">
                <div className="relative rounded-2xl overflow-hidden bg-black aspect-[9/16] shadow-2xl shadow-black/70 ring-1 ring-white/5">
                  <video
                    src={short.videoUrl}
                    controls
                    playsInline
                    className="absolute inset-0 w-full h-full object-cover"
                  />
                </div>
                <a
                  href={short.videoUrl}
                  download
                  className="flex items-center justify-center gap-2 w-full bg-neutral-900 hover:bg-neutral-800 border border-neutral-800 hover:border-neutral-700 text-neutral-300 hover:text-neutral-100 py-2.5 rounded-xl text-xs font-medium transition-all"
                >
                  ↓ Download MP4
                </a>
              </div>

              {/* ── Right: Tabbed panels ── */}
              <div className="flex-1 min-w-0">
                {/* Tab bar */}
                <div className="flex border-b border-neutral-800 mb-6">
                  {(["publish", "quality", "scenes"] as ActiveTab[]).map(
                    (tab) => {
                      const labels: Record<ActiveTab, string> = {
                        publish: "Publish",
                        quality: "Quality",
                        scenes: "Edit Scenes",
                      };
                      return (
                        <button
                          key={tab}
                          onClick={() => setActiveTab(tab)}
                          className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors ${
                            activeTab === tab
                              ? "border-amber-500 text-amber-400"
                              : "border-transparent text-neutral-500 hover:text-neutral-300"
                          }`}
                        >
                          {labels[tab]}
                          {tab === "quality" && short.evaluation && (
                            <span
                              className={`text-[10px] font-mono font-bold px-1 py-0.5 rounded ${
                                short.evaluation.overall_score >= 7
                                  ? "text-green-400 bg-green-900/30"
                                  : short.evaluation.overall_score >= 5
                                  ? "text-amber-400 bg-amber-900/30"
                                  : "text-red-400 bg-red-900/30"
                              }`}
                            >
                              {short.evaluation.overall_score.toFixed(1)}
                            </span>
                          )}
                        </button>
                      );
                    }
                  )}
                </div>

                {/* ── PUBLISH TAB ── */}
                {activeTab === "publish" && (
                  <div className="space-y-6">
                    {/* Description */}
                    {short.caption && (
                      <div className="space-y-2.5">
                        <div className="flex items-center justify-between">
                          <p className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">
                            YouTube Description
                          </p>
                          {!editingCaption && (
                            <div className="flex gap-3">
                              <button
                                onClick={() => {
                                  setCaptionDraft(short.caption!);
                                  setEditingCaption(true);
                                }}
                                className="text-xs text-neutral-500 hover:text-neutral-200 transition-colors"
                              >
                                Edit
                              </button>
                              <button
                                onClick={() =>
                                  navigator.clipboard.writeText(short.caption!)
                                }
                                className="text-xs text-amber-500 hover:text-amber-400 transition-colors font-medium"
                              >
                                Copy
                              </button>
                            </div>
                          )}
                        </div>

                        {editingCaption ? (
                          <div className="space-y-2">
                            <textarea
                              value={captionDraft}
                              onChange={(e) => setCaptionDraft(e.target.value)}
                              rows={8}
                              className="w-full bg-neutral-900 border border-amber-500/30 focus:border-amber-500/60 rounded-xl p-4 text-sm text-neutral-200 leading-relaxed resize-none focus:outline-none transition-colors"
                            />
                            <div className="flex gap-2 justify-end">
                              <button
                                onClick={() => setEditingCaption(false)}
                                className="text-xs text-neutral-500 hover:text-neutral-300 px-3 py-1.5 rounded-lg border border-neutral-800 transition-colors"
                              >
                                Cancel
                              </button>
                              <button
                                disabled={savingCaption || !captionDraft.trim()}
                                onClick={async () => {
                                  if (!short.shortId) return;
                                  setSavingCaption(true);
                                  try {
                                    await api.updateShortCaption(
                                      short.shortId,
                                      captionDraft.trim()
                                    );
                                    setShort((prev) =>
                                      prev
                                        ? { ...prev, caption: captionDraft.trim() }
                                        : prev
                                    );
                                    setEditingCaption(false);
                                  } catch (e) {
                                    setError(
                                      `Save failed: ${e instanceof Error ? e.message : String(e)}`
                                    );
                                  } finally {
                                    setSavingCaption(false);
                                  }
                                }}
                                className="text-xs bg-amber-500 hover:bg-amber-400 disabled:opacity-40 text-black font-bold px-4 py-1.5 rounded-lg transition-colors"
                              >
                                {savingCaption ? "Saving…" : "Save"}
                              </button>
                            </div>
                          </div>
                        ) : (
                          <div className="bg-neutral-900/50 border border-neutral-800 rounded-xl p-4">
                            <p className="text-sm text-neutral-300 leading-relaxed whitespace-pre-wrap">
                              {short.caption}
                            </p>
                          </div>
                        )}
                      </div>
                    )}

                    {/* Hashtags */}
                    {short.hashtags && short.hashtags.length > 0 && (
                      <div className="space-y-2.5">
                        <div className="flex items-center justify-between">
                          <p className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">
                            Hashtags
                          </p>
                          <button
                            onClick={() =>
                              navigator.clipboard.writeText(
                                short.hashtags!.join(" ")
                              )
                            }
                            className="text-xs text-amber-500 hover:text-amber-400 transition-colors font-medium"
                          >
                            Copy all
                          </button>
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                          {short.hashtags.map((tag) => (
                            <button
                              key={tag}
                              onClick={() =>
                                navigator.clipboard.writeText(
                                  tag.startsWith("#") ? tag : `#${tag}`
                                )
                              }
                              className="text-xs bg-neutral-900 border border-neutral-800 hover:border-amber-500/40 hover:text-amber-300 text-amber-400 rounded-lg px-2.5 py-1 cursor-pointer transition-all"
                            >
                              {tag.startsWith("#") ? tag : `#${tag}`}
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {!short.caption && !short.hashtags?.length && (
                      <div className="text-center py-10 text-neutral-700 text-sm">
                        No metadata generated yet
                      </div>
                    )}
                  </div>
                )}

                {/* ── QUALITY TAB ── */}
                {activeTab === "quality" && short.evaluation && (
                  <div className="space-y-6">
                    {/* Score ring + verdict */}
                    <div className="flex items-center gap-5">
                      {(() => {
                        const score = short.evaluation.overall_score;
                        const r = 28;
                        const circ = 2 * Math.PI * r;
                        const fill = circ * (score / 10);
                        const color =
                          score >= 7
                            ? "#4ade80"
                            : score >= 5
                            ? "#f59e0b"
                            : "#f87171";
                        return (
                          <svg
                            width="76"
                            height="76"
                            viewBox="0 0 76 76"
                            className="shrink-0"
                          >
                            <circle
                              cx="38"
                              cy="38"
                              r={r}
                              fill="none"
                              stroke="#1f1f1f"
                              strokeWidth="9"
                            />
                            <circle
                              cx="38"
                              cy="38"
                              r={r}
                              fill="none"
                              stroke={color}
                              strokeWidth="9"
                              strokeDasharray={`${fill} ${circ}`}
                              strokeLinecap="round"
                              transform="rotate(-90 38 38)"
                            />
                            <text
                              x="38"
                              y="43"
                              textAnchor="middle"
                              fill="white"
                              fontSize="15"
                              fontWeight="700"
                              fontFamily="ui-monospace,monospace"
                            >
                              {score.toFixed(1)}
                            </text>
                          </svg>
                        );
                      })()}
                      <div className="space-y-1.5">
                        <span
                          className={`inline-flex items-center px-3 py-1 rounded-full text-xs font-bold border ${
                            short.evaluation.verdict === "STRONG"
                              ? "bg-green-950/60 text-green-300 border-green-800/50"
                              : short.evaluation.verdict === "GOOD"
                              ? "bg-amber-950/50 text-amber-300 border-amber-800/40"
                              : "bg-red-950/50 text-red-300 border-red-800/40"
                          }`}
                        >
                          {short.evaluation.verdict}
                        </span>
                        <p className="text-xs text-neutral-600">
                          Screenplay quality · out of 10
                        </p>
                      </div>
                    </div>

                    {/* Score bars */}
                    <div className="space-y-3.5">
                      {[
                        {
                          label: "Visual Quality",
                          score: short.evaluation.visual_quality_score,
                        },
                        {
                          label: "Voiceover Alignment",
                          score: short.evaluation.alignment_score,
                        },
                        {
                          label: "House Style",
                          score: short.evaluation.house_style_score,
                        },
                      ].map(({ label, score }) => {
                        const barColor =
                          score >= 7
                            ? "bg-green-500"
                            : score >= 5
                            ? "bg-amber-500"
                            : "bg-red-400";
                        const numColor =
                          score >= 7
                            ? "text-green-400"
                            : score >= 5
                            ? "text-amber-400"
                            : "text-red-400";
                        return (
                          <div key={label} className="space-y-1.5">
                            <div className="flex justify-between items-center">
                              <span className="text-xs text-neutral-400">
                                {label}
                              </span>
                              <span
                                className={`text-xs font-mono font-semibold ${numColor}`}
                              >
                                {score.toFixed(1)}
                              </span>
                            </div>
                            <div className="h-1.5 bg-neutral-800 rounded-full overflow-hidden">
                              <div
                                className={`h-full ${barColor} rounded-full transition-all duration-700`}
                                style={{ width: `${score * 10}%` }}
                              />
                            </div>
                          </div>
                        );
                      })}
                    </div>

                    {/* Strengths */}
                    {short.evaluation.strengths?.length > 0 && (
                      <div className="space-y-2">
                        <p className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">
                          Strengths
                        </p>
                        <div className="space-y-2">
                          {short.evaluation.strengths.map((s, i) => (
                            <div
                              key={i}
                              className="flex gap-2.5 text-sm text-neutral-300 leading-snug"
                            >
                              <span className="text-green-500 shrink-0 mt-0.5 text-xs">
                                ✓
                              </span>
                              <span>{s}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Improvements */}
                    {short.evaluation.improvements?.length > 0 && (
                      <div className="space-y-2">
                        <p className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">
                          To Improve
                        </p>
                        <div className="space-y-2">
                          {short.evaluation.improvements.map((s, i) => (
                            <div
                              key={i}
                              className="flex gap-2.5 text-sm text-neutral-300 leading-snug"
                            >
                              <span className="text-amber-500 shrink-0 mt-0.5 text-xs">
                                ↗
                              </span>
                              <span>{s}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {activeTab === "quality" && !short.evaluation && (
                  <div className="text-center py-12 text-neutral-700 text-sm">
                    Quality evaluation not available for this short.
                    <br />
                    <span className="text-neutral-800 text-xs mt-1 block">
                      Newer generations include automatic scoring.
                    </span>
                  </div>
                )}

                {/* ── SCENES TAB ── */}
                {activeTab === "scenes" && (
                  <div>
                    {loadingScreenplay ? (
                      <div className="flex items-center justify-center gap-3 py-14 text-neutral-600 text-sm">
                        <div className="w-4 h-4 border-2 border-neutral-800 border-t-amber-500 rounded-full animate-spin" />
                        Loading scenes…
                      </div>
                    ) : editedBeats ? (
                      <div className="space-y-3">
                        {editedBeats.map((beat, bi) => {
                          const badgeClass =
                            purposeColors[beat.purpose] ??
                            "text-neutral-400 bg-neutral-800/50 border-neutral-700";
                          return (
                            <div
                              key={bi}
                              className="bg-neutral-900/60 border border-neutral-800 rounded-2xl overflow-hidden"
                            >
                              {/* Beat header */}
                              <div className="flex items-center gap-2.5 px-4 py-3 border-b border-neutral-800/60">
                                <span className="text-[10px] font-mono text-neutral-700">
                                  {bi + 1}
                                </span>
                                <span
                                  className={`text-[10px] px-2 py-0.5 rounded-md border font-bold uppercase tracking-wider ${badgeClass}`}
                                >
                                  {beat.purpose}
                                </span>
                                <span className="text-[10px] text-neutral-700 ml-auto font-mono">
                                  {fmtTime(beat.start)} – {fmtTime(beat.end)}
                                </span>
                              </div>

                              <div className="p-4 space-y-4">
                                {/* Voiceover */}
                                <div>
                                  <p className="text-[9px] font-bold text-neutral-600 uppercase tracking-widest mb-1.5">
                                    Voiceover
                                  </p>
                                  <p className="text-sm text-neutral-400 leading-relaxed">
                                    {beat.voiceover}
                                  </p>
                                </div>

                                {/* On-screen text */}
                                {beat.on_screen_text && (
                                  <div className="border-l-2 border-neutral-800 pl-3">
                                    <p className="text-[9px] font-bold text-neutral-600 uppercase tracking-widest mb-1">
                                      On Screen
                                    </p>
                                    <p className="text-xs text-neutral-500">
                                      {beat.on_screen_text}
                                    </p>
                                  </div>
                                )}

                                {/* Shots */}
                                <div className="space-y-2.5">
                                  {beat.shots.map((shot, si) => (
                                    <div
                                      key={si}
                                      className="bg-neutral-950/70 border border-neutral-800/50 rounded-xl p-3.5 space-y-2.5"
                                    >
                                      <div className="flex items-center gap-2 flex-wrap">
                                        <span className="text-[10px] font-bold text-neutral-600 uppercase tracking-widest">
                                          Shot {shot.shot_number}
                                        </span>
                                        <span className="text-[10px] text-neutral-800">
                                          {shot.shot_duration_sec}s
                                        </span>
                                        {shot.framing && (
                                          <span className="text-[10px] text-neutral-700 ml-auto">
                                            {shot.framing}
                                          </span>
                                        )}
                                        {shot.camera_movement && (
                                          <span className="text-[10px] text-neutral-700">
                                            · {shot.camera_movement}
                                          </span>
                                        )}
                                      </div>
                                      <div>
                                        <p className="text-[9px] font-bold text-amber-600/80 uppercase tracking-widest mb-1.5">
                                          Visual Prompt
                                        </p>
                                        <textarea
                                          rows={3}
                                          value={shot.visual}
                                          onChange={(e) => {
                                            const nb = JSON.parse(
                                              JSON.stringify(editedBeats)
                                            );
                                            nb[bi].shots[si].visual =
                                              e.target.value;
                                            setEditedBeats(nb);
                                          }}
                                          className="w-full bg-neutral-900 border border-neutral-800 focus:border-amber-500/40 rounded-lg p-2.5 text-xs text-neutral-300 leading-relaxed resize-none focus:outline-none transition-colors"
                                          placeholder="Describe what the camera should film…"
                                        />
                                      </div>
                                    </div>
                                  ))}
                                </div>
                              </div>
                            </div>
                          );
                        })}

                        {/* Sticky footer */}
                        <div className="flex gap-3 pt-2 sticky bottom-0 pb-2">
                          <button
                            onClick={() => {
                              if (screenplay)
                                setEditedBeats(
                                  JSON.parse(JSON.stringify(screenplay.beats))
                                );
                            }}
                            disabled={savingBeats}
                            className="text-sm text-neutral-500 hover:text-neutral-300 px-4 py-2.5 rounded-xl border border-neutral-800 hover:border-neutral-700 transition-colors"
                          >
                            Reset
                          </button>
                          <button
                            onClick={() => saveAndRerender(short.shortId)}
                            disabled={savingBeats || !editedBeats}
                            className="flex-1 bg-amber-500 hover:bg-amber-400 active:bg-amber-600 disabled:opacity-40 text-black font-bold py-2.5 rounded-xl text-sm transition-colors"
                          >
                            {savingBeats
                              ? "Saving & launching render…"
                              : "Save & Re-render"}
                          </button>
                        </div>
                      </div>
                    ) : screenplayError ? (
                      <div className="space-y-4 py-8">
                        <div className="bg-red-950/40 border border-red-800/40 rounded-xl px-4 py-3 text-sm text-red-400">
                          <p className="font-medium mb-1">Could not load scenes</p>
                          <p className="text-xs font-mono text-red-500/80">{screenplayError}</p>
                        </div>
                        <button
                          onClick={() => setScreenplayError(null)}
                          className="text-xs text-neutral-500 hover:text-neutral-300 border border-neutral-800 px-3 py-1.5 rounded-lg transition-colors"
                        >
                          Retry
                        </button>
                      </div>
                    ) : (
                      <div className="text-center py-12 text-neutral-700 text-sm">
                        No scene data found for this short.
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>

            {error && (
              <div className="bg-red-950/40 border border-red-800/40 rounded-xl px-4 py-3 text-sm text-red-400">
                {error}
              </div>
            )}
          </div>
        )}

        {/* ── HISTORY ── */}
        {(history.length > 0 || loadingHistory) && (
          <div className="border-t border-neutral-800/50 pt-8 space-y-3">
            <div className="flex items-center justify-between">
              <h2 className="text-[10px] font-bold text-neutral-600 uppercase tracking-widest">
                Previous Shorts
                {history.length > 0 && (
                  <span className="ml-1.5 text-neutral-700">
                    ({history.length})
                  </span>
                )}
              </h2>
              {loadingHistory && (
                <span className="text-xs text-neutral-700">Loading…</span>
              )}
            </div>

            <div className="space-y-1">
              {history.map((item) => {
                const isReady = item.status === "READY";
                const isFailed = [
                  "TRANSCRIBE_FAILED",
                  "GENERATE_FAILED",
                  "RENDER_FAILED",
                ].includes(item.status);
                const isActive = short?.shortId === item.short_id;
                const isLoading = loadingShortId === item.short_id;
                const date = item.created_at
                  ? new Date(item.created_at).toLocaleDateString(undefined, {
                      month: "short",
                      day: "numeric",
                    })
                  : "";

                return (
                  <button
                    key={item.short_id}
                    onClick={() =>
                      !isActive && !isLoading && loadFromHistory(item)
                    }
                    disabled={isLoading || isActive}
                    className={`w-full flex items-center gap-3 px-3.5 py-3 rounded-xl text-left transition-all ${
                      isActive
                        ? "bg-amber-500/8 border border-amber-500/15 cursor-default"
                        : "hover:bg-neutral-900/60 border border-transparent hover:border-neutral-800 cursor-pointer"
                    }`}
                  >
                    <div
                      className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                        isReady
                          ? "bg-green-500"
                          : isFailed
                          ? "bg-red-500"
                          : "bg-amber-500 animate-pulse"
                      }`}
                    />
                    <div className="flex-1 min-w-0">
                      <p className="text-sm font-medium text-neutral-300 truncate">
                        {item.title || "Untitled"}
                      </p>
                      <p className="text-xs text-neutral-700 mt-0.5">{date}</p>
                    </div>
                    <div className="shrink-0">
                      {isLoading ? (
                        <span className="text-xs text-neutral-700">
                          Loading…
                        </span>
                      ) : isActive ? (
                        <span className="text-xs text-amber-500 font-medium">
                          Viewing
                        </span>
                      ) : (
                        <span
                          className={`text-xs font-medium ${
                            isReady
                              ? "text-green-600"
                              : isFailed
                              ? "text-red-600"
                              : "text-amber-600"
                          }`}
                        >
                          {isReady ? "Ready" : isFailed ? "Failed" : "In progress"}
                        </span>
                      )}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </main>
  );
}
