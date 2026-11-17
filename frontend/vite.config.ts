import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const { BACKEND_ORIGIN } = loadEnv(mode, process.cwd(), "BACKEND_");
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: BACKEND_ORIGIN
        ? {
            "/api": {
              target: BACKEND_ORIGIN,
              changeOrigin: true,
              rewrite: (path) => path.replace(/^\/api/, ""),
            },
          }
        : undefined,
    },
  };
});
