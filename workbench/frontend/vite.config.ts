import react from "@vitejs/plugin-react";
import { loadEnv } from "vite";
import { defineConfig } from "vitest/config";

import { devBackend } from "./src/devProxy";

export default defineConfig(({ command, mode }) => {
  // 代理目标读环境变量，没设时指向开发端口而不是生产的 8765（在 worktree 里 npm run dev 不会写进生产库）；
  // vitest 也会加载这份配置，那时不用打警告。用 loadEnv 读（进程环境变量加上 frontend/ 下的 .env 文件），
  // 配置文件不碰 process 全局，没有装 @types/node 也能过类型检查
  const backend = devBackend(loadEnv(mode, ".", "MEETING_WORKBENCH_"));
  if (backend.warning && command === "serve" && mode !== "test") console.warn(backend.warning);
  return {
    plugins: [react()],
    build: {
      // Vite 8 换成了 rolldown：rollupOptions.output.manualChunks 已标为弃用，按 codeSplitting.groups 写。
      // 第三方库几个月才换一次，应用代码每次发布都变：分开放，发布后只有应用那几块换文件名（内容哈希），
      // 库的几块名字不变，浏览器里缓存的那份还能用。按「谁跟谁一起变」拆：react 全家一块，framer-motion 一块，
      // d3 一块，wavesurfer 一块，剩下的第三方（目前就是 react-markdown 那一串）一块
      rolldownOptions: {
        output: {
          codeSplitting: {
            groups: [
              { name: "vendor-react", test: /node_modules[\\/](?:react|react-dom|scheduler)[\\/]/, priority: 40 },
              { name: "vendor-motion", test: /node_modules[\\/](?:framer-motion|motion-dom|motion-utils)[\\/]/, priority: 30 },
              { name: "vendor-d3", test: /node_modules[\\/]d3-[^\\/]+[\\/]/, priority: 30 },
              { name: "vendor-wavesurfer", test: /node_modules[\\/]wavesurfer\.js[\\/]/, priority: 30 },
              { name: "vendor", test: /node_modules[\\/]/, priority: 10 },
            ],
          },
        },
      },
    },
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
