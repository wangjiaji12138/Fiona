"""登记 PDF：计算 SHA-256、移入 registered，同步路径配置与来源清单。"""

from __future__ import annotations

import argparse
from pathlib import Path

from fiona.ingestion.catalog import apply_catalog_update, plan_catalog_update

PROJECT_ROOT = Path(__file__).resolve().parents[1]

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-dir", type=Path, default=PROJECT_ROOT / "data/raw",
                        help="原始资料根目录；只扫描其 unregistered 子目录，默认 data/raw")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs/pdf_files.json",
                        help="输出的 ID -> 文件名映射；默认 configs/pdf_files.json")
    parser.add_argument('--manifest', type=Path,
                        help='来源清单；默认 raw 父目录下 source_manifest.jsonl，必须与 raw 同级')
    parser.add_argument('--refresh-metadata', action='store_true',
                        help='补齐已有记录的英文、文件名标题/年份/渠道，移除 downloaded_at 和 notes')
    parser.add_argument("--dry-run", action="store_true", help="只预览，不移动文件、不创建目录或写入配置")
    args = parser.parse_args()
    try:
        result = plan_catalog_update(args.raw_dir, args.config, args.manifest,
                                     refresh_metadata=args.refresh_metadata)
        if not args.dry_run:
            apply_catalog_update(result)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
        
    action = "预览，未移动或写入" if args.dry_run else "登记完成"
    print(f"{action}：待登记 {result.pdf_count} 份 PDF，配置共 {len(result.mapping)} 项；"
          f"保留 {len(result.skipped)} 个非 PDF 文件。")
    print(f"{'计划更新' if args.dry_run else '已更新'}已有元数据：{result.metadata_updates} 条。")
    for source, target in result.moves:
        print(f"{'计划移动' if args.dry_run else '已移动'}：{source.name} -> {target}")
    new_ids = {source.stem for source, _ in result.moves}
    for record in result.records:
        if record['doc_id'] in new_ids:
            print(f"SHA-256：{record['doc_id']} {record['sha256']}")
    for filename in result.skipped:
        print(f"未登记，保留原处（非 PDF）：{filename}")
    if not result.pdf_count and not result.metadata_updates:
        print("没有待登记 PDF；未重新处理 registered 目录，未改写配置或来源清单。")
    print(f"配置路径：{args.config.resolve()}")
    print(f'来源清单：{result.manifest_path.resolve()}')
    if result.pdf_count or result.metadata_updates:
        print('语言为 en；标题、年份、渠道按规范文件名填写；其余元数据等待 PDF 解析后补充。')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
