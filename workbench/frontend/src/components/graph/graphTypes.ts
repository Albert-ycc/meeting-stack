// 关系图（1g、1h）接口的数据形状，和后端 graph.py 一一对应。

import type { MeetingAttribution, MeetingCard } from "../../types";

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
  id: string; // root:<id> / cards / rf:<id> / sub:<root id>:<相对路径>
  /** subfolder 是前端按资料盘状态补上的：根目录里最近改过的子文件夹 */
  kind: "root" | "cards" | "requirement_folder" | "subfolder";
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

export type GraphEdgeKind = "attribution" | "cue" | "discussion" | "folder" | "write" | "cross";

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
  folders: GraphFolder[];
  folders_more: { id: string; count: number; paths: string[] } | null;
  loose: { id: string; kind: "loose"; name: string; ring: Ring; count: number | null } | null;
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
  }>;
  folders: Array<{ id: string; folder_id: number; requirement_id: string; path: string; state: DiskState }>;
  loose: { count: number; recent: Array<{ name: string; path: string; size: number; mtime: string }> };
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
  files: Array<{ name: string; path: string; size: number; mtime: string }>;
  files_total: number;
}

export interface FulltextPayload {
  variants: string[];
  total: number;
  meeting_count: number;
  meetings: Array<{ meeting_id: string; title: string; date: string; count: number; first_ms: number }>;
}

export interface FocusDecision {
  text: string;
  start_ms: number | null;
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
  deliverables: Array<{ kind: string; url: string; title: string }>;
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
  decisions: Array<{ text: string; start_ms: number | null }>;
  decisions_note: string | null;
  tasks: BriefTask[];
  tasks_more: number;
  requirements: Array<{ id: string; title: string; priority: string; status: string; project_id: string; project_name: string | null }>;
  card: MeetingCard | null;
  files_note: string;
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
