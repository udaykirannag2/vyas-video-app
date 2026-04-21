import React from "react";
import {
  AbsoluteFill,
  Sequence,
  Audio,
  OffthreadVideo,
  useVideoConfig,
} from "remotion";
import { TextOverlay } from "../components/TextOverlay";
import { BRollClip } from "../components/BRollClip";

// ---- Types ----

export interface Shot {
  shot_number: number;
  shot_duration_sec: number;
  shot_role: string;
  visual_mode: string;
  visual: string;
  framing: string;
  camera_movement: string;
  transition_hint: string;
  broll_queries: string[];
  broll_query: string;
}

export interface Beat {
  start: number;
  end: number;
  source_start: number | null;
  source_end: number | null;
  voiceover: string;
  on_screen_text: string;
  purpose: string;
  shots: Shot[];
}

export interface SceneAudio {
  index: number;
  audio_key: string;
  audio_url?: string;
  marks_key: string | null;
}

export interface ShotBroll {
  global_id: string;
  broll_key: string | null;
  broll_url?: string | null;
  source?: string;
}

export interface ReelProps {
  script: {
    title: string;
    duration_sec: number;
    aspect: string;
    beats: Beat[];
    // Legacy compat
    scenes?: any[];
    caption: string;
    hashtags: string[];
  };
  sceneAudio: SceneAudio[];
  shotBroll: ShotBroll[];
  // Legacy compat
  sceneBroll?: ShotBroll[];
  assetsBucket: string;
  projectId: string;
  // Outro: branded clip appended to every reel (optional).
  outroUrl?: string | null;
  outroDurationSec?: number;
}

const s3Url = (bucket: string, key: string) =>
  `https://${bucket}.s3.amazonaws.com/${key}`;

export const Reel: React.FC<ReelProps> = ({
  script,
  sceneAudio,
  shotBroll,
  sceneBroll,
  assetsBucket,
  outroUrl,
  outroDurationSec = 5,
}) => {
  const { fps } = useVideoConfig();
  const beats = script.beats?.length ? script.beats : [];
  const allBroll = shotBroll?.length ? shotBroll : sceneBroll || [];
  // Where the outro starts = end of the last beat.
  const lastBeatEnd = beats.length ? beats[beats.length - 1].end : 0;
  const outroStartFrame = Math.round(lastBeatEnd * fps);
  const outroDurationFrames = Math.max(1, Math.round(outroDurationSec * fps));

  // Build a flat shot index matching the global_id pattern "b{beatIdx}_s{shotIdx}"
  let globalShotIdx = 0;

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {beats.map((beat, bi) => {
        const beatStartFrame = Math.round(beat.start * fps);
        const beatDurationFrames = Math.max(
          1,
          Math.round((beat.end - beat.start) * fps)
        );
        const audio = sceneAudio.find((a) => a.index === bi);
        const audioSrc =
          audio?.audio_url ||
          (audio?.audio_key ? s3Url(assetsBucket, audio.audio_key) : null);

        // Render shots within the beat as sub-sequences.
        let shotOffset = 0;
        const shotElements = (beat.shots || []).map((shot, si) => {
          const shotFrames = Math.max(
            1,
            Math.round(shot.shot_duration_sec * fps)
          );
          const brollEntry = allBroll[globalShotIdx] || null;
          globalShotIdx++;
          const brollSrc =
            (brollEntry as any)?.broll_url ||
            ((brollEntry as any)?.broll_key
              ? s3Url(assetsBucket, (brollEntry as any).broll_key)
              : null);

          const el = (
            <Sequence
              key={`b${bi}_s${si}`}
              from={shotOffset}
              durationInFrames={shotFrames}
              name={`beat-${bi}-shot-${si}`}
            >
              {brollSrc ? (
                <BRollClip src={brollSrc} />
              ) : (
                <AbsoluteFill
                  style={{
                    background: "linear-gradient(135deg,#1a1a2e,#16213e)",
                  }}
                />
              )}
            </Sequence>
          );
          shotOffset += shotFrames;
          return el;
        });

        // If no shots, show a gradient for the whole beat.
        if (shotElements.length === 0) {
          const brollEntry = allBroll[globalShotIdx] || null;
          globalShotIdx++;
          const brollSrc =
            (brollEntry as any)?.broll_url ||
            ((brollEntry as any)?.broll_key
              ? s3Url(assetsBucket, (brollEntry as any).broll_key)
              : null);
          shotElements.push(
            <Sequence
              key={`b${bi}_fallback`}
              from={0}
              durationInFrames={beatDurationFrames}
              name={`beat-${bi}-fallback`}
            >
              {brollSrc ? (
                <BRollClip src={brollSrc} />
              ) : (
                <AbsoluteFill
                  style={{
                    background: "linear-gradient(135deg,#1a1a2e,#16213e)",
                  }}
                />
              )}
            </Sequence>
          );
        }

        return (
          <Sequence
            key={`beat-${bi}`}
            from={beatStartFrame}
            durationInFrames={beatDurationFrames}
            name={`beat-${bi}`}
          >
            {/* Visual shots */}
            {shotElements}

            {/* Text overlay spans the whole beat */}
            <TextOverlay text={beat.on_screen_text} />

            {/* Audio spans the whole beat */}
            {audioSrc && <Audio src={audioSrc} />}
          </Sequence>
        );
      })}

      {/* Outro — branded end-card appended to every reel. The outro has
          its own baked-in audio, so no separate <Audio> needed. */}
      {outroUrl && (
        <Sequence
          from={outroStartFrame}
          durationInFrames={outroDurationFrames}
          name="outro"
        >
          <AbsoluteFill style={{ backgroundColor: "black" }}>
            <OffthreadVideo
              src={outroUrl}
              style={{ width: "100%", height: "100%", objectFit: "cover" }}
            />
          </AbsoluteFill>
        </Sequence>
      )}
    </AbsoluteFill>
  );
};

export const defaultReelProps: ReelProps = {
  script: {
    title: "Sample Reel",
    duration_sec: 6,
    aspect: "9:16",
    beats: [
      {
        start: 0,
        end: 6,
        source_start: 0,
        source_end: 6,
        voiceover: "What if losing meant winning?",
        on_screen_text: "WHAT IF?",
        purpose: "hook",
        shots: [
          {
            shot_number: 1,
            shot_duration_sec: 3,
            shot_role: "hook",
            visual_mode: "metaphorical",
            visual: "Eye opening in darkness",
            framing: "extreme-close-up",
            camera_movement: "slow zoom",
            transition_hint: "cut",
            broll_queries: ["eye opening darkness"],
            broll_query: "eye opening darkness",
          },
          {
            shot_number: 2,
            shot_duration_sec: 3,
            shot_role: "establish",
            visual_mode: "metaphorical",
            visual: "Sunrise over calm lake",
            framing: "wide",
            camera_movement: "pull back",
            transition_hint: "dissolve",
            broll_queries: ["sunrise lake calm"],
            broll_query: "sunrise lake calm",
          },
        ],
      },
    ],
    caption: "A reframe from the Gita.",
    hashtags: ["#BhagavadGita"],
  },
  sceneAudio: [],
  shotBroll: [],
  assetsBucket: "local",
  projectId: "sample",
};
