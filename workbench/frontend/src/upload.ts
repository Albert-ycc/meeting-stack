import type { ApiClient, UploadReceipt } from "./api";

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

export async function uploadRecordingInChunks(
  apiClient: Pick<ApiClient, "startUpload" | "uploadChunk" | "completeUpload">,
  file: File,
  hotwords: string[] = [],
  /** 每传完一块回调一次（已传字节, 总字节），界面用来显示进度。 */
  onProgress?: (sentBytes: number, totalBytes: number) => void,
): Promise<UploadReceipt> {
  if (!ALLOWED_AUDIO_EXTENSION.test(file.name)) {
    throw new Error("仅支持 m4a、mp3、wav 录音");
  }
  if (file.size <= 0) throw new Error("录音文件为空");

  const session = await apiClient.startUpload(file.name, file.size, hotwords);
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
    const start = index * session.chunk_bytes;
    const end = Math.min(start + session.chunk_bytes, file.size);
    const contentBase64 = await blobToBase64(file.slice(start, end));
    await apiClient.uploadChunk(session.upload_id, index, contentBase64);
    onProgress?.(end, file.size);
  }

  return apiClient.completeUpload(session.upload_id);
}
