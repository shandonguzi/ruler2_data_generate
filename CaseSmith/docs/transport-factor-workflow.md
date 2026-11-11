# 中文交通因子用例与对话生成

## 准备种子和因子用例

从仓库根目录运行。本机需要安装 Microsoft PICT，且 `pict` 在 PATH 中。

```bash
python3 CaseSmith/scripts/convert_seed_cases.py --language Chinese
python3 CaseSmith/scripts/prepare_factor_cases.py \
  --profile transport \
  --qa CaseSmith/output/atomic_s4_case_transport_20261110_zh_qa.csv \
  --output-dir CaseSmith/output/transport_zh_factor_cases
```

转换脚本读取 [中文交通种子](../input/seed_case/atomic_s4_case_transport_20261110_zh.csv)，输出 `CaseSmith/output/atomic_s4_case_transport_20261110_zh_qa.csv`。截至 2026-11-11，源文件有 86 条种子，转换规则如下。

| 输入字段 | 转换结果 |
| --- | --- |
| `intent_id` | `id`，要求唯一 |
| `text` | `open_question` |
| `open_answer` | 原样保留，缺失时填空 |
| 命令参数 `--dialect` | `dialect`，默认 `zh` |
| 命令参数 `--language` | `language`，本例显式指定为 `Chinese` |

因子脚本读取转换表和 [因子库](../../initial_data/factor_library_test.json)，按批次运行 PICT，并穷举合法组合核对两两覆盖率。

| 批次 | 当前用例数 | 合法取值对覆盖 |
| --- | ---: | ---: |
| 仅通用 | 7 | 7/7 |
| 通用 + 上下文指代理解 | 11 | 93/93 |
| 通用 + 模糊意图识别 | 14 | 76/76 |
| 通用 + 依赖、转折多意图 | 20 | 142/142 |
| 通用 + 上下文修正 | 13 | 93/93 |

当前批次从 86 条种子中按输入顺序选取 65 条，每条种子使用一次。指代批次排除“跨话题（不考虑）”及单轮用例，修正批次也要求有前文。输出目录为 `CaseSmith/output/transport_zh_factor_cases/`。

| 文件 | 内容 |
| --- | --- |
| `QA_case_constrained.csv` | 65 条可供 CaseSmith 读取的带因子用例 |
| `general_combinations.tsv` | 各批次的组合编号、批次和因子组合 |
| `*_factors.pict` | 各批次的 PICT 模型 |
| `coverage_report.md` | 各批次的合法取值对覆盖率 |

用例 ID 采用“种子 ID + 统一组合编号”，例如 `r2016:i0__C036`。组合编号为 `C001–C065`，具体分组和取值保存在 `case_factors` JSON 列中，内部用 `通用因子` 和 `特殊因子` 组织。

## 配置输入和轮数

在 `CaseSmith/config.yaml` 的现有段落中设置下列字段。相对路径以配置文件所在目录为基准。

```yaml
paths:
  input_csv: output/transport_zh_factor_cases/QA_case_constrained.csv

interaction_generation:
  max_turns: 20
```

`dialogue_plan_json.turn_range` 保存 PICT 选择的区间，取值为 `[1,1]`、`[2,5]`、`[6,10]` 或 `[10,20]`。CaseSmith 在运行时结合 `minimum_turns` 和 `max_turns` 随机抽取一次，保存为 assignment 的 `required_turns`。同一批运行的重试和续跑复用该值。无可用轮数时，用例记录失败。

每轮包含一条用户消息和一条 Assistant 回复。旧输入表中的固定 `required_turns` 仍可读取。

## 试跑一条、全量运行与续跑

以下命令都从仓库根目录进入 `CaseSmith/` 后运行。`../.venv/bin/python` 指向仓库虚拟环境，可替换成已安装依赖的 Python。

```bash
cd CaseSmith
../.venv/bin/python run.py --num 1 --num-personas 1 --max-workers 1 \
  --skip-distractor --strict \
  --output-csv output/transport_zh_smoke.csv
```

`--num 1` 随机选择一条用例，轮数按抽中的区间决定。全量生成用下列命令。

```bash
../.venv/bin/python run.py --num 65 --num-personas 65 --max-workers 1 \
  --skip-distractor --strict \
  --output-csv output/transport_zh_dialogues_v3.csv
```

