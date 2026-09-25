import { useEffect, useMemo, useRef, useState } from "react";

import type { ApiClient } from "../api";
import type { Project, RequirementRef } from "../types";
import { PriorityBadge } from "./RequirementBadges";
import { RequirementModal } from "./RequirementModal";
import "./MeetingRequirementPicker.css";

interface MeetingRequirementPickerProps {
  apiClient: ApiClient;
  projects: Project[];
  /** 当前选中的主项目（可能还没保存）；空字符串＝未选，控件整体置灰 */
  projectId: string;
  /** 已勾选的需求，可能跨项目、也可能不是进行中（保留下来是为了能在下拉里取消勾选） */
  selected: RequirementRef[];
  onChange: (next: RequirementRef[]) => void;
  onOpenRequirement?: (requirementId: string) => void;
  disabled?: boolean;
}

/** 会议详情「归档归属」卡片里的关联需求控件：下拉里勾选，「确定」只把选择填进字段，
 * 真正落库跟随外层「保存归档归属」一起提交。自带字段标签（不能借用外层 <label>，
 * 那样浏览器会把内部第一个按钮当成 label 关联的控件，吞掉按钮自己的文案）。 */
export function MeetingRequirementPicker({
  apiClient,
  projects,
  projectId,
  selected,
  onChange,
  onOpenRequirement,
  disabled = false,
}: MeetingRequirementPickerProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [options, setOptions] = useState<RequirementRef[]>([]);
  const [loading, setLoading] = useState(false);
  const [draft, setDraft] = useState<RequirementRef[]>(selected);
  const [creating, setCreating] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    setDraft(selected);
    setSearch("");
    let active = true;
    setLoading(true);
    void apiClient
      .requirements({ project_id: projectId, status: "active", limit: 200 })
      .then((payload) => {
        if (!active) return;
        setOptions(
          payload.items.map((item) => ({
            id: item.id,
            title: item.title,
            priority: item.priority,
            status: item.status,
            project_id: item.project_id,
          })),
        );
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiClient, open, projectId]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [open]);

  // 下拉列表＝项目下进行中的需求 ∪ 已勾选的需求（哪怕跨项目或不是进行中，便于取消）
  const rows = useMemo(() => {
    const byId = new Map<string, RequirementRef>();
    options.forEach((item) => byId.set(item.id, item));
    draft.forEach((item) => {
      if (!byId.has(item.id)) byId.set(item.id, item);
    });
    const list = [...byId.values()];
    const keyword = search.trim();
    return keyword ? list.filter((item) => item.title.includes(keyword)) : list;
  }, [draft, options, search]);

  const toggle = (item: RequirementRef) => {
    setDraft((current) =>
      current.some((existing) => existing.id === item.id)
        ? current.filter((existing) => existing.id !== item.id)
        : [...current, item],
    );
  };

  const confirm = () => {
    onChange(draft);
    setOpen(false);
  };

  // 后端不校验会议主项目和需求所属项目是否一致（D9），挂了的需求不会因为
  // 主项目被清空就跟着消失——禁用态只是不让继续挑选/新增，已关联的胶囊要
  // 照常显示，不能连着数据一起藏起来。
  const chips = selected.map((item) => (
    <button
      className="requirement-chip"
      key={item.id}
      onClick={() => onOpenRequirement?.(item.id)}
      type="button"
    >
      <PriorityBadge priority={item.priority} />
      {item.title}
    </button>
  ));

  if (disabled) {
    return (
      <div className="requirement-field">
        <span className="requirement-field__label">关联需求</span>
        <div className="requirement-picker requirement-picker--disabled">
          {selected.length > 0 ? (
            <div className="requirement-picker__chips">{chips}</div>
          ) : (
            <span className="requirement-picker__placeholder">先选择主项目</span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="requirement-field">
      <span className="requirement-field__label">关联需求</span>
      <div className="requirement-picker" ref={containerRef}>
        <div className="requirement-picker__chips">
          {chips}
          <button
            className="requirement-picker__trigger"
            onClick={() => setOpen((value) => !value)}
            type="button"
          >
            {selected.length > 0 ? "编辑" : "＋ 关联需求"}
          </button>
        </div>
        {open && (
          <div className="requirement-picker__flyout">
            <input
              aria-label="搜索需求"
              className="requirement-picker__search"
              onChange={(event) => setSearch(event.target.value)}
              placeholder="搜索需求"
              value={search}
            />
            <div className="requirement-picker__list">
              {loading ? (
                <div className="requirement-picker__empty">加载中…</div>
              ) : rows.length === 0 ? (
                <div className="requirement-picker__empty">没有匹配的需求</div>
              ) : (
                rows.map((item) => (
                  <label className="requirement-picker__row" key={item.id}>
                    <input
                      checked={draft.some((existing) => existing.id === item.id)}
                      onChange={() => toggle(item)}
                      type="checkbox"
                    />
                    <PriorityBadge priority={item.priority} />
                    <span>{item.title}</span>
                  </label>
                ))
              )}
            </div>
            <div className="requirement-picker__footer">
              <button className="requirement-picker__create" onClick={() => { setOpen(false); setCreating(true); }} type="button">
                ＋ 新建需求
              </button>
              <button className="requirement-picker__confirm" onClick={confirm} type="button">
                确定
              </button>
            </div>
          </div>
        )}
        {creating && (
          <RequirementModal
            apiClient={apiClient}
            canPickFolders
            defaultProjectId={projectId || null}
            mode="create"
            onClose={() => setCreating(false)}
            onSaved={(requirement) => {
              const ref: RequirementRef = {
                id: requirement.id,
                title: requirement.title,
                priority: requirement.priority,
                status: requirement.status,
                project_id: requirement.project_id,
              };
              setCreating(false);
              onChange([...selected, ref]);
            }}
            projects={projects}
          />
        )}
      </div>
    </div>
  );
}
