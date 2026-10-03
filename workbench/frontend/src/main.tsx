import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import "./pageStyles";
import App from "./App";
import { preloadPages } from "./pages";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

// 第一屏画出来以后，才在后台把其余页面的代码拉下来，不跟第一屏抢带宽；拉完以后点到哪一页都是同步渲染，不闪加载占位
requestAnimationFrame(() => setTimeout(() => void preloadPages(), 0));
