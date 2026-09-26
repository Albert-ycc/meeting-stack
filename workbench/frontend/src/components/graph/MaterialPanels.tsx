/* 关系图面板里项目、材料、跨项目信标这几类节点的完整版（1h） */
import { useEffect, useState } from "react";

import { formatBytes } from "../../format";
import type { GraphPanelProps } from "./GraphPanel";
import type { DiskState, ExpandPayload, GraphBeacon, GraphBeaconItem } from "./graphTypes";
import { CopyPath, Section, localUndoUntil } from "./panelParts";

const DISK_TEXT: Record<DiskState, string> = {
  online: "在线",
  volume_offline: "资料盘未连接",
  missing: "找不到了",
  checking: "正在检查…",
};

function shortDate(mtime: string) {
  const [, month, day] = mtime.slice(0, 10).split("-");
  return month && day ? `${Number(month)}/${Number(day)}` : mtime.slice(0, 10);
}

export function baseName(path: string) {
  return path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
}

// ------------------------------------------------------------------ 项目

export function ProjectPanelBody({ props }: { props: GraphPanelProps }) {
  const { graph, roots } = props;
  const openTasks = graph.meetings.reduce((sum, meeting) => sum + meeting.open_tasks, 0);
  const pendingTasks = graph.meetings.reduce((sum, meeting) => sum + meeting.pending_tasks, 0);
  const requirementCount = graph.requirements.length + (graph.requirements_more?.count ?? 0);
  const cards = graph.folders.find((folder) => folder.kind === "cards");
  const rootFolders = graph.folders.filter((folder) => folder.kind === "root");
  const cardsNote = !cards
    ? "没开"
    : [cards.stopped ? `停了 ${cards.stopped} 张` : "", cards.waiting ? `在等 ${cards.waiting} 张` : ""].filter(Boolean).join("，");

  return (
    <>
      <dl className="graph-panel__counts">
        <div>
          <dt>会</dt>
          <dd>{graph.project.meeting_count} 场</dd>
          <small>{graph.window.days ? `最近 ${graph.window.days} 天` : "全部"}</small>
        </div>
        <div>
          <dt>进行中的需求</dt>
          <dd>{requirementCount} 个</dd>
        </div>
        <div>
          <dt>没做完的任务</dt>
          <dd>{openTasks} 条</dd>
          <small>{pendingTasks ? `${pendingTasks} 条待确认` : "图上这些会里的"}</small>
        </div>
        <div>
          <dt>会议卡片</dt>
          <dd>{cards ? `已写 ${cards.written ?? 0} 张` : "没开"}</dd>
          {cards && cardsNote && <small>{cardsNote}</small>}
        </div>
      </dl>
      <Section title="材料文件夹">
        {rootFolders.length ? (
          <ul className="graph-panel__list">
            {rootFolders.map((folder) => {
              const root = roots?.roots.find((item) => item.root_id === folder.root_id);
              return (
                <li key={folder.id}>
                  <button className="text-button" onClick={() => props.onSelect(folder.id)} type="button">
                    {folder.name}/
                  </button>
                  <small className={root && root.state !== "online" ? "graph-panel__warn" : undefined}>
                    {root ? DISK_TEXT[root.state] : "正在检查…"}
                    {root?.state === "online" && root.loose_count ? ` · 根目录散放 ${root.loose_count} 个文件` : ""}
                  </small>
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="graph-panel__muted">还没挂材料文件夹，去清单视图里挂上，材料就会出现在右边。</p>
        )}
      </Section>
      <Section title="词典">
        <p>
          图上有 {graph.cues.length} 个线索词。AI 靠项目词和文件夹名判断一场会归哪个项目。
          <button className="text-button" onClick={() => props.onOpenGlossary(graph.project.id)} type="button">
            看这个项目的词典 →
          </button>
        </p>
      </Section>
      <p className="graph-panel__muted">
        位置按类型和时间排：左边是会议，右边是材料，上面是进行中的需求，下面是线索词；离中心越近越新。
      </p>
    </>
  );
}

// ------------------------------------------------------------------ 文件夹

/** 材料文件夹逐层看：面包屑、子文件夹（按修改时间）、最近的文件 */
export function FolderBrowser({
  props,
  rootId,
  rootName,
  initialDir = "",
}: {
  props: GraphPanelProps;
  rootId: number;
  rootName: string;
  initialDir?: string;
}) {
  const [dir, setDir] = useState(initialDir);
  const [state, setState] = useState<{ key: string; payload: ExpandPayload | null; error: string }>({
    key: "",
    payload: null,
    error: "",
  });
  const key = `${rootId}|${dir}`;

  useEffect(() => {
    setDir(initialDir);
  }, [initialDir, rootId]);

  useEffect(() => {
    let active = true;
    props.apiClient
      .graphExpand(rootId, dir)
      .then((payload) => active && setState({ key, payload, error: "" }))
      .catch(
        (reason: unknown) =>
          active && setState({ key, payload: null, error: reason instanceof Error ? reason.message : "读不了这个文件夹" }),
      );
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, props.version]);

  const payload = state.key === key ? state.payload : null;
  const error = state.key === key ? state.error : "";
  const canReveal = Boolean(props.roots?.can_reveal);

  return (
    <>
      <nav aria-label="文件夹位置" className="graph-panel__crumbs">
        <button className="text-button" disabled={!dir} onClick={() => setDir("")} type="button">
          {rootName}
        </button>
        {(payload?.crumbs ?? []).map((crumb) => (
          <span key={crumb.dir}>
            <span aria-hidden="true">/</span>
            <button className="text-button" disabled={crumb.dir === dir} onClick={() => setDir(crumb.dir)} type="button">
              {crumb.name}
            </button>
          </span>
        ))}
      </nav>
      {error ? (
        <p className="graph-panel__error">{error}</p>
      ) : !payload ? (
        <p className="graph-panel__muted">正在读文件夹…</p>
      ) : payload.state !== "online" ? (
        <>
          <CopyPath apiClient={props.apiClient} onNotice={props.onNotice} path={payload.path} />
          <p className="graph-panel__muted">
            {payload.state === "volume_offline" ? "资料盘没连上，连上后再看。" : "这个文件夹现在找不到了，可能被移走或改了名。"}
          </p>
        </>
      ) : (
        <>
          <CopyPath apiClient={props.apiClient} canReveal={canReveal} onNotice={props.onNotice} path={payload.path} />
          <Section title={payload.dirs_total ? `子文件夹 ${payload.dirs_total}` : "子文件夹"}>
            {payload.dirs.length ? (
              <ul className="graph-panel__list">
                {payload.dirs.map((item) => (
                  <li key={item.dir}>
                    <button className="text-button" onClick={() => setDir(item.dir)} type="button">
                      {item.name}/
                    </button>
                    <small>{shortDate(item.mtime)} 改过</small>
                  </li>
                ))}
                {payload.dirs_total > payload.dirs.length && (
                  <li className="graph-panel__muted">还有 {payload.dirs_total - payload.dirs.length} 个，改得早的没列出来</li>
                )}
              </ul>
            ) : (
              <p className="graph-panel__muted">没有子文件夹</p>
            )}
          </Section>
          <Section title={payload.files_total ? `最近的文件（共 ${payload.files_total} 个）` : "文件"}>
            {payload.files.length ? (
              <ul className="graph-panel__files">
                {payload.files.map((file) => (
                  <li key={file.path} title={file.path}>
                    {file.name}{" "}
                    <small>
                      {shortDate(file.mtime)} · {formatBytes(file.size)}
                    </small>
                  </li>
                ))}
                {payload.files_total > payload.files.length && (
                  <li className="graph-panel__muted">还有 {payload.files_total - payload.files.length} 个更早的文件</li>
                )}
              </ul>
            ) : (
              <p className="graph-panel__muted">这一层没有文件</p>
            )}
          </Section>
        </>
      )}
    </>
  );
}

// ------------------------------------------------------------------ 散放文件

export function LoosePanelBody({ props }: { props: GraphPanelProps }) {
  const { roots } = props;
  if (!roots) return <p className="graph-panel__muted">正在读资料盘…</p>;
  const canReveal = Boolean(roots.can_reveal);
  return (
    <>
      <p className="graph-panel__meta">项目文件夹根目录里没放进子文件夹的文件，按修改时间排</p>
      <ul className="graph-panel__files">
        {roots.loose.recent.map((file) => (
          <li className="graph-panel__file-row" key={file.path} title={file.path}>
            <span>
              {file.name}{" "}
              <small>
                {shortDate(file.mtime)} · {formatBytes(file.size)}
              </small>
            </span>
            {canReveal && (
              <button
                className="text-button"
                onClick={async () => {
                  try {
                    await props.apiClient.revealMaterial(file.path);
                  } catch (reason) {
                    props.onNotice(reason instanceof Error ? reason.message : "打不开访达", undefined, "error");
                  }
                }}
                type="button"
              >
                在访达中显示
              </button>
            )}
          </li>
        ))}
        {roots.loose.count > roots.loose.recent.length && (
          <li className="graph-panel__muted">共 {roots.loose.count} 个，只列最近的</li>
        )}
      </ul>
    </>
  );
}

// ------------------------------------------------------------------ 跨项目信标

function projectName(props: GraphPanelProps, beacon: GraphBeacon, projectId: string | null | undefined) {
  if (!projectId) return "不归项目";
  if (projectId === props.graph.project.id) return props.graph.project.name;
  if (projectId === beacon.project_id) return beacon.project_name;
  return props.projects.find((project) => project.id === projectId)?.name ?? "别的项目";
}

/** 任务跟着会走：搬到会所在的项目；挂着需求的会移出需求，撤销时连需求一起搬回来 */
export function moveTaskText(props: GraphPanelProps, beacon: GraphBeacon, item: GraphBeaconItem) {
  const target = projectName(props, beacon, item.meeting_project_id);
  return item.requirement_id ? `移到 ${target}（会移出需求『${item.requirement_title ?? "…"}』）` : `移到 ${target}`;
}

export function BeaconPanelBody({ props, beacon }: { props: GraphPanelProps; beacon: GraphBeacon }) {
  const [busy, setBusy] = useState(false);

  const run = async (work: () => Promise<unknown>, done: () => void) => {
    setBusy(true);
    try {
      await work();
      done();
      await props.onChanged();
    } catch (reason) {
      props.onNotice(reason instanceof Error ? reason.message : "操作失败", undefined, "error");
    } finally {
      setBusy(false);
    }
  };

  const unlink = (item: GraphBeaconItem) => {
    const requirementId = item.requirement_id!;
    const meetingId = item.meeting_id!;
    void run(
      () => props.apiClient.removeRequirementMeeting(requirementId, meetingId),
      () =>
        props.onNotice("已解除这条跨项目的关联", {
          kind: "unlink",
          requirementId,
          meetingId,
          title: item.requirement_title ?? "这个需求",
          until: localUndoUntil(),
        }),
    );
  };

  const moveTask = (item: GraphBeaconItem) => {
    const taskId = item.task_id!;
    const title = item.task_title ?? "任务";
    const target = projectName(props, beacon, item.meeting_project_id);
    void run(
      () => props.apiClient.updateTask(taskId, { project_id: item.meeting_project_id ?? null, requirement_id: null }),
      () =>
        props.onNotice(`已把任务「${title}」移到 ${target}`, {
          kind: "task",
          what: "move",
          taskId,
          title,
          before: { project_id: item.task_project_id ?? null, requirement_id: item.requirement_id ?? null },
          until: localUndoUntil(),
        }),
    );
  };

  const look = (item: GraphBeaconItem) => {
    if (item.kind === "meeting_requirement" && item.requirement_id) props.onOpenRequirement(item.requirement_id);
    else if (item.meeting_id) props.onOpenMeeting(item.meeting_id);
  };

  return (
    <>
      <p className="graph-panel__meta">
        和「{beacon.project_name}」之间有 {beacon.count} 处交叉：会和需求跨了项目，或者任务和它的会不在一个项目
      </p>
      <ul className="graph-panel__list graph-panel__beacon">
        {beacon.items.map((item, index) => {
          const isLink = item.kind === "meeting_requirement" || item.kind === "requirement_meeting";
          const isTask = item.kind === "task_elsewhere" || item.kind === "task_from_elsewhere";
          return (
            <li key={`${item.kind}-${item.task_id ?? ""}-${item.meeting_id ?? ""}-${item.requirement_id ?? ""}-${index}`}>
              <span className="graph-panel__beacon-text">
                {item.text}
                {isTask && item.task_id && <small>{moveTaskText(props, beacon, item)}</small>}
              </span>
              <span className="graph-panel__actions">
                {isLink && item.requirement_id && item.meeting_id && (
                  <button className="text-button" disabled={busy} onClick={() => unlink(item)} type="button">
                    解除
                  </button>
                )}
                {isTask && item.task_id && (
                  <button
                    className="ghost-button"
                    disabled={busy}
                    onClick={() => moveTask(item)}
                    title={moveTaskText(props, beacon, item)}
                    type="button"
                  >
                    也移过去
                  </button>
                )}
                {(item.meeting_id || item.requirement_id) && (
                  <button className="text-button" onClick={() => look(item)} type="button">
                    去看
                  </button>
                )}
              </span>
            </li>
          );
        })}
      </ul>
      <p className="graph-panel__muted">
        <button className="text-button" onClick={() => props.onOpenProject(beacon.project_id)} type="button">
          打开「{beacon.project_name}」→
        </button>
      </p>
    </>
  );
}
