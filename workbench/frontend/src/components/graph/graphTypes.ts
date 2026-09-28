// 关系图（1g、1h）接口的数据形状，和后端 graph.py 一一对应。

import type { MaterialDeliverable, MaterialFileState, MeetingAttribution, MeetingCard } from "../../types";

export type GraphWindow = "7d" | "28d" | "90d" | "all";
export type Ring = "inner" | "middle" | "outer";
export type CardCategory = "ok" | "waiting" | "stopped";

export interface GraphMeeting {
  id: string; // m:<meeting_id>
  meeting_id: string;
  title: string;
  date: string;
  age_days: number;
  ring: Ring;
  state: string;
  attribution: { label: string; source: "review" | "confirmed" | "manual" | "ai" | "legacy" };
  open_tasks: number;
  pending_tasks: number;
  /** 改到别的项目时跟着走的任务数、挂在本项目需求上留下的任务数（拖放前的预览用） */
  tasks_follow: number;
  tasks_stay: number;
  card: CardCategory | null;
  card_text: string | null;
  has_minutes: boolean;
}

export interface GraphCandidate {
  project_id: string;
  project_name: string;
  project_color: string;
  count: number;
}

export interface GraphDoorstep {
  id: string; // d:<meeting_id>
  meeting_id: string;
  title: string;
  date: string;
  age_days: number;
  candidates: GraphCandidate[];
  reason: string;
}

export interface GraphRequirement {
  id: string; // r:<requirement_id>
  requirement_id: string;
  title: string;
  priority: "P0" | "P1" | "P2" | "P3";
  open_tasks: number;
  pending_tasks: number;
  last_activity: string;
  age_days: number;
  ring: Ring;
  stale: boolean;
  stale_text: string | null;
}

export interface GraphFolder {
  id: string; // root:<id> / cards / rf:<id> / sub:<root id>:<相对路径的短 hash> / pending:<project_id>
  /** subfolder 是前端按资料盘状态补上的：根目录里最近改过的子文件夹；pending 是盘不在时等补建的文件夹（2c） */
  kind: "root" | "cards" | "requirement_folder" | "subfolder" | "pending";
  name: string;
  path: string;
  ring: Ring;
  root_id?: number;
  folder_id?: number;
  requirement_id?: string;
  written?: number;
  stopped?: number;
  waiting?: number;
  enabled?: boolean;
  /** subfolder：相对根目录的路径和修改时间 */
  dir?: string;
  mtime?: string;
  /** pending：要建在哪、在等还是停了、停了的原因 */
  parent?: string;
  state?: "waiting" | "stopped";
  reason?: string | null;
}

/** 「像是新需求」（2c）：和会议页同一个提示，按名字分组；meeting_ids 第一场是最近的 */
export interface GraphSuggestedRequirement {
  id: string; // nr:<hash>
  kind: "suggested_requirement";
  name: string;
  meeting_ids: string[];
  /** 会上的叫法（最多 3 个） */
  spoken: string[];
  last_day: string;
  count: number;
}

/** 会上提到的文件（2d）：挂在它所在根目录的文件夹节点外侧 */
export interface GraphFile {
  id: string; // file:<file_id>
  kind: "file";
  file_id: number;
  name: string;
  ext: string;
  rel_path: string;
  root_id: number;
  /** 所在根目录的文件夹节点 id（root:<root_id>） */
  folder: string;
  /** 被几场会提到（全部有效提到）；前端从简报补出来的没有 */
  meeting_count?: number;
  /** 选中一场会时前端从简报补出来的，不在后端的 files 里 */
  extra?: boolean;
  /** 3g：根目录、需求文件夹里最近改过的文件（前端从资料盘状态补的），挂在那个文件夹外侧 */
  recent?: true;
  /** 3g：从面板点出来、要在图上补出的那一个文件 */
  pinned?: true;
  /** 3g：读到哪一步，画节点上的小标记；会上提到的文件没有 */
  state?: FileNodeState;
}

