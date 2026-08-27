import type { Metadata } from "next";
import { Albert_Sans } from "next/font/google";
import "./globals.css";

// OpenDesign's interface typeface. Self-hosted by next/font at build time
// rather than linked from Google's CDN: the dashboard is deployed behind an
// allowlist with a restricted egress policy, and a page that needs a
// third-party host to render correctly would break there.
const albertSans = Albert_Sans({
  subsets: ["latin"],
  display: "swap",
  variable: "--font-albert-sans",
});

export const metadata: Metadata = {
  title: "swarmd",
  description:
    "Live view of a swarm run: criterion, plan, agents, reasoning, cost.",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="en" className={albertSans.variable} suppressHydrationWarning>
      <head>
        {/*
          Applied before first paint so a reload does not flash the wrong
          palette. Inline rather than in an effect because an effect runs after
          hydration, which is exactly one frame too late to prevent the flash.
        */}
        <script
          dangerouslySetInnerHTML={{
            __html: `(function(){try{var t=localStorage.getItem('swarmd-theme');if(t&&t!=='system')document.documentElement.setAttribute('data-theme',t);}catch(e){}})();`,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
