"""维护已登记文件名与现有逐页解析结果的关联，保留登记前备份。"""

from __future__ import annotations

import json
from pathlib import Path

from fiona.ingestion.catalog import (
    _load_records, _move_without_overwrite, _unique_pairs, _validate_mapping,
    _write_bytes, normalize_pdf_name, sha256_file,
)


def normalize_registered_names(
    raw_dir: Path, config: Path, backup_dir: Path, *, dry_run: bool = True,
) -> dict[str, str]:
    """全批预检后迁移 PDF、配置、清单和逐页 JSONL；异常时尝试恢复。"""
    raw_dir = raw_dir.resolve()
    data_dir = raw_dir.parent
    config = config.absolute()
    manifest = data_dir / 'source_manifest.jsonl'
    # 本工具只迁移当前页面格式，避免悄悄留下其他下游产物的旧引用。
    for path in (data_dir / 'chunks', data_dir / 'indexes', data_dir / 'indexs'):
        if path.exists() and any(item.is_file() for item in path.rglob('*')):
            raise ValueError(f'已有下游产物，需先制定 ID 迁移方案：{path}')
    questions = data_dir / 'sample_questions.jsonl'
    if questions.exists() and questions.stat().st_size:
        raise ValueError(f'已有问题引用，需先制定 ID 迁移方案：{questions}')
    if config.is_symlink() or manifest.is_symlink():
        raise ValueError('配置和来源清单不能为符号链接')
    originals = {config: config.read_bytes(), manifest: manifest.read_bytes()}
    mapping = _validate_mapping(json.loads(originals[config], object_pairs_hook=_unique_pairs))
    records = _load_records(originals[manifest], mapping, raw_dir.name)
    ids = {doc_id: normalize_pdf_name(doc_id) for doc_id in mapping}
    updated_mapping = {
        ids[doc_id]: str(Path(filename).with_name(normalize_pdf_name(Path(filename).name)))
        for doc_id, filename in mapping.items()
    }
    if len(updated_mapping) != len(mapping):
        raise ValueError('规范化后 ID 冲突，未修改文件')
    _validate_mapping(updated_mapping)
    changes = {old: new for old, new in ids.items() if old != new}
    if not changes:
        return changes
    moves: list[tuple[Path, Path]] = []
    parsed_updates: dict[Path, bytes] = {}
    parsed_sources: list[Path] = []
    expected = {record['doc_id']: record['sha256'] for record in records}
    for old, new in changes.items():
        source = raw_dir / mapping[old]
        target = raw_dir / updated_mapping[new]
        if source.stem != old or target.stem != new:
            raise ValueError(f'ID 与文件名不一致：{source}')
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
            raise ValueError(f'原文路径不能含符号链接：{source}')
        if target.exists() or target.is_symlink():
            raise ValueError(f'目标已存在：{target}')
        if sha256_file(source) != expected[old]:
            raise ValueError(f'原文与登记哈希不一致：{source}')
        moves.append((source, target))
        parsed = data_dir / 'parsed' / f'{old}.jsonl'
        if parsed.is_symlink():
            raise ValueError(f'解析文件不能是符号链接：{parsed}')
        if not parsed.exists():
            continue
        destination = parsed.with_name(f'{new}.jsonl')
        if destination.exists() or destination.is_symlink():
            raise ValueError(f'解析目标已存在：{destination}')
        originals[parsed] = parsed.read_bytes()
        pages = []
        for line in originals[parsed].decode('utf-8').splitlines():
            page = json.loads(line, object_pairs_hook=_unique_pairs)
            if not isinstance(page, dict) or page.get('doc_id') != old:
                raise ValueError(f'解析记录 ID 不匹配：{parsed}')
            page['doc_id'] = new
            pages.append(json.dumps(page, ensure_ascii=False) + '\n')
        parsed_sources.append(parsed)
        parsed_updates[destination] = ''.join(pages).encode('utf-8')
    updated_records = []
    for record in records:
        record = dict(record)
        old = str(record['doc_id'])
        record['doc_id'] = ids[old]
        record['raw_path'] = f'{raw_dir.name}/{updated_mapping[ids[old]]}'
        if 'related_doc_ids' in record:
            related = record['related_doc_ids']
            if not isinstance(related, list) or not all(isinstance(item, str) for item in related):
                raise ValueError(f'related_doc_ids 必须是字符串列表：{old}')
            record['related_doc_ids'] = [ids.get(item, item) for item in related]
        updated_records.append(record)
    if dry_run:
        return changes
    backup_dir = backup_dir.absolute()
    if backup_dir.resolve().is_relative_to(raw_dir):
        raise ValueError('备份目录不能位于 raw 内')
    backup_dir.mkdir(parents=True, exist_ok=False)
    journal = []
    for index, (path, content) in enumerate(originals.items()):
        if path.read_bytes() != content:
            raise ValueError(f'预检后产物变化：{path}')
        backup = backup_dir / f'{index:03d}-{path.name}'
        backup.write_bytes(content)
        journal.append({'path': str(path), 'backup': backup.name})
    (backup_dir / 'plan.json').write_text(json.dumps({
        'id_changes': changes, 'originals': journal,
        'pdf_moves': [[str(a), str(b)] for a, b in moves],
        'new_parsed_paths': [str(path) for path in parsed_updates],
        'sha256_before': {old: expected[old] for old in changes},
    }, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    outputs = {
        config: (json.dumps(dict(sorted(updated_mapping.items())), ensure_ascii=False, indent=2) + '\n').encode('utf-8'),
        manifest: ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in updated_records).encode('utf-8'),
        **parsed_updates,
    }
    moved: list[tuple[Path, Path]] = []
    written: list[Path] = []
    deleted: list[Path] = []
    try:
        for source, target in moves:
            _move_without_overwrite(source, target)
            moved.append((source, target))
            if sha256_file(target) != expected[source.stem]:
                raise ValueError(f'改名前后 PDF 哈希不一致：{target}')
        for path, content in outputs.items():
            _write_bytes(path, content)
            written.append(path)
        for path in parsed_sources:
            path.unlink()
            deleted.append(path)
    except BaseException as exc:
        failures = []
        for path in reversed(written + deleted):
            try:
                if path in originals:
                    _write_bytes(path, originals[path])
                else:
                    path.unlink(missing_ok=True)
            except OSError as error:
                failures.append(f'{path}: {error}')
        for source, target in reversed(moved):
            try:
                _move_without_overwrite(target, source)
            except OSError as error:
                failures.append(f'{target} -> {source}: {error}')
        if failures:
            raise RuntimeError(f'部分恢复失败；备份 {backup_dir}；' + '; '.join(failures)) from exc
        raise
    return changes
