# 5. PDF 名称规范化与关联迁移

更新日期：2026-10-17。适用环境：Linux、Bash、Conda `fiona`，Python 3.14.8。本功能已实现并用于当前论文目录；没有新增依赖。整体进度见 [README](../../README.md)。

## 1. 目标与范围

用户要求文件名中 ASCII 连字符 `-` 两侧不能有空白。例如：

```text
修改前：ESWA-2023-DAGCRN - Graph convolutional recurrent network for traffic forecasting with dynamic adjacency matrix.pdf
修改后：ESWA-2023-DAGCRN-Graph convolutional recurrent network for traffic forecasting with dynamic adjacency matrix.pdf
```

其他单词间空格保留，PDF 内容不改写。新论文在正常登记时自动规范化；已登记论文通过维护工具同步迁移所有当前支持的关联。

|项目|说明|
|---|---|
|输入|已登记 PDF、路径配置、来源清单，以及存在时的逐页解析 JSONL|
|输出|规范文件名、对应新 ID、更新的路径配置和清单；存在的解析文件与页面记录 ID 同步更新|
|完成标志|文件名无连字符相邻空白；ID 与文件名一致；PDF 哈希不变；已支持的关联不残留旧 ID|
|限制|不改变论文内容、自然语言显示标题或作者等已填写元数据；不处理已有块、索引或样例问题引用|

