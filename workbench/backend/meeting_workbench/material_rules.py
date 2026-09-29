"""哪些材料文件读什么（第三期）：文件名索引、内容读取、1f 盘点、关系图浏览共用这一套规则。

| 层 | 怎么读 |
|---|---|
| text | 文档正文：纯文字和代码、OOXML/ODF/EPUB/HTML、textutil 读的老 Word 和 RTF、老 Office 复合文档 |
| pdf | PDF：文字层由 PDFKit 读，字少的页再认字（3c） |
| image | 图片文字（3c） |
| media | 音视频转写（3d） |
| unsupported | Keynote、Pages、Numbers：不读，按扩展名直接记「格式不支持」 |
| 其余 | 只收文件名，不读，也不算读不了 |
"""

from __future__ import annotations

AUDIO_EXTS = frozenset(
    {"mp3", "m4a", "wav", "aac", "flac", "ogg", "opus", "wma", "amr", "aiff", "aif", "caf"}
)
VIDEO_EXTS = frozenset(
    {"mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv", "flv", "3gp", "mts", "m2ts"}
)
# 系统影子文件：静默跳过，不进任何统计
SYSTEM_NAMES = frozenset(
    {
        ".DS_Store",
        "__MACOSX",
        ".Spotlight-V100",
        ".Trashes",
        ".fseventsd",
        ".TemporaryItems",
        ".DocumentRevisions-V100",
        ".VolumeIcon.icns",
        ".apdisk",
        "Thumbs.db",
        "desktop.ini",
        "Icon\r",
    }
)

LAYER_TEXT = "text"
LAYER_PDF = "pdf"
LAYER_IMAGE = "image"
LAYER_MEDIA = "media"
LAYER_UNSUPPORTED = "unsupported"
CONTENT_LAYERS = (LAYER_TEXT, LAYER_PDF, LAYER_IMAGE, LAYER_MEDIA)

# 纯文字（含代码文件）：按编码猜着读
PLAIN_TEXT_EXTS = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "log",
        "csv",
        "tsv",
        "json",
        "yaml",
        "yml",
        "xml",
        "ini",
        "toml",
        "conf",
        "sql",
        "ipynb",
        "srt",
        "vtt",
        "tex",
        "eml",
        "mht",
        "mhtml",
        "py",
        "js",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "jsx",
        "css",
        "scss",
        "less",
        "sh",
        "bat",
        "ps1",
        "java",
        "go",
        "rs",
        "c",
        "h",
        "cpp",
        "hpp",
        "cs",
        "rb",
        "php",
        "swift",
        "kt",
        "gradle",
        "vue",
        "svelte",
        "plist",
        "cfg",
    }
)
# 标准库解压，按 XML 或 HTML 读
OOXML_EXTS = frozenset(
    {"docx", "docm", "dotx", "xlsx", "xlsm", "xltx", "pptx", "pptm", "ppsx", "potx"}
)
ODF_EXTS = frozenset({"odt", "ods", "odp"})
MARKUP_EXTS = frozenset({"epub", "html", "htm"})
# macOS 自带的 textutil
TEXTUTIL_EXTS = frozenset({"doc", "dot", "rtf", "rtfd", "wps"})
# 老 Office 复合文档
LEGACY_OFFICE_EXTS = frozenset({"xls", "xlt", "et", "ppt", "pps", "dps"})
TEXT_LAYER_EXTS = (
    PLAIN_TEXT_EXTS | OOXML_EXTS | ODF_EXTS | MARKUP_EXTS | TEXTUTIL_EXTS | LEGACY_OFFICE_EXTS
)
PDF_EXTS = frozenset({"pdf"})
IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "heic", "heif", "gif", "bmp", "tif", "tiff", "webp"})
MEDIA_EXTS = frozenset(AUDIO_EXTS | VIDEO_EXTS)
IWORK_EXTS = frozenset({"key", "pages", "numbers"})
# 二进制和可能带密钥的配置：只收文件名
NAME_ONLY_EXTS = frozenset({"pyc", "class", "o", "so", "dll", "dylib", "whl", "env", "lock", "map"})

