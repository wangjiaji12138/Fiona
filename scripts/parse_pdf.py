"""读取 configs/pdf_files.json，批量提取 PDF，每份文档保存一个 JSONL。"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from fiona.parsing.pdf import PdfExtractionError, iter_pages


PROJECT_ROOT = Path(__file__).resolve().parents[1]

def load_pdf_files(config_path: Path) -> dict[str, str]:
    """只读取配置，不扫描原始目录或修改文档清单。"""
    with config_path.open(encoding="utf-8") as source:
        pdf_files = json.load(source)
    if not isinstance(pdf_files, dict) or not pdf_files:
        raise ValueError("PDF 配置必须是非空字典：文档 ID -> PDF 文件名")
    for doc_id, filename in pdf_files.items():
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise ValueError("文档 ID 必须是非空字符串")
        if Path(doc_id).name != doc_id or doc_id in {".", ".."}:
            raise ValueError(f"文档 ID 不能包含路径：{doc_id}")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(f"PDF 文件名必须是非空字符串：{doc_id}")
    return pdf_files


def parse_pdf(
    source: Path,
    *,
    doc_id: str,
    output_path: Path,
    keyword: str | None = None,
) -> int:
    """处理一份 PDF，成功返回 0，失败返回 1。"""
    print(f"\n处理文档：{doc_id}（{source.name}）")
    if source.resolve() == output_path.resolve():
        print(f"处理失败 [{doc_id}]：输出路径不能与原始 PDF 相同", file=sys.stderr)
        return 1

    temporary_path = None
    count = 0
    empty_pages = []
    matched_pages = []
    previews = []
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # 全部页面处理完才替换正式文件，避免把半份结果当成完整产物。
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=output_path.parent,
            prefix=f".{output_path.name}.", suffix=".tmp", delete=False,
        ) as output:
            temporary_path = Path(output.name)
            with closing(iter_pages(source)) as pages:
                for page in pages:
                    record = {
                        "doc_id": doc_id,
                        "page_number": page.page_number,
                        "text": page.text,
                        "status": "empty" if page.is_empty else "extracted",
                    }
                    if page.is_empty:
                        record["reason"] = "未提取到文字；需检查空白页、图片页或文字编码"
                        empty_pages.append(page.page_number)
                    output.write(json.dumps(record, ensure_ascii=False) + "\n")
                    count += 1
                    if keyword and keyword in page.text:
                        matched_pages.append(page.page_number)
                        if len(previews) < 3:
                            start = page.text.index(keyword)
                            previews.append((page.page_number, page.text[max(0, start - 100):start + 250]))
        temporary_path.replace(output_path)
    except (PdfExtractionError, OSError) as exc:
        print(f"处理失败 [{doc_id}]：{exc}", file=sys.stderr)
        return 1
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    print(f"输出：{output_path}")
    print(f"总页数：{count}；有文字：{count - len(empty_pages)}；无文字：{len(empty_pages)}")
    if empty_pages:
        print(f"待检查的 PDF 页码：{empty_pages}")
    if keyword:
        print(f"包含“{keyword}”的 PDF 页码（{len(matched_pages)} 页）：{matched_pages}")
        for page_number, text in previews:
            print(f"\n--- PDF 第 {page_number} 页 ---\n{text}")
    if count == 0 or len(empty_pages) == count:
        print("未获得可用文字，请检查原文件是否需要 OCR。", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=PROJECT_ROOT / "configs/pdf_files.json",
        help="文档 ID 到 PDF 文件名的 JSON 字典，默认 configs/pdf_files.json",
    )
    parser.add_argument(
        "--doc-id", "--doc_id", nargs="+",
        help="只处理指定的文档 ID；默认处理配置中的全部文档",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=PROJECT_ROOT / "data/parsed",
        help="输出目录，默认是项目的 data/parsed/",
    )
    parser.add_argument("--keyword", help="显示每份文档的匹配页码及前 3 页文字片段")
    args = parser.parse_args()
    try:
        pdf_files = load_pdf_files(args.config)
    except (OSError, ValueError) as exc:
        parser.error(f"无法读取 PDF 配置 {args.config}：{exc}")

    unknown_ids = [doc_id for doc_id in (args.doc_id or []) if doc_id not in pdf_files]
    if unknown_ids:
        parser.error(f"配置中不存在这些文档 ID：{', '.join(unknown_ids)}")

    # dict.fromkeys 去重并保持用户指定的顺序。
    doc_ids = list(dict.fromkeys(args.doc_id)) if args.doc_id else list(pdf_files)

    failed_ids = []
    for doc_id in doc_ids:
        result = parse_pdf(
            PROJECT_ROOT / "data/raw" / pdf_files[doc_id],
            doc_id=doc_id,
            output_path=args.output_dir / f"{doc_id}.jsonl",
            keyword=args.keyword,
        )
        if result != 0:
            failed_ids.append(doc_id)

    print(f"\n批处理完成：共 {len(doc_ids)} 份，成功 {len(doc_ids) - len(failed_ids)} 份，失败 {len(failed_ids)} 份")
    if failed_ids:
        print(f"失败文档：{', '.join(failed_ids)}")
    return 1 if failed_ids else 0


if __name__ == "__main__":
    raise SystemExit(main())
