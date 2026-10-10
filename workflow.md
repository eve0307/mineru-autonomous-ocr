# 执行参考

本包包含主脚本，不依赖作者电脑上的旧脚本。基于 MinerU 官方 V4 文档（2026-10-10核对）：https://mineru.net/apiManage/docs 。提交限制是200页和200MB，脚本采用保守的200,000,000字节上限。Markdown/JSON为默认结果。接口参数或输出结构变化时先核对文档。

## 参数

| 参数 | 默认与作用 |
| --- | --- |
| `--input` | 本次 PDF 文件或目录，必填（状态查询除外） |
| `--output` | 本次输出目录，必填 |
| `--dry-run` | 只盘点，不上传、不写输出 |
| `--status` | 只返回已有运行的简短状态 |
| `--no-recursive` | 不递归；默认递归 |
| `--exclude` | 可重复指定排除文件/目录 |
| `--model` / `--language` | `vlm` / `ch` |
| `--no-printed-pages` | 默认关闭；用户确认原书无印刷页码才启用 |
| `--poll-seconds` | 15秒；轮询细节仅写日志 |
| `--max-wait-minutes` | 每块120分钟；超时保留批次 |
| `--network-retries` | 查询/下载额外重试2次；申请/上传不盲目重试 |
| `--retry-failed` | 对已明确失败的块重新提交 |
| `--max-submissions` | 每块累计提交最多2次，可设1–3 |
| `--pause-file` | 默认 `~/.mineru_pause`；存在时整条管线停止 |

凭据为环境变量 `MINERU_API_TOKEN`，不支持命令行直接传密钥。API授权头仅用于 MinerU API，不传到上传/下载地址。状态保存批次ID但不保存带授权参数的 URL，因此在申请已完成但文件未上传时中断，续跑可能需要 AI 判断；这优先保证不会盲目重传。删除失败状态不是常规重试办法。

## 输出与完成

```text
输出目录/
  _run.log          详细阶段、轮询与失败日志（不包含正文全文）
  _status.json      当前来源和聚合状态
  _report.json      全批次最终结果
  <来源名__内容哈希_路径哈希>/
    document.md
    page_map.json
    page_blocks.jsonl
    images/chunk_0001/images/...
    _raw/chunk_0001/
      full.md
      content_list.json
      images/...
      structure/...   其余原始 JSON
    _state.json      成功块哈希、远端批次和续跑状态
    manifest.json    全部检查后才标 complete
    _tmp/           下载ZIP、临时页边图；成功后清理
    _working/       PDF分块；成功后清理
```

成功分块保存原始 JSON、Markdown和图片，下一次先核对校验值；最终输出、映射或图片缺失时重新校验缓存并重建，不仅判断 Markdown 是否存在。源文件和配置发生变化时停止复用，使用新的输出目录或检查旧任务。完整报告可能较长，应默认读取脚本的简短摘要或 `--status`，只按需要定位失败项。

## 印刷页码证据

默认尝试文字层上/下15%页边的独立页码；没有文字候选时用Tesseract识别扫描页边（默认英文字母和数字，支持阿拉伯/罗马页码）。候选需有唯一连续三页序列支持才自动采用，`detected`不等于人工核实。不自动插值、不将 PDF 页标签当成原书印刷页码。其他页码位置、中文数字、竖排双页和复杂跳号可能仍待确认，需要另行适配或人工映射。

映射每页保存 `pdf_page`、`printed_label`、`status`、`method`、`evidence`、候选和正文块状态。状态为 `detected`（自动识别）、`observed`（提供的直接观察）、`inferred`（提供的推算）、`absent`（明确无页码）或 `unknown`。手工映射放在源 PDF 旁的 `<文件名>.pdf.pages.csv`，每行都须有证据；文件读入后不修改：

```csv
pdf_page,printed_label,status,evidence
3,iii,observed,人工检查PDF第3页页脚
4,iv,inferred,前后原页确认的短缺口推算
5,,absent,人工检查该原页没有印刷页码
```

例：`**【PDF 第 3 页｜原书第 iii 页】**`；推算加`（推算）`；未知写`原书页码未识别／待确认`；明确无页码时只有物理页标记。无正文块不强行解释为空白页，需要回查原 PDF。

## 异常处理

- 401/403：停止整批，修复账户凭据后重跑；不能用证书绕过解决认证失败。
- 查询/下载网络失败：有限重试后保留批次；重跑查询原批次。
- POST结果不确定：状态为`allocating`且无批次ID。交给AI核查账户任务，不能自动清状态后再提交。
- PUT结果不确定：先查询远端；若已排队/运行/完成继续，仍等待文件则交给AI判断。
- 远端`failed`：保留错误，查明原因后用`--retry-failed`；超过累计上限不再上传。
- 内容V2/页索引异常/图片缺失：保存已下载分块，但不发布完成标记。AI检查原始结构后修复适配并重新验证。
- 暂停/中断：不丢成功块；需要恢复时先核对暂停标记，不擅自移除用户主动设置的暂停。

## 离线验证

在 Skill 根目录运行：

```bash
python3 -m unittest discover -s tests -v
```

测试不调用真实MinerU：使用生成PDF和模拟API验证切块、失败不发布、成功缓存、数据损坏重建、认证/上传不确定、双页码和清理边界。扫描页码测试需要Tesseract和测试字体，缺失时会显示跳过。离线通过不能替代真实账户联调。
