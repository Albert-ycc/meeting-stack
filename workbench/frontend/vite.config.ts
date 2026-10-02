import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

import { devBackend } from "./src/devProxy";

export default defineConfig(({ command, mode }) => {
  // 代理目标读环境变量，没设时指向开发端口而不是生产的 8765（在 worktree 里 npm run dev 不会写进生产库）；
  // vitest 也会加载这份配置，那时不用打警告
  const backend = devBackend(process.env);
  if (backend.warning && command === "serve" && mode !== "test") console.warn(backend.warning);
  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      port: 5173,
      proxy: {
        "/api": backend.target,
      },
    },
    test: {
      environment: "jsdom",
      setupFiles: "./src/test-setup.ts",
      css: true,
      // 用例里的时间按 +08:00 写，界面按本机时区显示日期；钉住时区，别的时区的机器上跑也不差一天
      env: { TZ: "Asia/Shanghai" },
    },
  };
});
