import React from "react";
import { AbsoluteFill, OffthreadVideo } from "remotion";

// OffthreadVideo decodes frames on-demand inside Remotion Lambda's Chromium,
// avoiding the full-clip preload that Video does (which blows past the
// default 28s delayRender timeout for scenes >10s).
export const BRollClip: React.FC<{ src: string }> = ({ src }) => {
  return (
    <AbsoluteFill>
      <OffthreadVideo
        src={src}
        muted
        style={{
          width: "100%",
          height: "100%",
          objectFit: "cover",
        }}
      />
      {/* Darkening overlay so white text stays legible */}
      <AbsoluteFill style={{ backgroundColor: "rgba(0,0,0,0.35)" }} />
    </AbsoluteFill>
  );
};
