// 关系图里的文件（3g）：最近改过的文件、从面板点出来要补到图上的那一个（pinned）、节点上的状态标记。
// 都是纯函数：同样的图和资料盘状态每次补出同样的节点，只加在最后，不挪已有节点。

import type { FileNodeState, GraphEdge, GraphFile, GraphPayload, GraphRootsPayload, RecentFile, RelatedEdges } from "./graphTypes";

/** 全图最多画这么多个节点（和后端 VISIBLE_BUDGET 一致） */
export const VISIBLE_BUDGET = 40;
/** 最近改过的文件：每个文件夹最多 3 个，全图最多 12 个 */
export const RECENT_PER_FOLDER = 3;
export const RECENT_MAX = 12;

/** 从面板、深链点出来要在图上补出的文件 */
export interface PinnedFile {
  file_id: number;
  name: string;
  rel_path: string;
  root_id: number;
  /** 挂在哪个文件夹节点外侧：root:<id> 或 rf:<id> */
  folder: string;
  /** 4f：从状态句、N 键钉上的在问的文件，照样画琥珀色 */
  stale?: boolean;
  asks_deliverable?: boolean;
}

export function fileExt(name: string) {
  const dot = name.lastIndexOf(".");
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : "";
}

/** 画布上现有多少个节点：会、残影、折叠、门口、需求、文件夹（含子文件夹）、散放、文件、线索词、信标 */
export function visibleCount(graph: GraphPayload): number {
  return (
    1 +
    graph.meetings.length +
    (graph.moved_out?.length ?? 0) +
    graph.collapsed.length +
    graph.doorstep.length +
    (graph.doorstep_more > 0 ? 1 : 0) +
    graph.requirements.length +
    (graph.requirements_more ? 1 : 0) +
    (graph.suggested_requirements?.length ?? 0) +
    graph.folders.length +
    (graph.folders_more ? 1 : 0) +
    (graph.loose ? 1 : 0) +
    (graph.files?.length ?? 0) +
    (graph.files_more ? 1 : 0) +
    graph.cues.length +
    graph.beacons.length
  );
}

function recentNode(file: RecentFile, folder: string, rootId: number): GraphFile {
  return {
    id: `file:${file.file_id}`,
    kind: "file",
    file_id: file.file_id,
    name: file.name,
    ext: file.ext || fileExt(file.name),
    rel_path: file.dir_rel ? `${file.dir_rel}/${file.name}` : file.name,
    root_id: rootId,
    folder,
    recent: true,
    state: file.state,
  };
}

/** 每个在图上的根目录、需求文件夹的候选（图上已有的去掉），按图上文件夹的顺序 */
function recentGroups(graph: GraphPayload, roots: GraphRootsPayload, known: Set<string>) {
  const groups: GraphFile[][] = [];
  for (const folder of graph.folders) {
    let candidates: RecentFile[] = [];
    let rootId: number | null | undefined = null;
    if (folder.kind === "root") {
      const root = roots.roots.find((item) => item.id === folder.id);
      candidates = root?.recent_files ?? [];
      rootId = root?.root_id;
    } else if (folder.kind === "requirement_folder") {
      const entry = roots.folders.find((item) => item.id === folder.id);
      candidates = entry?.recent_files ?? [];
      rootId = entry?.root_id;
    } else {
      continue;
    }
    if (rootId === null || rootId === undefined) continue;
    const picked: GraphFile[] = [];
    for (const file of candidates) {
      const id = `file:${file.file_id}`;
      if (known.has(id)) continue;
      known.add(id);
      picked.push(recentNode(file, folder.id, rootId));
      if (picked.length >= RECENT_PER_FOLDER) break;
    }
    if (picked.length) groups.push(picked);
  }
  return groups;
}

/**
 * 在 withMentionedFiles 之后调用：先补 pinned（它不占最近文件的名额以外的位置，但会挤掉一个最近的），
 * 再按文件夹轮流补最近改过的文件（每个文件夹先 1 个、再第 2 个、第 3 个），总数不超过 40 个可见节点
 * 剩下的名额和 12 个。每个补出的文件有一条细线连回它的文件夹。
 */
export function withRecentFiles(
  graph: GraphPayload,
  roots: GraphRootsPayload | null,
  pinned: PinnedFile | null,
): GraphPayload {
  const known = new Set((graph.files ?? []).map((file) => file.id));
  const folderIds = new Set(graph.folders.map((folder) => folder.id));
  const addFiles: GraphFile[] = [];
  const addEdges: GraphPayload["edges"] = [];
  const link = (file: GraphFile) => {
    if (folderIds.has(file.folder)) {
      addEdges.push({ id: `e:${file.id}:${file.folder}`, kind: "folder", from: file.folder, to: file.id, label: "" });
    }
  };
  if (pinned && !known.has(`file:${pinned.file_id}`)) {
    const recent = [...(roots?.roots ?? []), ...(roots?.folders ?? [])]
      .flatMap((item) => item.recent_files ?? [])
      .find((file) => file.file_id === pinned.file_id);
    const node: GraphFile = {
      id: `file:${pinned.file_id}`,
      kind: "file",
      file_id: pinned.file_id,
      name: pinned.name,
      ext: fileExt(pinned.name),
      rel_path: pinned.rel_path,
      root_id: pinned.root_id,
      folder: pinned.folder,
      pinned: true,
      ...(pinned.stale ? { stale: true } : {}),
      ...(pinned.asks_deliverable ? { asks_deliverable: true } : {}),
      ...(recent ? { state: recent.state } : {}),
    };
    known.add(node.id);
    addFiles.push(node);
    link(node);
  }
  if (roots) {
    let room = Math.min(RECENT_MAX, VISIBLE_BUDGET - visibleCount(graph) - addFiles.length);
    const groups = recentGroups(graph, roots, known);
    for (let round = 0; round < RECENT_PER_FOLDER && room > 0; round += 1) {
      for (const group of groups) {
        if (room <= 0) break;
        const file = group[round];
        if (!file) continue;
        addFiles.push(file);
        link(file);
        room -= 1;
      }
    }
  }
  if (!addFiles.length) return graph;
  return { ...graph, files: [...(graph.files ?? []), ...addFiles], edges: [...graph.edges, ...addEdges] };
}

