/** @type {import('next').NextConfig} */
const nextConfig = {
  // Standalone output: the container ships only the server and the traced
  // dependencies rather than the whole node_modules tree. Smaller image, less
  // to patch, and nothing in it the app does not actually import.
  output: "standalone",
  reactStrictMode: true,

  // DEVELOPMENT ONLY.
  //
  // Next.js evaluates rewrites at BUILD time and bakes the result into
  // routes-manifest.json, so a destination read from the environment is fixed
  // at the moment the image is built. Shipping that would hardcode one
  // backend URL into an image that is built once and promoted through every
  // environment -- the exact failure a CI guard in this repo tests for.
  //
  // In deployment there is no rewrite: the Ingress serves the dashboard and
  // /api from the same host, so the browser's same-origin request reaches the
  // control plane without the frontend knowing where it lives. This rewrite
  // only reproduces that arrangement locally, where the two run on separate
  // ports. The dashboard runs on 3001 (3000 is commonly taken) and the
  // control plane on 8000 unless SWARMD_API says otherwise.
  async rewrites() {
    if (process.env.NODE_ENV === "production") return [];
    const backend = process.env.SWARMD_API ?? "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};
export default nextConfig;
