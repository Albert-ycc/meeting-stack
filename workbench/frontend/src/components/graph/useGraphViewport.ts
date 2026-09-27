/*
 * 关系图画布的平移缩放（d3-zoom），项目图和全部项目概览共用这一份：
 * 拖空白处平移；触控板双指滑动（普通滚轮）平移，捏合或 Ctrl/⌘ 滚动缩放；双击不缩放；
 * 视角按 viewKey 记住，进对象页再回来还在；没记住时第一次打开把 initialBounds 放进可见区。
 */
import { useReducedMotion } from "framer-motion";
import { select } from "d3-selection";
import "d3-transition";
import { zoom, zoomIdentity, type ZoomBehavior, type ZoomTransform } from "d3-zoom";
import { useCallback, useEffect, useRef, useState, type RefObject } from "react";

import type { Box } from "./layout";

export interface ViewTransform {
  x: number;
  y: number;
  k: number;
}

export const VIEWPORT_MIN_ZOOM = 0.3;
export const VIEWPORT_MAX_ZOOM = 2.5;
/** 适配时最多放大到这么多，免得节点少时字大得离谱 */
const FIT_MAX_ZOOM = 1.25;
const FIT_PAD = 32;

const savedViews = new Map<string, ViewTransform>();

/** 测试之间清空记住的视角 */
export function forgetViewportViews() {
  savedViews.clear();
}

export interface GraphViewportOptions {
  /** 视角按这个键记住；换键时重新挂平移缩放 */
  viewKey: string;
  /** 没记住视角时，第一次打开放进可见区的范围 */
  initialBounds: Box;
  /** 返回 true 时在这个元素上按下不平移（比如节点上的按钮、能拖的节点）；读的是最新的一份 */
  ignorePointer?: (target: HTMLElement) => boolean;
  /** 适配时不缩到比这更小（默认是缩放下限）：概览要保证项目名读得清 */
  minFitZoom?: number;
}

export interface GraphViewport {
  /** 挂在视口元素上 */
  viewportRef: RefObject<HTMLDivElement | null>;
  /** 当前视角：世界坐标 × k + (x, y) = 视口里的像素 */
  transform: ViewTransform;
  /** 事件处理里读最新的视角 */
  transformRef: RefObject<ViewTransform>;
  /** 把一个范围放进可见区（「0」键、复位按钮） */
  fitView: (box: Box, animate?: boolean) => void;
  /** 以视口中心缩放（「+」「-」键、缩放按钮） */
  zoomBy: (factor: number) => void;
  /** 让一个框露在可见区里，右边留出 rightInset 像素（面板挡着的地方）；已经看得见就不动 */
  reveal: (box: Box, rightInset?: number) => void;
  /** 屏幕坐标换成世界坐标 */
  toWorld: (clientX: number, clientY: number) => { x: number; y: number };
}

