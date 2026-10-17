# 2. PDF 解析：逐页提取、清洗与 JSONL 输出

更新日期：2026-10-17。适用环境：Linux、Bash、Conda `fiona`。本教程整理已经实现的通用 PDF 解析能力；当前项目定位为 Fiona（曌），面向时空预测论文。整体进度与新的论文准备任务查 [README](../../README.md)。

本教程使用 `paper_example_v1` 作为论文 ID 占位示例，不表示文件已经存在或通过验证。执行前需选定论文、保存 PDF 并通过[批量登记脚本](03-论文清单批量登记.md)登记映射；页数、关键词和目标页以实际论文为准。

## 1. 本次要完成什么

输入一份 PDF，按物理页提取文字，清理重复空白，保存到一份 JSONL。每条文字都能根据文档 ID 和页码回到原文件；无文字页也保存位置和待检查原因。

|对象|本教程样例|
|---|---|
|输入映射|`configs/pdf_files.json` 中的 `paper_example_v1`|
|原始 PDF|`data/raw/registered/paper_example_v1.pdf`|
|正式输出|`data/parsed/paper_example_v1.jsonl`|
|完成标志|能逐行读取输出、核对页面数量与物理页序，并在原 PDF 找到对应文字|
|能力范围|提取与基础清洗；暂不执行 OCR、结构恢复、切块、索引或回答|

解析输出是后续切块的输入。一条页面记录不等于一个适合检索的证据块，文本非空也不能证明公式、阅读顺序和表格正确。

## 2. 先理解三个概念

**物理页序：** PDF 查看器从文件第一页开始计数的位置，程序输出从 1 开始。PDF 内印刷的页码可能不同；例如封面未编号时，物理页序与正文页码可能不同。引用打开页面时使用物理页序，不能把正文第一页重新编号为 1。

**JSONL：** 一行一个 JSON 对象，适合逐行处理。正文中的换行在文件内编码为 `\n`，不会把一页拆成多条记录；`json.loads()` 还原后才重新显示为多行文字。

**规范化：** 清理提取产生的重复空白，减少无意义差异。它不负责修正错字、识别公式或重建列关系。保留单个空行作为段落边界，后续切块可以继续使用。

