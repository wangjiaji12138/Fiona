from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import pymupdf as fitz

logger = logging.getLogger(__name__)

__all__ = [
    "PdfPage",
    "PdfExtractionError",
    "extract_pages",
    "extract_text",
    "iter_pages",
]

# PDF 里常见的“假空格”：不换行空格、窄空格、窄不换行空格
_INLINE_WS = re.compile(r"[ \t\u00a0\u2007\u202f]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_LINE_BREAK_HYPHEN = re.compile(r"(\w)-\n(\w)")

class PdfExtractionError(RuntimeError):
    """PDF 无法打开或解析时抛出。"""
    
@dataclass(frozen=True, slots=True)
class PdfPage:
    """一页提取结果；page_number 是从 1 开始的 PDF 物理页序。"""
    page_number: int
    text: str
    width: float = 0.0
    height: float = 0.0
    
    @property
    def is_empty(self) -> bool:
        return not self.text.strip()
    
    def as_metadata(self, source: str | Path | None = None) -> dict[str,Any]:
        meta: dict[str, Any] = {"page_number": self.page_number}
        if source is not None:
            meta["source"] = str(source)
        return meta
    
def _normalize(text: str, *,dehyphenate: bool = False) -> str:
    """
    PDF 的 ``get_text`` 会返回大量视觉换行和多余空格，直接喂给
    embedding 会稀释语义。这里做四件事：
    1. 统一换行符；
    2. 行内连续空白压成一个空格；
    3. 去掉每行首尾空白；
    4. 连续空行压成一个空行（保留段落边界）
    """
    text=text.replace("\r\n","\n").replace("\r","\n")
    
    text=_INLINE_WS.sub(" ", text)
    
    text = "\n".join(line.strip() for line in text.split("\n"))
    if dehyphenate:
        text = _LINE_BREAK_HYPHEN.sub(r"\1\2", text)
    
    text = _BLANK_LINES.sub("\n\n",text)
    return text.strip()

def _open_document(source: str | Path | bytes | BinaryIO) -> "fitz.Document":
    try:
        if isinstance(source, (str, Path)):
            path = Path(source)
            if not path.is_file():
                raise PdfExtractionError(f"PDF not found: {path}")
            return fitz.open(str(path))

        if isinstance(source, (bytes, bytearray, memoryview)):
            return fitz.open(stream=bytes(source), filetype="pdf")

        return fitz.open(stream=source.read(), filetype="pdf")
    except PdfExtractionError:
        raise
    except Exception as e:
        raise PdfExtractionError(f"Could not open PDF: {e}") from e
    
def iter_pages(
    source: str | Path | bytes | BinaryIO,
    *,
    normalize: bool = True,
    dehyphenate: bool = False,
    min_chars: int = 0,
) -> Iterator[PdfPage]:
    """逐页提取文字，默认保留空页；提前停止迭代时应关闭生成器。

    min_chars 只过滤结果，不改变原始页码。图片页不自动执行 OCR，
    无文字时返回空文本；页面解析失败则抛出带页码的 PdfExtractionError。
    """

    doc = _open_document(source)
    try:
        if doc.needs_pass:
            raise PdfExtractionError("PDF is password-protected; decrypt it before extraction")
        
        for index in range(doc.page_count):
            try:
                page = doc.load_page(index)
                raw = page.get_text("text", sort=True)
                width = float(page.rect.width)
                height = float(page.rect.height)
            except Exception as e:
                raise PdfExtractionError(
                    f"Could not extract PDF page {index + 1}: {e}"
                ) from e
            text = _normalize(raw, dehyphenate=dehyphenate) if normalize else raw
            
            if len(text) < min_chars:
                continue
            
            yield PdfPage(
                page_number = index+1,
                text=text,
                width=width,
                height=height,
            )
    finally:
        doc.close()
        
def extract_pages(
    source: str | Path | bytes | BinaryIO,
    *,
    keep_empty: bool = False,
    **kwargs: Any,
) -> list[PdfPage]:
    pages = list(iter_pages(source, **kwargs))
    if not keep_empty:
        pages = [p for p in pages if not p.is_empty]
    return pages

def extract_text(
    source: str | Path | bytes | BinaryIO,
    *,
    page_separator: str = "\n\n",
    include_page_markers: bool = False,
    **kwargs: Any,
) -> str:
    parts: list[str] = []
    for page in iter_pages(source, **kwargs):
        if page.is_empty:
            continue
        if include_page_markers:
            parts.append(f"[page {page.page_number}]\n{page.text}")
        else:
            parts.append(page.text)
    return page_separator.join(parts)
