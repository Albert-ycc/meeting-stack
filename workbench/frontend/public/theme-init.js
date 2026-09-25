// 首帧前写入主题，避免浅色偏好的用户先看到一闪深色。逻辑与 src/theme.ts 保持一致。
// 走外链脚本而不是内联：服务端 CSP 是 script-src 'self'，内联脚本会被拦。
(function () {
  var preference = "system";
  try {
    var stored = window.localStorage.getItem("meeting-workbench:theme");
    if (stored === "dark" || stored === "light" || stored === "system") preference = stored;
  } catch (error) {
    // 隐私模式等场景读不到存储，按跟随系统处理。
  }
  var resolved = preference;
  if (preference === "system") {
    resolved =
      window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  document.documentElement.dataset.theme = resolved;
})();
