# ScopeCrawl

ScopeCrawl 根据 S3/S4 场景、地点与语言，检索关联网页，清洗正文并输出纯正文 Markdown 文档与元数据 JSON，供后续生成 QA 数据使用。

## 项目简介

- **S3**：大范围场景，如「天气查询」。
- **S4**：小范围场景，如「气象灾害查询」，是检索重点。
- **地点 / 语言**：可选地点限定与关键词语言。

程序使用 LLM 将场景展开为具体搜索词，检索网页，按 URL 结构与域名筛选，通过 Tavily Extract 批量获取正文，清洗并评估后保留文档。中间数据只在内存中使用，不支持断点续跑。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

`.env` 必填项：

| 键 | 说明 |
|---|---|
| `SCOPECRAWL_TAVILY_API_KEY` | Tavily 搜索与正文提取 Key |
| `SCOPECRAWL_LLM_MODEL` | 兼容 `/v1/chat/completions` 的模型名 |
| `SCOPECRAWL_LLM_API_KEY` | LLM Key |
| `SCOPECRAWL_LLM_BASE_URL` | 可选，留空使用 OpenAI 官方地址 |

密钥与模型名只从项目根目录 `.env` 读取，不读取进程环境变量。

## 运行

```bash
python main.py
python main.py --s3 "天气查询" --s4 "气象灾害查询" --location "埃及" --language zh
```

- 不带参数时使用 `config.yaml` 的 `run` 段。
- `--s3/--s4/--location/--language` 逐项覆盖 `run` 段。
- `language` 支持 `zh`、`en`、`ar`、`ar-EG`。
- `--restart` 清空跨运行 URL 历史表后重新采集。

运行过程中会打印关键词生成、搜索、粗筛、提取、评估、多样性限流与写盘进度。单个 URL、单次搜索或单次 LLM 调用失败不会中断运行，结束后统一列出。

## 跨运行去重

去重、跳过历史 URL 并完成 `search.dedupe.max_urls` 截断后，进入预筛队列的 URL 会立即写入项目根目录的 `.history.json`，全场景共享。下次运行（无论场景是否相同）时：

- 搜索结果中命中历史 URL 的条目会被跳过，只按规范化 URL 精确匹配，不按域名或前缀排除。
- 跳过后由队列中后续的 URL 补位，再按 `search.dedupe.max_urls` 截断。
- 进入队列即记录，`--restart` 删除整张表，重新开始。

历史表记录所有进入预筛队列的 URL，包括后续被 URL 粗筛、域名预筛、质量闸门、评估丢弃或提取失败的 URL，避免同一页面在后续运行中被反复召回。被 `max_urls` 截断、未进入队列的 URL 不记录，下次仍有机会被采集。

退出码：

- `0`：正常完成（允许部分失败）
- `1`：配置错误、目录不可写、搜索全部失败、提取鉴权/额度失败等致命错误
- `2`：命令行参数错误

## 输出

```text
data/{run_id}/{标题}.md
data/{run_id}/metadata.json
.history.json
```

- `run_id` 为本地时间 `YYYYMMDD-HHMMSS`。
- Markdown 文件名取页面标题，非法字符替换为 `_`，超长截断，重名追加序号；文件内容只保留清洗后的正文 Markdown。
- `metadata.json` 记录本次运行的 S3/S4、地点、语言、时间范围，以及每篇文档对应的文件名、`doc_id`、URL、匹配查询、相关性与内容/权威度评分、评估结论等元数据。
- `.history.json` 为跨运行 URL 历史表，位于项目根目录，全场景共享，供下次运行跳过已处理 URL。

## 配置

- `config.yaml`：本次运行输入，以及搜索、提取、评估、限流参数。
- `authority.yaml`：域名白名单 / 黑名单，支持 `*.gov.eg` 通配。
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
