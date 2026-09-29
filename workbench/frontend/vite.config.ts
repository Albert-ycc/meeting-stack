import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8765",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test-setup.ts",
    css: true,
    // 用例里的时间按 +08:00 写，界面按本机时区显示日期；钉住时区，别的时区的机器上跑也不差一天
    env: { TZ: "Asia/Shanghai" },
  },
});
