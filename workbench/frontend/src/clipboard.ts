/**
 * 复制文本。navigator.clipboard 只在安全上下文（https 或 localhost）里有；手机经 http://<局域网 IP>
 * 访问时它是 undefined，这时退回到隐藏 textarea + execCommand("copy")，两条路都失败才算复制失败。
 */
export async function copyText(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // 权限被拒等情况，继续走兜底。
    }
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.top = "-1000px";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  textarea.select();
  let copied = false;
  try {
    copied = typeof document.execCommand === "function" && document.execCommand("copy");
  } finally {
    textarea.remove();
    previousFocus?.focus();
  }
  if (!copied) throw new Error("clipboard unavailable");
}