`--num-personas 65` 准备最多 65 个画像，已有画像会复用并按需补齐。每条用例随机选择一个画像，允许重复。`--max-workers 1` 逐条处理用例，单条对话中的各轮顺序生成。`--skip-distractor` 关闭随机加噪，末轮仍会注入用例因子。`--strict` 在存在失败用例时返回退出码 2。

全量运行中断后，使用相同参数加 `--resume`。

```bash
../.venv/bin/python run.py --num 65 --num-personas 65 --max-workers 1 \
  --skip-distractor --strict --resume \
  --output-csv output/transport_zh_dialogues_v3.csv
```

普通全流程命令会启动新一批运行。输入 CSV、样本数或画像池大小改变后，应启动新运行。一次单条试跑的状态只能续跑那一条，扩展到 65 条需要新运行。

进度条显示当前批次的处理数量。配置 `experiment.batch_size: 20` 时，65 条分成 20、20、20、5 四批。成功数以最终 `Interactions complete: N/65` 为准。

## 对话生成与 prompt

带因子用例先提取宽泛的 `topic`。首轮用 persona 和主题生成用户消息，从第二轮开始加入此前的双方历史。末轮再加入 `open_question` 和 `case_factors`。Assistant 每轮接收历史和当前消息，末轮额外接收原始种子问题和 `open_answer` 作为参考，并结合当前约束回答。

| 模板 | 用途 |
| --- | --- |
| [seed_topic_extraction.txt](../prompts/seed_topic_extraction.txt) | 提取宽泛主题，避免提前透露种子题具体查询目标 |
| [user_message.txt](../prompts/user_message.txt) | User 公共模板，包含 persona、主题、语言、历史及简洁和措辞要求 |
| [user_topic_task.txt](../prompts/user_topic_task.txt) | 首轮和中间轮任务 |
| [user_final_task.txt](../prompts/user_final_task.txt) | 末轮种子问题和因子任务 |
| [assistant_response.txt](../prompts/assistant_response.txt) | Assistant 简洁、纯文本回复及参考资料 |

语言优先读取每行的 `language`，留空时映射已识别的 `dialect`，例如 `zh → Chinese`。语言指令会进入双方 prompt。

[采样配置](../input/sampling_config.yaml) 的 `disabled_dimensions` 当前包含 `format_pref_primary`、`deliverable_type`、`code_preference`、`multi_intent_rate`、`feedback_style`、`query_length_pref`、`length_pref`、`learning_style`。新画像采样排除这些维度，复用画像及读取续跑状态时也会过滤对应特征。

普通 QA 输入沿用 persona 改写首问和模拟用户追问的流程。模型、密钥与依赖配置见 [单条用例流程](single-case-workflow.md)。

## 查看输出

| 产物 | 默认位置及字段 |
| --- | --- |
| 逐轮 CSV | 命令的 `--output-csv` 路径，包含 `open_question`、`open_answer`、`case_factors` 和逐轮问答 |
| 完整对话 JSON | `output/interactions/`，元数据包含参考答案和计划用例信息 |
| 训练样本 JSON | `output/training_data/train_samples_*.json`，包含 `original_query`、`open_answer`、`case_factors`、完整对话 |
| Token 统计 | `output/training_data/token_usage_*.json` |
| 断点状态 | `output/state/query_assignments.json` |

CSV 的用例级字段只在该用例的首轮行填写，后续行主要填写 `turn`、`improved_query`、`noisy_query` 和 `answer`。输入参考答案为 `open_answer`，Assistant 实际回复为 `answer`。CSV 字段可能含换行，读取时使用 CSV 解析器。

重复使用同一 `--output-csv` 路径会在导出时覆盖该 CSV。需要保留各次结果时使用不同文件名。完整对话和训练产物文件名带时间戳。

## 当前限制

- 因子批次模型和部分兼容性规则仍有硬编码。新增因子组时需要调整生成脚本，自动扩展尚未完成。
- PICT 报告统计离线计划的两两覆盖。实际对话中的因子达成率、语言和重复问题目前依赖 prompt，自动验收尚未实现。
- `dialogue_plan_json.events` 是离线计划信息，运行时没有按事件驱动中间轮，因子只在末轮 User 生成时注入。
- 主题提取和计划用例的 User 消息调用尚未全部计入 token 统计，统计文件可能低于整批实际调用量。
