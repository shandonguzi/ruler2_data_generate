# CaseSmith 单条用例生成流程

CaseSmith 从 [QA_case.csv](../QA_case.csv) 读取原始 QA，给抽中的问题分配一个 persona，按画像改写问法，再让 Assistant 生成回复。配置模板默认生成一轮。启用 Distractor 时，每条用户消息在发给 Assistant 前有 25% 的概率触发噪声注入。

## 1. 准备配置

从仓库根目录执行：

```bash
cd CaseSmith
python -m pip install -r requirements.txt
cp -n config.yaml.example config.yaml
```

配置模板见 [config.yaml.example](../config.yaml.example)。`run.py` 默认读取 `config.yaml`，也可以用 `--config` 指定配置文件。配置里的相对路径以该文件所在目录为基准。

`paths.input_csv` 默认指向 `QA_case.csv`。`--num 1` 会随机抽取一条。若要测试指定的 QA，可以新建一个带表头、只含目标行的 CSV，并让 `paths.input_csv` 指向它。输入至少需要 `id`、`dialect`、`open_question`、`open_answer` 四列。

## 2. 跑一条冒烟测试

在 `CaseSmith/` 目录执行：

```bash
python run.py --mock --num 1 --num-personas 1 --strict
```

`--mock` 使用模拟模型，无需 API 密钥，适合检查流水线和输出结构。`--strict` 在有失败用例时返回退出码 2。

## 3. 生成一条真实用例

使用模板默认的 OpenAI 兼容配置时，在 `config.yaml` 中设置 `api.base_url` 和 `api.model`，并把 `interaction_generation.assistant_model.model_pool` 和 `interaction_generation.user_model.model` 的占位模型名替换为可用模型。Assistant 模型池的权重之和必须为 1.0。将密钥设置在 `OPENAI_API_KEY` 环境变量中即可，`api.api_key` 可留空。

```bash
python run.py --num 1 --num-personas 1 --strict
```

全流程默认重新抽样。需要续跑同一批用例时加 `--resume`。只想看 persona 改写和 Assistant 回复时，可以加 `--skip-distractor` 关闭加噪。

## 4. 查看和校验结果

默认输出是 `output/QA_case_improved.csv`。先对照下面几个字段：

| 字段 | 含义 |
| --- | --- |
| `open_question`、`open_answer` | 输入 CSV 的原始问题和参考答案 |
| `improved_query` | 该轮用户消息的干净版本 |
| `noisy_query` | 实际发给 Assistant 的消息。未触发噪声时与干净版相同 |
| `answer` | Assistant 针对该轮消息生成的回复 |

完整对话在 `output/interactions/`，失败用例在 `output/failed_queries.csv`。用 [verify_export.py](../scripts/verify_export.py) 检查 CSV 表头、轮次和字段，并与对话 JSON 交叉核对：

```bash
python scripts/verify_export.py output/QA_case_improved.csv \
  --state output/state/query_assignments.json \
  --interactions-dir output/interactions
```

`run.py --mock` 和校验脚本可以一起用作单条用例的端到端结构测试。更多参数见 [CaseSmith README](../README.md)。
