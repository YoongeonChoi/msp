import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  resolve: {
    dedupe: ["zod"]
  },
  build: {
    target: "es2022",
    modulePreload: { polyfill: false },
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "supabase-sdk",
              test: /node_modules[\\/]@supabase[\\/]/
            }
          ]
        }
      }
    }
  },
  server: {
    port: 1420,
    strictPort: false
  }
});

