import type { NextConfig } from "next";

const config: NextConfig = {
  reactStrictMode: true,
  // 密码页拦的是人，不是机器人 —— 但把这两条也带上，成本为零（§10.5）。
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "X-Robots-Tag", value: "noindex, nofollow" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "X-Content-Type-Options", value: "nosniff" },
        ],
      },
    ];
  },
};

export default config;
