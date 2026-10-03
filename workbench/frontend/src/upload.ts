import { ApiError, type ApiClient, type UploadReceipt } from "./api";

const ALLOWED_AUDIO_EXTENSION = /\.(m4a|mp3|wav)$/i;

function blobToBase64(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取录音分块"));
    reader.onload = () => {
      const value = reader.result;
      if (typeof value !== "string") {
        reject(new Error("无法编码录音分块"));
        return;
      }
      const separator = value.indexOf(",");
      if (separator < 0) {
        reject(new Error("无法编码录音分块"));
        return;
      }
      resolve(value.slice(separator + 1));
    };
    reader.readAsDataURL(blob);
  });
}

// 一块传失败先等一下再试：网络抖一下、后端忙一下不至于让几百兆的录音整段重来。同一块重复写是覆盖，重试安全
const CHUNK_RETRY_DELAYS_MS = [1_000, 3_000];

/** 后端明确拒收的（4xx，比如分块大小不对、会话不存在）重试也没用；没连上（status 0）、5xx 才重试 */
function worthRetrying(error: unknown): boolean {
  return !(error instanceof ApiError) || error.status === 0 || error.status >= 500;
}

/** 用户点了［取消上传］：不是故障，界面不弹失败，只说一句已取消 */
export class UploadCancelledError extends Error {
  constructor() {
    super("上传已取消");
    this.name = "UploadCancelledError";
  }
}

/** 等 ms 毫秒；等的时候信号取消了就提前醒来，醒来以后由调用方查信号 */
function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal?.addEventListener("abort", done, { once: true });
  });
}

export async function uploadRecordingInChunks(
  apiClient: Pick<ApiClient, "startUpload" | "uploadChunk" | "completeUpload" | "cancelUpload">,
  file: File,
  hotwords: string[] = [],
  /** 每传完一块回调一次（已传字节, 总字节），界面用来显示进度。 */
  onProgress?: (sentBytes: number, totalBytes: number) => void,
  /** signal：用户点了取消就中止正在发的那一块、不再发后面的，并清掉服务端的会话，最后抛 UploadCancelledError */
  { retryDelaysMs = CHUNK_RETRY_DELAYS_MS, signal }: { retryDelaysMs?: number[]; signal?: AbortSignal } = {},
): Promise<UploadReceipt> {
  if (!ALLOWED_AUDIO_EXTENSION.test(file.name)) {
    throw new Error("仅支持 m4a、mp3、wav 录音");
  }
  if (file.size <= 0) throw new Error("录音文件为空");

  const session = await apiClient.startUpload(file.name, file.size, hotwords);
  try {
    const stopIfCancelled = () => {
      if (signal?.aborted) throw new UploadCancelledError();
    };
    const expectedChunkCount = Math.ceil(file.size / session.chunk_bytes);
    if (
      !Number.isSafeInteger(session.chunk_bytes) ||
      session.chunk_bytes <= 0 ||
      !Number.isSafeInteger(session.chunk_count) ||
      session.chunk_count !== expectedChunkCount
    ) {
      throw new Error("服务端返回的上传分块参数无效");
    }

    for (let index = 0; index < session.chunk_count; index += 1) {
      stopIfCancelled();
      const start = index * session.chunk_bytes;
      const end = Math.min(start + session.chunk_bytes, file.size);
      const contentBase64 = await blobToBase64(file.slice(start, end));
      for (let attempt = 0; ; attempt += 1) {
        try {
          await apiClient.uploadChunk(session.upload_id, index, contentBase64, signal);
          break;
        } catch (error) {
          stopIfCancelled(); // 被中止的这一块不算失败，也不重试
          if (attempt >= retryDelaysMs.length || !worthRetrying(error)) throw error;
          await wait(retryDelaysMs[attempt], signal);
          stopIfCancelled();
        }
      }
      onProgress?.(end, file.size);
    }

    stopIfCancelled(); // 最后一块刚传完就点了取消：别让它入队
    return await apiClient.completeUpload(session.upload_id);
  } catch (error) {
    // 没传完的会话留在服务端会占着未完成上传的配额（24 小时才过期），失败两次大文件就导不进新录音了。
    // 取消是尽力而为：取消也失败（比如断网）就留给服务端过期清理，报的还是原来的错（取消上传报「已取消」）
    try {
      await apiClient.cancelUpload(session.upload_id);
    } catch {
      // 忽略
    }
    throw error;
  }
}
