# 多轮 QA 质检

这是 `multi_turn_qa_qc_design.md` v1.1 对应的独立实现，单轮 QA 五维评分集中在 [single_turn](../single_turn/README.md)。主流程为「完整四维评估 → 汇总全部反馈 → 单 Prompt 修复 → 完整四维复检」，默认最多三轮修复。

在仓库根目录执行：

```bash
python -m pip install -r rubric/requirements.txt
cp rubric/mtqc/config.example.yaml rubric/mtqc/config.yaml
python -m rubric.mtqc.cli preview --config rubric/mtqc/config.yaml
```

编辑配置里的接口、四个 Judge 模型、修复模型及输入路径，直接在 `llm.api_key` 中填写密钥。程序仅从该配置字段读取凭据。然后执行：

```bash
python -m rubric.mtqc.cli run --config rubric/mtqc/config.yaml
python -m rubric.mtqc.cli export --config rubric/mtqc/config.yaml
```

所有配置中的相对路径以配置文件所在目录为起点。示例配置可直接预览；正式调用前需要替换占位接口和模型。`preview` 不调用模型、不创建输出目录或运行记录；`export` 仅从已有记录重建结果，不调用模型。

输出只需配置一个总目录，不再分别指定各类文件路径：

```yaml
output: output
```

每次执行 `run` 命令，都会在该总目录下新建一个本地时间戳文件夹，例如 `output/20261110_203040_123456/`。程序固定生成 `results.jsonl`、`results.xlsx`、`report.html` 和 `records/`，统一放入该文件夹；即使时间戳碰撞，也会添加数字后缀创建独立目录。终端会打印实际输出目录，已有运行不会被覆盖。

终端对每个 Case 显示两行：Case 编号与进度，以及因子、问答、连贯、画像的评分和最终结果。有变化的评分显示为“初评→最终”，并简要标出修复轮数；错误、跳过和无评分分别显示为“错误”“跳过”和“—”。完整评审理由保留在结果文件和报告中。

`export` 默认选择最近一次包含 manifest 的时间戳目录；使用 `--run-dir` 可选择指定运行，路径相对于执行命令时的工作目录。例如：

```bash
python -m rubric.mtqc.cli export --config rubric/mtqc/config.yaml \
  --run-dir rubric/mtqc/output/20261110_203040_123456
```

## 输入与准入

JSONL 每行一个 Case，完整例子见 [examples/cases.jsonl](examples/cases.jsonl)。必需字段是 `case_id / scenario / seed_qa / factors / persona / dialogue`；`scenario` 包含 s1–s4，Persona 至少有 description，Seed 至少有 question/answer。因子使用 `{value, definition, required?, unit?, scope?}`。缺省 `required` 补为 true，至少有一个必选因子。`case_id` 在批次内必须唯一；重复 ID 的所有行均标为输入错误。

`history` 中每轮是 User–Assistant 交互对，`current` 为最后一轮；编号依次为 `1..N+1`。允许空历史。结构合法但轮数不符由 Factor Judge 评分，程序不作语义评分。非法输入保留源位置、原始内容和错误，不产生质量分或调用模型。

入库必须四维全部完成且全部为 3 分，四个维度的通过门槛均固定为 3。完整评估后，只要任一维度评分低于 3，就会在修复已启用且预算允许时，根据全部反馈修改对话并重新执行四维评分。Final QA=1 为 REJECT，Factor=1 或连贯性、画像低于 3 为 REPAIR；没有已知失败但结果缺失、或仅 Final QA=2 为 REVIEW。REJECT/REPAIR/REVIEW 均可在完整四维反馈下尝试修复。

Judge 只收到设计规定的最小输入。评分与修复响应都经严格 Schema 校验；Factor 额外校验证据轮次及跨字段一致性。Factor 输入会先过滤 required=false 的因子，Judge 检查传入的全部因子，仅通过 failed_factors 返回失败详情，每项恰好包含 `key / expected / reason`；key 不得重复，expected 保留原始值和 JSON 类型，数组为空时为 3 分，否则为 1 分。报告中的逐因子分母来自原始 Case 的必选因子配置，仅统计该维度完成的 Case。Coherence、Final QA 和 Persona 仅输出评分及理由。

## 修复与停止

修复只修改 dialogue，固定 Seed、因子、Persona、场景及 ID 由程序从原始快照合并，响应夹带这些字段会被拒绝。新增/删除历史后由程序重新连续编号；修改说明中的 feedback_refs 必须是可解析的 JSON Pointer，指向当前四维反馈。

每次真正进入修复任务前，终端会简要显示修复轮次和低分维度，例如 `↳ 触发修复 1/3：连贯 2、画像 2（低于 3 分）`；中断后接续原任务时显示“继续修复”。四维全部为 3 分时不触发修复，API 错误或跳过导致评估不完整时也不触发内容修复。

