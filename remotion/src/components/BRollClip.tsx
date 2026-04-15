import React from "react";
import { AbsoluteFill, Video } from "remotion";

export const BRollClip: React.FC<{ src: string }> = ({ src }) => {
  return (
    <AbsoluteFill>
      <Video
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
