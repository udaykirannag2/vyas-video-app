"use client";

import { useState, useRef, useCallback } from "react";
import { api } from "@/lib/api";

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

interface ShortState {
  shortId: string;
  title: string;
  status: ShortStatus;
  mp4Key?: string;
  videoUrl?: string;
  failureReason?: string;
}

const STATUS_LABEL: Record<ShortStatus, string> = {
  idle: "Ready",
  uploading: "Uploading…",
  TRANSCRIBING: "Transcribing audio…",
  TRANSCRIBED: "Transcribed — generating video…",
  GENERATING: "Writing screenplay & visuals…",
  RENDERING: "Rendering video…",
  READY: "Done!",
  TRANSCRIBE_FAILED: "Transcription failed",
  GENERATE_FAILED: "Generation failed",
  RENDER_FAILED: "Render failed",
};

const FAILED_STATUSES: ShortStatus[] = ["TRANSCRIBE_FAILED", "GENERATE_FAILED", "RENDER_FAILED"];

export default function ShortsPage() {
  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [short, setShort] = useState<ShortState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [isDragging, setIsDragging] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const stopPolling = () => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const startPolling = useCallback((shortId: string) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const s = await api.shortStatus(shortId);
        setShort((prev) =>
          prev ? { ...prev, status: s.status as ShortStatus, mp4Key: s.mp4_key, failureReason: s.failure_reason } : prev
        );

        if (s.status === "READY" && s.mp4_key) {
          stopPolling();
          const { url } = await api.assetUrl(s.mp4_key);
          setShort((prev) => (prev ? { ...prev, videoUrl: url } : prev));
        }

        if (FAILED_STATUSES.includes(s.status as ShortStatus)) {
          stopPolling();
        }

        // Auto-trigger generate once transcription completes.
        if (s.status === "TRANSCRIBED") {
          stopPolling();
          try {
            await api.generateShort(shortId);
            setShort((prev) => (prev ? { ...prev, status: "GENERATING" } : prev));
            startPolling(shortId);
          } catch (e) {
            setError(`Generate failed: ${e instanceof Error ? e.message : String(e)}`);
          }
        }
      } catch (e) {
        console.error("poll error", e);
      }
    }, 7000);
  }, []);

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
      // 1. Get presigned URL (server creates short_id).
      setShort({ shortId: "", title: title || file.name, status: "uploading" });
      const { url, video_key, short_id, content_type } = await api.shortUploadUrl(file.name);

      // 2. PUT file directly to S3.
      await api.uploadFile(url, content_type, file);

      // 3. Register short — triggers FFmpeg audio extraction + Transcribe.
      await api.createShort(title || file.name, video_key);

      setShort({ shortId: short_id, title: title || file.name, status: "TRANSCRIBING" });
      startPolling(short_id);
    } catch (e) {
      setError(`Upload failed: ${e instanceof Error ? e.message : String(e)}`);
      setShort(null);
    }
  };

  const handleRetry = async () => {
    if (!short?.shortId) return;
    setError(null);
    try {
      await api.generateShort(short.shortId);
      setShort((prev) => (prev ? { ...prev, status: "GENERATING", failureReason: undefined } : prev));
      startPolling(short.shortId);
    } catch (e) {
      setError(`Retry failed: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const resetForm = () => {
    stopPolling();
    setFile(null);
    setTitle("");
    setShort(null);
    setError(null);
  };

  const isProcessing = short && !["idle", "READY", ...FAILED_STATUSES].includes(short.status);

  return (
    <main className="min-h-screen bg-neutral-950 text-neutral-100 p-6 max-w-2xl mx-auto">
      <div className="mb-8">
        <h1 className="text-2xl font-bold tracking-tight">Short → Video</h1>
        <p className="text-neutral-400 mt-1 text-sm">
          Upload a short clip with audio — we keep your voice and generate fresh visuals.
        </p>
      </div>

      {/* Upload form — hide while processing or done */}
      {!short && (
        <div className="space-y-4">
          {/* Drop zone */}
          <div
            onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
            onDragLeave={() => setIsDragging(false)}
            onDrop={handleDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-xl p-10 text-center cursor-pointer transition-colors ${
              isDragging
                ? "border-amber-400 bg-amber-400/5"
                : file
                ? "border-green-500 bg-green-500/5"
                : "border-neutral-700 hover:border-neutral-500"
            }`}
          >
            <input
              ref={fileInputRef}
              type="file"
              accept="video/mp4,video/quicktime,video/x-m4v,video/webm,.mp4,.mov,.m4v,.webm"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) { setFile(f); setError(null); }
              }}
            />
            {file ? (
              <div>
                <p className="font-medium text-green-400">✓ {file.name}</p>
                <p className="text-xs text-neutral-500 mt-1">{(file.size / 1024 / 1024).toFixed(1)} MB</p>
              </div>
            ) : (
              <div>
                <p className="text-neutral-400">Drop a video file here</p>
                <p className="text-xs text-neutral-600 mt-1">.mp4 · .mov · .m4v · .webm</p>
              </div>
            )}
          </div>

          {/* Title input */}
          <input
            type="text"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Title (optional)"
            className="w-full bg-neutral-900 border border-neutral-700 rounded-lg px-4 py-2.5 text-sm placeholder-neutral-500 focus:outline-none focus:border-amber-500"
          />

          {error && <p className="text-red-400 text-sm">{error}</p>}

          <button
            onClick={handleUpload}
            disabled={!file}
            className="w-full bg-amber-500 hover:bg-amber-400 disabled:opacity-40 disabled:cursor-not-allowed text-black font-semibold py-3 rounded-lg transition-colors"
          >
            Upload & Generate
          </button>
        </div>
      )}

      {/* Status card */}
      {short && (
        <div className="border border-neutral-800 rounded-xl p-6 space-y-4">
          <div className="flex items-start justify-between">
            <div>
              <p className="font-semibold">{short.title}</p>
              <p className={`text-sm mt-0.5 ${FAILED_STATUSES.includes(short.status) ? "text-red-400" : short.status === "READY" ? "text-green-400" : "text-amber-400"}`}>
                {STATUS_LABEL[short.status]}
              </p>
            </div>
            {!isProcessing && (
              <button onClick={resetForm} className="text-xs text-neutral-500 hover:text-neutral-300">
                New short
              </button>
            )}
          </div>

          {/* Progress bar */}
          {isProcessing && (
            <div className="h-1 bg-neutral-800 rounded-full overflow-hidden">
              <div className="h-full bg-amber-500 rounded-full animate-pulse w-full" />
            </div>
          )}

          {/* Failure message + retry */}
          {FAILED_STATUSES.includes(short.status) && (
            <div className="space-y-3">
              {short.failureReason && (
                <p className="text-xs text-neutral-500 font-mono bg-neutral-900 p-3 rounded-lg">
                  {short.failureReason}
                </p>
              )}
              <button
                onClick={handleRetry}
                className="w-full border border-amber-500 text-amber-400 hover:bg-amber-500/10 font-medium py-2.5 rounded-lg transition-colors text-sm"
              >
                Retry Generation
              </button>
            </div>
          )}

          {/* Video player */}
          {short.status === "READY" && short.videoUrl && (
            <div className="space-y-3">
              <video
                src={short.videoUrl}
                controls
                className="w-full rounded-lg bg-black aspect-[9/16] max-h-[480px] object-contain"
              />
              <a
                href={short.videoUrl}
                download
                className="flex items-center justify-center gap-2 w-full border border-neutral-700 text-neutral-300 hover:border-neutral-500 py-2.5 rounded-lg text-sm transition-colors"
              >
                ↓ Download MP4
              </a>
            </div>
          )}
        </div>
      )}
    </main>
  );
}