`repair.max_rounds` 是逻辑修复预算；`llm.max_attempts` 是每个 Judge/修复任务的 API 尝试总数，均包含首次。网络、截断或非法响应共享同一任务的技术预算，不增加逻辑修复轮次。blocked、unchanged、无实际改动、重复旧版本或技术错误都会停止修复，并保留具体原因。首次 PASS 立即停止；没有通过时选择最后保存的有效版本，预算耗尽不会放行。

默认每个有效版本执行四个 Judge，即使其他 Judge 失败也继续。`fail_fast=true` 仅能在 `repair.enabled=false` 时启用：按 Factor→Final QA→Coherence→Persona 顺序，合法 1 分后其余记 SKIPPED；技术失败不触发提前停止。

## 调用与恢复

`llm.api_url` 可以填写完整的 Chat Completions 地址，也可以填写以 `/v1` 结尾的 Base URL（如 `https://example.com/compatible-mode/v1`）；后者自动补齐 `/chat/completions`，完整地址不会重复追加。`response_format=auto` 对 deepseek-* 使用 json_object，其他使用严格 json_schema；可按供应商能力显式选择。API 使用 Schema 的结构子集，本地仍校验全部约束。对于不接受 temperature 的模型可配置 null。`request_parameters` 可配置 token 上限等参数；不允许覆盖 messages/model/response_format。并发和请求速率限制由同一运行的客户端统一控制，Case 逐条处理，每个版本的四维可并行。

超时、408/429、可恢复 5xx、截断和结构错误退避重试；鉴权等明确不兼容错误不重试。配置中的 api_key 不进入 manifest 或缓存身份，调用日志中的密钥会脱敏。技术失败的维度为 ERROR/null，保留其他维度的结果，不触发内容修复。CLI 遇到技术失败返回 1，中断返回 130；质量未通过仍正常导出。

中断后使用相同配置并通过 `--run-dir` 指定原输出目录续跑；不带该参数的 `run` 始终创建新运行：

```bash
python -m rubric.mtqc.cli run --config rubric/mtqc/config.yaml \
  --run-dir rubric/mtqc/output/20261110_203040_123456
```

manifest 固定输入 hash、Prompt 正文、Schema、接口、模型、参数、策略和预算；发生变化需重新执行不带 `--run-dir` 的 `run` 创建新运行。API 开始前记录尝试编号，恢复不重置预算；已保存的成功响应直接重放。候选先持久化再复检，中断恢复不会再生成一次候选。目录进程锁防止双写；尾部半条日志会备份并明确告警后截断恢复，中间损坏行直接报错。启用时间戳目录之前的旧记录仍可导出；恢复旧运行时，`--run-dir` 指向原结果文件与 records 所在目录。

记录目录包括 manifest.json、original_cases.jsonl、revisions.jsonl、judge_results.jsonl、repair_attempts.jsonl、attempts.jsonl。最终 results.jsonl 每行一个自包含 CaseResult，包含全部版本完整对话、四维详情、修复记录、实际差异、分数及准入去向。导出使用原子替换；中断期间也尽量导出已有结果。

## Excel 与报告

Excel 可选择一个完整 JSON 列：

```yaml
input:
  format: excel
  path: cases.xlsx
  sheet_name: 输入
  columns: {case: case_json}
```

也可映射六列；case_id 为文本，其余五列为 JSON：

```yaml
columns:
  case_id: 样本ID
  scenario: 场景JSON
  seed_qa: SeedJSON
  factors: 因子JSON
  persona: PersonaJSON
  dialogue: 对话JSON
```

输入公式需有 Excel 保存的缓存值。原表、公式和常规格式保留，在原数据右侧追加结果，并新增 Case汇总、版本明细工作表；同名工作表/列自动加后缀。完整对话和证据以 JSONL 为准，Excel 提供记录定位和摘要。export 从 Excel 原表重建输出时仍需保留源工作簿。

Coherence、Final QA 和 Persona Judge 均仅返回 `score / reason`，具体问题及依据写入 reason。统一结果记录中的 `evidence_turn_ids=[]` 和 `details={}` 由程序补齐，用于保持导出结构一致。

HTML 报告分别展示初评/最终的分数分布、完整/部分评估分母、逐因子失败率、最终问答通过率（Final QA 为 3 分的数量 / 该维评估完成的数量）、各维评分理由、修复收益、停止原因及请求/token/耗时；并可展开查看所有版本和证据。

## 验证

```bash
python -m pytest -q rubric
python -m pyflakes rubric/mtqc rubric/tests
```

离线测试包括 Mock API、本地 HTTP 服务、修复退化、断点恢复、损坏日志恢复和 Excel 保留；不调用外部供应商。真实语义准确性、对话内指令干扰和修复效果仍需按设计文档用人工标注集校准。