/** 文件读到哪一步（同预览的 state.kind，不含 gone） */
export type FileNodeState = "done" | "pending" | "waiting" | "unreadable" | "names_only";

/** 最近改过的文件（3g）：每个根目录、需求文件夹最多 6 个候选 */
export interface RecentFile {
  file_id: number;
  name: string;
  ext: string;
  /** 相对根目录的所在文件夹 */
  dir_rel: string;
  mtime: string | null;
  state: FileNodeState;
}

export interface GraphCue {
  id: string;
  text: string;
  source: "term" | "folder";
  term_id: string | null;
  total: number;
  meetings: Array<{ meeting_id: string; count: number; anchors_ms: number[] }>;
}

export interface GraphCollapsed {
  id: string; // c:older / c:YYYY-MM
  kind: "older" | "month";
  label: string;
  count: number;
  from: string;
  to: string;
  meeting_ids: string[];
  ring: Ring;
}

export interface GraphBeaconItem {
  kind: "meeting_requirement" | "requirement_meeting" | "task_elsewhere" | "task_from_elsewhere";
  text: string;
  meeting_id?: string;
  requirement_id?: string | null;
  requirement_project_id?: string | null;
  requirement_title?: string | null;
  task_id?: string;
  task_title?: string;
  task_project_id?: string;
  meeting_project_id?: string;
}

export interface GraphBeacon {
  id: string; // b:<project_id>
  project_id: string;
  project_name: string;
  project_color: string;
  count: number;
  label: string;
  items: GraphBeaconItem[];
}

export type GraphEdgeKind =
  | "attribution"
  | "cue"
  | "discussion"
  | "folder"
  | "write"
  | "cross"
  /** 会和「像是新需求」之间的虚线（2c） */
  | "suggested"
  /** 会上提到文件（2d） */
  | "mentioned";

export interface GraphEdge {
  id: string;
  kind: GraphEdgeKind;
  from: string;
  to: string;
  label: string;
  state?: string;
  source?: string;
  count?: number;
  meeting_id?: string;
  requirement_id?: string;
  anchors_ms?: number[];
  /** suggested：新需求的名字 */
  name?: string;
  /** mentioned：会上说的那个词、它的 stem_key（［不是这份文件］用） */
  needle?: string;
  stem_key?: string;
}

export interface StatusPhrase {
  text: string;
  node_ids: string[];
}

export interface GraphPayload {
  project: { id: string; name: string; color: string; meeting_count: number };
  window: { requested: GraphWindow | null; effective: GraphWindow; days: number | null; widened_reason: string | null };
  today: string;
  meetings: GraphMeeting[];
  collapsed: GraphCollapsed[];
  doorstep: GraphDoorstep[];
  doorstep_more: number;
  requirements: GraphRequirement[];
  requirements_more: { id: string; count: number; requirement_ids: string[] } | null;
  /** 像是新需求，最多 3 个，排在需求那一侧末尾 */
  suggested_requirements: GraphSuggestedRequirement[];
  folders: GraphFolder[];
  folders_more: { id: string; count: number; paths: string[] } | null;
  loose: { id: string; kind: "loose"; name: string; ring: Ring; count: number | null } | null;
  /** 会上提到的文件：每场可见的会最多 3 个，全图最多 12 个 */
  files: GraphFile[];
  files_more: { id: string; count: number; file_ids: number[] } | null;
  cues: GraphCue[];
  beacons: GraphBeacon[];
  edges: GraphEdge[];
  status: { ok: StatusPhrase[]; waiting: StatusPhrase[]; stopped: StatusPhrase[]; note: string | null };
  weekly: Array<{ from: string; to: string; count: number }>;
  moved_out: GraphMovedOut[];
}

export interface GraphMovedOut {
  meeting_id: string;
  title: string;
  date: string;
  age_days: number;
  to_project_id: string | null;
  to_project_name: string | null;
  undo_until: string;
}

export type DiskState = "online" | "missing" | "volume_offline" | "checking";

