import React from "react";
import { Composition } from "remotion";
import { Reel, ReelProps, defaultReelProps } from "./compositions/Reel";

const FPS = 30;

export const Root: React.FC = () => {
  return (
    <Composition<typeof schemaStub, ReelProps>
      id="Reel"
      component={Reel}
      defaultProps={defaultReelProps}
      calculateMetadata={({ props }) => {
        const baseDuration = props.script?.duration_sec ?? 30;
        // Extend the composition duration so the outro sequence has room
        // to play. If outroUrl is missing, no extension.
        const outroDuration = props.outroUrl ? (props.outroDurationSec ?? 5) : 0;
        const totalDuration = baseDuration + outroDuration;
        return {
          fps: FPS,
          durationInFrames: Math.round(totalDuration * FPS),
          width: 1080,
          height: 1920,
        };
      }}
      fps={FPS}
      width={1080}
      height={1920}
      durationInFrames={30 * FPS}
    />
  );
};

const schemaStub = undefined as any;
