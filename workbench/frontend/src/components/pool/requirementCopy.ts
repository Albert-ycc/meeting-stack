import { ApiError } from "../../api";
import { copyText } from "../../clipboard";
import type { RequirementContext } from "../../types";

/**
 * 海报上的「接下」和需求详情的「复制给 Claude Code」是同一个动作（R02-9、R05-5）：
 * 把需求背景复制出去，轻提示是同一句。没有任何文件路径时不说「0 个」。
 * 有会的纪要不在项目文件夹里时多带一句，让人知道粘出去的是归档文件夹里的纪要。
 */
export function copiedMessage(
  context: Pick<RequirementContext, "paths" | "markdown_paths" | "cards_missing">,
): string {
  // 数正文里真写进去的路径：paths 还带着有卡片的会的归档文件夹，正文里没写（第二轮审查一般-2）
  const pathCount = (context.markdown_paths ?? context.paths)?.length ?? 0;
  const cardsMissing = context.cards_missing ?? 0;
  const head = pathCount > 0 ? `已复制需求背景和 ${pathCount} 个文件路径` : "已复制需求背景";
  const tail = cardsMissing > 0 ? `；有 ${cardsMissing} 场会的纪要不在项目文件夹里，带的是归档文件夹` : "";
  return `${head}，去 Claude Code 粘贴${tail}`;
}

/** 剪贴板两条路都不通（权限被拒、不是安全上下文）：给个能照着做的出路 */
export class ClipboardBlockedError extends Error {
  constructor() {
    super("浏览器不让写剪贴板，到需求详情里点「复制给 Claude Code」再试");
    this.name = "ClipboardBlockedError";
  }
}

/** 轻提示「没复制成功：……」后面的原因：接口的错照原话，剪贴板的错给出路，其余统一说读取失败 */
export function copyFailureReason(error: unknown): string {
  if (error instanceof ClipboardBlockedError || error instanceof ApiError) return error.message;
  return "读取需求背景失败，请稍后重试";
}

/**
 * 在用户点击的手势里把需求背景写进剪贴板，背景还没取到也行。必须在点击处理函数里同步调用。
 *
 * WebKit 只认手势里同步发起的那一次写入，所以这里同步调 clipboard.write，内容交一个 Promise，取到了才兑现。
 * 这样每次点都拿最新的背景：不靠悬停预取（键盘、触屏没有悬停，预取的内容还会过期）。
 * 浏览器没有 ClipboardItem、或这条路被拒时，等背景到了再走 copyText（writeText，再退到 execCommand）；
 * 两条路都不通才抛 ClipboardBlockedError。取背景本身失败时抛接口的错，不当成剪贴板的错。
 */
export async function copyRequirementBackground(
  load: () => Promise<RequirementContext>,
): Promise<RequirementContext> {
  const loading = load();
  const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
  if (clipboard?.write && typeof ClipboardItem !== "undefined") {
    try {
      const blob = loading.then((context) => {
        if (typeof context?.markdown !== "string") throw new Error("需求背景的内容不对");
        return new Blob([context.markdown], { type: "text/plain" });
      });
      // write 同步抛错时没人接这个 Promise 的失败，先挂一个空的接住；真正的失败由下面的 await 报
      blob.catch(() => undefined);
      await clipboard.write([new ClipboardItem({ "text/plain": blob })]);
      return await loading;
    } catch {
      // 落到下面：背景取不到就抛它的错；取到了只是这条路被拒，换 copyText 再试一次
    }
  }
  const context = await loading;
  if (typeof context?.markdown !== "string") throw new Error("需求背景的内容不对");
  try {
    await copyText(context.markdown);
  } catch {
    throw new ClipboardBlockedError();
  }
  return context;
}
