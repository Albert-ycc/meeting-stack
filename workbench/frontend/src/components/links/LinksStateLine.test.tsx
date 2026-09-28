import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";

import { ApiError, type LinksState } from "../../api";
import { LinksFlagsContext, linksFlagsFrom } from "./LinksFlagsContext";
import { LinksStateLine } from "./LinksStateLine";
import { OLD_BACKEND_TEXT } from "./useRelationAnswer";

const FLAGS = { linksEnabled: true, semanticEnabled: true, llmConfigured: true };

function withFlags(children: ReactNode) {
  return <LinksFlagsContext.Provider value={FLAGS}>{children}</LinksFlagsContext.Provider>;
}

const FAILED: LinksState = { kind: "stopped", text: "这场会的 AI 整理没做成", action: { kind: "retry", label: "现在重试" } };

describe("LinksStateLine", () => {
  it("一句话，最多一个按钮；不用 role=alert", () => {
    const { rerender } = render(
      withFlags(<LinksStateLine state={{ kind: "waiting", text: "会上换了叫法的文件还在整理", action: null }} />),
    );
    expect(screen.getByText("会上换了叫法的文件还在整理")).toBeInTheDocument();
    expect(screen.queryAllByRole("button")).toHaveLength(0);

    rerender(withFlags(<LinksStateLine apiClient={{ retryLinks: vi.fn() }} state={FAILED} />));
    expect(screen.getByText("这场会的 AI 整理没做成")).toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    // 好了、没有句子时什么都不写
    rerender(withFlags(<LinksStateLine state={{ kind: "ok", text: "", action: null }} />));
    expect(screen.queryByText(/./)).not.toBeInTheDocument();
  });

  it("［现在重试］调 retryLinks，之后宿主重取；按钮收起", async () => {
    const retryLinks = vi.fn(async () => ({ requeued: 3 }));
    const onRetried = vi.fn();
    render(withFlags(<LinksStateLine apiClient={{ retryLinks }} onRetried={onRetried} state={FAILED} />));
    await userEvent.click(screen.getByRole("button", { name: "现在重试" }));
    expect(retryLinks).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onRetried).toHaveBeenCalled());
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("重试撞上旧后台时写旧后台那句；别的按钮交给宿主", async () => {
    const retryLinks = vi.fn(async () => {
      throw new ApiError("Method Not Allowed", 405, { detail: "Method Not Allowed" });
    });
    const { rerender } = render(withFlags(<LinksStateLine apiClient={{ retryLinks }} state={FAILED} />));
    await userEvent.click(screen.getByRole("button", { name: "现在重试" }));
    expect(await screen.findByText(OLD_BACKEND_TEXT)).toBeInTheDocument();

    const onAction = vi.fn();
    const mount: LinksState = { kind: "stopped", text: "这个项目还没挂材料文件夹", action: { kind: "project", label: "去项目页" } };
    rerender(withFlags(<LinksStateLine onAction={onAction} state={mount} />));
    await userEvent.click(screen.getByRole("button", { name: "去项目页" }));
    expect(onAction).toHaveBeenCalledWith(mount.action);
  });

  it("旧后台（useLinksFlags 为 null）时不画；bootstrap 里 links_enabled 不是布尔值就是旧后台", () => {
    const { container } = render(<LinksStateLine state={FAILED} />);
    expect(container).toBeEmptyDOMElement();
    expect(linksFlagsFrom({ semantic_enabled: true })).toBeNull();
    expect(linksFlagsFrom({ links_enabled: false, semantic_enabled: true, llm_configured: false })).toEqual({
      linksEnabled: false,
      semanticEnabled: true,
      llmConfigured: false,
    });
  });
});
