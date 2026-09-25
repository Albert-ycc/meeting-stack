import type { KeyboardEvent as ReactKeyboardEvent } from "react";

/** 中文输入法用 Enter 确认候选时浏览器同样派发 keydown Enter（isComposing / keyCode 229），
 *  这种回车不能当成「提交」，否则会把没敲完的拼音提前提交并清空输入框。 */
export function isComposingKeydown(event: ReactKeyboardEvent): boolean {
  return event.nativeEvent.isComposing || event.keyCode === 229;
}
