import React from "react";
import { AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig } from "remotion";

export const TextOverlay: React.FC<{ text: string }> = ({ text }) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const scale = spring({ frame, fps, config: { damping: 12 } });
  const opacity = interpolate(frame, [0, 10], [0, 1], { extrapolateRight: "clamp" });

  // Anchored near the bottom (typical short-form subtitle position) with a
  // safe margin so captions don't collide with TikTok/Instagram/YouTube UI.
  return (
    <AbsoluteFill
      style={{
        justifyContent: "flex-end",
        alignItems: "center",
        paddingLeft: 60,
        paddingRight: 60,
        paddingBottom: 260,  // ~13% from bottom — above platform UI overlays
      }}
    >
      <div
        style={{
          transform: `scale(${scale})`,
          opacity,
          fontSize: 96,
          fontWeight: 900,
          color: "white",
          textAlign: "center",
          lineHeight: 1.05,
          letterSpacing: -1.5,
          textShadow: "0 6px 24px rgba(0,0,0,0.85), 0 2px 6px rgba(0,0,0,0.9)",
          fontFamily: "system-ui, -apple-system, sans-serif",
          textTransform: "uppercase",
        }}
      >
        {text}
      </div>
    </AbsoluteFill>
  );
};
