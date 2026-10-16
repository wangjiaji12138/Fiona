# 2. PDF 解析：逐页提取、清洗与 JSONL 输出

更新日期：2026-10-15。适用环境：Linux、Bash、Conda `fiona`。本教程整理已经实现的通用 PDF 解析能力；当前项目定位为 Fiona（曌），面向时空预测论文。整体进度与新的论文准备任务查 [README](../../README.md)。

教程保留已有招商银行年报作为**历史解析回归样例**，因为文件和输出均已存在，方便核对实现；它不属于新的论文知识库。新论文尚未通过本教程验证，双栏、公式与实验表格需要重新抽查。不要把旧样例的成功视为论文解析质量已经达标。

## 1. 本次要完成什么

输入一份 PDF，按物理页提取文字，清理重复空白，保存到一份 JSONL。每条文字都能根据文档 ID 和页码回到原文件；无文字页也保存位置和待检查原因。

|对象|本教程样例|
|---|---|
|输入映射|`configs/pdf_files.json` 中的 `cmb_2024_annual`|
|原始 PDF|`data/raw/招商银行2024年年度报告.pdf`|
|正式输出|`data/parsed/cmb_2024_annual.jsonl`|
|完成标志|能逐行读取输出、核对页面数量与物理页序，并在原 PDF 找到对应文字|
|能力范围|提取与基础清洗；暂不执行 OCR、结构恢复、切块、索引或回答|

解析输出是后续切块的输入。一条页面记录不等于一个适合检索的证据块，文本非空也不能证明公式、阅读顺序和表格正确。

## 2. 先理解三个概念

**物理页序：** PDF 查看器从文件第一页开始计数的位置，程序输出从 1 开始。PDF 内印刷的页码可能不同；例如本样例 PDF 第 14 页印刷页码为 13。引用打开页面时使用物理页序，不能把正文第一页重新编号为 1。

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

检查样例映射与输入文件：

```bash
python - <<'PY'
import json
from pathlib import Path

mapping = json.loads(Path("configs/pdf_files.json").read_text(encoding="utf-8"))
doc_id = "cmb_2024_annual"
pdf_path = Path("data/raw") / mapping[doc_id]
print("文档 ID：", doc_id)
print("PDF 路径：", pdf_path)
assert pdf_path.is_file(), f"找不到文件：{pdf_path}"
PY
```

脚本不自动扫描 `data/raw/`。配置中没有登记的文件，不会因为放进目录就被处理。

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
python scripts/parse_pdf.py --doc-id cmb_2024_annual --keyword 营业收入 --output-dir "$FIONA_PDF_CHECK_DIR"
```

这会生成临时目录中的 `cmb_2024_annual.jsonl`，不改写正式解析产物。预期摘要为 362 页、有文字 362 页、无文字 0 页，19 页包含关键词。终端只预览前 3 个匹配页面片段，输出文件仍包含全部页面。

|参数|作用与注意点|
|---|---|
|`--doc-id` / `--doc_id`|一个或多个已登记文档 ID，重复 ID 会去重；不指定则处理全部配置|
|`--keyword`|预览匹配页码与片段，不过滤输出|
|`--config`|读取另一份文档映射，不修改它|
|`--output-dir`|改变保存目录；相对路径按当前终端工作目录解释|

以下命令用于正式重跑，会替换对应同名 JSONL：

```bash
python scripts/parse_pdf.py --doc-id cmb_2024_annual cmb_2023_annual
python scripts/parse_pdf.py
```

当前配置仍包含旧金融资料，全量运行不代表已处理新的时空预测论文。执行后紧接着运行 `echo $?` 可查看退出码：0 表示所选文件均获得可用文字，1 表示有文件处理失败，2 表示配置或参数错误。部分页面无文字不会单独让整个文件失败，但需要后续检查。

## 7. 查看页面记录与核对原文

先查看临时输出的第 14 页：

```bash
python - "$FIONA_PDF_CHECK_DIR/cmb_2024_annual.jsonl" <<'PY'
import json
import sys
from pathlib import Path

with Path(sys.argv[1]).open(encoding="utf-8") as source:
    for line in source:
        page = json.loads(line)
        if page["page_number"] == 14:
            print("状态：", page["status"])
            print(page["text"])
            break
    else:
        raise ValueError("未找到 PDF 第 14 页")