export interface RecentDir {
  name: string;
  /** 相对根目录的路径，给 /api/graph/expand 用 */
  dir: string;
  path: string;
  mtime: string;
}

export interface GraphRootsPayload {
  roots: Array<{
    id: string;
    root_id: number;
    path: string;
    state: DiskState;
    loose_count: number | null;
    checked_at: string | null;
    recent_dirs?: RecentDir[];
    /** 3g：最近改过的文件（只查库，不含声档会议记录、不含需求文件夹里的） */
    recent_files?: RecentFile[];
    /** 3g：文件名多少个、内容读完多少、读不了多少；内容循环还没数过时为 null */
    content?: { files: number; done: number; unreadable: number } | null;
  }>;
  folders: Array<{
    id: string;
    folder_id: number;
    requirement_id: string;
    path: string;
    state: DiskState;
    /** 3g：所在的根目录（不在任何根目录里时为 null）和里面最近改过的文件 */
    root_id?: number | null;
    recent_files?: RecentFile[];
  }>;
  loose: {
    count: number;
    /** file_id：3g，文件名索引里有的才有 */
    recent: Array<{ name: string; path: string; size: number; mtime: string; file_id?: number | null }>;
  };
  checking: boolean;
  /** 本机打开声档时才给「在访达中显示」 */
  can_reveal?: boolean;
}

export interface ExpandPayload {
  root_id: number;
  project_id: string;
  root_path: string;
  dir: string;
  path: string;
  state: DiskState;
  crumbs: Array<{ name: string; dir: string }>;
  dirs: Array<{ name: string; dir: string; path: string; mtime: string }>;
  dirs_total: number;
  /** file_id：3g，文件名索引里有的才有，能在图上打开 */
  files: Array<{ name: string; path: string; size: number; mtime: string; file_id?: number | null }>;
  files_total: number;
}

/** 声档会议记录里的文件（3g）：只查库，最多 20 个 */
export interface CardsFilesPayload {
  files: Array<{ file_id: number; name: string; rel_path: string; root_id: number; mtime: string | null }>;
}

export interface FulltextPayload {
  variants: string[];
  total: number;
  meeting_count: number;
  meetings: Array<{ meeting_id: string; title: string; date: string; count: number; first_ms: number }>;
}

export interface FocusDecision {
  /** 决议台账里的 id（4a）；台账落后或旧后台时为 null 或没有 */
  id?: string | null;
  text: string;
  start_ms: number | null;
  end_ms?: number | null;
  detail?: string;
}

export interface FocusTask {
  id: string;
  title: string;
  detail: string;
  status: string;
  anchor_ms: number | null;
  anchor_quote: string;
  project_id: string | null;
  requirement_id: string | null;
  requirement_title: string | null;
  deliverables: FocusDeliverable[];
}

/** 展开一场会时任务的交付物；file 类带 file_id、name、gone（3g，旧后端没有） */
export interface FocusDeliverable {
  id?: number;
  kind: string;
  url: string;
  title: string;
  file_id?: number | null;
  name?: string;
  gone?: boolean;
}

export interface FocusNeighbour {
  meeting_id: string;
  title: string;
  date: string;
}

export interface MeetingFocus {
  meeting: MeetingBrief["meeting"];
  summary: string;
  decisions: FocusDecision[];
  decisions_note: string | null;
  tasks: FocusTask[];
  tasks_more: number;
  requirements: Array<{ id: string; title: string; priority: string; status: string; project_id: string }>;
  previous: FocusNeighbour | null;
  next: FocusNeighbour | null;
}

export interface CollapsedPayload {
  id: string;
  label: string;
  months: Array<{
    month: string;
    label: string;
    meetings: Array<{ meeting_id: string; title: string; date: string; open_tasks: number }>;
  }>;
}

export interface BriefTask {
  id: string;
  title: string;
  status: string;
  anchor_ms: number | null;
  anchor_quote: string;
  project_id: string | null;
  requirement_id: string | null;
}

