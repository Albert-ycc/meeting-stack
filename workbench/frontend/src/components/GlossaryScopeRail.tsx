import { useState, type ReactNode } from "react";

import type { GlossaryScope } from "../types";
import { ALL_KEY, chipKey } from "./glossaryModel";
import { IconAll, IconCheck, IconChevron, IconGlobe, IconTray } from "./glossaryUi";

interface ScopeRailProps {
  /** 范围列表：公共在最前，再是各项目，最后是旧分组桶（已按显示顺序排好） */
  chips: GlossaryScope[];
  totalTerms: number;
  activeChipKey: string;
  /** 术语库页签、且不在收件箱里，范围才有选中态 */
  scopeActive: boolean;
  inbox: { available: boolean; total: number; active: boolean };
  suggestions: { count: number; active: boolean };
  /** 某个项目还有几个待认词 */
  pendingIn: (projectId: string) => number;
  onInbox: () => void;
  onSuggestions: () => void;
  onScope: (key: string) => void;
  /** 旧分组整理提示，放在最底下 */
  legacy: (withHeading: boolean) => ReactNode;
}

interface ItemProps {
  label: string;
  count?: number | string;
  icon?: ReactNode;
  dot?: string | null;
  hollow?: boolean;
  current: boolean;
  extra?: ReactNode;
  onClick: () => void;
}

function RailItem({ label, count, icon, dot, hollow, current, extra, onClick }: ItemProps) {
  return (
    <button aria-current={current} className="gw-si" onClick={onClick} type="button">
      {icon}
      {(dot || hollow) && (
        <i
          aria-hidden="true"
          className={`gw-dot${hollow ? " gw-dot--hollow" : ""}`}
          style={dot ? { background: dot } : undefined}
        />
      )}
      <span className="gw-si__lbl">{label}</span>
      {extra}
      {count !== undefined && <span className="gw-si__n">{count}</span>}
    </button>
  );
}

/** 左栏：先是等你处理的两件事，再是术语库的各个范围。 */
export function GlossaryScopeRail({
  chips, totalTerms, activeChipKey, scopeActive, inbox, suggestions, pendingIn, onInbox, onSuggestions, onScope, legacy,
}: ScopeRailProps) {
  const [emptyOpen, setEmptyOpen] = useState(false);
  const general = chips.find((chip) => chip.kind === "general");
  const projectChips = chips.filter((chip) => chip.kind === "project");
  const filled = projectChips.filter((chip) => chip.count > 0);
  const empty = projectChips.filter((chip) => chip.count === 0);
  const buckets = chips.filter((chip) => chip.kind === "bucket");
  const isCurrent = (key: string) => scopeActive && activeChipKey === key;
  // 正在看的就是一个空项目时，折叠区自己展开，选中项才看得见
  const emptyShown = emptyOpen || empty.some((chip) => isCurrent(chipKey(chip)));

  return (
    <nav aria-label="范围" className="gw-col gw-scopes">
      <div className="gw-sec">等你处理</div>
      {inbox.available && (
        <RailItem
          count={inbox.total > 0 ? undefined : 0}
          current={inbox.active}
          extra={inbox.total > 0 ? <span className="gw-cnt gw-cnt--hot">{inbox.total}</span> : undefined}
          icon={<IconTray />}
          label="待认词"
          onClick={onInbox}
        />
      )}
      <RailItem
        count={suggestions.count}
        current={suggestions.active}
        icon={<IconCheck />}
        label="待确认"
        onClick={onSuggestions}
      />

      <div className="gw-sec">术语库</div>
      <RailItem count={totalTerms} current={isCurrent(ALL_KEY)} icon={<IconAll />} label="全部" onClick={() => onScope(ALL_KEY)} />
      {general && (
        <RailItem
          count={general.count}
          current={isCurrent(chipKey(general))}
          icon={<IconGlobe />}
          label={general.label}
          onClick={() => onScope(chipKey(general))}
        />
      )}
      {filled.map((chip) => (
        <RailItem
          count={chip.count}
          current={isCurrent(chipKey(chip))}
          dot={chip.color ?? "var(--muted-2)"}
          key={chipKey(chip)}
          label={chip.label}
          onClick={() => onScope(chipKey(chip))}
        />
      ))}
      {empty.length > 0 && (
        <>
          <button
            aria-expanded={emptyShown}
            className="gw-si gw-si--more"
            onClick={() => setEmptyOpen(!emptyShown)}
            type="button"
          >
            <IconChevron />
            <span className="gw-si__lbl">还没有词的项目</span>
            <span className="gw-si__n">{empty.length}</span>
          </button>
          {emptyShown &&
            empty.map((chip) => {
              const pending = inbox.available ? pendingIn(chip.key) : 0;
              return (
                <RailItem
                  count={chip.count}
                  current={isCurrent(chipKey(chip))}
                  dot={chip.color ?? "var(--muted-2)"}
                  extra={pending > 0 ? <em className="gw-pend" title={`${pending} 个待认词`}>待认 {pending}</em> : undefined}
                  key={chipKey(chip)}
                  label={chip.label}
                  onClick={() => onScope(chipKey(chip))}
                />
              );
            })}
        </>
      )}

      {buckets.length > 0 && (
        <>
          <div className="gw-sec">旧分组</div>
          {buckets.map((chip) => (
            <RailItem
              count={chip.count}
              current={isCurrent(chipKey(chip))}
              hollow
              key={chipKey(chip)}
              label={chip.label}
              onClick={() => onScope(chipKey(chip))}
            />
          ))}
        </>
      )}
      {legacy(buckets.length === 0)}
    </nav>
  );
}
