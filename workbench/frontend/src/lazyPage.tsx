import { lazy, Suspense, type ComponentType } from "react";

import { AsyncState } from "./components/AsyncState";

type PageModule<P> = { default: ComponentType<P> };

/**
 * 按页拆块：页面的代码单独一个文件，用到时才加载。和直接用 React.lazy 的区别是页面已经拉到手的话，
 * lazy 当场就拿到它、渲染是同步的、不经过 Suspense：没有加载占位一闪而过，
 * 滚动恢复、后退恢复面对的还是整包时那种同步挂载。启动后 preloadPages 会在后台把所有页都拉下来，
 * 所以只有启动头一两秒里就点进去的页才会看到占位。
 *
 * LazyPage 外面的 Suspense 和里面的 lazy 组件位置一直不变：页面到了以后父组件再重渲染，页面不会被卸掉重建
 * （key 加在 LazyPage 上，换 key 照常重建）。
 */
export function lazyPage<P extends object>(load: () => Promise<ComponentType<P>>) {
  let loaded: ComponentType<P> | undefined;
  let pending: Promise<ComponentType<P>> | undefined;

  // React.lazy 只认 then：已经到手的页面交一个同步调用回调的 then，lazy 初始化时就地拿到页面
  const ready = (page: ComponentType<P>) =>
    ({ then: (resolve: (module: PageModule<P>) => void) => resolve({ default: page }) }) as unknown as Promise<PageModule<P>>;
  const wrap = () => lazy<ComponentType<P>>(() => (loaded ? ready(loaded) : preload().then((page) => ({ default: page }))));
  let Page = wrap();

  function preload() {
    pending ??= load().then(
      (page) => (loaded = page),
      (error: unknown) => {
        // React.lazy 记住失败就不再重试：换一个新的 lazy，下一次渲染（［重新载入］）才能重新加载。
        // 注意浏览器对同一个地址失败过的动态 import 在本页面里多半不会再取，彻底恢复要刷新页面
        pending = undefined;
        Page = wrap();
        throw error;
      },
    );
    return pending;
  }

  function LazyPage(props: P) {
    return (
      <Suspense fallback={<AsyncState state="loading" />}>
        <Page {...props} />
      </Suspense>
    );
  }

  return Object.assign(LazyPage, { preload });
}
