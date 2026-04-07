"""
Shared extension → doc_type buckets for chunk metadata and list_silos breakdown.

Keeps ingest path tagging aligned with operations._doc_type_breakdown.
"""
from __future__ import annotations

# Union of ingest ADD_CODE_EXTENSIONS and operations _CODE_EXTS (single source of truth).
CODE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".c",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
        ".php",
        ".kt",
        ".sql",
        ".swift",
        ".scala",
        ".r",
        ".lua",
        ".pl",
    }
)

# Structured office / PDF categories (matches historical list_silos breakdown).
STRUCTURED_DOC_EXTENSIONS: dict[str, str] = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "docx",
    ".xlsx": "xlsx",
    ".xls": "xlsx",
    ".csv": "xlsx",
    ".pptx": "pptx",
    ".ppt": "pptx",
}


def doc_type_bucket_for_extension(ext: str) -> str:
    """
    Map a file extension (with leading dot, lowercased) to breakdown/chunk bucket:
    pdf, docx, xlsx, pptx, code, or other.
    """
    e = (ext or "").lower()
    if e in STRUCTURED_DOC_EXTENSIONS:
        return STRUCTURED_DOC_EXTENSIONS[e]
    if e in CODE_EXTENSIONS:
        return "code"
    return "other"
