import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // Prevent bundling of native Node modules in server components
  serverExternalPackages: ["better-sqlite3"],
  output: "standalone",
};

export default nextConfig;
