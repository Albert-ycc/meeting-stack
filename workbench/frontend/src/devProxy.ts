/*
 * 开发服务器（npm run dev）的 /api 代理指向哪份后端，只给 vite.config.ts 用，应用代码不引用它。
 * 起一份隔离数据目录的后端、再让代理连上去的办法见 workbench/README.md 的「前端开发」。
 */

/** 生产工作台的端口：开发时连它，页面上读到的就是生产数据 */
const PRODUCTION_PORT = 8765;
/** 没设环境变量时代理指向的端口：平时没有服务监听，忘了起隔离后端只会请求失败，不会误碰生产 */
const DEV_PORT = 8865;

export function devBackend(env: Record<string, string | undefined>): { target: string; warning: string | null } {
  const raw = env.MEETING_WORKBENCH_PORT?.trim();
  // 和后端读同一个环境变量名，同一组变量起前后端就对得上
  const port = raw ? Number(raw) : DEV_PORT;
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error(`MEETING_WORKBENCH_PORT 不是有效的端口号：${raw}`);
  }
  return {
    target: `http://127.0.0.1:${port}`,
    // 按数字比：08765 在网址里照样是 8765
    warning:
      port === PRODUCTION_PORT
        ? `\u001b[1;31m[注意] 开发代理指向 127.0.0.1:${PRODUCTION_PORT}，这是生产工作台：页面上读到的是生产数据。` +
          `写操作眼下会被后端的跨源校验拒掉，别指望它兜底。要连隔离的后端，换个端口再起，` +
          `如 MEETING_WORKBENCH_PORT=${DEV_PORT} npm run dev（见 workbench/README.md「前端开发」）\u001b[0m`
        : null,
  };
}
