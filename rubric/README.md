# QA 语料质检

单轮与多轮 QA 使用独立流程：

| 目录 | 面向对象 | 评分与处理 |
| --- | --- | --- |
| [single_turn](single_turn/README.md) | 单轮 QA 的问句改写（原问题、原答案、候选问题） | 原五维评分、CRITIC 聚合、Excel 输出与断点续跑 |
| [mtqc](mtqc/README.md) | 带历史上下文、Seed QA、因子及 Persona 的多轮 QA | 四维独立评估、统一修复、版本留存、JSONL/Excel/HTML 导出 |

在仓库根目录安装两套流程的依赖：

```bash
python -m pip install -r rubric/requirements.txt
```

单轮 QA 的评分文件、配置、工具和原有测试已集中到 `single_turn/`。复制 `rubric/single_turn/config.example.yaml` 为同目录 `config.yaml`，配置输入、接口及模型后运行：

```bash
python -m rubric.single_turn.evaluate preview
python -m rubric.single_turn.evaluate run
```

多轮 QA 使用独立配置和入口：

```bash
python -m rubric.mtqc.cli preview --config rubric/mtqc/config.example.yaml
# 复制为同目录 config.yaml 并填写接口及模型后运行：
python -m rubric.mtqc.cli run --config rubric/mtqc/config.yaml
python -m rubric.mtqc.cli export --config rubric/mtqc/config.yaml
```

多轮 `run` 每次在 output 下新建时间戳目录保存全部结果，`export` 默认导出最近一次运行。续跑或导出指定运行时添加 `--run-dir rubric/mtqc/output/<时间戳目录>`。

多轮流程的设计依据见 [multi_turn_qa_qc_design.md](multi_turn_qa_qc_design.md)。运行全部离线测试：

```bash
python -m pytest -q rubric
```
