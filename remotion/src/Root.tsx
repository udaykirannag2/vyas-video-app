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
        const duration = props.script?.duration_sec ?? 30;
        return {
          fps: FPS,
          durationInFrames: Math.round(duration * FPS),
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
