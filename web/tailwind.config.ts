import type { Config } from "tailwindcss";

// §10.4：深色优先、单一强调色、方向色不用红绿裸配。
export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // 背景用近黑的中性灰，**不用纯黑** —— 纯黑上的细线会糊。
        ink: { 950: "#0b0d10", 900: "#12151a", 800: "#1a1f26", 700: "#252b34" },
        // 方向色：teal（涨）/ rose（跌），低饱和，对红绿色盲更友好。
        // 且**永不单靠颜色传达方向** —— 始终同时有符号或箭头。
        up: "#2dd4bf",
        down: "#fb7185",
        // 单一强调色，只用于「今日三强」这一个语义。
        accent: "#f5b93b",
      },
      fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"] },
    },
  },
  plugins: [],
} satisfies Config;
