# mineru-autonomous-ocr

由 AI 启动、脚本自动完成的 MinerU PDF 转 Markdown Skill。默认保留 PDF 物理页码与原书印刷页码，适用于批量扫描书、论文等 PDF 的整理与回查。

## 能做什么

- 自动切块（每块最多200页；超过大小限制继续拆分）、上传、轮询、下载和合并。
- 保留文字、图片、页级结构、双页码映射与识别证据；印刷页码无法确认时标为待确认。
- 保存成功分块和批次状态，续跑先校验已完成结果。
- 自动检查完整性，并清理本任务临时文件。
- 详细日志写入文件；向 AI 仅返回简短进度、失败摘要与最终报告。

## 最简单的使用方法

下载整个仓库，交给能够访问本机文件并执行脚本的 AI 工具，说：

> 请安装 mineru-autonomous-ocr，检查运行环境，把【输入目录】里的 PDF 转成 Markdown，保存到【输出目录】。

需要你提供：输入与输出路径，以及自己的 MinerU API 密钥。PDF 会上传到 MinerU；密钥仅配置在本机私有环境中，不要粘贴到聊天、脚本或仓库。

这个 Skill 的名称就是 `mineru-autonomous-ocr`，自然语言调用无需添加 `$`。把文件上传到普通聊天不等于获得本机执行权限。

## 安装与依赖

需要 Python 3.10+。完整保留仓库文件结构，把文件夹作为本地 Skill 安装到你所用工具支持的目录；也可让 AI 按 [SKILL.md](SKILL.md) 检查并安装。

在仓库根目录建立独立 Python 环境：

```bash
python3 -m venv .venv
```

macOS / Linux：

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Windows PowerShell：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

扫描页边的印刷页码识别另需系统程序 Tesseract（可访问 https://tesseract-ocr.github.io/tessdoc/Installation.html 查看安装方式）。没有 Tesseract 时仍可转换正文、尝试文字层页码，但扫描页码可能待确认。

通过安全的本机配置把自己的密钥注入环境变量 `MINERU_API_TOKEN`；不要把密钥作为脚本参数或提交到 GitHub。

## 脚本命令

以下使用 macOS/Linux 环境中的 Python；Windows 改用 `.venv\Scripts\python.exe`。

```bash
.venv/bin/python scripts/run_ocr.py --input "/输入PDF或目录" --output "/输出目录" --dry-run
.venv/bin/python -u scripts/run_ocr.py --input "/输入PDF或目录" --output "/输出目录"
.venv/bin/python scripts/run_ocr.py --output "/输出目录" --status
```

AI 应启动并跟进进程。使用者保持电脑联网、避免休眠，并在完成后抽查文字与页码。遇到需要判断的异常，脚本保留结果交给 AI；不自动盲目重新上传。

## 输出

所有最终 Markdown 默认集中在输出目录的 `Markdown/` 文件夹，名称与原 PDF 一致：`书名.pdf` → `Markdown/书名.md`。配套图片放在 `Markdown/images/`，相对链接一并整理。不同来源同名 PDF 会在上传前提示重命名，避免覆盖。

每个来源另保留独立恢复目录，包含内部缓存 `document.md`、`page_map.json`、`page_blocks.jsonl`、图片、原始 Markdown 和结构数据。集中目录或图片删除后，续跑可从缓存重新整理，无需重新上传。人工修改过的集中 Markdown 会提示保留或移走，不直接覆盖。输出根目录保存 `_run.log`、`_status.json` 与 `_report.json`。

具体参数、断点续跑、人工页码映射与异常处理见 [执行参考](references/workflow.md)。

## 当前验证与限制

[32项离线测试通过](validation.md)，覆盖切块、模拟 API、失败续跑、完整性检查和页码识别。尚未进行真实 MinerU 账户在线联调。

默认页码识别面向页眉/页脚的阿拉伯和罗马数字；复杂版式、竖排双页和中文数字可能仍需人工映射。自动识别不是人工核实，正式引文应回查原 PDF。只支持已适配的 V1 页级内容列表；不兼容结果会停止该文件并报告。

## 离线测试

```bash
.venv/bin/python -m unittest discover -s tests -v
```

不使用真实 MinerU 账户，不上传资料。扫描页码测试需要 Tesseract 和测试字体。

## 分享

分享整个仓库或压缩包，接收方配置自己的依赖和 MinerU 密钥。仓库用于发布 Skill 文件；当前没有 GitHub Actions 云端 OCR 工作流或在线上传网站。