CONTENT_EXTS = TEXT_LAYER_EXTS | PDF_EXTS | IMAGE_EXTS | MEDIA_EXTS
# 预览按表格画（前 5 行）
TABLE_EXTS = frozenset({"xlsx", "xlsm", "xltx", "xls", "xlt", "et", "csv", "tsv", "ods"})
# 浏览器放得了的音视频：只有这些给播放地址，别的回 415，不转码
PLAYABLE_TYPES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "m4v": "video/mp4",
    "webm": "video/webm",
}

# —— 读不了的五种原因（1f 盘点、覆盖率、预览、搜索共用）——
PASSWORD = "password"
CORRUPT = "corrupt"
UNSUPPORTED = "unsupported"
TIMEOUT = "timeout"
PERMISSION = "permission"
REASON_LABELS = {
    PASSWORD: "要密码",
    CORRUPT: "文件损坏",
    UNSUPPORTED: "格式不支持",
    TIMEOUT: "处理超时",
    PERMISSION: "没有权限",
}

# —— 文件状态的说法（第三期 3e）：预览抽屉、文件面板、搜索结果、项目页、关系图共用，每种一句话 ——
STATE_TEXTS = {
    "pending": "还没读到内容，读完后这里能预览",
    "paused": "转写会议时先停，转写完接着读",
    "offline": "资料盘未连接，先看上次读到的",
    "retrying": "上次没读出来，过一会儿再试",
    "names_only": "这种文件只收文件名",
    "cards": "声档写的会议卡片，只收文件名",
    "gone": "找不到这个文件了，可能已经删掉或挪走了",
    "small_image": "图太小，没认字",
    "no_text": "图里没认出字",
    "no_speech": "没听到说话声",
    "vision_failed": "认字程序没编译成功，在终端运行 meeting-workbench doctor 看原因",
}
# 识别程序没装：缺什么写什么，装好后自动接着读
WAITING_TEXTS = {
    "vision": "要先装 Xcode 命令行工具才能读 PDF、认图片里的字：在终端运行 xcode-select --install",
    "tesseract": "要先装 tesseract 才能认图片里的字：在终端运行 brew install tesseract tesseract-lang",
    "textutil": "这种老文档要用 Mac 自带的 textutil 读，在 Mac 上运行声档才能读",
    "ffmpeg": "要先装 ffmpeg 才能转写录音：在终端运行 brew install ffmpeg",
    "funasr": "没找到转写会议用的 FunASR，在终端运行 meeting-workbench doctor 看原因",
}
TRUNCATED_TEXTS = {
    "text": "只收了前 20 万字",
    "media": "只转了前 6 小时",
}


def unreadable_text(reason: str) -> str:
    return f"读不了：{REASON_LABELS.get(reason, '原因不明')}。文件名照样能搜到"


def truncated_text(layer: str | None, pages: int | None = None) -> str:
    if layer == "pdf" and pages:
        return f"只读了前 {pages} 页"
    return TRUNCATED_TEXTS.get(layer or "", "只收了前面一部分")


def meeting_audio_text(title: str | None) -> str:
    return f"这是会议『{title or '未命名会议'}』的录音，内容看会议"


def file_ext(name: str) -> str:
    _stem, dot, ext = name.rpartition(".")
    return ext.lower() if dot and _stem else ""


def layer_for_ext(ext: str) -> str | None:
    """扩展名对应的层；None 是只收文件名。"""
    ext = ext.lower()
    if ext in NAME_ONLY_EXTS:
        return None
    if ext in IWORK_EXTS:
        return LAYER_UNSUPPORTED
    if ext in TEXT_LAYER_EXTS:
        return LAYER_TEXT
    if ext in PDF_EXTS:
        return LAYER_PDF
    if ext in IMAGE_EXTS:
        return LAYER_IMAGE
    if ext in MEDIA_EXTS:
        return LAYER_MEDIA
    return None


def layer_for(name: str) -> str | None:
    return layer_for_ext(file_ext(name))


def silent_skip(name: str) -> bool:
    """静默跳过：系统影子文件、Office 的 ~$ 锁文件、LibreOffice 的 .~lock.*# 锁文件。"""
    return (
        name.startswith("._")
        or name in SYSTEM_NAMES
        or name.startswith("~$")
        or (name.startswith(".~lock.") and name.endswith("#"))
    )


def hidden_in_browse(name: str) -> bool:
    """浏览时不列：静默跳过的，和点开头的文件、文件夹。"""
    return silent_skip(name) or name.startswith(".")
