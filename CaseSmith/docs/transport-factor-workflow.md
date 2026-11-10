# 中文交通因子用例与对话生成

## 1. 准备带因子的用例

从仓库根目录运行。第二条命令需要本机已安装 Microsoft PICT，且 `pict` 在 PATH 中。

```bash
python3 CaseSmith/scripts/convert_seed_cases.py
python3 CaseSmith/scripts/prepare_factor_cases.py \
  --profile transport \
  --qa CaseSmith/output/atomic_s4_case_transport_20261110_zh_qa.csv \
  --output-dir CaseSmith/output/transport_zh_factor_cases
```

转换脚本读取 [中文交通种子](../input/seed_case/atomic_s4_case_transport_20261110_zh.csv)，输出 `CaseSmith/output/atomic_s4_case_transport_20261110_zh_qa.csv`。因子脚本读取这份转换表和 [因子库](../../initial_data/factor_library_test.json)，按批次运行 PICT。

| 批次 | 当前用例数 |
| --- | ---: |
| 仅通用 | 7 |
| 通用 + 上下文指代理解 | 11 |
| 通用 + 模糊意图识别 | 14 |
| 通用 + 依赖、转折多意图 | 20 |
| 通用 + 上下文修正 | 13 |

本批从 88 条种子中选取 65 条，每条种子使用一次。输出目录为 `CaseSmith/output/transport_zh_factor_cases/`。

| 文件 | 内容 |
| --- | --- |
| `QA_case_constrained.csv` | 带因子的种子用例，可直接作为对话生成输入 |
| `general_combinations.tsv` | 各批次的因子组合 |
| `*_factors.pict` | 各批次的 PICT 模型 |
| `coverage_report.md` | 各批次的合法取值对覆盖率 |

## 2. 生成实际对话

在 `CaseSmith/config.yaml` 的现有段落中修改这两个字段。相对路径以配置文件所在目录为基准。

```yaml
paths:
  input_csv: output/transport_zh_factor_cases/QA_case_constrained.csv

interaction_generation:
  max_turns: 20
```

从仓库根目录运行：

```bash
cd CaseSmith
../.venv/bin/python run.py --num 65 --strict \
  --output-csv output/transport_zh_dialogues.csv
```

`--num 65` 处理当前整批。把它改成 `--num 1` 可以随机试跑一条。CaseSmith 根据每条用例的轮数区间和最低轮数要求，在 `max_turns` 上限内抽取实际轮数，末轮生成用户问题时才注入种子问题和因子。

逐轮 CSV 写入 `CaseSmith/output/transport_zh_dialogues.csv`，完整对话 JSON 写入 `CaseSmith/output/interactions/`。模型配置和密钥准备见 [单条用例生成流程](single-case-workflow.md)。
