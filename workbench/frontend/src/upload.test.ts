import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "./api";
import { uploadRecordingInChunks } from "./upload";

describe("uploadRecordingInChunks", () => {
  it("reads and uploads one bounded File slice at a time", async () => {
    const file = new File(["abcdefg"], "meeting.m4a", { type: "audio/mp4" });
    const slice = vi.spyOn(file, "slice");
    const uploadChunk = vi.fn().mockResolvedValue({});
    const completeUpload = vi.fn().mockResolvedValue({
      path: "/tmp/meeting.m4a",
      size_bytes: file.size,
      status: "queued",
      job_id: "job-1",
    });
    const apiClient = {
      startUpload: vi.fn().mockResolvedValue({
        upload_id: "upload-1",
        chunk_bytes: 3,
        chunk_count: 3,
      }),
      uploadChunk,
      completeUpload,
    } as unknown as ApiClient;

    const receipt = await uploadRecordingInChunks(apiClient, file, ["ACME", "云图"]);

    expect(slice.mock.calls).toEqual([
      [0, 3],
      [3, 6],
      [6, 7],
    ]);
    expect(uploadChunk.mock.calls.map(([, index]) => index)).toEqual([0, 1, 2]);
    expect(uploadChunk.mock.calls.map(([, , base64]) => atob(base64))).toEqual([
      "abc",
      "def",
      "g",
    ]);
    expect(completeUpload).toHaveBeenCalledWith("upload-1");
    expect(apiClient.startUpload).toHaveBeenCalledWith("meeting.m4a", file.size, ["ACME", "云图"]);
    expect(receipt.job_id).toBe("job-1");
  });

  it("rejects unsupported extensions before creating an upload session", async () => {
    const startUpload = vi.fn();
    const apiClient = { startUpload } as unknown as ApiClient;

    await expect(
      uploadRecordingInChunks(apiClient, new File(["x"], "meeting.aac")),
    ).rejects.toThrow("仅支持 m4a、mp3、wav 录音");
    expect(startUpload).not.toHaveBeenCalled();
  });

  const session = { upload_id: "upload-1", chunk_bytes: 3, chunk_count: 3 };
  const receipt = { path: "/tmp/meeting.m4a", size_bytes: 7, status: "queued", job_id: "job-1" };
  const noWait = { retryDelaysMs: [0, 0] };

  it("一块传失败（网络抖了、后端 5xx）先重试，重试成功整段照常传完", async () => {
    const uploadChunk = vi
      .fn()
      .mockResolvedValueOnce({})
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockRejectedValueOnce(new ApiError("Internal Server Error", 502))
      .mockResolvedValue({});
    const apiClient = {
      startUpload: vi.fn().mockResolvedValue(session),
      uploadChunk,
      completeUpload: vi.fn().mockResolvedValue(receipt),
      cancelUpload: vi.fn(),
    } as unknown as ApiClient;

    const result = await uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, noWait);

    expect(result.job_id).toBe("job-1");
    expect(uploadChunk.mock.calls.map(([, index]) => index)).toEqual([0, 1, 1, 1, 2]);
    expect(apiClient.cancelUpload).not.toHaveBeenCalled();
  });

  it("重试用完还失败：取消服务端的上传会话放掉配额，报原来的错", async () => {
    const uploadChunk = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const cancelUpload = vi.fn().mockResolvedValue({ ok: true });
    const apiClient = {
      startUpload: vi.fn().mockResolvedValue(session),
      uploadChunk,
      completeUpload: vi.fn(),
      cancelUpload,
    } as unknown as ApiClient;

    await expect(
      uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, noWait),
    ).rejects.toThrow("Failed to fetch");
    expect(uploadChunk).toHaveBeenCalledTimes(3);
    expect(cancelUpload).toHaveBeenCalledWith("upload-1");
    expect(apiClient.completeUpload).not.toHaveBeenCalled();
  });

  it("后端明确拒收的块（409）不重试；取消会话失败也不盖掉原来的错", async () => {
    const uploadChunk = vi.fn().mockRejectedValue(new ApiError("分块超过限制", 409));
    const cancelUpload = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    const apiClient = {
      startUpload: vi.fn().mockResolvedValue(session),
      uploadChunk,
      completeUpload: vi.fn(),
      cancelUpload,
    } as unknown as ApiClient;

    await expect(
      uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, noWait),
    ).rejects.toThrow("分块超过限制");
    expect(uploadChunk).toHaveBeenCalledTimes(1);
    expect(cancelUpload).toHaveBeenCalledWith("upload-1");
  });
});