export interface MeetingBrief {
  meeting: {
    id: string;
    title: string;
    date: string;
    duration_ms: number | null;
    project_id: string | null;
    project_name: string | null;
    project_color: string | null;
    has_minutes: boolean;
    audio_url: string | null;
  };
  attribution: MeetingAttribution;
  evidence_quotes: Array<{
    cue: string;
    source: string;
    count: number;
    quotes: Array<{ start_ms: number; text: string }>;
  }>;
  summary: string;
  /** id：决议台账里的 id（4a），台账落后时为 null */
  decisions: Array<{ id?: string | null; text: string; start_ms: number | null }>;
  decisions_note: string | null;
  tasks: BriefTask[];
  tasks_more: number;
  requirements: Array<{ id: string; title: string; priority: string; status: string; project_id: string; project_name: string | null }>;
  card: MeetingCard | null;
  /** 这场会提到的全部文件（最多 20 条，按次数排，通用的排最后） */
  files: BriefFile[];
  files_state: FilesState;
}

/** 简报里的文件列表为空时按这个说：认完了没提到、还在认、盘没插、会没归项目、项目没挂文件夹 */
export type FilesState = "done" | "indexing" | "offline" | "no_project" | "no_root";

export interface BriefFile {
  file_id: number;
  name: string;
  rel_path: string;
  root_id: number;
  /** reject / restore / pick 的路径参数 */
  stem_key: string;
  /** 会上说的那个词，如「报价单」 */
  needle: string;
  /** 逐字稿里说了几次；只在纪要里写到时是 0 */
  count: number;
  minutes_count: number;
  first_ms: number | null;
  source: "transcript" | "minutes";
  /** 你手动换过文件 */
  picked: boolean;
  /** 通用词：本项目一半以上的会都提到，排在最后、淡一点 */
  generic: boolean;
}

/** 文件面板 GET /api/graph/files/{id}（2d） */
export interface GraphFileDetail {
  file: {
    id: number;
    name: string;
    ext: string;
    stem: string;
    stem_key: string;
    rel_path: string;
    root_id: number;
    root_path: string;
    /** 所在文件夹的绝对路径 */
    folder_path: string;
    /** 文件的绝对路径 */
    path: string;
    size: number | null;
    modified_at: string | null;
    zone: string;
    gone: boolean;
    project_id: string;
    project_name: string;
  };
  /** 3g：读到哪一步（同预览）、是哪些任务的交付物；旧后端没有 */
  state?: MaterialFileState;
  deliverables?: MaterialDeliverable[];
  /** 同名的其他文件（报价单 v1、v2） */
  siblings: Array<{ id: number; name: string; rel_path: string; root_id: number; modified_at: string | null }>;
  meetings: Array<{
    meeting_id: string;
    title: string;
    date: string;
    stem_key: string;
    needle: string;
    count: number;
    minutes_count: number;
    first_ms: number | null;
    anchors_ms: number[];
    source: "transcript" | "minutes";
    status: "active" | "rejected";
    picked: boolean;
    /** 第一次说到的那段原话 */
    quote: string;
  }>;
  active_meetings: number;
}

/** ［不是这份文件］［撤销］［换成这份］的结果 */
export interface FileMentionResult {
  meeting_id: string;
  project_id: string;
  stem_key: string;
  file_id: number;
  status: "active" | "rejected";
  picked: boolean;
}

export interface QuotesPayload {
  meeting_id: string;
  quotes: Array<{
    at: number;
    segments: Array<{ segment_id: string; start_ms: number; end_ms: number; text: string; speaker: string | null }>;
  }>;
}

export interface CueTermDetail {
  id: string;
  term: string;
  aliases: string[];
  also: string[];
  project_id: string | null;
  project_name: string | null;
  is_cue: boolean;
  confirmed: boolean;
  category: string;
  source: string;
  cue_meetings: Array<{
    meeting_id: string;
    title: string;
    date: string;
    count: number;
    anchors_ms: number[];
    origin: string | null;
    only_cue: boolean;
  }>;
}
