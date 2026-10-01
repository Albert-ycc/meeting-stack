import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../api";
import type { RequirementContext } from "../../types";
import { installClipboard, uninstallClipboard, writtenText } from "./clipboardStub";
import { ClipboardBlockedError, copiedMessage, copyFailureReason, copyRequirementBackground } from "./requirementCopy";

/* 背景里的原话取自京东科研仓对接那场会的逐字稿（00:13:45，825270 ms），和 poolFixtures 同一份 */
const MARKDOWN =
  "# 京东科研仓对接（医米科研用药 · 需求 · P0 · 进行中）\n\n" +
  "## 来源原话\n\n- 00:13:45 就是这个入库单的这个单据，你得需要从你们的一米这个系统里面给我们这个库房推过来。\n";

function context(overrides: Partial<RequirementContext> = {}): RequirementContext {
  return {
    markdown: MARKDOWN,
    paths: ["/Volumes/资料盘/会议纪要与录音/260916 医米京东科研仓系统对接", "/Volumes/资料盘/医朵云/医米科研用药/对接京东科研仓"],
    cards_missing: 0,
    ...overrides,
  };
}

/** 一个手动兑现的 Promise：用例里控制「背景什么时候才取到」 */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

afterEach(() => uninstallClipboard());

describe("copiedMessage（海报「接下」和详情「复制给 Claude Code」同一句）", () => {
  it("已复制需求背景和 N 个文件路径，去 Claude Code 粘贴；N 是背景里带回来的路径数", () => {
    expect(copiedMessage(context())).toBe("已复制需求背景和 2 个文件路径，去 Claude Code 粘贴");
    expect(copiedMessage(context({ paths: ["/a"] }))).toBe("已复制需求背景和 1 个文件路径，去 Claude Code 粘贴");
  });

  it("一个路径都没有时不说「0 个文件路径」", () => {
    expect(copiedMessage(context({ paths: [] }))).toBe("已复制需求背景，去 Claude Code 粘贴");
  });

  it("有会的纪要不在项目文件夹里：后面多带一句说明", () => {
    expect(copiedMessage(context({ cards_missing: 2 }))).toBe(
      "已复制需求背景和 2 个文件路径，去 Claude Code 粘贴；有 2 场会的纪要不在项目文件夹里，带的是归档文件夹",
    );
  });
});

describe("copyRequirementBackground", () => {
  it("在调用的当下同步写剪贴板：背景还没取到，write 已经调了，内容是一个还没兑现的 Promise（WebKit 的手势限制）", async () => {
    const stubs = installClipboard();
    const loading = deferred<RequirementContext>();
    const load = vi.fn(() => loading.promise);

    const copying = copyRequirementBackground(load);
    // 没有 await：同步调用返回时，接口已经发出、剪贴板已经开写
    expect(load).toHaveBeenCalledTimes(1);
    expect(stubs.write).toHaveBeenCalledTimes(1);
    const [items] = stubs.write.mock.calls[0];
    expect(Object.keys(items[0].items)).toEqual(["text/plain"]);
    expect(typeof items[0].items["text/plain"].then).toBe("function");

    loading.resolve(context());
    const copied = await copying;
    expect(copied.paths).toHaveLength(2);
    expect(await writtenText(stubs.write)).toBe(MARKDOWN);
    expect(stubs.writeText).not.toHaveBeenCalled();
  });

  it("浏览器没有 ClipboardItem（旧版本）：等背景到了再用 writeText 复制", async () => {
    const stubs = installClipboard({}, false);

    await copyRequirementBackground(() => Promise.resolve(context()));
    expect(stubs.write).not.toHaveBeenCalled();
    expect(stubs.writeText).toHaveBeenCalledWith(MARKDOWN);
  });

  it("write 被拒（权限、失焦）：背景取到后退回 writeText 再试一次，这条路通就算复制成功", async () => {
    const stubs = installClipboard({ write: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")) });

    const copied = await copyRequirementBackground(() => Promise.resolve(context()));
    expect(copied.markdown).toBe(MARKDOWN);
    expect(stubs.writeText).toHaveBeenCalledWith(MARKDOWN);
  });

  it("两条路都不通：抛 ClipboardBlockedError，话里给出路（到需求详情里点「复制给 Claude Code」）", async () => {
    installClipboard({
      write: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")),
      writeText: vi.fn().mockRejectedValue(new DOMException("denied", "NotAllowedError")),
    });

    const failure = await copyRequirementBackground(() => Promise.resolve(context())).catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ClipboardBlockedError);
    expect(copyFailureReason(failure)).toBe("浏览器不让写剪贴板，到需求详情里点「复制给 Claude Code」再试");
  });

  it("取背景失败：抛接口的错、原话保留，不去碰剪贴板的备用路径", async () => {
    const stubs = installClipboard();
    const failure = await copyRequirementBackground(() =>
      Promise.reject(new ApiError("需求不存在：requirement-jd", 404, { detail: "需求不存在：requirement-jd" })),
    ).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    expect(copyFailureReason(failure)).toBe("需求不存在：requirement-jd");
    expect(stubs.writeText).not.toHaveBeenCalled();
  });

  it("取背景时断网（不是接口给的错）：统一说读取失败，不把英文报错露给人", async () => {
    installClipboard();
    const failure = await copyRequirementBackground(() => Promise.reject(new TypeError("Failed to fetch"))).catch(
      (error: unknown) => error,
    );
    expect(copyFailureReason(failure)).toBe("读取需求背景失败，请稍后重试");
  });

  it("接口同步抛错（方法都没有）也走失败，不让调用方的点击处理函数炸掉", async () => {
    installClipboard();
    const failure = await copyRequirementBackground(() => {
      throw new TypeError("apiClient.requirementContext is not a function");
    }).catch((error: unknown) => error);
    expect(copyFailureReason(failure)).toBe("读取需求背景失败，请稍后重试");
  });

  it("背景的内容不对（没有 markdown）：不把 undefined 写进剪贴板", async () => {
    const stubs = installClipboard();
    const failure = await copyRequirementBackground(
      () => Promise.resolve({ paths: [], cards_missing: 0 } as unknown as RequirementContext),
    ).catch((error: unknown) => error);

    expect(copyFailureReason(failure)).toBe("读取需求背景失败，请稍后重试");
    expect(stubs.writeText).not.toHaveBeenCalled();
  });
});
