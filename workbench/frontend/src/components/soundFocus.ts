/**
 * 同一时刻只放一个声音（3e）：关系图的迷你播放器、预览抽屉、会议页的播放器在开始放时认领，
 * 之前在放的那个自动暂停。
 */
let current: HTMLMediaElement | null = null;

export function claimSound(element: HTMLMediaElement): void {
  if (current && current !== element && !current.paused) current.pause();
  current = element;
}