契约见[项目开发文档第 6.1 节](../../项目开发文档.md#61-论文语料准备与来源登记)。

## 2. 原理与关键取舍

`normalize_pdf_name()` 使用 `re.sub(r'\s*-\s*', '-', name)`，只去掉连字符两侧的空白。普通空格、多个空格和制表符都属于该规则；标题中不相邻连字符的空格保持原样。大小写和扩展名不变，非 ASCII 横线不在此次清理范围内。

文件 ID 约定等于归档文件名去扩展名，所以不能只改磁盘上的 PDF。路径配置的键和值、来源清单的 `doc_id`、`raw_path`、`related_doc_ids` 都需要同步。若已有逐页结果，还要修改 JSONL 文件名和每条页面记录的 `doc_id`。正文、物理页码、状态和其他页面字段保持原值。

SHA-256 标识文件字节，重命名不会改变它。工具在移动前后都核对哈希，禁止把内容已变的文件配上旧清单。显示标题属于元数据，可能仍有自然语言中的空格；本次文件名迁移不会改写已填写的 `title`。

## 3. 前置准备

在服务器终端执行：

```bash
cd /data2/wangjiaji/Fiona
conda activate fiona
python -c "import sys, fiona; print(sys.executable); print(fiona.__file__)"
```

使用现有环境，不需要安装额外依赖。配置和来源清单必须相互匹配。执行期间不要同时运行登记、解析或手动编辑这些文件。源与目标在同一文件系统，且文件系统需支持硬链接。

工具只支持当前逐页 JSONL 格式。如果发现非空 `data/chunks/`、`data/indexes/`、旧 `data/indexs/` 或非空 `data/sample_questions.jsonl`，会拒绝执行，要求先制定这些下游引用的迁移方案。不要通过删除正式产物绕过检查。

## 4. 文件与职责

|文件|职责|
|---|---|
|[catalog.py](../../src/fiona/ingestion/catalog.py)|`normalize_pdf_name()` 提供名称规则；正常登记在构造目标路径及 ID 前应用规则|
|[update_pdf_files.py](../../scripts/update_pdf_files.py)|显示实际归档目标和对应新 ID 的哈希|
|[naming.py](../../src/fiona/ingestion/naming.py)|`normalize_registered_names()` 完成旧资料预检、备份、移动、更新与异常恢复|
|[normalize_pdf_names.py](../../scripts/normalize_pdf_names.py)|命令行参数、预览及错误提示|
|[test_catalog.py](../../tests/test_catalog.py)|新登记自动规范化及规范化后冲突检查|
|[test_naming.py](../../tests/test_naming.py)|旧资料关联迁移、正文与元数据保护、失败恢复及边界检查|

## 5. 实现流程拆解

### 5.1 正常登记防止再次出现

`plan_catalog_update()` 先规范化待登记文件的目标文件名，再从目标名称生成 ID。源文件仍保持原状直到正式执行。两个名称规范化后相同时，即使原文件名不同，也会整批预检失败，不移动任何文件。

正式执行把原文件移到规范化后的目标名称，随后沿用既有哈希复核、配置与来源清单提交流程。预览同样展示新目标，但不修改源文件。详见[统一登记教程](04-统一登记与来源清单.md)。

### 5.2 已登记资料全批预检

维护工具读取配置与来源清单，建立旧 ID → 新 ID 映射，检查规范化后的 ID 和路径冲突。对受影响 PDF 检查文件存在、目标未占用和实际哈希。读取存在的逐页结果时，要求每条记录 ID 与原文件 ID 相符，不容忍混入其他论文页面。

上述步骤均在写入前完成，后面的文件有冲突也不会导致前面的文件先被改名。没有名称需要变化时直接返回，不创建备份、不重写产物。

### 5.3 备份与迁移

正式执行先创建一个此前不存在的备份目录，保存配置、来源清单及受影响的旧解析文件原字节；`plan.json` 保存旧/新 ID、PDF 移动路径、每个原文件的备份名、新解析路径和迁移前哈希。

PDF 采用排他创建目标硬链接后删除源路径的移动方式，禁止覆盖目标。移动后核对哈希，再通过同目录临时文件原子替换配置、清单和新解析结果，最后移除对应旧解析路径。文件内容不经过 PDF 读写库重编码。

### 5.4 失败恢复与限制

普通异常会尝试恢复已写入产物原字节、删除本次新增的解析目标，并反向移回已移动 PDF。恢复失败会列出路径和备份目录；备份不会因失败而删除。

各文件分别原子替换，整个批次不是数据库事务。断电、强制终止及并发写入没有自动恢复保证。恢复方式见第 9 节。工具不会重建缺失的解析目录，也不自动重新解析论文。

## 6. 运行命令与参数

在项目根目录先预览：

```bash
python scripts/normalize_pdf_names.py --dry-run
```

预览只检查并打印旧 ID → 新 ID，不创建目录、不改写文件。正式执行：

```bash
python scripts/normalize_pdf_names.py
```

正式执行会改名 PDF、替换配置与清单；有旧解析结果时同步迁移。默认备份目录为 `data/maintenance/2026-10-17-hyphen-spacing/`。本批已经执行完成，无需重复运行；当前预览应显示 0 份。

|参数|含义|
|---|---|
|`--raw-dir`|默认项目 `data/raw/`，来源清单和解析目录位于其父目录下|
|`--config`|默认项目 `configs/pdf_files.json`|
|`--backup-dir`|保存迁移前产物与计划；需要实际修改时必须是此前不存在的目录，且不能位于 raw 内|
|`--dry-run`|只预检，不写入|

后续另批迁移应指定新的备份目录，不覆盖已有备份。例如以下是未来批次的操作示意：

```bash
python scripts/normalize_pdf_names.py --backup-dir data/maintenance/hyphen-spacing-next-batch --dry-run
python scripts/normalize_pdf_names.py --backup-dir data/maintenance/hyphen-spacing-next-batch
```

隔离测试必须同时指定临时 raw 和临时 config，默认来源清单和解析路径会跟随该 raw 的父目录，避免混用正式配置。

## 7. 验收方法

以下只读核验会读取已登记 PDF，检查全部名称、路径和实际哈希：

```bash
python - <<'PY'
import json
import re
from pathlib import Path
from fiona.ingestion.catalog import sha256_file

mapping = json.loads(Path('configs/pdf_files.json').read_text(encoding='utf-8'))
records = [json.loads(line) for line in Path('data/source_manifest.jsonl').read_text(encoding='utf-8').splitlines()]
assert len(mapping) == len(records)
assert set(mapping) == {record['doc_id'] for record in records}
for record in records:
    path = Path('data') / record['raw_path']
    assert path == Path('data/raw') / mapping[record['doc_id']]
    assert path.stem == record['doc_id']
    assert not re.search(r'\s-|-\s', path.name)
    assert sha256_file(path) == record['sha256']
print(f'PASS：{len(records)} 份名称、ID、路径和实际哈希一致')
PY
```

有解析结果时，还需逐行检查新 ID，并与备份逐字段比较，确认只有 `doc_id` 发生变化。自动测试包含这一检查。实际正文质量和物理页码正确性仍要在解析阶段核对，名称迁移不证明解析正确。

## 8. 实际验证记录

日期：2026-10-17。解释器 `/home/wangjiaji/miniconda3/envs/fiona/bin/python`，Python 3.14.8；本功能仅使用标准库。

```bash
conda run -n fiona python -m unittest discover -s tests -v
conda run -n fiona python scripts/normalize_pdf_names.py --dry-run
conda run -n fiona python scripts/normalize_pdf_names.py
```

|检查|实际结果|
|---|---|
|自动测试|28 项通过，包括正常登记规范化、名称归并冲突、页面正文和页码保持、关联 ID 迁移、目标已存在、哈希不符、错误页面 ID、下游产物拒绝及写入失败恢复|
|正式预检与执行|预检 22 份需要修改，无冲突；正式完成 22 份改名，更新配置与来源清单|
|全量字节核验|44 份 PDF 数量不变，全部名称不含连字符相邻空白，ID 与路径一致；逐份重算 SHA-256，与当前及迁移前清单一致|
|重复预览|显示需修改 0 份|
|备份|`data/maintenance/2026-10-17-hyphen-spacing/` 保存两份原产物及 `plan.json`|
|正式解析产物|任务开始时曾看到解析目录，但实际迁移时该目录已不存在，计划中没有解析迁移条目。本次没有重建解析目录；正式页面迁移未执行，页面保护通过隔离测试验证|
|未验证|未全量重新解析 PDF；未模拟断电、强制中断、并发写入、跨文件系统移动及恢复过程自身失败|

一次最初的全量检查误以为仍有 44 份解析结果，在读取已不存在的解析目录时失败；随后按实际目录状态重新核验了 44 份 PDF 与两份元数据产物，全部通过，没有将缺失解析文件算作迁移成功。

## 9. 排错、重跑与恢复

|现象|处理方式|
|---|---|
|规范化后 ID 或路径冲突|保持原文件，人工核对重复论文或版本，不能直接覆盖；无冲突后再预览|
|原文哈希不一致|比较原 PDF、当前清单与来源版本，明确内容变化原因；不要为了通过检查直接改哈希|
|解析记录 ID 不匹配|检查文件是否被混写、改名或来自旧版本；恢复正确对应关系后重试|
|备份目录已存在|保留已有备份；确认前次结果，再为新批次指定全新 `--backup-dir`|
|已有下游引用|先设计证据块、索引或问题引用迁移，不能通过删除产物绕过|
|普通写入失败|确认产物和 PDF 已恢复，修复权限或磁盘问题后用新备份目录重试|
|恢复失败或强制中断|暂停相关任务，保留现场，按备份计划逐项核对并恢复|

需要人工恢复时先读取备份 `plan.json`，按 `originals` 的 `path` 和 `backup` 恢复元数据及原解析文件；按 `pdf_moves` 将已完成的目标反向移回原路径，先确认目标哈希及原路径未被占用。若有 `new_parsed_paths`，核对后移走本次生成的新解析路径，避免新旧文件同时被误用。本批该列表为空。

恢复后应同时核对配置、来源清单、PDF 与解析引用，不能仅恢复旧配置；后续若已有新登记或人工编辑，不应直接覆盖为旧备份，需合并这些新增变化。备份只保存元数据与解析产物，PDF 本身通过哈希和反向路径保护，没有额外复制大文件。

## 10. 学习与下一步

文件名、文档 ID、来源路径和证据引用是一组关联，修复名称必须一起迁移。字节哈希能验证 PDF 未变，逐字段比较能验证页面正文和页码未变；两种验证作用不同。

下一步继续[PDF 解析教程](02-PDF解析.md)和 [README](../../README.md#下一步结构解析再切块)中的结构解析任务，使用当前配置里的新 ID。历史[最初命名记录](../catalog/2026-10-17-paper-renames.json)保留当时名称，不作为当前路径配置使用；此次映射以迁移备份计划为准。
