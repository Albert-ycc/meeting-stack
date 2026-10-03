import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState, type ComponentType } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { lazyPage } from "./lazyPage";

type PageProps = { name: string };

function Hello({ name }: PageProps) {
  return <p>你好 {name}</p>;
}

/** 手动放行的加载：模拟还在路上的代码块 */
function gate() {
  let resolve: (page: ComponentType<PageProps>) => void = () => undefined;
  const promise = new Promise<ComponentType<PageProps>>((ok) => {
    resolve = ok;
  });
  return { promise, resolve: (page: ComponentType<PageProps>) => resolve(page) };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("lazyPage", () => {
  it("还没拉到时显示「正在读取」占位，拉到了换成页面", async () => {
    const block = gate();
    const Page = lazyPage(() => block.promise);
    render(<Page name="甲" />);
    expect(screen.getByText("正在读取本地档案…")).toBeInTheDocument();
    expect(screen.queryByText("你好 甲")).not.toBeInTheDocument();

    await act(async () => block.resolve(Hello));
    expect(await screen.findByText("你好 甲")).toBeInTheDocument();
    expect(screen.queryByText("正在读取本地档案…")).not.toBeInTheDocument();
  });

  it("已经拉到的页面第一帧就是页面：同步渲染，占位一次都没出现", async () => {
    const Page = lazyPage(async () => Hello);
    await Page.preload();
    render(<Page name="乙" />);
    // 不 await：和整包时「渲染完就能查到」一样
    expect(screen.getByText("你好 乙")).toBeInTheDocument();
    expect(screen.queryByText("正在读取本地档案…")).not.toBeInTheDocument();
  });

  it("页面到了以后父组件再重渲染：页面不卸载重建，里面输入的字还在", async () => {
    const mounted = vi.fn();
    function Form({ name }: PageProps) {
      useEffect(() => {
        mounted();
      }, []);
      return <input aria-label={name} />;
    }
    const block = gate();
    const Page = lazyPage(() => block.promise);
    function Parent() {
      const [ticks, setTicks] = useState(0);
      return (
        <>
          <button onClick={() => setTicks((n) => n + 1)} type="button">
            父组件重渲染 {ticks}
          </button>
          <Page name="名称" />
        </>
      );
    }
    render(<Parent />);
    await act(async () => block.resolve(Form));
    const user = userEvent.setup();
    await user.type(await screen.findByLabelText("名称"), "草稿");

    await user.click(screen.getByRole("button", { name: /父组件重渲染/ }));
    await user.click(screen.getByRole("button", { name: /父组件重渲染/ }));

    expect(screen.getByLabelText("名称")).toHaveValue("草稿");
    expect(mounted).toHaveBeenCalledTimes(1);
  });

  it("preload 只发起一次加载", async () => {
    const load = vi.fn(async () => Hello);
    const Page = lazyPage(load);
    await Promise.all([Page.preload(), Page.preload()]);
    await Page.preload();
    expect(load).toHaveBeenCalledTimes(1);
  });

  it("加载失败：错误交给外层的错误边界；再渲染一次会重试，成功就显示页面", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const load = vi
      .fn<() => Promise<ComponentType<PageProps>>>()
      .mockRejectedValueOnce(new Error("Failed to fetch dynamically imported module"))
      .mockResolvedValueOnce(Hello);
    const Page = lazyPage(load);
    const view = (resetKey: string) => (
      <ErrorBoundary resetKey={resetKey}>
        <Page name="丙" />
      </ErrorBoundary>
    );
    const { rerender } = render(view("第一次"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to fetch dynamically imported module");

    rerender(view("第二次"));
    expect(await screen.findByText("你好 丙")).toBeInTheDocument();
    expect(load).toHaveBeenCalledTimes(2);
  });
});
