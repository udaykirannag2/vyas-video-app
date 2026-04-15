import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export const TextOverlay: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const scale = spring({ frame, fps, config: { damping: 12 } });
  const opacity = interpolate(frame, [0, 10], [0, 1], { extrapolateRight: "clamp" });

  return (
    <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", padding: 80 }}>
      <div
        style={{
          transform: `scale(${scale})`,
          opacity,
          fontSize: 128,
          fontWeight: 900,
          color: "white",
          textAlign: "center",
          lineHeight: 1.05,
          letterSpacing: -2,
          textShadow: "0 8px 32px rgba(0,0,0,0.7)",
          fontFamily: "system-ui, -apple-system, sans-serif",
          textTransform: "uppercase",
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};
