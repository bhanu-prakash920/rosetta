import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the UI runs on :5173 and proxies the API, so the browser only
// ever talks to one origin and no CORS relaxation is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: process.env.ROSETTA_API ?? "http://127.0.0.1:8000", changeOrigin: false },
      "/metrics": { target: process.env.ROSETTA_API ?? "http://127.0.0.1:8000" },
    },
  },
  build: {
    target: "es2020",
    sourcemap: false,
    // Fonts stay files. Inlined data: URIs would need a looser content security policy.
    assetsInlineLimit: 0,
    chunkSizeWarningLimit: 700,
    rollupOptions: { output: { manualChunks: { react: ["react", "react-dom", "react-router-dom"], map: ["leaflet"] } } },
  },
});
