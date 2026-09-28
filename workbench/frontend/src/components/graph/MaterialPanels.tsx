/* 关系图面板里项目、材料、跨项目信标这几类节点的完整版（1h） */
import { useEffect, useState } from "react";

import { copyText } from "../../clipboard";
import { formatBytes } from "../../format";
import type { MaterialRoot, MaterialRootRepoint, RequirementFilesPayload } from "../../types";
import { MaterialRootPickerModal } from "../MaterialRootPickerModal";
import { RootRenameQuestion, movedNote } from "../RootRenameQuestion";
import type { GraphPanelProps } from "./GraphPanel";
import type { PinnedFile } from "./graphFiles";
import type { CardsFilesPayload, DiskState, ExpandPayload, GraphBeacon, GraphBeaconItem, RecentFile } from "./graphTypes";
import { pendingNote } from "./layout";
import { CopyPath, Section, localUndoUntil } from "./panelParts";
import { MentionedBadge } from "../files/MentionedBadge";
import { useMentionedCounts } from "../files/useMentionedCounts";

const COUNT_FORMAT = new Intl.NumberFormat("en-US");
/** 需求文件夹面板里最多列这么多个文件，多的去需求页看 */
const FOLDER_FILES_SHOWN = 40;

/** 项目面板「材料文件夹」每个根目录那一句（3g），说法和项目页一致；内容循环还没数过时不写 */
export function rootContentText(content: { files: number; done: number; unreadable: number } | null | undefined) {
  if (!content) return "";
  const parts = [`文件名 ${COUNT_FORMAT.format(content.files)} 个`, `已读 ${COUNT_FORMAT.format(content.done)} 个`];
  if (content.unreadable > 0) parts.push(`读不了 ${COUNT_FORMAT.format(content.unreadable)} 个`);
  return parts.join(" · ");
}

const RECENT_STATE_TEXT: Partial<Record<RecentFile["state"], string>> = {
  pending: "还没读到",
  waiting: "在等你装识别程序",
  unreadable: "读不了",
};

/** 根目录、需求文件夹面板顶上的「最近改过的文件」（和画布同一份，最多 6 个）：每行能点，画布上放不下的在这里能看到 */
export function RecentFilesSection({
  props,
  files,
  rootId,
  folder,
}: {
  props: GraphPanelProps;
  files: RecentFile[] | undefined;
  rootId: number | null | undefined;
  folder: string;
}) {
  // 4f：小签「3 场会提到」，一个列表一次批量请求；0 时不画（hook 要在 return 之前）
  const mentioned = useMentionedCounts(props.apiClient, (files ?? []).map((file) => file.file_id), String(props.version));
  if (!files?.length || rootId === null || rootId === undefined) return null;
  return (
    <Section title="最近改过的文件">
      <ul aria-label="最近改过的文件" className="graph-panel__list">
        {files.map((file) => (
          <li key={file.file_id}>
            <FileButton
              file={{
                file_id: file.file_id,
                name: file.name,
                rel_path: file.dir_rel ? `${file.dir_rel}/${file.name}` : file.name,
                root_id: rootId,
                folder,
              }}
              props={props}
            />
            <small>
              {file.mtime ? `${shortDate(file.mtime)} 改过` : ""}
              {RECENT_STATE_TEXT[file.state] ? ` · ${RECENT_STATE_TEXT[file.state]}` : ""}
              {mentioned && <MentionedBadge count={mentioned.get(file.file_id)} />}
            </small>
          </li>
        ))}
      </ul>
    </Section>
  );
}

