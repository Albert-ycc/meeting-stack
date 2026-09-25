import { afterEach, describe, expect, it, vi } from "vitest";

import { api, setCsrfToken } from "./api";

describe("API write protection", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCsrfToken("");
  });

  it("sends JSON, same-origin credentials and the CSRF token on writes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("local-token");

    await api.write("/api/meetings/vm-1/publish", "POST", {});

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/meetings/vm-1/publish",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        headers: expect.objectContaining({
          "Content-Type": "application/json",
          "X-CSRF-Token": "local-token",
        }),
        body: "{}",
      }),
    );
  });

  it("formats FastAPI validation detail arrays without leaking object strings", async () => {
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({
          detail: [
            { loc: ["body", "hotwords", 0], msg: "String should have at most 80 characters", type: "string_too_long" },
            { loc: ["body", "hotwords"], msg: "Value error, control characters are not allowed", type: "value_error" },
          ],
        }), { status: 422, headers: { "Content-Type": "application/json" } }),
    )));

    const error = await api.startUpload("meeting.m4a", 8, ["bad"]).catch((reason: unknown) => reason);
    expect(error).toMatchObject({ status: 422 });
    expect((error as Error).message).toContain("热词 第 1 项：String should have at most 80 characters");
    expect((error as Error).message).toContain("Value error, control characters are not allowed");
    expect((error as Error).message).not.toContain("[object Object]");
  });

  it("reads the selected transcript version segments from the version endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          version: { id: "tv-whisper", meeting_id: "vm-1", version_no: 2, kind: "whisper_reference" },
          items: [{ id: "seg-w", ordinal: 0, start_ms: 1000, end_ms: 2000, text: "对照稿" }],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const payload = await api.transcriptVersionSegments("vm-1", "tv-whisper");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/meetings/vm-1/transcript-versions/tv-whisper/segments",
      expect.objectContaining({ credentials: "same-origin" }),
    );
    expect(payload.items[0].text).toBe("对照稿");
  });

  it("creates projects and tags, then saves meeting classification with protected JSON writes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "project-1", name: "星河", color: "#376f68" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "tag-1", name: "待跟进", color: "#f0783b" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: "vm-1", title: "会议" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("classification-token");

    await api.createProject("星河", "#376f68");
    await api.createTag("待跟进", "#f0783b");
    await api.updateMeeting("vm-1", { project_id: "project-1", tag_ids: ["tag-1"] });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/projects",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ name: "星河", color: "#376f68" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/meetings/vm-1",
      expect.objectContaining({
        method: "PATCH",
        headers: expect.objectContaining({ "X-CSRF-Token": "classification-token" }),
        body: JSON.stringify({ project_id: "project-1", tag_ids: ["tag-1"] }),
      }),
    );
  });

  it("uses protected chunked JSON upload endpoints", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ upload_id: "upload-1", chunk_bytes: 4, chunk_count: 2 }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ upload_id: "upload-1", index: 0, bytes: 4 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ path: "/tmp/meeting.m4a", size_bytes: 4, status: "queued", job_id: "job-1" }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("upload-token");

    await api.startUpload("meeting.m4a", 8);
    await api.uploadChunk("upload-1", 0, "YXVkaQ==");
    await api.completeUpload("upload-1");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/uploads/start",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ filename: "meeting.m4a", size_bytes: 8 }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/uploads/upload-1/chunks/0",
      expect.objectContaining({
        method: "PUT",
        headers: expect.objectContaining({ "X-CSRF-Token": "upload-token" }),
        body: JSON.stringify({ content_base64: "YXVkaQ==" }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/uploads/upload-1/complete",
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
  });

  it("requests meeting retranscription and resolves each supported conflict action", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ status: "queued" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.retranscribe("vm-1");
    await api.resolveConflict("vm-1", "conflict-1", "keep_draft");
    await api.resolveConflict("vm-1", "conflict-1", "accept_external");
    await api.resolveConflict("vm-1", "conflict-1", "discard_draft");

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/meetings/vm-1/retranscribe",
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/meetings/vm-1/conflicts/conflict-1/resolve",
      expect.objectContaining({ body: JSON.stringify({ action: "keep_draft" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/meetings/vm-1/conflicts/conflict-1/resolve",
      expect.objectContaining({ body: JSON.stringify({ action: "accept_external" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/api/meetings/vm-1/conflicts/conflict-1/resolve",
      expect.objectContaining({ body: JSON.stringify({ action: "discard_draft" }) }),
    );
  });

  it("sends optimistic base versions when saving transcript and minutes", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ version_id: "version-new" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.saveTranscript(
      "vm-1",
      [{ id: "seg-1", ordinal: 0, start_ms: 0, end_ms: 1_000, text: "正文" }],
      "tv-current",
    );
    await api.saveMinutes("vm-1", "# 纪要", null);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/meetings/vm-1/transcript",
      expect.objectContaining({
        body: JSON.stringify({
          base_version_id: "tv-current",
          segments: [
            {
              id: "seg-1",
              ordinal: 0,
              start_ms: 0,
              end_ms: 1_000,
              text: "正文",
            },
          ],
        }),
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "/api/meetings/vm-1/minutes",
      expect.objectContaining({
        body: JSON.stringify({ base_version_id: null, markdown: "# 纪要" }),
      }),
    );
  });

  it("requests a fixed 50-item meeting page and preserves the total", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], limit: 50, offset: 50, total: 120 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    const payload = await api.meetings({ project_id: "project-1", limit: 50, offset: 50 });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/meetings?project_id=project-1&limit=50&offset=50",
      expect.any(Object),
    );
    expect(payload.total).toBe(120);
  });

  it("retries a non-blocking job substate independently", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ status: "pending" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);

    await api.retryJobSubstate("job-1", "whisper");

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/jobs/job-1/substates/whisper/retry",
      expect.objectContaining({ method: "POST", body: "{}" }),
    );
  });

  it("keeps main state and non-blocking substate statuses separate when listing jobs", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          items: [
            {
              job_id: "job-1",
              status: "completed_unreviewed",
              whisper_status: "failed",
              index_status: "ready",
              created_at: "2026-07-10T00:00:00Z",
              updated_at: "2026-07-10T00:01:00Z",
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    const payload = await api.jobs();

    expect(payload.items[0]).toMatchObject({
      id: "job-1",
      state: "completed_unreviewed",
      whisper_status: "failed",
      index_status: "ready",
    });
  });

  it("uses the quality review endpoints and keeps hotwords in JSON bodies", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ items: [], coverage: {}, topics: [], anchors: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("quality-token");

    await api.transcriptComparison("vm-1", "tv-qwen");
    await api.asrGoldSamples("vm-1");
    await api.saveAsrGoldSample("vm-1", {
      segment_id: "seg-1",
      reference: "人工校正",
      entities: [],
      numbers: [],
      tags: [],
    });
    await api.requestQwenShadow("vm-1");
    await api.retryQwenShadow("vm-1", "shadow-1");
    await api.minutesEvidence("vm-1");
    await api.retryJob("job-1", "transcribing", ["ACME", "云图"]);
    await api.retranscribe("vm-1", ["MDT"]);
    await api.startUpload("meeting.m4a", 8, ["药品名"]);

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/meetings/vm-1/transcript-comparison?candidate_version_id=tv-qwen",
      expect.any(Object),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/meetings/vm-1/asr-gold-samples",
      expect.objectContaining({ body: JSON.stringify({ segment_id: "seg-1", reference: "人工校正", entities: [], numbers: [], tags: [] }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(4, "/api/meetings/vm-1/asr-shadow/qwen", expect.objectContaining({ body: "{}" }));
    expect(fetchMock).toHaveBeenNthCalledWith(5, "/api/meetings/vm-1/asr-shadow/qwen/shadow-1/retry", expect.objectContaining({ body: "{}" }));
    expect(fetchMock).toHaveBeenNthCalledWith(7, "/api/jobs/job-1/retry", expect.objectContaining({ body: JSON.stringify({ stage: "transcribing", hotwords: ["ACME", "云图"] }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(8, "/api/meetings/vm-1/retranscribe", expect.objectContaining({ body: JSON.stringify({ hotwords: ["MDT"] }) }));
    expect(fetchMock).toHaveBeenNthCalledWith(9, "/api/uploads/start", expect.objectContaining({ body: JSON.stringify({ filename: "meeting.m4a", size_bytes: 8, hotwords: ["药品名"] }) }));
  });

  it("normalizes the Qwen job substate without changing the main pipeline", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        items: [{
          job_id: "job-qwen",
          status: "completed_unreviewed",
          created_at: "2026-07-10T00:00:00Z",
          updated_at: "2026-07-10T00:01:00Z",
          substates: { qwen: { status: "queued" } },
        }],
      }), { status: 200, headers: { "Content-Type": "application/json" } }),
    ));

    const payload = await api.jobs();

    expect(payload.items[0].state).toBe("completed_unreviewed");
    expect(payload.items[0].substates?.qwen?.status).toBe("queued");
  });

  it("按 project_id 查询词典术语、拉取分组 chips、按归属三态写 project_id/scope", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    setCsrfToken("glossary-token");

    await api.glossaryTerms({ project_id: "project-1" });
    await api.glossaryScopes();
    await api.createGlossaryTerm({ term: "生长激素", project_id: "project-1" });
    await api.updateGlossaryTerm("term-1", { project_id: null, scope: "儿科" });

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/glossary/terms?project_id=project-1", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/glossary/scopes", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      "/api/glossary/terms",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ term: "生长激素", project_id: "project-1" }) }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      "/api/glossary/terms/term-1",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ project_id: null, scope: "儿科" }) }),
    );
  });
});
