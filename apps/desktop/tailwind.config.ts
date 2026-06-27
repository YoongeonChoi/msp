import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: "#111827",
        muted: "#6b7280",
        line: "#d8dee8",
        danger: "#b91c1c",
        safe: "#047857"
      }
    }
  },
  plugins: []
} satisfies Config;