/** 文件名：能在图上打开时是按钮（补出节点并打开文件面板，放不下时打开预览抽屉），否则是纯文字 */
export function FileButton({ props, file }: { props: GraphPanelProps; file: PinnedFile }) {
  if (!props.onOpenFile) return <span title={file.rel_path}>{file.name}</span>;
  return (
    <button className="text-button" onClick={() => props.onOpenFile?.(file)} title={file.rel_path} type="button">
      {file.name}
    </button>
  );
}

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
  const pending = graph.folders.find((folder) => folder.kind === "pending");
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
                  {rootContentText(root?.content) && <small>{rootContentText(root?.content)}</small>}
                </li>
              );
            })}
          </ul>
        ) : pending ? (
          <ul className="graph-panel__list">
            <li>
              <button className="text-button" onClick={() => props.onSelect(pending.id)} type="button">
                {pending.name}/
              </button>
              <small className={pending.state === "stopped" ? "graph-panel__warn" : undefined}>{pendingNote(pending)}</small>
            </li>
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
  const recent = props.roots?.roots.find((item) => item.root_id === rootId)?.recent_files;
  const mentioned = useMentionedCounts(props.apiClient, (payload?.files ?? []).map((file) => file.file_id), String(props.version));

  return (
    <>
      {!dir && <RecentFilesSection files={recent} folder={`root:${rootId}`} props={props} rootId={rootId} />}
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
                    {file.file_id ? (
                      <FileButton
                        file={{
                          file_id: file.file_id,
                          name: file.name,
                          rel_path: payload.dir ? `${payload.dir}/${file.name}` : file.name,
                          root_id: rootId,
                          folder: `root:${rootId}`,
                        }}
                        props={props}
                      />
                    ) : (
                      file.name
                    )}{" "}
                    <small>
                      {shortDate(file.mtime)} · {formatBytes(file.size)}
                      {mentioned && file.file_id ? <MentionedBadge count={mentioned.get(file.file_id)} /> : null}
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

/**
 * 根目录「找不到」（盘在、文件夹没了）：和项目页同一个改名找回的问题，［是它］［不是］，没有候选时［重新选…］。
 */
export function RootMissingBody({ props, rootId, path }: { props: GraphPanelProps; rootId: number; path: string }) {
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const projectId = props.graph.project.id;
  const root: MaterialRoot = { id: rootId, project_id: projectId, path, exists: false, state: "missing", created_at: "" };

  const repointed = async (result: MaterialRootRepoint) => {
    props.onNotice(`材料根目录已改到 ${result.path}${movedNote(result)}`);
    await props.onChanged();
  };

  const reselect = async (next: string) => {
    setBusy(true);
    setError("");
    try {
      const replaced = await props.apiClient.replaceProjectMaterialRoot(projectId, rootId, next);
      setPicking(false);
      props.onNotice(`材料根目录已更新${movedNote(replaced)}`);
      await props.onChanged();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "更新失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <CopyPath apiClient={props.apiClient} onNotice={props.onNotice} path={path} />
      <div className="graph-panel__rename">
        <RootRenameQuestion
          apiClient={props.apiClient}
          canManage
          onRepointed={repointed}
          onReselect={() => {
            setError("");
            setPicking(true);
          }}
          projectId={projectId}
          root={root}
        />
      </div>
      {picking && (
        <MaterialRootPickerModal
          apiClient={props.apiClient}
          busy={busy}
          error={error}
          onClose={() => setPicking(false)}
          onConfirm={(next) => void reselect(next)}
        />
      )}
    </>
  );
}

// ------------------------------------------------------------------ 需求文件夹、声档会议记录（3g）

/** 需求文件夹面板：最近改过的文件、文件夹里的文件（先查库，没扫完时照旧读盘）、路径、所属需求 */
export function RequirementFolderBody({
  props,
  folderId,
  requirementId,
  path,
  graphId,
}: {
  props: GraphPanelProps;
  folderId: number;
  requirementId: string;
  path: string;
  graphId: string;
}) {
  const entry = props.roots?.folders.find((item) => item.id === graphId);
  const canList = typeof props.apiClient.requirementFolderFiles === "function";
  const [state, setState] = useState<{ key: string; payload: RequirementFilesPayload | null; error: string }>({
    key: "",
    payload: null,
    error: "",
  });
  const key = `${requirementId}|${folderId}`;
  useEffect(() => {
    if (!canList) return;
    let active = true;
    props.apiClient
      .requirementFolderFiles(requirementId, folderId, { limit: FOLDER_FILES_SHOWN })
      .then((payload) => active && setState({ key, payload, error: "" }))
      .catch((reason: unknown) =>
        active && setState({ key, payload: null, error: reason instanceof Error ? reason.message : "读不了这个文件夹" }),
      );
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canList, key, props.version]);
  const payload = state.key === key ? state.payload : null;
  const error = state.key === key ? state.error : "";
  const rootId = entry?.root_id;
  const relative = rootId ? relativeTo(props, rootId, path) : null;
  const mentioned = useMentionedCounts(props.apiClient, (payload?.items ?? []).map((item) => item.file_id), String(props.version));
  return (
    <>
      <RecentFilesSection files={entry?.recent_files} folder={graphId} props={props} rootId={rootId} />
      {canList && (
        <Section title={payload?.total ? `文件 ${payload.total}${payload.capped ? "+" : ""}` : "文件"}>
          {error ? (
            <p className="graph-panel__error">{error}</p>
          ) : !payload ? (
            <p className="graph-panel__muted">正在读文件…</p>
          ) : !payload.exists ? (
            <p className="graph-panel__muted">这个文件夹现在找不到了，可能被移走或改了名。</p>
          ) : payload.items.length === 0 ? (
            <p className="graph-panel__muted">文件夹里还没有文件</p>
          ) : (
            <ul aria-label="文件夹里的文件" className="graph-panel__files">
              {payload.items.map((item) => {
                const name = baseName(item.relative_path);
                return (
                  <li key={item.relative_path} title={item.relative_path}>
                    {item.file_id && rootId ? (
                      <FileButton
                        file={{
                          file_id: item.file_id,
                          name,
                          rel_path: relative ? `${relative}/${item.relative_path}` : item.relative_path,
                          root_id: rootId,
                          folder: graphId,
                        }}
                        props={props}
                      />
                    ) : (
                      item.relative_path
                    )}{" "}
                    <small>
                      {shortDate(item.modified_at)} · {formatBytes(item.size_bytes)}
                      {mentioned && item.file_id ? <MentionedBadge count={mentioned.get(item.file_id)} /> : null}
                    </small>
                  </li>
                );
              })}
              {payload.total > payload.items.length && (
                <li className="graph-panel__muted">还有 {payload.total - payload.items.length} 个，去需求页看全部</li>
              )}
            </ul>
          )}
        </Section>
      )}
      <CopyPath apiClient={props.apiClient} canReveal={Boolean(props.roots?.can_reveal)} onNotice={props.onNotice} path={path} />
      <button className="text-button" onClick={() => props.onSelect(`r:${requirementId}`)} type="button">
        看它所属的需求
      </button>
    </>
  );
}

/** 需求文件夹相对它所在根目录的路径（给补出来的文件节点写 rel_path） */
function relativeTo(props: GraphPanelProps, rootId: number, path: string): string | null {
  const root = props.roots?.roots.find((item) => item.root_id === rootId);
  if (!root) return null;
  const base = root.path.replace(/\/+$/, "");
  return path.startsWith(`${base}/`) ? path.slice(base.length + 1) : null;
}

/** 声档会议记录面板的「文件」：只查库，最多 20 个，只给文件名；点了在图上补出、打开文件面板 */
export function CardsFilesSection({ props }: { props: GraphPanelProps }) {
  const canList = typeof props.apiClient.graphCardsFiles === "function";
  const [files, setFiles] = useState<CardsFilesPayload["files"] | null>(null);
  const projectId = props.graph.project.id;
  useEffect(() => {
    if (!canList) return;
    let active = true;
    props.apiClient
      .graphCardsFiles(projectId)
      .then((payload) => active && setFiles(payload.files))
      .catch(() => active && setFiles([]));
    return () => {
      active = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canList, projectId, props.version]);
  if (!canList || files === null) return null;
  return (
    <Section title="文件">
      {files.length ? (
        <ul aria-label="会议记录文件" className="graph-panel__files">
          {files.map((file) => (
            <li key={file.file_id}>
              <FileButton
                file={{ file_id: file.file_id, name: file.name, rel_path: file.rel_path, root_id: file.root_id, folder: "cards" }}
                props={props}
              />
              {file.mtime && <small> {shortDate(file.mtime)} 改过</small>}
            </li>
          ))}
        </ul>
      ) : (
        <p className="graph-panel__muted">文件名还没认到这里的会议记录</p>
      )}
    </Section>
  );
}

// ------------------------------------------------------------------ 散放文件

/** 散放文件挂在它所在根目录的节点上（按路径前缀找根目录） */
function looseFile(roots: NonNullable<GraphPanelProps["roots"]>, path: string, fileId: number, name: string): PinnedFile {
  const root = roots.roots.find((item) => path.startsWith(`${item.path.replace(/\/+$/, "")}/`));
  return {
    file_id: fileId,
    name,
    rel_path: name,
    root_id: root?.root_id ?? 0,
    folder: root ? `root:${root.root_id}` : "loose",
  };
}

export function LoosePanelBody({ props }: { props: GraphPanelProps }) {
  const { roots } = props;
  const mentioned = useMentionedCounts(props.apiClient, (roots?.loose.recent ?? []).map((file) => file.file_id), String(props.version));
  if (!roots) return <p className="graph-panel__muted">正在读资料盘…</p>;
  const canReveal = Boolean(roots.can_reveal);
  return (
    <>
      <p className="graph-panel__meta">项目文件夹根目录里没放进子文件夹的文件，按修改时间排</p>
      <ul className="graph-panel__files">
        {roots.loose.recent.map((file) => (
          <li className="graph-panel__file-row" key={file.path} title={file.path}>
            <span>
              {file.file_id ? (
                <FileButton file={looseFile(roots, file.path, file.file_id, file.name)} props={props} />
              ) : (
                file.name
              )}{" "}
              <small>
                {shortDate(file.mtime)} · {formatBytes(file.size)}
                {mentioned && file.file_id ? <MentionedBadge count={mentioned.get(file.file_id)} /> : null}
              </small>
            </span>
            <span className="graph-panel__path-actions">
              <button
                className="text-button"
                onClick={async () => {
                  try {
                    await copyText(file.path);
                    props.onNotice("已复制路径");
                  } catch {
                    props.onNotice("复制失败，请手动选中路径", undefined, "error");
                  }
                }}
                type="button"
              >
                复制路径
              </button>
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
            </span>
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