/** 4f：相关线最多补这么多个新文件、每个节点（含折叠组）最多几条、全图最多几条 */
export const RELATED_FILES_MAX = 6;
export const RELATED_PER_NODE = 3;
export const RELATED_DRAW_MAX = 36;

/** 相关线上的字：「共同词：报价单、驻场」 */
export function relatedLabel(words: string[]): string {
  return words.length ? `共同词：${words.slice(0, 4).join("、")}` : "共同词";
}

/**
 * 4f：把 4d 的相关线接到图上（在 withMentionedFiles 之后、withRecentFiles 之前）。会议一端：会在图上用
 * m:<id>，否则用装着它的折叠组，都没有就跳过；同一对已有提到线时跳过；文件一端图上有就用，没有时按
 * files 新建一个（related: true），新文件最多 RELATED_FILES_MAX 个且不超过剩下的空位；按 rank 贪心，
 * 每个节点最多 RELATED_PER_NODE 条，全图最多 RELATED_DRAW_MAX 条。
 */
export function withRelatedEdges(graph: GraphPayload, related: RelatedEdges | null): GraphPayload {
  if (!related || !related.edges.length) return graph;
  const meetings = new Set(graph.meetings.map((meeting) => meeting.meeting_id));
  const groupOf = new Map<string, string>();
  for (const group of graph.collapsed) for (const meetingId of group.meeting_ids) groupOf.set(meetingId, group.id);
  const known = new Set((graph.files ?? []).map((file) => file.id));
  const edgeIds = new Set(graph.edges.map((edge) => edge.id));
  const pairs = new Set(graph.edges.filter((edge) => edge.kind === "mentioned").map((edge) => `${edge.from}|${edge.to}`));
  const perNode = new Map<string, number>();
  const addFiles: GraphFile[] = [];
  const addEdges: GraphEdge[] = [];
  let room = VISIBLE_BUDGET - visibleCount(graph);
  const ranked = [...related.edges].sort((a, b) => a.rank - b.rank || a.relation_id - b.relation_id);
  for (const item of ranked) {
    if (addEdges.length >= RELATED_DRAW_MAX) break;
    const from = meetings.has(item.meeting_id) ? `m:${item.meeting_id}` : groupOf.get(item.meeting_id);
    if (!from) continue;
    const to = `file:${item.file_id}`;
    if (edgeIds.has(item.id) || edgeIds.has(`e:file:${item.file_id}:${item.meeting_id}`) || pairs.has(`${from}|${to}`)) continue;
    if ((perNode.get(from) ?? 0) >= RELATED_PER_NODE || (perNode.get(to) ?? 0) >= RELATED_PER_NODE) continue;
    if (!known.has(to)) {
      const info = related.files[String(item.file_id)];
      if (!info || addFiles.length >= RELATED_FILES_MAX || room <= 0) continue;
      addFiles.push({
        id: to,
        kind: "file",
        file_id: info.file_id,
        name: info.name,
        ext: info.ext || fileExt(info.name),
        rel_path: info.rel_path,
        root_id: info.root_id,
        folder: `root:${info.root_id}`,
        related: true,
      });
      known.add(to);
      room -= 1;
    }
    perNode.set(from, (perNode.get(from) ?? 0) + 1);
    perNode.set(to, (perNode.get(to) ?? 0) + 1);
    pairs.add(`${from}|${to}`);
    addEdges.push({
      id: item.id,
      kind: "related",
      from,
      to,
      label: relatedLabel(item.words),
      state: "ok",
      relation_id: item.relation_id,
      meeting_id: item.meeting_id,
      file_id: item.file_id,
      at_ms: item.at_ms,
      quote: item.quote,
      words: item.words,
      passage: item.passage,
    });
  }
  if (!addEdges.length) return graph;
  return { ...graph, files: [...(graph.files ?? []), ...addFiles], edges: [...graph.edges, ...addEdges] };
}

export type FileMark = { symbol: "◷" | "⊘" | "?"; tone: "system" | "you" | "unreadable"; text: string };

/**
 * 文件节点上的小标记：还没读到（含转写会议时、资料盘未连接）画灰色 ◷（在等系统）；识别程序没装画琥珀色 ◷
 * （在等你）；读不了画灰色 ⊘；读完了、只收文件名的不画。
 */
export function fileMark(state: FileNodeState | undefined, offline = false, file?: Pick<GraphFile, "stale" | "asks_deliverable">): FileMark | null {
  // 4f：在问的文件画「?」，优先于读到哪一步的标记
  if (file?.stale) return { symbol: "?", tone: "you", text: "可能过时" };
  if (file?.asks_deliverable) return { symbol: "?", tone: "you", text: "等你认交付物" };
  if (state === "waiting") return { symbol: "◷", tone: "you", text: "在等你装识别程序" };
  if (state === "unreadable") return { symbol: "⊘", tone: "unreadable", text: "读不了" };
  if (state === "pending") return { symbol: "◷", tone: "system", text: offline ? "资料盘未连接" : "还没读到" };
  return null;
}
