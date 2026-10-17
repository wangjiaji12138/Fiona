"""归档待登记 PDF，维护路径配置与含 SHA-256 的来源清单。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile


@dataclass(frozen=True)
class CatalogUpdate:
    raw_dir: Path
    config_path: Path
    original_config: bytes | None
    mapping: dict[str, str]
    moves: tuple[tuple[Path, Path], ...]
    skipped: tuple[str, ...]
    manifest_path: Path
    original_manifest: bytes | None
    records: tuple[dict[str, object], ...]
    metadata_updates: int = 0

    @property
    def pdf_count(self) -> int:
        return len(self.moves)



def _unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"配置中有重复键：{key}")
        result[key] = value
    return result


def _validate_mapping(mapping: object) -> dict[str, str]:
    if not isinstance(mapping, dict):
        raise ValueError("配置必须是 JSON 对象：doc_id -> PDF 相对文件名")
    seen_ids: set[str] = set()
    seen_paths: set[str] = set()
    result: dict[str, str] = {}
    for doc_id, filename in mapping.items():
        if (
            not isinstance(doc_id, str) or not doc_id.strip()
            or doc_id in {".", ".."} or "/" in doc_id or "\\" in doc_id
        ):
            raise ValueError(f"无效文档 ID：{doc_id!r}")
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError(f"无效文件名：{filename!r}")
        path = Path(filename)
        if path.is_absolute() or ".." in path.parts or "\\" in filename:
            raise ValueError(f"文件名必须是 raw 内的相对路径：{filename}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"PDF 配置不能登记非 PDF 文件：{filename}")
        if doc_id.casefold() in seen_ids or path.as_posix().casefold() in seen_paths:
            raise ValueError(f"配置存在大小写或重复路径冲突：{doc_id} -> {filename}")
        seen_ids.add(doc_id.casefold())
        seen_paths.add(path.as_posix().casefold())
        result[doc_id] = path.as_posix()
    return result


def sha256_file(path: Path) -> str:
    """分块计算文件字节的 SHA-256，不将整份 PDF 读入内存。"""
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def filename_metadata(doc_id: str) -> tuple[str, int | None, str | None]:
    """按 渠道-年份-标题 拆分，保留标题内连字符；Unknown 不变成事实。"""
    match = re.fullmatch(r'(.+?)-([12][0-9]{3}|Unknown)-(.+)', doc_id)
    if match is None:
        return doc_id, None, None
    venue, year, title = (part.strip() for part in match.groups())
    if not venue or not title:
        return doc_id, None, None
    return title, int(year) if year != 'Unknown' else None, venue if venue != 'Unknown' else None


def refresh_record_metadata(record: dict[str, object]) -> dict[str, object]:
    """补全旧清单中的文件名字段；保留已填写标题、年份、渠道和其他元数据。"""
    result = dict(record)
    doc_id = str(record['doc_id'])
    title, year, venue = filename_metadata(doc_id)
    if not result.get('title') or result['title'] == doc_id:
        result['title'] = title
    result['language'] = 'en'
    existing_metadata = result.get('metadata', {})
    if not isinstance(existing_metadata, dict):
        raise ValueError(f'metadata 必须是 JSON 对象：{doc_id}')
    metadata = dict(existing_metadata)
    for key, value in (('year', year), ('venue', venue)):
        if metadata.get(key) is None:
            metadata[key] = value
    result['metadata'] = metadata
    result.pop('downloaded_at', None)
    result.pop('notes', None)
    return result


def _load_records(content: bytes | None, mapping: dict[str, str],
                  raw_name: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    ids: set[str] = set()
    hashes: set[str] = set()
    for line in content.decode('utf-8').splitlines() if content is not None else []:
        if not line.strip():
            continue
        record = json.loads(line, object_pairs_hook=_unique_pairs)
        if not isinstance(record, dict):
            raise ValueError('来源清单每行必须是 JSON 对象')
        doc_id = record.get('doc_id')
        digest = record.get('sha256')
        if not isinstance(doc_id, str) or doc_id not in mapping or doc_id in ids:
            raise ValueError(f'来源清单 ID 重复或与配置不一致：{doc_id}')
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdef' for c in digest)):
            raise ValueError(f'来源清单 SHA-256 无效：{doc_id}')
        if digest in hashes:
            raise ValueError(f'来源清单存在重复内容（SHA-256）：{doc_id}')
        if record.get('raw_path') != f'{raw_name}/{mapping[doc_id]}':
            raise ValueError(f'来源清单 raw_path 与配置不一致：{doc_id}')
        ids.add(doc_id)
        hashes.add(digest)
        records.append(record)
    if ids != set(mapping):
        raise ValueError('路径配置与来源清单 ID 不一致；请同时恢复匹配的两份文件，不要只删除其中一份')
    return records


def plan_catalog_update(raw_dir: Path, config_path: Path,
                        manifest_path: Path | None = None, *,
                        refresh_metadata: bool = False) -> CatalogUpdate:
    """只扫描 unregistered；预检全批冲突，不移动文件或修改配置。"""
    raw_dir = raw_dir.resolve()
    config_path = config_path.absolute()
    manifest_path = (manifest_path or raw_dir.parent / 'source_manifest.jsonl').absolute()
    if not raw_dir.is_dir():
        raise ValueError(f"原始资料目录不存在或不是目录：{raw_dir}")
    if config_path.is_symlink() or config_path.resolve().is_relative_to(raw_dir):
        raise ValueError("配置不能是符号链接，也不能位于 raw 内")
    if (manifest_path.is_symlink() or manifest_path.resolve().is_relative_to(raw_dir)
            or manifest_path.resolve() == config_path.resolve()
            or manifest_path.resolve().parent != raw_dir.parent):
        raise ValueError('来源清单必须位于 raw 的父目录，不能是符号链接或与配置同路径')
    pending = raw_dir / "unregistered"
    registered = raw_dir / "registered"
    for directory in (pending, registered):
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f"必须是实际目录，不能是符号链接：{directory}")
    original = config_path.read_bytes() if config_path.exists() else None
    mapping = _validate_mapping(
        json.loads(original.decode("utf-8"), object_pairs_hook=_unique_pairs)
        if original is not None else {}
    )
    original_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    records = _load_records(original_manifest, mapping, raw_dir.name)
    metadata_updates = 0
    if refresh_metadata:
        updated_records = [refresh_record_metadata(record) for record in records]
        metadata_updates = sum(old != new for old, new in zip(records, updated_records))
        records = updated_records
    hashes = {record['sha256']: record['doc_id'] for record in records}
    # 路径统一相对于来源清单目录，即默认 data/；不依赖终端当前目录。
    ids = {key.casefold() for key in mapping}
    paths = {value.casefold() for value in mapping.values()}
    moves: list[tuple[Path, Path]] = []
    skipped: list[str] = []
    # 不扫描 registered，也不逐项 stat 已登记文件。
    for source in sorted(pending.rglob("*")) if pending.exists() else []:
        relative = source.relative_to(pending)
        if source.is_symlink():
            raise ValueError(f"待登记目录内存在符号链接：{relative}")
        if not source.is_file():
            continue
        if source.suffix.lower() != ".pdf":
            skipped.append(relative.as_posix())
            continue
        doc_id = source.stem
        target = registered / relative
        filename = target.relative_to(raw_dir).as_posix()
        _validate_mapping({doc_id: filename})
        if doc_id.casefold() in ids or filename.casefold() in paths:
            raise ValueError(f"登记冲突（ID 或路径已占用）：{doc_id} -> {filename}")
        for parent in (target, *target.parents):
            if parent == registered:
                break
            if parent.is_symlink():
                raise ValueError(f"目标路径含符号链接：{parent}")
        if target.exists():
            raise ValueError(f"目标已存在，不能覆盖：{target}")
        digest = sha256_file(source)
        if digest in hashes:
            raise ValueError(f'重复内容（SHA-256）：{doc_id} 与 {hashes[digest]}；请人工核对版本')
        hashes[digest] = doc_id
        title, year, venue = filename_metadata(doc_id)
        records.append({
            'doc_id': doc_id, 'title': title, 'doc_type': 'research_paper',
            'language': 'en', 'source_url': None, 'attachment_url': None,
            'published_at': None,
            'raw_path': f'{raw_dir.name}/{filename}', 'sha256': digest, 'status': 'downloaded',
            'related_doc_ids': [],
            'metadata': {'authors': [], 'year': year, 'venue': venue, 'doi': None,
                         'arxiv_id': None, 'version': None, 'paper_role': None,
                         'research_topic': None},
        })
        mapping[doc_id] = filename
        ids.add(doc_id.casefold())
        paths.add(filename.casefold())
        moves.append((source, target))
    return CatalogUpdate(raw_dir, config_path, original, dict(sorted(mapping.items())),
                         tuple(moves), tuple(skipped), manifest_path, original_manifest,
                         tuple(sorted(records, key=lambda record: str(record['doc_id']))),
                         metadata_updates)


def _move_without_overwrite(source: Path, target: Path) -> None:
    """同文件系统内移动；排他创建目标，防止覆盖同名文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(source, target)
    try:
        source.unlink()
    except BaseException:
        target.unlink()
        raise


