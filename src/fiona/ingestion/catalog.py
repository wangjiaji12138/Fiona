"""将待登记 PDF 移入已登记目录，维护解析配置，不改写 PDF 内容。"""

from __future__ import annotations

import json
import os
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


def plan_catalog_update(raw_dir: Path, config_path: Path) -> CatalogUpdate:
    """只扫描 unregistered；预检全批冲突，不移动文件或修改配置。"""
    raw_dir = raw_dir.resolve()
    config_path = config_path.absolute()
    if not raw_dir.is_dir():
        raise ValueError(f"原始资料目录不存在或不是目录：{raw_dir}")
    if config_path.is_symlink() or config_path.resolve().is_relative_to(raw_dir):
        raise ValueError("配置不能是符号链接，也不能位于 raw 内")
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
        mapping[doc_id] = filename
        ids.add(doc_id.casefold())
        paths.add(filename.casefold())
        moves.append((source, target))
    return CatalogUpdate(raw_dir, config_path, original, dict(sorted(mapping.items())),
                         tuple(moves), tuple(skipped))


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
    """移动后提交配置；可捕获的移动/写入失败会反向移回本批文件。"""
    current = plan.config_path.read_bytes() if plan.config_path.exists() else None
    if current != plan.original_config:
        raise ValueError("配置在预检后发生变化，请重新运行")
    pending = plan.raw_dir / "unregistered"
    registered = plan.raw_dir / "registered"
    pending.mkdir(exist_ok=True)
    registered.mkdir(exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        for source, target in plan.moves:
            _move_without_overwrite(source, target)
            moved.append((source, target))
        if moved:
            write_catalog(plan.config_path, plan.mapping)
    except BaseException as exc:
        failures = []
        for source, target in reversed(moved):
            try:
                _move_without_overwrite(target, source)
            except OSError as rollback_error:
                failures.append(f"{target} -> {source}: {rollback_error}")
        if failures:
            raise RuntimeError("登记失败且部分文件未能移回，请按路径恢复：" + "; ".join(failures)) from exc
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
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=config_path.parent,
                                prefix=f".{config_path.name}.", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            output.write(content)
        temporary.replace(config_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return True
