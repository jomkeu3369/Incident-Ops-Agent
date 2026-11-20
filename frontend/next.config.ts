import type { NextConfig } from "next";

const supervisorUrl = process.env.SUPERVISOR_URL?.replace(/\/$/, "");

const config: NextConfig = {
  output: "standalone",
  agentRules: false,
  devIndicators: false,
  async rewrites() {
    if (!supervisorUrl) return [];
    return [
      {
        source: "/api/ag-ui/:path*",
        destination: `${supervisorUrl}/api/ag-ui/:path*`,
      },
      {
        source: "/api/ag-ui",
        destination: `${supervisorUrl}/api/ag-ui`,
      },
    ];
  },
};

export default config;
