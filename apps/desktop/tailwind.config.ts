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
        canvas: "#f4f7fb",
        surface: "#ffffff",
        ink: "#191f28",
        muted: "#6b7684",
        mutedStrong: "#657180",
        line: "#d9e0ea",
        controlLine: "#8b95a1",
        primary: "#2563eb",
        primarySoft: "#eff6ff",
        danger: "#b42318",
        dangerSoft: "#fef3f2",
        warning: "#b54708",
        warningSoft: "#fffaeb",
        success: "#067647",
        successSoft: "#ecfdf3",
        safe: "#067647"
      },
      borderRadius: {
        md: "12px",
        lg: "16px",
        xl: "20px",
        "2xl": "20px"
      },
      fontFamily: {
        sans: [
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

