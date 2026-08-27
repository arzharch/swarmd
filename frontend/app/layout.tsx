import type { Metadata } from "next";
import "./globals.css";

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
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
