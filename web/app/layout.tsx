import type { Metadata } from "next";
import { app } from "@/lib/config";
import "./globals.css";

export const metadata: Metadata = {
  title: app.site.title,
  description: app.site.subtitle,
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" className="dark">
      <body className="min-h-screen bg-ink-950 text-zinc-200 antialiased">{children}</body>
    </html>
  );
}
