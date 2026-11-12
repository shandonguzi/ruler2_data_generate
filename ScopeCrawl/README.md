# ScopeCrawl

ScopeCrawl 根据 S3/S4 场景、地点与语言，检索关联网页，清洗正文并输出纯正文 Markdown 文档与元数据 JSON，供后续生成 QA 数据使用。

## 项目简介

- **S3**：大范围场景，如「天气查询」。
- **S4**：小范围场景，如「气象灾害查询」，是检索重点。
- **地点 / 语言**：可选地点限定与关键词语言。

程序按「关键词生成 → 检索候选网页 → 筛选候选 URL → 提取并清洗正文 → 评估并筛选文档 → 生成文档」6 个阶段生成文档，每个阶段在 `scopecrawl/stages/` 下独立实现。中间数据只在内存中使用，不支持断点续跑。

## 处理流程

一次运行按 6 个阶段依次执行，每个阶段接收上一阶段的输出并返回本阶段的结果：

| # | 阶段 | 输入 → 输出 | 主要动作 |
|---|---|---|---|
| 1 | 关键词生成 | S3/S4、地点、语言 → 搜索词 | 查场景覆盖要点，调用 LLM 生成关键词 |
| 2 | 检索候选网页 | 搜索词 → 原始搜索结果 | 并发调用搜索接口；单条查询失败不中断 |
| 3 | 筛选候选 URL | 原始结果 → 候选 URL | 去重、跳过历史、按关键词轮转截断、URL 粗筛、域名黑白名单 |
| 4 | 提取并清洗正文 | 候选 URL → 正文文档 | Tavily Extract 批量提取，清洗导航/页脚噪声，过质量闸门 |
| 5 | 评估并筛选文档 | 正文文档 → 带评分的文档 | LLM 内容复核、权威度评估、丢弃判定、多样性限流 |
| 6 | 生成文档 | 文档 → Markdown + metadata.jsonl | 分配文件名、原子写盘、逐篇追加元数据 |

- 阶段 3 在筛选完成后会把进入队列的 URL 写入 `config/.history.json`，供后续运行跳过。
- 阶段 1、2、4 的致命错误（关键词生成失败、搜索全部失败或 HTTP 400、提取鉴权/额度/全批失败）会终止 run，退出码为 `1`。
- 阶段 3、5、6 只做剔除或降级：单个 URL、单篇文档或单次 LLM 调用失败不会中断运行，结束后统一汇总。

代码结构：

```text
scopecrawl/
  pipeline.py                # 串联 6 个阶段
  stages/
    keyword_generation.py    # 1 关键词生成
    search.py                # 2 检索候选网页
    url_filter.py            # 3 筛选候选 URL
    extract.py               # 4 提取并清洗正文
    evaluate.py              # 5 评估并筛选文档
    document_generation.py   # 6 生成文档
  summary.py                 # 运行统计与控制台汇总
```

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env
```

`config/.env` 必填项：

| 键 | 说明 |
|---|---|
| `SCOPECRAWL_TAVILY_API_KEY` | Tavily 搜索与正文提取 Key |
| `SCOPECRAWL_LLM_MODEL` | 兼容 `/v1/chat/completions` 的模型名 |
| `SCOPECRAWL_LLM_API_KEY` | LLM Key |
| `SCOPECRAWL_LLM_BASE_URL` | 可选，留空使用 OpenAI 官方地址 |

密钥与模型名只从 `config/.env` 读取，不读取进程环境变量。

## 运行

```bash
python main.py
python main.py --s3 "天气查询" --s4 "气象灾害查询" --location "埃及" --language zh
```

- 不带参数时使用 `config/config.yaml` 的 `run` 段。
- `--s3/--s4/--location/--language` 逐项覆盖 `run` 段。
- `language` 支持 `zh`、`en`、`ar`、`ar-EG`。
- `--restart` 清空跨运行 URL 历史表后重新采集。

运行过程中每个阶段开始时打印一行分隔日志（如 `================ 评估筛选 ================`），阶段内日志带对应前缀（`[关键词生成]`、`[检索]`、`[URL 筛选]`、`[提取清洗]`、`[评估筛选]`、`[生成文档]`）。单个 URL、单次搜索或单次 LLM 调用失败不会中断运行，结束后统一列出。

## 跨运行去重

去重、跳过历史 URL 并完成 `search.dedupe.max_urls` 截断后，进入预筛队列的 URL 会立即写入 `config/.history.json`，全场景共享。下次运行（无论场景是否相同）时：

- 搜索结果中命中历史 URL 的条目会被跳过，只按规范化 URL 精确匹配，不按域名或前缀排除。
- 跳过后由队列中后续的 URL 补位，再按关键词轮转取数并截断到 `search.dedupe.max_urls`，保证靠后的关键词也有机会进入队列。
- 进入队列即记录，`--restart` 删除整张表，重新开始。

历史表记录所有进入预筛队列的 URL，包括后续被 URL 粗筛、域名预筛、质量闸门、评估丢弃或提取失败的 URL，避免同一页面在后续运行中被反复召回。被 `max_urls` 截断、未进入队列的 URL 不记录，下次仍有机会被采集。

退出码：

- `0`：正常完成（允许部分失败）
- `1`：配置错误、目录不可写、搜索全部失败、提取鉴权/额度失败等致命错误
- `2`：命令行参数错误

## 输出

```text
data/{run_id}/{标题}.md
data/{run_id}/metadata.jsonl
config/.history.json
```

- `run_id` 为 `{S3}-{S4}-{地点}-{语言}-{本地时间}`，例如 `天气查询-气象灾害查询-埃及-zh-20261111-163300`；地点为空时省略该段。
- Markdown 文件名取页面标题，非法字符替换为 `_`，超长截断，重名追加序号；文件内容只保留清洗后的正文 Markdown。
- `metadata.jsonl` 为 JSON Lines：首行记录本次运行的 S3/S4、地点、语言、时间范围（`type` 为 `run`），之后每篇文档追加一行（`type` 为 `document`），记录文件名、`doc_id`、URL、匹配查询、相关性与内容/权威度评分、评估结论等；可按行读取，例如 `pd.read_json(path, lines=True)` 或逐行 `json.loads`。
- `config/.history.json` 为跨运行 URL 历史表，全场景共享，供下次运行跳过已处理 URL。

## 配置

所有配置集中在 `config/` 目录：

- `config/config.yaml`：本次运行输入，以及搜索、提取、评估、限流参数。
- `config/.env`：密钥与 LLM 模型名，模板见 `config/.env.example`。
- `config/scenarios.json`：场景表（S1 → S2 → S3 → S4 嵌套），记录每个 S4 的 `coverage_points`。关键词生成时按 `run.s3` / `run.s4` 精确查找并注入提示词，作为关键词必须覆盖的下限；场景未收录时告警并按无要点继续。路径由 `keywords.scenarios_path` 指定。
- `config/authority.yaml`：域名白名单 / 黑名单，支持 `*.gov.eg` 通配。路径由 `authority.config_path` 指定。
- `prompts/`：关键词生成与内容评估提示词。

## 测试

```bash
python -m pytest -q
ruff check .
```

测试全部 mock 网络与 LLM，不需要真实 API Key。

## 文档

- `README.md`：项目介绍与使用说明
- `CLAUDE.md`：代码规范
- `CHANGELOG.md`：变更日志
