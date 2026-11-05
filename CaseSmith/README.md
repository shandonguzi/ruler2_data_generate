# CaseSmith

把 `LLM-PPOpt` 的 persona 五步流水线迁移到本项目的实现：读取 `QA_case.csv` 中的 query，按 persona 风格改写，跑多轮对话（含 distractor 噪声），最终导出逐轮 CSV。

## 五步流水线

1. **Persona 池**：从 `input/persona.yaml`（33 维）采样 persona，并用 LLM 生成 system prompt。
2. **Query 改写**：按 `--num` 随机抽取 CSV 行，每条 query 从 persona 池随机分配一个 persona，用 `prompts/query_style_transfer*.txt` 改写成自然口语 query。
3. **多轮交互**：Assistant / User 双智能体对话（默认固定 1 轮，由 `interaction_generation.min_turns` / `max_turns` 控制），产生每轮回复。
4. **Distractor 噪声**：25% 概率对每条 user 消息注入语义噪声（内联在步骤 3）。
5. **导出**：训练数据 JSON（中间产物）+ 最终逐轮 CSV。

## 安装

```bash
pip install -r requirements.txt
```

## 配置

仓库提供脱敏模板 `config.yaml.example`，包含全部可配置项。首次使用：

```bash
cp config.yaml.example config.yaml
export OPENAI_API_KEY="<你的 key>"   # 或直接填写 config.yaml 的 api.api_key
```

- **不要提交填好真实密钥的 `config.yaml`**；模板里的 `api_key` 为空，`base_url`、模型名均为占位符。
- `config.yaml` 中所有相对路径都相对于该配置文件所在目录（`run.py` 会切换到该目录）。
- 只做结构冒烟测试时无需任何密钥：`python run.py --mock`。

### 必填配置

| 配置项 | 说明 |
| --- | --- |
| `api.provider` | `openai` / `openrouter` / `anthropic` |
| `api.api_key` | 留空或为占位符时，读取 `<PROVIDER>_API_KEY` 环境变量（如 `OPENAI_API_KEY`） |
| `api.base_url` | OpenAI 兼容网关地址；用官方 API 可填 `https://api.openai.com/v1` |
| `api.model` | 主模型名，用于 persona 生成、query 改写、user 模拟 |
| `paths.input_csv` | 输入 CSV，必须含 `id, dialect, open_question, open_answer` 列 |
| `paths.persona_bank.persona` | persona 池定义，默认 `input/persona.yaml` |

### 常用可选项

- `interaction_generation.assistant_model.model_pool`：assistant 模型池，按 `weight` 加权随机选择，权重之和必须为 `1.0`；条目未指定 `api_key` / `base_url` 时回退到顶层 `api` 配置。
- `interaction_generation.user_model`：模拟用户模型及其反馈概率。
- `interaction_generation.max_workers`：并发生成对话的 worker 数（也可用 `--max-workers` 覆盖）。
- `query_selection.num`：默认处理的 query 数（`null` = 全量；`--num` 优先）。
- `persona_generation.num_personas`：persona 池上限，实际池大小 = `min(num, 该值)`。
- `paths.output_csv` / `paths.failed_csv` / `paths.state_dir`：输出与断点状态路径。
- `distractor.*`：噪声注入开关、激活概率与策略；`distractor.llm.enabled=true` 时需要额外的 LLM 提示词文件。
- `output.encoding`：默认 `utf-8`；Excel 打开乱码可改 `utf-8-sig`。
- `openrouter`：仅在使用 OpenRouter 时才需要；模板中整段被注释，取消注释后必须提供有效密钥。

### 配置校验

启动时会自动校验配置（文件是否存在、密钥是否缺失、模型池权重是否为 1.0、OpenRouter endpoint 格式等），错误会在运行前直接列出并以非零码退出。`--mock` 模式会跳过密钥校验。

## 运行

```bash
export OPENAI_API_KEY="<你的 key>"

# 小样本验证（3 条 query，3 个 persona）
python run.py --num 3 --num-personas 3

# 全量 1978 条
python run.py
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--num N` | 随机抽取 N 条 query；`N >= 总行数` 时按 CSV 原顺序全量 |
| `--num-personas P` | persona 池大小 |
| `--stage all\|persona\|query\|interaction\|training\|export` | 分阶段执行 / 断点续跑 |
| `--resume` | 继续上一次运行（默认全量运行每次开新样本） |
| `--restart` | 归档旧状态并重新抽样 |
| `--fresh-personas` | 不复用已有 persona 池，重新生成一批 persona |
| `--skip-distractor` | 关闭噪声注入 |
| `--skip-query-transfer` | 不改写 query（`improved_query` = 原文） |
| `--output-csv PATH` | 覆盖输出路径 |
| `--max-workers N` | 覆盖并发数 |
| `--mock` | 零成本结构冒烟（Mock LLM） |
| `--strict` | 存在失败 query 时退出码 2 |

### 关于随机性

- 每次执行 `--stage all` 都会生成一个新的内部随机种子，自动归档旧状态，重新抽样 query、重新随机分配 persona。**不提供 seed 参数，也不追求可复现**；日志里的 `Run id (random seed): N` 只用于标识本次运行。
- **persona 池默认复用**：只让每条 query 分到的 persona 每次都不同；加 `--fresh-personas` 才会重新生成 persona 身份。
- 想接着上一次中断的运行，用 `--resume`。
- 只用 `--stage persona/query/interaction/export` 分阶段执行时，会复用当前状态，不会每次重新抽样。

## 输出 CSV

列顺序：

```
id,dialect,open_question,open_answer,persona_id,turn,improved_query,noisy_query,answer,domain,scenario,query_id,source
```

- 同一 query 各轮完全相同的列（`id`、`dialect`、`open_question`、`open_answer`、`persona_id`、`domain`、`scenario`、`query_id`、`source`）只在第一轮行填写，后续轮次留空；每轮行只填 `turn`、`improved_query`、`noisy_query`、`answer`。
- `improved_query`：干净版（persona 改写后）query。
- `noisy_query`：实际参与对话的版本；未注入噪声时与 `improved_query` 相同。
- `answer`：该轮 assistant 的回复。
- `turn`：从 1 开始。
- 失败 query 记录在 `output/failed_queries.csv`。

注意：`answer` 是模型原文，可能包含换行，因此 CSV 字段内会有 `\n`；请始终用 CSV 解析器读取，不要按行切分。文件使用 UTF-8 无 BOM、CRLF 行分隔，与 `QA_case.csv` 一致。

## 校验

```bash
python scripts/verify_export.py output/QA_case_improved.csv \
  --state output/state/query_assignments.json \
  --interactions-dir output/interactions
```

检查表头、query 级列只在首轮出现、轮次连续性、空值，并与 interaction JSON 逐轮交叉核对。

## 断点续跑

`output/state/query_assignments.json` 记录每条 query 的状态（`pending` → `styled` → `done` / `failed_*`），交互 JSON 一完成就落盘，中断后可从 index 恢复已完成的对话。

- 每次 `--stage all` 默认开一批新样本（旧状态自动归档）。要接着上次中断的运行，加 `--resume`。
- 改变了输入 CSV、`--num` 或 persona 池大小时，会提示签名不一致，需加 `--restart`。
- 只用 `--stage persona/query/interaction/export` 分阶段执行时，会复用当前状态，不会每次重新抽样。

## 中间产物

- `output/personas/`：persona 与 system prompt
- `output/interactions/`：每条 query 的完整对话 JSON（含 `clean_version` 与噪声元信息）
- `output/training_data/`：TrainingSample JSON 与 token 统计
- `logs/`：运行日志
