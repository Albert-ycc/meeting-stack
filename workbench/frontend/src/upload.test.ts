import { describe, expect, it, vi } from "vitest";

import { ApiError, type ApiClient } from "./api";
import { UploadCancelledError, uploadRecordingInChunks } from "./upload";

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

  describe("用户点了［取消上传］", () => {
    /** 会听信号的 uploadChunk：中止时和真的 fetch 一样抛 AbortError；index 小于 hangFrom 的块照常传完 */
    function chunkSender(hangFrom: number) {
      const signals: Array<AbortSignal | undefined> = [];
      const uploadChunk = vi.fn((_uploadId: string, index: number, _content: string, signal?: AbortSignal) => {
        signals.push(signal);
        if (index < hangFrom) return Promise.resolve({});
        return new Promise((_resolve, reject) => {
          signal?.addEventListener("abort", () => reject(new DOMException("请求已取消", "AbortError")), { once: true });
        });
      });
      return { uploadChunk, signals };
    }

    function clientWith(overrides: Record<string, unknown>) {
      return {
        startUpload: vi.fn().mockResolvedValue(session),
        uploadChunk: vi.fn().mockResolvedValue({}),
        completeUpload: vi.fn().mockResolvedValue(receipt),
        cancelUpload: vi.fn().mockResolvedValue({ ok: true }),
        ...overrides,
      } as unknown as ApiClient;
    }

    it("分块发到一半点取消：中止这一块的请求、不再发后面的块、清掉服务端会话；被中止的块不算失败、不重试", async () => {
      const controller = new AbortController();
      const { uploadChunk, signals } = chunkSender(1);
      const apiClient = clientWith({ uploadChunk });
      const onProgress = vi.fn();

      const settled = uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], onProgress, {
        ...noWait,
        signal: controller.signal,
      }).catch((error: unknown) => error);
      await vi.waitFor(() => expect(uploadChunk).toHaveBeenCalledTimes(2)); // 第 0 块传完了，第 1 块在路上
      expect(signals[1]).toBeInstanceOf(AbortSignal);
      expect(signals[1]?.aborted).toBe(false);

      controller.abort();
      const error = await settled;

      expect(error).toBeInstanceOf(UploadCancelledError);
      expect(signals[1]?.aborted).toBe(true);
      expect(uploadChunk).toHaveBeenCalledTimes(2); // 被中止的那块没重试，第 2 块没发
      expect(apiClient.cancelUpload).toHaveBeenCalledTimes(1);
      expect(apiClient.cancelUpload).toHaveBeenCalledWith("upload-1");
      expect(apiClient.completeUpload).not.toHaveBeenCalled();
      expect(onProgress).toHaveBeenCalledTimes(1); // 只有第 0 块报过进度
    });

    it("已经没有重试余量（最后一次尝试）时被中止：报的还是「已取消」，不是浏览器的 AbortError", async () => {
      const controller = new AbortController();
      const { uploadChunk } = chunkSender(0);
      const apiClient = clientWith({ uploadChunk });

      const settled = uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, {
        retryDelaysMs: [],
        signal: controller.signal,
      }).catch((error: unknown) => error);
      await vi.waitFor(() => expect(uploadChunk).toHaveBeenCalledTimes(1));
      controller.abort();

      const error = await settled;
      expect(error).toBeInstanceOf(UploadCancelledError);
      expect(apiClient.cancelUpload).toHaveBeenCalledWith("upload-1");
    });

    it("没取消时信号照样交给每一块，整段照常传完", async () => {
      const controller = new AbortController();
      const apiClient = clientWith({});

      const result = await uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, {
        ...noWait,
        signal: controller.signal,
      });

      expect(result.job_id).toBe("job-1");
      expect(vi.mocked(apiClient.uploadChunk).mock.calls.map((call) => call[3])).toEqual([
        controller.signal,
        controller.signal,
        controller.signal,
      ]);
      expect(apiClient.cancelUpload).not.toHaveBeenCalled();
    });

    it("取消发生在重试的等待期间：马上结束，不等那几秒、不再重试", async () => {
      const controller = new AbortController();
      const uploadChunk = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
      const apiClient = clientWith({ uploadChunk });

      const settled = uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, {
        retryDelaysMs: [60_000, 60_000], // 等不及的话这条用例会卡到超时
        signal: controller.signal,
      }).catch((error: unknown) => error);
      await vi.waitFor(() => expect(uploadChunk).toHaveBeenCalledTimes(1)); // 第 0 块失败，进了重试前的等待
      controller.abort();

      expect(await settled).toBeInstanceOf(UploadCancelledError);
      expect(uploadChunk).toHaveBeenCalledTimes(1);
      expect(apiClient.cancelUpload).toHaveBeenCalledWith("upload-1");
    });

    it("startUpload 还在路上时点了取消：回来以后一块都不发，把刚建出来的会话清掉", async () => {
      const controller = new AbortController();
      let created: (value: typeof session) => void = () => undefined;
      const startUpload = vi.fn(
        () =>
          new Promise<typeof session>((resolve) => {
            created = resolve;
          }),
      );
      const apiClient = clientWith({ startUpload });

      const settled = uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, {
        ...noWait,
        signal: controller.signal,
      }).catch((error: unknown) => error);
      await vi.waitFor(() => expect(startUpload).toHaveBeenCalledTimes(1));
      controller.abort();
      created(session);

      expect(await settled).toBeInstanceOf(UploadCancelledError);
      expect(apiClient.uploadChunk).not.toHaveBeenCalled();
      expect(apiClient.cancelUpload).toHaveBeenCalledWith("upload-1");
    });

    it("最后一块刚传完、complete 还没发就点了取消：照样取消，不让它入队", async () => {
      const controller = new AbortController();
      const uploadChunk = vi.fn(async () => {
        controller.abort();
        return {};
      });
      const apiClient = clientWith({
        startUpload: vi.fn().mockResolvedValue({ upload_id: "upload-1", chunk_bytes: 3, chunk_count: 1 }),
        uploadChunk,
      });

      await expect(
        uploadRecordingInChunks(apiClient, new File(["abc"], "meeting.m4a"), [], undefined, {
          ...noWait,
          signal: controller.signal,
        }),
      ).rejects.toBeInstanceOf(UploadCancelledError);
      expect(apiClient.completeUpload).not.toHaveBeenCalled();
      expect(apiClient.cancelUpload).toHaveBeenCalledWith("upload-1");
    });

    it("清服务端会话时断网了：报的还是「已取消」，不是网络错误", async () => {
      const controller = new AbortController();
      const { uploadChunk } = chunkSender(0);
      const apiClient = clientWith({ uploadChunk, cancelUpload: vi.fn().mockRejectedValue(new TypeError("Failed to fetch")) });

      const settled = uploadRecordingInChunks(apiClient, new File(["abcdefg"], "meeting.m4a"), [], undefined, {
        ...noWait,
        signal: controller.signal,
      }).catch((error: unknown) => error);
      await vi.waitFor(() => expect(uploadChunk).toHaveBeenCalledTimes(1));
      controller.abort();

      const error = await settled;
      expect(error).toBeInstanceOf(UploadCancelledError);
      expect((error as Error).message).toBe("上传已取消");
    });
  });
});
