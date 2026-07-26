import type { Config } from "tailwindcss";

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    screens: {
      sm: "640px",
      md: "720px",
      lg: "960px",
      xl: "1180px",
      "2xl": "1440px"
    },
    extend: {
      colors: {
        canvas: "#020101",
        surface: "#0f1113",
        surfaceRaised: "#15181b",
        ink: "#f4f5f5",
        muted: "#7d8186",
        mutedStrong: "#a3a6aa",
        lineSubtle: "#202326",
        line: "#292c2f",
        controlLine: "#59616a",
        primary: "#5b8fce",
        primaryAction: "#3c74c0",
        primarySoft: "#122033",
        danger: "#e2757b",
        dangerSoft: "#2c1518",
        warning: "#e0ad68",
        warningSoft: "#2b2113",
        success: "#6ecdb0",
        successSoft: "#10271f",
        safe: "#6ecdb0"
      },
      borderRadius: {
        sm: "var(--radius-micro)",
        md: "var(--radius-sm)",
        lg: "var(--radius-md)",
        xl: "var(--radius-lg)",
        "2xl": "14px"
      },
      fontFamily: {
        sans: [
          '"Pretendard Variable"',
          '"Pretendard"',
          "-apple-system",
          "BlinkMacSystemFont",
          '"Segoe UI"',
          '"Noto Sans KR"',
          '"Apple SD Gothic Neo"',
          '"Malgun Gothic"',
          "ui-sans-serif",
          "system-ui",
          "sans-serif"
        ]
      },
      fontSize: {
        xs: ["13px", { lineHeight: "18px" }],
        base: ["15px", { lineHeight: "24px" }],
        lg: ["16px", { lineHeight: "26px" }],
        meta: ["13px", { lineHeight: "18px" }],
        body: ["15px", { lineHeight: "24px" }],
        reading: ["16px", { lineHeight: "26px" }]
      },
      minHeight: {
        control: "44px"
      },
      minWidth: {
        control: "44px"
      },
      transitionDuration: {
        press: "120ms",
        state: "160ms",
        reveal: "180ms",
        surface: "220ms"
      },
      transitionTimingFunction: {
        product: "cubic-bezier(0.2, 0, 0, 1)"
      }
    }
  },
  plugins: []
} satisfies Config;