export function useGraphViewport({
  viewKey,
  initialBounds,
  ignorePointer,
  minFitZoom = VIEWPORT_MIN_ZOOM,
}: GraphViewportOptions): GraphViewport {
  const viewportRef = useRef<HTMLDivElement>(null);
  const zoomRef = useRef<ZoomBehavior<HTMLDivElement, unknown> | null>(null);
  const [transform, setTransform] = useState<ViewTransform>(() => savedViews.get(viewKey) ?? { x: 500, y: 320, k: 1 });
  const transformRef = useRef(transform);
  transformRef.current = transform;
  const ignoreRef = useRef(ignorePointer);
  ignoreRef.current = ignorePointer;
  const reduceMotion = useReducedMotion();

  const fitView = useCallback(
    (box: Box, animate = false) => {
      const viewport = viewportRef.current;
      const behaviour = zoomRef.current;
      if (!viewport || !behaviour) return;
      const width = viewport.clientWidth || 1000;
      const height = viewport.clientHeight || 600;
      const k = Math.max(
        minFitZoom,
        Math.min(FIT_MAX_ZOOM, (width - FIT_PAD * 2) / Math.max(box.w, 1), (height - FIT_PAD * 2) / Math.max(box.h, 1)),
      );
      const x = width / 2 - (box.x + box.w / 2) * k;
      const y = height / 2 - (box.y + box.h / 2) * k;
      const target = zoomIdentity.translate(x, y).scale(k);
      const selection = select(viewport);
      if (animate && !reduceMotion) selection.transition().duration(260).call(behaviour.transform, target);
      else selection.call(behaviour.transform, target);
    },
    [minFitZoom, reduceMotion],
  );

  // 只在换 viewKey 时重新挂；布局变化不重置视角
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const behaviour = zoom<HTMLDivElement, unknown>()
      .scaleExtent([VIEWPORT_MIN_ZOOM, VIEWPORT_MAX_ZOOM])
      .clickDistance(6)
      .filter((event: Event) => {
        if (event.type === "wheel" || event.type === "dblclick") return false;
        // 程序派发的鼠标事件没有 view，d3 拖拽要用它找 document
        if (!(event as UIEvent).view) return false;
        const target = event.target as HTMLElement | null;
        if (target && ignoreRef.current?.(target)) return false;
        return !(event as MouseEvent).button;
      })
      .on("zoom", (event: { transform: ZoomTransform }) => {
        const next = { x: event.transform.x, y: event.transform.y, k: event.transform.k };
        savedViews.set(viewKey, next);
        setTransform(next);
      });
    zoomRef.current = behaviour;
    const selection = select(viewport);
    selection.call(behaviour);
    const saved = savedViews.get(viewKey);
    if (saved) selection.call(behaviour.transform, zoomIdentity.translate(saved.x, saved.y).scale(saved.k));
    else fitView(initialBounds);
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      if (event.ctrlKey || event.metaKey) {
        const factor = Math.pow(2, -event.deltaY * 0.01);
        const rect = viewport.getBoundingClientRect();
        behaviour.scaleBy(selection, factor, [event.clientX - rect.left, event.clientY - rect.top]);
      } else {
        const current = savedViews.get(viewKey) ?? { k: 1 };
        behaviour.translateBy(selection, -event.deltaX / current.k, -event.deltaY / current.k);
      }
    };
    viewport.addEventListener("wheel", onWheel, { passive: false });
    return () => {
      viewport.removeEventListener("wheel", onWheel);
      selection.on(".zoom", null);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewKey]);

  const zoomBy = useCallback((factor: number) => {
    const viewport = viewportRef.current;
    const behaviour = zoomRef.current;
    if (!viewport || !behaviour) return;
    behaviour.scaleBy(select(viewport), factor);
  }, []);

  const reveal = useCallback(
    (box: Box, rightInset = 0) => {
      const viewport = viewportRef.current;
      const behaviour = zoomRef.current;
      if (!viewport || !behaviour) return;
      const width = viewport.clientWidth;
      const height = viewport.clientHeight;
      if (!width || !height) return;
      const t = transformRef.current;
      const margin = 24;
      const right = width - rightInset - margin;
      const left = box.x * t.k + t.x;
      const top = box.y * t.k + t.y;
      const boxRight = left + box.w * t.k;
      const bottom = top + box.h * t.k;
      let dx = 0;
      let dy = 0;
      if (boxRight > right) dx = right - boxRight;
      if (left + dx < margin) dx = margin - left;
      if (bottom > height - margin) dy = height - margin - bottom;
      if (top + dy < margin) dy = margin - top;
      if (!dx && !dy) return;
      const selection = select(viewport);
      if (reduceMotion) behaviour.translateBy(selection, dx / t.k, dy / t.k);
      else selection.transition().duration(220).call(behaviour.translateBy, dx / t.k, dy / t.k);
    },
    [reduceMotion],
  );

  const toWorld = useCallback((clientX: number, clientY: number) => {
    const rect = viewportRef.current?.getBoundingClientRect();
    const t = transformRef.current;
    return { x: (clientX - (rect?.left ?? 0) - t.x) / t.k, y: (clientY - (rect?.top ?? 0) - t.y) / t.k };
  }, []);

  return { viewportRef, transform, transformRef, fitView, zoomBy, reveal, toWorld };
}
