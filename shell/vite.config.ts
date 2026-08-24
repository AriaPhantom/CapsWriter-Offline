import { defineConfig } from "vite";
import { resolve } from "path";

// Tauri 期望固定端口，失败即退出而不是自动换端口
export default defineConfig({
  clearScreen: false,
  server: {
    port: 6021,
    strictPort: true,
  },
  build: {
    // WebView2 基于 Chromium，无需为旧浏览器降级
    target: "chrome110",
    sourcemap: false,
    rollupOptions: {
      input: {
        dashboard: resolve(__dirname, "index.html"),
        overlay: resolve(__dirname, "overlay.html"),
      },
    },
  },
});
