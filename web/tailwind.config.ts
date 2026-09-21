import type { Config } from "tailwindcss";

// §10.4：深色优先、单一强调色、方向色不用红绿裸配。
export default {
  // `lib/` 也要扫：那里已经有「给 class 用」的判定（`format.ts` 的 `direction()`），
  // 而 `globals.css` 的 `@layer components` 里的规则**要在 content 里扫到类名才会输出**。
  // 把行类名的判断挪进 `lib/` 是个很自然的重构，扫不到就会静默少掉一条规则。
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}", "./lib/**/*.{ts,tsx}"],
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
