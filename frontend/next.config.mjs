/** @type {import('next').NextConfig} */
const nextConfig = {
  // Standalone output: the container ships only the server and the traced
  // dependencies rather than the whole node_modules tree. Smaller image, less
  // to patch, and nothing in it that the app does not actually import.
  output: "standalone",
  reactStrictMode: true,
  // The control plane is same-origin through the Ingress in deployment. In
  // development it runs on :8000, so proxy rather than hardcoding a host into
  // the client bundle -- an absolute backend URL baked at build time breaks
  // every environment that is not the one it was built for.
  async rewrites() {
    const backend = process.env.SWARMD_API ?? "http://127.0.0.1:8000";
    return [{ source: "/api/:path*", destination: `${backend}/api/:path*` }];
  },
};
export default nextConfig;
