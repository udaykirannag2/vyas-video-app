import React from "react";
import {
  AbsoluteFill,
  Sequence,
  Audio,
  Video,
  useVideoConfig,
} from "remotion";
import { TextOverlay } from "../components/TextOverlay";
import { BRollClip } from "../components/BRollClip";

export interface Scene {
  start: number;
  end: number;
  voiceover: string;
  on_screen_text: string;
  visual: string;
  broll_query: string;
}

export interface Script {
  title: string;
  duration_sec: number;
  aspect: string;
  scenes: Scene[];
  caption: string;
  hashtags: string[];
}

export interface SceneAudio {
  index: number;
  audio_key: string;
  audio_url?: string; // presigned URL preferred; audio_key kept for debugging
  marks_key: string | null;
}

export interface SceneBroll {
  index: number;
  broll_key: string | null;
  broll_url?: string | null;
}

export interface ReelProps {
  script: Script;
  sceneAudio: SceneAudio[];
  sceneBroll: SceneBroll[];
  assetsBucket: string;
  projectId: string;
}

const s3Url = (bucket: string, key: string) =>
  `https://${bucket}.s3.amazonaws.com/${key}`;

export const Reel: React.FC<ReelProps> = ({
  script,
  sceneAudio,
  sceneBroll,
  assetsBucket,
}) => {
  const { fps } = useVideoConfig();

  return (
    <AbsoluteFill style={{ backgroundColor: "black" }}>
      {script.scenes.map((scene, i) => {
        const startFrame = Math.round(scene.start * fps);
        const durationFrames = Math.max(1, Math.round((scene.end - scene.start) * fps));
        const audio = sceneAudio.find((a) => a.index === i);
        const broll = sceneBroll.find((b) => b.index === i);

        const brollSrc =
          broll?.broll_url ||
          (broll?.broll_key ? s3Url(assetsBucket, broll.broll_key) : null);
        const audioSrc =
          audio?.audio_url ||
          (audio?.audio_key ? s3Url(assetsBucket, audio.audio_key) : null);

        return (
          <Sequence
            key={i}
            from={startFrame}
            durationInFrames={durationFrames}
            name={`scene-${i}`}
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

            <TextOverlay text={scene.on_screen_text} />

            {audioSrc && <Audio src={audioSrc} />}
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

export const defaultReelProps: ReelProps = {
  script: {
    title: "Sample Reel",
    duration_sec: 6,
    aspect: "9:16",
    scenes: [
      {
        start: 0,
        end: 3,
        voiceover: "What if losing meant winning?",
        on_screen_text: "WHAT IF?",
        visual: "still lake at dawn",
        broll_query: "calm lake dawn",
      },
      {
        start: 3,
        end: 6,
        voiceover: "Detach from the outcome. Focus on the act.",
        on_screen_text: "ACT WITHOUT ATTACHMENT",
        visual: "runner mid-stride",
        broll_query: "runner slow motion",
      },
    ],
    caption: "A 15-second reframe from BG 2.47.",
    hashtags: ["#BhagavadGita", "#Wisdom", "#SelfGrowth"],
  },
  sceneAudio: [],
  sceneBroll: [],
  assetsBucket: "vyas-video-assets-local",
  projectId: "sample",
};
