/*
 * 波形相关用例共用的替身：峰值接口（/api/media/<id>/peaks）按录音 id 返回、或者失败。
 * 柱子的高低不影响任何断言，只有时长要对（时间签的位置、坐标轴的终点都按它算）。
 */
import { vi } from "vitest";

export function peaksFor(durationMs: number, bars = 600) {
  return {
    duration_seconds: durationMs / 1000,
    peaks: Array.from({ length: bars }, (_, index) => 0.2 + 0.6 * Math.abs(Math.sin(index / 7))),
  };
}

/**
 * 替换全局 fetch：登记了时长（毫秒）的录音返回峰值，"fail" 和没登记的都回 404，"hang" 一直不回（还在取）。
 * 用例结束时要 vi.unstubAllGlobals()，并清掉 PosterWaveform 的峰值缓存（clearPeaksCache）。
 */
export function stubPeaksFetch(byArtifact: Record<number, number | "fail" | "hang">) {
  const fetchMock = vi.fn((input: RequestInfo | URL) => {
    const match = String(input).match(/\/api\/media\/(\d+)\/peaks/);
    const spec = match ? byArtifact[Number(match[1])] : undefined;
    if (spec === "hang") return new Promise<Response>(() => undefined);
    const headers = { "content-type": "application/json" };
    if (spec === undefined || spec === "fail") {
      return Promise.resolve(new Response(JSON.stringify({ detail: "not found" }), { status: 404, headers }));
    }
    return Promise.resolve(new Response(JSON.stringify(peaksFor(spec)), { status: 200, headers }));
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
