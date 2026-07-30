import { describe, expect, it, vi } from "vitest";

import type { ApiClient } from "./api";
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
});