def apply_catalog_update(plan: CatalogUpdate) -> None:
    """复核后提交两份产物；异常时尝试恢复原字节并移回本批 PDF。"""
    current = plan.config_path.read_bytes() if plan.config_path.exists() else None
    if current != plan.original_config:
        raise ValueError("配置在预检后发生变化，请重新运行")
    current_manifest = plan.manifest_path.read_bytes() if plan.manifest_path.exists() else None
    if current_manifest != plan.original_manifest:
        raise ValueError('来源清单在预检后发生变化，请重新运行')
    pending = plan.raw_dir / "unregistered"
    registered = plan.raw_dir / "registered"
    pending.mkdir(exist_ok=True)
    registered.mkdir(exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    writing = False
    try:
        for source, target in plan.moves:
            _move_without_overwrite(source, target)
            moved.append((source, target))
        if moved:
            expected = {record['doc_id']: record['sha256'] for record in plan.records}
            for _, target in moved:
                if sha256_file(target) != expected[target.stem]:
                    raise ValueError(f'PDF 在预检后发生变化：{target.name}；已取消登记，请重新运行')
        if moved or plan.metadata_updates:
            writing = True
        if moved:
            write_catalog(plan.config_path, plan.mapping)
        if moved or plan.metadata_updates:
            write_manifest(plan.manifest_path, plan.records)
    except BaseException as exc:
        failures = []
        if writing:
            outputs = [(plan.manifest_path, plan.original_manifest)]
            if moved:
                outputs.append((plan.config_path, plan.original_config))
            for path, original in outputs:
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _write_bytes(path, original)
                except OSError as rollback_error:
                    failures.append(f'恢复 {path}: {rollback_error}')
        for source, target in reversed(moved):
            try:
                _move_without_overwrite(target, source)
            except OSError as rollback_error:
                failures.append(f"{target} -> {source}: {rollback_error}")
        if failures:
            raise RuntimeError("登记失败且部分产物未能恢复，请按路径核对：" + "; ".join(failures)) from exc
        raise
    # 仅删除成功处理后留下的空子目录，始终保留 unregistered 根目录。
    for directory in sorted((p for p in pending.rglob("*") if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass  # 有未支持文件或后续新放入的文件时保留目录。


def write_catalog(config_path: Path, mapping: dict[str, str]) -> bool:
    """临时文件写完后替换配置；内容相同不写入，返回是否实际更新。"""
    content = json.dumps(_validate_mapping(mapping), ensure_ascii=False, indent=2) + "\n"
    if config_path.exists() and config_path.read_text(encoding="utf-8") == content:
        return False
    _write_bytes(config_path, content.encode('utf-8'))
    return True


def write_manifest(path: Path, records: tuple[dict[str, object], ...]) -> None:
    """保留现有来源记录的字段，按一行一条 JSON 写入。"""
    content = ''.join(json.dumps(record, ensure_ascii=False) + '\n' for record in records)
    _write_bytes(path, content.encode('utf-8'))


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="wb", dir=path.parent,
                                prefix=f".{path.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
