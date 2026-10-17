"""同步清理已登记 PDF、文档 ID 和逐页结果中的连字符两侧空白。"""

import argparse
from pathlib import Path

from fiona.ingestion.naming import normalize_registered_names

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-dir', type=Path, default=PROJECT_ROOT / 'data/raw')
    parser.add_argument('--config', type=Path, default=PROJECT_ROOT / 'configs/pdf_files.json')
    parser.add_argument('--backup-dir', type=Path,
                        default=PROJECT_ROOT / 'data/maintenance/2026-10-17-hyphen-spacing')
    parser.add_argument('--dry-run', action='store_true', help='仅预检，不创建备份或改写文件')
    args = parser.parse_args()
    try:
        changes = normalize_registered_names(args.raw_dir, args.config, args.backup_dir,
                                             dry_run=args.dry_run)
    except (OSError, ValueError, RuntimeError) as error:
        parser.error(str(error))
    for old, new in changes.items():
        print(f'{old} -> {new}')
    print(f"{'计划修改' if args.dry_run else '已修改'} {len(changes)} 份论文名称及关联记录。")
    if changes and not args.dry_run:
        print(f'迁移前产物与恢复计划：{args.backup_dir.resolve()}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