PY
```

历史样例的核对目标是该页的一张财务表：文本中应能一起找到年份列头、单位、指标行与数值。这个例子的意义是检查表格必要限定是否还在；并不将该问题继续作为新论文知识库的验收题。

打开原 PDF 的物理第 14 页，检查数值所在列和单位，不能只搜索到数字就认定正确。此前样例记录核对的是 2024 年“营业收入”337,488，单位为人民币百万元，口径为本集团；本次自动检查只检查该字符串存在，没有重新人工核对原图。

新论文抽查至少覆盖方法正文与一张目标实验表：确认双栏顺序、方法名、数据集、指标、预测步长和脚注对应关系。对公式记录“可读/需要增强”，不根据乱码猜原式。缺失或错误页记下物理位置，作为后续结构增强的输入。

## 8. 可复现验证与当次记录

以下程序自己创建并清理临时目录，调用真实批处理脚本后核对页面结构，与现有正式样例逐字节比较。它不会替换正式输出；只用于本仓库仍保留此历史样例时的回归检查。

**操作位置：已激活 `fiona` 的服务器终端，项目根目录。**

```bash
python - <<'PY'
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

with TemporaryDirectory(prefix="fiona-docs-parse-") as output_dir:
    command = [
        sys.executable, "scripts/parse_pdf.py",
        "--doc-id", "cmb_2024_annual", "--keyword", "营业收入",
        "--output-dir", output_dir,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    parsed = Path(output_dir) / "cmb_2024_annual.jsonl"
    pages = [json.loads(line) for line in parsed.read_text(encoding="utf-8").splitlines()]
    assert len(pages) == 362
    assert [p["page_number"] for p in pages] == list(range(1, 363))
    assert all(p["doc_id"] == "cmb_2024_annual" for p in pages)
    assert all(p["status"] == "extracted" and p["text"].strip() for p in pages)
    assert sum("营业收入" in p["text"] for p in pages) == 19
    assert "337,488" in pages[13]["text"]
    assert parsed.read_bytes() == Path("data/parsed/cmb_2024_annual.jsonl").read_bytes()
    print("PASS：362 页、连续页码、非空状态、关键词及输出一致性检查通过。")
PY
```

与正式输出不一致时先检查输入文件、PyMuPDF 版本与规范化规则是否变化；不能直接覆盖参考文件让检查通过。上述字符串检查只覆盖数据流和回归行为，不证明原文含义正确。

2026-10-15 本次整理的实际记录：

|项目|结果与范围|
|---|---|
|解释器与库|Conda `fiona`，Python 3.14.8，PyMuPDF 1.28.2，导入路径为本项目|
|命令帮助与依赖|`conda run -n fiona python scripts/parse_pdf.py --help` 和 `conda run -n fiona python -m pip check` 通过|
|单文件重跑|由上述 Python 子进程流程使用 `fiona` 解释器在临时目录执行，退出码 0；362 页检查及与正式输出逐字节一致性通过|
|已有产物检查|读取当前配置及其 JSONL，核对文档 ID、连续物理页码、文本与页面状态是否一致；统计写入 README 的历史样例状态|
|未验证项|未重跑全部旧 PDF，未测试新的时空论文，未执行 OCR、双栏/公式恢复或下游切块；未在本次重新人工核对原图|

表中结果是本次记录；其余逐步命令供复现使用，不表示每个示例变体都已单独运行。

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
- [ ] 对新论文重新检查双栏、公式和实验表格，不继承旧样例的质量结论。

逐页解析完成后，下一模块将这些记录转换为可定位原文的证据块。新的论文种类与数量在[项目开发文档](../../项目开发文档.md#61-论文语料准备与来源登记)中规划，当前任务回到 [README](../../README.md#下一步重建论文语料再切块) 查看。切块规则、字段和验收查技术文档；本教程不宣称切块已经实现。

## 11. 参考

本教程以仓库内解析器和批处理脚本为实现依据。进一步理解第三方 API 时查 [PyMuPDF 文本提取文档](https://pymupdf.readthedocs.io/en/latest/app1.html)与 [Page.get_text()](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_text)。依赖行为以本次使用的版本验证为准。
