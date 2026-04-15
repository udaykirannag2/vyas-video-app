import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Vyas-Video",
  description: "Bhagavad Gita reels generator",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
