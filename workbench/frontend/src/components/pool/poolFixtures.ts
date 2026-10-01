/*
 * 需求池用例的数据。会名、录音时间、时长、会上原话和时间锚照生产库逐字稿抄（和后端
 * requirement_pool_world 是同一份），需求和候选的标题、说明用《口径书》的示意数据。
 * 用例的时区钉在 Asia/Shanghai：-07:00 的 09-16 19:01 显示成 09-17 10:01。
 */
import type { PoolItem, PoolProject, RequirementPoolPayload, RequirementSource } from "../../types";

export const YIMI = "project-59314319a40c43ea";
export const HENGRUI = "project-587f318a77c84d46";
export const HUAXIA = "project-2de1cdc1e9364813";
export const CVM = "project-592ef4b19a60442a";

export const JD_SOURCE: RequirementSource = {
  id: 1,
  kind: "origin",
  meeting_id: "vm-20260916-190150-2eebb406",
  meeting_title: "260916 医米京东科研仓系统对接",
  recording_date: "2026-09-16T19:01:50-07:00",
  duration_ms: 3033387,
  audio_artifact_id: null,
  quote: "就是这个入库单的这个单据，你得需要从你们的一米这个系统里面给我们这个库房推过来。",
  anchor_ms: 825270,
  via_candidate_title: null,
};

export const RECEIPT_SOURCE: RequirementSource = {
  ...JD_SOURCE,
  id: 2,
  quote: "强制要求患者除了传教和随行码嗯患者手持身份证跟药盒拍照，然后还要传这个随货通行单，",
  anchor_ms: 1909360,
};

export const EXPORT_SOURCE: RequirementSource = {
  id: 3,
  kind: "origin",
  meeting_id: "vm-20260929-192637-f3947874",
  meeting_title: "260929 云课堂直播运营问题对齐",
  recording_date: "2026-09-29T19:26:37-07:00",
  duration_ms: 747000,
  audio_artifact_id: null,
  quote: "那我有办法导出 excel 吗？是没有办法，",
  anchor_ms: 576900,
  via_candidate_title: null,
};

export function requirementItem(overrides: Partial<PoolItem> = {}): PoolItem {
  return {
    kind: "requirement",
    id: "requirement-jd",
    title: "京东科研仓对接",
    summary: "把京东科研仓当作一个药房接进医米：采购单入库、销售单、订单取消、物流轨迹四类接口必须对上，签收凭证怎么拿还悬着。",
    status: "active",
    priority: "P0",
    project_id: YIMI,
    project_name: "医米科研用药",
    project_color: "#2c8d83",
    project_seat: 1,
    open_task_count: 0,
    meeting_count: 2,
    folder_count: 1,
    latest_meeting_date: "2026-09-16T19:01:50-07:00",
    source: JD_SOURCE,
    follow_up_count: 1,
    similar_requirement: null,
    default_action: null,
    can_merge: false,
    created_at: "2026-09-16T06:48:37+00:00",
    updated_at: "2026-09-16T06:48:37+00:00",
    ...overrides,
  };
}

export function candidateItem(overrides: Partial<PoolItem> = {}): PoolItem {
  return {
    kind: "candidate",
    id: "candidate-receipt",
    title: "京东仓签收凭证",
    summary: "京东妥投只靠配送员点击，医米拿不到患者签收凭证；月结上千万物流费不能只凭汇总表付款。",
    status: "pending",
    priority: null,
    project_id: YIMI,
    project_name: "医米科研用药",
    project_color: "#2c8d83",
    project_seat: 1,
    open_task_count: 0,
    meeting_count: 1,
    folder_count: 0,
    latest_meeting_date: "2026-09-16T19:01:50-07:00",
    source: RECEIPT_SOURCE,
    follow_up_count: 0,
    similar_requirement: { id: "requirement-jd", title: "京东科研仓对接", status: "active" },
    default_action: "merge",
    can_merge: true,
    created_at: "2026-10-01T02:00:00+00:00",
    updated_at: "2026-10-01T02:00:00+00:00",
    requirement_id: null,
    dropped_at: null,
    ...overrides,
  };
}

export const DIRECTION: PoolProject[] = [
  { id: YIMI, name: "医米科研用药", color: "#2c8d83", seat: 1, latest_meeting_date: null, count: 2 },
  { id: HENGRUI, name: "恒瑞健康", color: "#549c8a", seat: 2, latest_meeting_date: null, count: 2 },
  { id: HUAXIA, name: "华夏基金会科普同行", color: "#2c8d83", seat: 3, latest_meeting_date: null, count: 1 },
  { id: CVM, name: "CVM 云讲堂", color: "#5090ff", seat: null, latest_meeting_date: "2026-09-29T19:26:37-07:00", count: 1 },
];

export function poolPayload(overrides: Partial<RequirementPoolPayload> = {}): RequirementPoolPayload {
  return {
    items: [requirementItem()],
    total: 1,
    limit: 500,
    offset: 0,
    status: "active",
    counts: { pending: 3, active: 9, done: 1, shelved: 1, all: 14 },
    projects: DIRECTION,
    unassigned_count: 0,
    dropped_count: 0,
    ...overrides,
  };
}
