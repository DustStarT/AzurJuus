import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:4173",
      "/resources": "http://127.0.0.1:4173",
      "/ws": { target: "ws://127.0.0.1:4173", ws: true },
    },
  },
  build: { target: "es2022", sourcemap: true, chunkSizeWarningLimit: 600 },
});
