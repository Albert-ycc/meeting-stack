import { createContext, useContext } from "react";

import type { BootstrapPayload } from "../../types";

/** 第四期的开关，从 bootstrap 来；为 null 时是旧后台，第四期的控件一律不画 */
export interface LinksFlags {
  linksEnabled: boolean;
  semanticEnabled: boolean;
  llmConfigured: boolean;
}

export const LinksFlagsContext = createContext<LinksFlags | null>(null);

export function useLinksFlags(): LinksFlags | null {
  return useContext(LinksFlagsContext);
}

/** bootstrap 里的 links_enabled 是布尔值才算新后台 */
export function linksFlagsFrom(boot: Partial<BootstrapPayload>): LinksFlags | null {
  if (typeof boot.links_enabled !== "boolean") return null;
  return {
    linksEnabled: boot.links_enabled,
    semanticEnabled: Boolean(boot.semantic_enabled),
    llmConfigured: Boolean(boot.llm_configured),
  };
}
