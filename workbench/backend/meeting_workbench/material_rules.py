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

from .material_walk import AUDIO_EXTS, SYSTEM_NAMES, VIDEO_EXTS, file_ext

LAYER_TEXT = "text"
LAYER_PDF = "pdf"
LAYER_IMAGE = "image"
LAYER_MEDIA = "media"
LAYER_UNSUPPORTED = "unsupported"
CONTENT_LAYERS = (LAYER_TEXT, LAYER_PDF, LAYER_IMAGE, LAYER_MEDIA)

# 纯文字（含代码文件）：按编码猜着读
PLAIN_TEXT_EXTS = frozenset(
    {
        "txt", "md", "markdown", "log", "csv", "tsv", "json", "yaml", "yml", "xml", "ini", "toml",
        "conf", "sql", "ipynb", "srt", "vtt", "tex", "eml", "mht", "mhtml",
        "py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "css", "scss", "less", "sh", "bat", "ps1",
        "java", "go", "rs", "c", "h", "cpp", "hpp", "cs", "rb", "php", "swift", "kt", "gradle",
        "vue", "svelte", "plist", "cfg",
    }
)
# 标准库解压，按 XML 或 HTML 读
OOXML_EXTS = frozenset({"docx", "docm", "dotx", "xlsx", "xlsm", "xltx", "pptx", "pptm", "ppsx", "potx"})
ODF_EXTS = frozenset({"odt", "ods", "odp"})
MARKUP_EXTS = frozenset({"epub", "html", "htm"})
# macOS 自带的 textutil
TEXTUTIL_EXTS = frozenset({"doc", "dot", "rtf", "rtfd", "wps"})
# 老 Office 复合文档
LEGACY_OFFICE_EXTS = frozenset({"xls", "xlt", "et", "ppt", "pps", "dps"})
TEXT_LAYER_EXTS = PLAIN_TEXT_EXTS | OOXML_EXTS | ODF_EXTS | MARKUP_EXTS | TEXTUTIL_EXTS | LEGACY_OFFICE_EXTS
PDF_EXTS = frozenset({"pdf"})
IMAGE_EXTS = frozenset({"png", "jpg", "jpeg", "heic", "heif", "gif", "bmp", "tif", "tiff", "webp"})
MEDIA_EXTS = frozenset(AUDIO_EXTS | VIDEO_EXTS)
IWORK_EXTS = frozenset({"key", "pages", "numbers"})
# 二进制和可能带密钥的配置：只收文件名
NAME_ONLY_EXTS = frozenset({"pyc", "class", "o", "so", "dll", "dylib", "whl", "env", "lock", "map"})

CONTENT_EXTS = TEXT_LAYER_EXTS | PDF_EXTS | IMAGE_EXTS | MEDIA_EXTS


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
