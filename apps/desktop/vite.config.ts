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
    rollupOptions: {
      output: {
        manualChunks(id, { getModuleInfo }) {
          const isStaticEntryDependency = (moduleId: string, visited: Set<string>): boolean => {
            if (visited.has(moduleId)) {
              return false;
            }
            visited.add(moduleId);
            const info = getModuleInfo(moduleId);
            return info?.isEntry === true ||
              info?.importers.some((importer) => isStaticEntryDependency(importer, visited)) === true;
          };

          return isStaticEntryDependency(id, new Set()) ? "core-app" : "secondary-ui";
        }
      }
    }
  },
  server: {
    port: 1420,
    strictPort: false
  }
});