完整记录格式、状态及错误约定统一见[项目开发文档第 6.4 节](../../项目开发文档.md#64-页面解析与批量输出约定)。

## 3. 环境与输入检查

**操作位置：服务器 Bash 终端，项目根目录。** 首次安装先完成 [1.环境搭建.md](../../1.环境搭建.md)。

```bash
cd /data2/wangjiaji/Fiona
conda activate fiona
python -c "import sys, fiona, pymupdf; print(sys.executable); print(fiona.__file__); print(pymupdf.__version__)"
python -m pip check
```

成功标志：解释器位于 `fiona` 环境，项目路径指向本仓库的 `src/fiona/__init__.py`，PyMuPDF 可以导入，依赖检查没有冲突。本功能复用已声明的 PyMuPDF，无需新增依赖。

先将新论文放到 `data/raw/unregistered/`，运行 `python scripts/update_pdf_files.py` 登记并移入 `registered/`，然后将下方占位 ID `paper_example_v1` 替换为实际文件名去掉 `.pdf` 的完整部分。终端 `--doc-id` 参数中的完整标题必须加引号；Python 字符串保留原大小写。以下代码检查映射与输入文件：

```bash
python - <<'PY'
import json
from pathlib import Path

mapping = json.loads(Path("configs/pdf_files.json").read_text(encoding="utf-8"))
doc_id = "paper_example_v1"
pdf_path = Path("data/raw") / mapping[doc_id]
print("文档 ID：", doc_id)
print("PDF 路径：", pdf_path)
assert pdf_path.is_file(), f"找不到文件：{pdf_path}"
PY
```

解析脚本不自动扫描 `data/raw/`。新文件先放入 `unregistered/`，由登记脚本加入配置并移入 `registered/`；配置中没有登记的文件不会被解析。

## 4. 文件与关键调用链

|文件|职责|关键入口|
|---|---|---|
|[pdf.py](../../src/fiona/parsing/pdf.py)|打开 PDF、逐页提取、规范化文本|`PdfPage`、`iter_pages()`、`extract_pages()`、`extract_text()`|
|[parse_pdf.py](../../scripts/parse_pdf.py)|参数、配置读取、输出保存与批处理汇总|`load_pdf_files()`、`parse_pdf()`、`main()`|
|[pdf_files.json](../../configs/pdf_files.json)|文档 ID 与文件名映射|JSON 字典|

调用链是 `main → load_pdf_files → parse_pdf → iter_pages → PyMuPDF`；页面对象依次返回给脚本，由脚本添加文档 ID、状态并保存。解析模块不负责来源登记和检索。

## 5. 关键实现如何工作

### 5.1 打开文档与资源释放

**查看位置：** `pdf.py` 的 `_open_document()` 和 `iter_pages()`。

`_open_document()` 支持路径、字节和二进制流，将打开错误转换成 `PdfExtractionError`。逐页提取前检查密码保护；遇到单页异常，异常信息带实际页码。

`iter_pages()` 是生成器，按页返回结果，不必一次拼接整本文件。其 `finally` 关闭 PDF；脚本用 `contextlib.closing()` 包裹生成器，即使中途失败也释放资源。直接调用生成器且提前停止时同样需要关闭它。

### 5.2 提取页面并保持原始页序

**查看位置：** `iter_pages()` 中的页面循环。

PyMuPDF 以 0 开始访问页面，返回对象的 `page_number` 使用 `index + 1`。默认提取调用为 `get_text("text", sort=True)`，并保留页面宽高；当前批处理输出未保存宽高。

`sort=True` 是文本提取排序选项，不是论文双栏结构识别。先抽查实际论文的阅读顺序，再决定增强目标。`min_chars` 只是过滤返回页面的选项，默认值为 0；即使过滤，也不会改变原始页码。批处理脚本默认不启用页面过滤。

### 5.3 规范化空白，但保留段落

**查看位置：** `_normalize()`。

处理顺序为：统一 CRLF/CR → 压缩行内连续空格及常见特殊空格 → 去掉每行首尾空白 → 压缩连续空行 → 去掉全文首尾空白。

三个及以上连续换行压为两个换行，即段落之间保留一个空行；不是删除所有空行。可选的 `dehyphenate` 默认关闭，批处理脚本没有开放该参数；后续处理英文论文断词时需结合原文验证，不能把连字符全部删掉。

### 5.4 空文本与页面状态

**查看位置：** `PdfPage.is_empty` 与 `parse_pdf()` 的记录构造。

`is_empty` 检查去掉首尾空白后是否还有文本。无文字时仍保存页码，添加 `status="empty"` 与待检查原因。它可能是空白页、图片页或文字编码问题，需要查看原页才能分类。

`extract_pages()` 这一便捷函数默认过滤空文本，但批处理使用的是默认保留空页的 `iter_pages()`；不要将两个接口的默认行为混淆。

### 5.5 临时输出、替换与失败

**查看位置：** `parse_pdf()` 的 `NamedTemporaryFile`、`replace()` 和错误处理。

每份文档先在目标目录写临时文件，完成所有页面后替换正式输出。打开或解析异常时删除临时文件，保留旧正式结果，继续处理后续文档。

注意边界：**所有页面都提取为空时，页面记录会先保存到正式路径，再报告该文档失败。** 因而“失败时保留旧文件”仅适用于替换前发生的打开、解析等异常；不能概括成所有失败都保留旧输出。文件存在不等于解析成功。

## 6. 运行方式与参数

**操作位置：已激活 `fiona` 的服务器终端，项目根目录。**

先查看帮助，再创建本次检查专用的临时目录；后续使用该变量的命令需在同一终端执行：

```bash
python scripts/parse_pdf.py --help
FIONA_PDF_CHECK_DIR="$(mktemp -d /tmp/fiona-pdf-check.XXXXXX)"
python scripts/parse_pdf.py --doc-id paper_example_v1 --keyword forecasting --output-dir "$FIONA_PDF_CHECK_DIR"
```

这会生成临时目录中的 `paper_example_v1.jsonl`，不改写正式解析产物。摘要显示实际总页数、有文字页数、无文字页数与关键词命中页数；这些数值需与所选论文核对。终端只预览前 3 个匹配页面片段，输出文件仍包含全部页面。

|参数|作用与注意点|
|---|---|
|`--doc-id` / `--doc_id`|一个或多个已登记文档 ID，重复 ID 会去重；不指定则处理全部配置|
|`--keyword`|预览匹配页码与片段，不过滤输出|
|`--config`|读取另一份文档映射，不修改它|
|`--output-dir`|改变保存目录；相对路径按当前终端工作目录解释|

以下命令用于正式重跑，会替换对应同名 JSONL：

```bash
python scripts/parse_pdf.py --doc-id paper_example_v1
python scripts/parse_pdf.py
```

全量运行前须确认配置内所有文件均属于本次处理范围。执行后紧接着运行 `echo $?` 可查看退出码：0 表示所选文件均获得可用文字，1 表示有文件处理失败，2 表示配置或参数错误。部分页面无文字不会单独让整个文件失败，但需要后续检查。

## 7. 查看页面记录与核对原文

先查看临时输出的第 1 页；检查实验表时，将代码中的页码改为实际目标页：

```bash
python - "$FIONA_PDF_CHECK_DIR/paper_example_v1.jsonl" <<'PY'
import json
import sys
from pathlib import Path

with Path(sys.argv[1]).open(encoding="utf-8") as source:
    for line in source:
        page = json.loads(line)
        if page["page_number"] == 1:
            print("状态：", page["status"])
            print(page["text"])
            break
    else:
        raise ValueError("未找到 PDF 第 1 页")
PY
```

打开原 PDF 的对应物理页，对照提取文字检查阅读顺序、表头、单位和数值对应关系；关键词命中不能代替原文核验。

新论文抽查至少覆盖方法正文与一张目标实验表：确认双栏顺序、方法名、数据集、指标、预测步长和脚注对应关系。对公式记录“可读/需要增强”，不根据乱码猜原式。缺失或错误页记下物理位置，作为后续结构增强的输入。

## 8. 可复现验证与当次记录

以下程序在完成论文登记后执行，使用临时目录调用批处理，再核对页数、连续物理页码和页面状态。它不会替换正式输出。检查通过只代表页面结构一致，论文内容质量仍需按第 7 节人工核对。

**操作位置：已激活 `fiona` 的服务器终端，项目根目录。**

```bash
python - <<'PYTHON'
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
import pymupdf

mapping = json.loads(Path("configs/pdf_files.json").read_text(encoding="utf-8"))
doc_id = "paper_example_v1"  # 替换为已登记的实际 ID
with pymupdf.open(Path("data/raw") / mapping[doc_id]) as document:
    expected_pages = len(document)
with TemporaryDirectory(prefix="fiona-docs-parse-") as output_dir:
    subprocess.run([
        sys.executable, "scripts/parse_pdf.py", "--doc-id", doc_id,
        "--output-dir", output_dir,
    ], check=True)
    parsed = Path(output_dir) / f"{doc_id}.jsonl"
    pages = [json.loads(line) for line in parsed.read_text(encoding="utf-8").splitlines()]
    assert len(pages) == expected_pages
    assert [p["page_number"] for p in pages] == list(range(1, expected_pages + 1))
    assert all(p["doc_id"] == doc_id for p in pages)
    for page in pages:
        assert page["status"] == ("extracted" if page["text"].strip() else "empty")
        if page["status"] == "empty":
            assert page.get("reason")
    print(f"PASS：{expected_pages} 页，页码、文档 ID 与页面状态一致。")
PYTHON
```

2026-10-17 文档整理记录：示例已统一为待登记论文，未执行上述论文解析流程，未验证论文阅读顺序、公式、实验表格或下游切块。准备实际论文后，应记录文件版本、运行命令、页数、抽查位置和发现的问题，不预先填写成功结果。

## 9. 失败处理与重跑

|现象|可能原因与检查方法|处理方式|
|---|---|---|
|`No module named fiona/pymupdf`|解释器未指向 `fiona`，或项目未安装；打印 `sys.executable` 和导入路径|按环境教程检查安装，再重试|
|配置读取失败或未知 ID|JSON 格式错误，或 ID 未在映射中；检查 `--help` 与配置|修正配置或命令；参数错误退出码为 2|
|PDF 不存在|文件名、目录或配置有误；检查 `data/raw/` 实际路径|修正映射，不修改原文件内容来绕过错误|
|密码保护或页解析异常|查看错误信息与物理页码|获取可读取版本或定位目标页；异常时旧正式输出保留|
|部分页为空|可能是空白、图片或编码问题|打开对应页分类，需要文字的图片页后续做 OCR；保持原页码|
|所有页为空且返回 1|本次没有可用文字，页面记录仍已写入|不要当作成功入库；使用隔离输出检查输入与 OCR 需求|
|有文字但顺序混乱|双栏、布局或表格提取不正确|记录目标页与问题，不继续把它当作可靠证据；安排结构增强|
|无权限写输出|检查输出路径权限|选自己可写的临时目录或项目目录，不用 `sudo` 修改环境|

重跑成功会替换同名输出，因此检查时使用独立临时目录。第 6 节用 `mktemp` 建立的目录不会自动清理，可在核对后只删除那个明确的检查目录，或等待系统临时目录清理；第 8 节的程序会自动清理自身目录，不需要手动删除。不要清空 `data/raw/` 或 `data/parsed/` 排错。

## 10. 完成自查与下一步

- [ ] 理解文档 ID 与物理页码如何保留。
- [ ] 能运行批处理并区分页面状态、文件失败和参数错误。
- [ ] 能查看一页完整文字，并与原 PDF 核对阅读顺序及必要限定。
- [ ] 知道默认重跑、全空结果及临时输出对文件的影响。
- [ ] 对所选论文检查双栏、公式和实验表格，记录实际质量结论。

逐页解析完成后，下一模块将这些记录转换为可定位原文的证据块。新的论文种类与数量在[项目开发文档](../../项目开发文档.md#61-论文语料准备与来源登记)中规划，当前任务回到 [README](../../README.md#下一步结构解析再切块) 查看。切块规则、字段和验收查技术文档；本教程不宣称切块已经实现。

## 11. 参考

本教程以仓库内解析器和批处理脚本为实现依据。进一步理解第三方 API 时查 [PyMuPDF 文本提取文档](https://pymupdf.readthedocs.io/en/latest/app1.html)与 [Page.get_text()](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_text)。依赖行为以本次使用的版本验证为准。
