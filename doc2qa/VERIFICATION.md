# 验证记录

最新验证日期：2026-11-04。环境：macOS arm64，项目 `.venv` Python 3.11.13，DeepEval 4.3.2。

## 本地 HTML 流程与输出观察台（2026-11-04）

- 新增 `tools/output_viewer.html` 页面模板与标准库构建脚本 `scripts/build_output_viewer.py`；根目录 `output_viewer.html` 内嵌生成时最近更新批次的快照，可以直接离线打开。支持具体批次或 output 父目录导入、批次切换、目录刷新和可选每 10 秒刷新。
- 展示当前项目流程、实际批次版本、原始评分维度、问答与引用、完整 Context、初始问题、运行事件、原始文件、分布统计，以及搜索/筛选/排序/JSONL 导出。最终 candidates 优先；未导出批次合并生成与审核检查点，保留未审核记录并标明中间状态。旧批次不改写评分键。
- Node 运行检查使用实际 v16 批次及独立边界数据，验证页面脚本启动、检查点合并、最终结果优先、搜索及状态筛选、初始问题与日志筛选、转义、坏行提示、父目录发现和旧维度展示。Python 检查验证最近批次选择、已知产物白名单、安全内嵌和禁止覆盖原始 output 文件。JS 语法、构建脚本 Ruff/格式、差异空白检查通过；本地预览 HTTP 200。
- 浏览器访问 host 不可用，原生 Chrome 验证接口超时；未完成真实浏览器视觉检查及原生目录选择器操作验证。未调用模型或修改原批次；运行中的业务批次独立进行。

## 初始生成及五维评分 Prompt 精简（2026-11-04）

- 初始 Role 仅保留证据、独立理解、受众及自然表达要求；恢复爱因斯坦诺贝尔奖示例，采用略偏口语的 query。
- 精简五个维度及公共评分规则，移除交通等特定场景和示例中的 Task；所有评分示例仅含 Query 和 score。示例只展示格式，答案完整性、正确性仍依据实际 QA、Context 和已有引用判断。
- Prompt 版本更新为 `evidence_qa_v16_concise_generic`；README 和完整 Prompt 参考同步更新，五次评分、类别映射及汇总阈值保持原逻辑。
- `.venv/bin/python -m pytest -q`：167 项通过；Ruff、相关文件格式及差异空白检查通过。额外核对五份评分 Prompt 与参考文档一致，示例仅含 query/score，未混入交通场景。使用离线模拟响应，未运行真实模型批次。

## 生活化初始问题与本地用户拟人评分（2026-11-04）

- 初始生成 Role 改为从本地用户的日常用途出发，示例采用埃及博物馆下车站、Cairo LRT 月票及 ENR App 付款；题型示例同步调整。自然问法不强制第一人称，也不借虚构个人经历或项目统计题制造生活感。
- 最终评分依次为 question_clarity、self_containment、completeness、answer_correctness、personification。answer_correctness 合并原 groundedness / evidence_coverage，检查答案是否正确回应 query 且有 Context 和已有引用支持；completeness 检查是否答全；personification 检查目标本地用户的合理日常需求与自然表达。
- 审核 user JSON 增加配置语言和当前 Context 匹配的 task_rules，复用来源作用域选择逻辑；文档规则及精确 Context 规则不泄漏到其他来源。规则只用于评价要求，不作为事实证据。
- 保留五次独立请求、1/2/3 映射为 0/0.5/1、每项 20% 和总分阈值 0.8；未增加单项否决。新生成/复验使用新评分键，历史导出和仅重跑去重保留原评分。Prompt 版本更新为 evidence_qa_v15_local_personification。
- `.venv/bin/python -m pytest -q`：167 项通过；Ruff、格式和差异空白检查通过。覆盖新维度请求与导出、规则作用域、拟人评分参与汇总及非法分数处理。使用离线模拟响应，未重跑真实业务批次，未据此宣称模型实际遵循率已改善。

## 题型规则列表格式及五类收敛（2026-11-04）

- `question_type_rules()` 改为 `##` 标题与 `-` 要点列表，五个类别各有独立标题、定义和示例，保留逐条选择与句式要求，不设等量配额。
- 生成规则、QuestionType 枚举、默认 Python/YAML 配置及本地生成 schema 仅保留 factual、numeric_fact、list、conditional、comparison；移除其他三类标签，表格数值归 numeric_fact。参考文档及 schema 快照同步更新。
- 为避免旧批次无法复验/去重，仅在读取历史复验配置时过滤已退役的生成选项和权重，保留原始 QA 与标签；新生成配置仍严格拒绝已移除标签。
- 164 项测试通过，Ruff、格式及差异空白检查通过。校验渲染规则的非空行均为标题或列表项，生成 schema 与默认配置仅含五类。无额外远程模型请求。

## 每个 Context 混合题型及逐条生成标签（2026-11-04）

- 移除按 Context 序号轮转单一题型的 requested_type 逻辑。每个 Context 都参考全部已配置题型，模型按证据返回不等量、可缺省部分类型的有用组合；type_weights 改为软偏好，不作为配额。
- 增加 question_types.py 的八类定义和句式多样性规则，以及 synthesis_schema.py 的必填 qa_type。初始生成返回 input + qa_type，可选演化后表述调整返回最终 qa_type；同步/异步及 docs 转 Context 路径均保留初始和最终标签，映射到 QA、中间文件、检查点和统计。缺失/未知/未配置标签明确失败，不再整批默认 factual。Prompt 版本更新为 v14，题型定义进入 prompt 哈希。
- 标签描述信息需求，支持谁/什么/何时/哪里/为什么/如何/是否及指令式句式；要求证据支持原因、流程和推断。默认采用已有八类：factual、numeric_fact、list、conditional、comparison、multi_hop、table_lookup、source_limitation。
- 159 项测试通过；Ruff、格式与差异空白检查通过。新增覆盖多个 Context 的不等量混合标签、同步/异步、演化关闭/开启后的标签更新、初始/答案中间文件、最终 Judge/导出统计、标签缺失和配置范围、题型定义哈希。
- 真实 BRT Context 小样生成 6 条：numeric_fact=2，其余 factual/list/conditional/source_limitation 各 1，标签随每条问题返回。预览位于 data/output/type_label_preview_20261104；小样仅验证生成，不执行最终评分。已有批次保留原始标签，新逻辑用于新批次。

## Embedding 批量大小兼容（2026-11-04）

- 将 `Remote.embed()` 的单批上限从 64 改为 20，兼容当前服务的最多 20 条限制；75 条输入拆为 20/20/20/15。维持按批内 index 排序、按原始输入顺序拼接和用量累计。
- `test_remote.py` / `test_dedup_cli.py` 共 26 项通过；新增 0/1/20/21/75 条边界、批内乱序返回、全局顺序及 token/请求数统计检查。Ruff、格式及差异空白检查通过。
- 对 `run_20261103T134402Z_9e1da937` 的 75 条已评分候选真实重跑去重，4 次 Embedding 请求成功，后续模型判重完成。结果位于 `data/output/dedup_only_batch20`：accepted=75、pending_review=0、duplicates=0；原始 QA 和评分未重跑。

## 独立重跑去重入口（2026-11-04）

- 新增 `python -m doc2qa dedup --run-dir <原批次> --output <新目录>`，默认优先读取已评分的 `scored_candidates.jsonl`，否则读取 `checkpoints/*.validated.jsonl`，并按原批次候选顺序处理。可用 `--input` 指定已评分文件，`--exact-only` 仅执行本地精确去重。
- 复用全部质量评分，不调用生成或五维评分模型；完整语义去重只调用 Embedding 和 DuplicateResult。当前环境模型名称覆盖批次模型名称，输出保存评分、去重结果、日志、来源信息及调用用量，禁止覆盖源批次目录。
- 139 项测试通过，Ruff、格式和差异空白检查通过。新增回归覆盖仅判重调用、checkpoint 来源及顺序、精确模式无远程调用、显式输入、源目录保护和拒绝去重后错误输入。验证使用离线响应，没有再次调用真实服务。

## 终端原行进度标记与 HTTP 日志降噪（2026-11-03）

- CLI 使用独立 `ProgressConsoleHandler`，交互终端操作开始显示一行，完成后原行追加 ✅，失败及中断追加 ❌；保留耗时与具体错误。并发操作及中途普通日志由 Rich Live 协调显示，无定时重绘；非终端输出保留普通追加行，不输出光标控制码。
- 屏蔽 httpx/httpcore 及 httpx2/httpcore2 的 INFO 请求日志，保留 WARNING/ERROR。`run.log`、`events.jsonl`、`progress.json` 的持久化记录继续保留开始及结束；操作错误文字增加异常类型和消息，文件不含终端控制码。
- 新增终端光标模拟测试，验证开始立即可见、成功/失败/中断在原行更新、并发乱序完成及中途日志、持久化日志完整性、重定向输出和 HTTP 日志过滤。真实 PTY 预览验证 ✅ / ❌ 显示。
- `.venv/bin/python -m pytest -q`：**135 项通过**；Ruff、格式及差异空白检查通过。预览仅使用本地模拟操作，无额外模型请求。

## Judge 结构化输出兼容与真实复验（2026-11-03）

- `Remote.structured()` 在接口明确返回格式不支持（HTTP 400/422）时依次尝试 json_schema → json_object → 普通文本，在实例内按模型记住可用格式。后两种模式附带 JSON schema，并在本地严格校验；其他 API 错误不触发格式切换，拒绝/截断/空内容仍失败。
- Judge 和语义去重异常写入具体日志，保留原有待审原因标记。独立 validate 复验增加阶段日志、事件和当前进度，以及去重前的 `scored_candidates.jsonl`，便于在 Embedding 配置修复后复用已完成评分。
- 128 项测试通过，Ruff 与差异空白检查通过。新增 HTTP mock 覆盖原生结构化输出、逐级回退及跨 schema 缓存、用量、API 错误、非法 JSON/分数、拒绝和截断；覆盖向量服务失败后评分快照及错误日志仍保留。离线测试夹具显式指定 tfidf，避免实际 YAML 改用远程召回后测试意外请求服务。
- 对 `run_20261103T130513Z_dc0ebcf0` 的 75 条候选真实复验，结果位于 `data/output/revalidated_20261103T130513Z_dc0ebcf0`。首个 json_schema 请求收到原始 HTTP 400 后成功切换 json_object，375 个维度评分成功，75 条均达到 0.8，评分快照已保存。原批次不变。
- 后续语义去重因原 Embedding 模型 text-embedding-3-small 不存在（HTTP 404）而将最终记录保留待审。另行查询服务列出的 qwen3.7-text-embedding-flash / qwen3.7-text-embedding，实际调用均返回 HTTP 403 Model access denied；这需要可访问的 Embedding 配置或明确关闭本次语义去重，不能视为已完成最终去重。

## 移除内部质检并要求问题独立可理解（2026-11-03）

- 同步/异步及 docs 转入 Context 的 QA 合成流程改为：初始问题生成 → 可选演化及表述调整 → 答案生成 → 五维 Judge。移除内部问题评分、低分改写 prompt 和调用，删除 `synthesis_quality.py`，不再创建内部 Judge 模型、内部质量元数据、进度计数和评分文件；旧配置的 `input_quality_threshold` 需删除。
- 同步证据作用域由生成入口直接设置；异步问题编号从初始问题列表登记，保留同文本问题的独立编号、Context 隔离及异常后的清理。同步和异步均执行候选数量上限。
- 初始生成规则及示例要求问题脱离 Context 仍能确定实体、来源范围和条件；用明确名称替换 “this bridge”等缺失先行词的指代。允许问题内部已有先行词的代词，不要求答案脱离证据可得，不可编造名称或把答案填入问题。可选演化和表述调整同步采用该约束。
- Judge 的 `self_containment` 字段保留，改评问题本身的独立可理解性，不借助 Context 或答案补齐缺失主体；问题未明确桥名的示例为 1 分，明确 Golden Gate Bridge 的示例为 3 分。五维映射、20% 等权和 0.8 总分阈值不变。
- `.venv/bin/python -m pytest -q`：**110 项通过**。覆盖所有演化方法的同步/异步编排、0/1/2 轮开关、禁止旧评分/改写调用、完整生成到五维 Judge 的导出、并发编号和失败落盘。Ruff 与格式检查通过；使用离线响应，未评估真实模型的遵循率。README 和 prompt 参考文档已同步。

## 最终 QA 五维独立评分与等权汇总（2026-11-03）

- `JUDGE_RULES` 拆为五个独立 system prompt，每次仅评一个维度；`JudgeResult` 只接收 `score` 整数类别 1/2/3，拒绝多余字段、非法类别以及布尔/字符串/浮点分数。
- Python 按 1→0、2→0.5、3→1 映射，五项各占 20%，总分达到 `quality.overall_threshold`（默认 0.8）自动通过；替换原五个维度最低分配置，恰好 0.8 通过。
- 原始类别分保存到 `judge_result`，映射分及 `overall` 保存到 `quality_scores`。保留已有引用、题型和难度，本地来源/表头/数字等检查继续生效，跨文档主来源引用缺失由本地拒绝；来源实质必要性由证据覆盖 prompt 评分。
- 任一次调用失败或非法输出均待审，无部分评分或旧分数残留，继续使用 `judge_error:` 标记支持恢复重试。Prompt 版本和包含五份规则的哈希已更新。
- `.venv/bin/python -m pytest -q`：**109 项通过**；`.venv/bin/ruff check src tests`、本次四个格式化文件的格式检查及 `git diff --check` 均通过。回归使用离线响应，未调用真实模型评估评分效果。

## 最终 QA 审核 Prompt 结构统一（2026-11-03）

- `JUDGE_RULES` 改为 Role、Example、Rules、Output 分区，示例覆盖通过、编造币种而拒绝、忠实报告来源冲突而待审。
- 明确五项分数、逐字引用与表头、跨文档必要性、问题可回答性与答案支持度的区别；保留按给定 Context 解析指代的标准。JudgeResult、调用消息结构、阈值和本地判定代码不变。
- 三个示例均通过 JudgeResult 校验，引用的 chunk_id 存在且 quote 是对应示例证据的精确子串。
- `test_quality.py` 与 `test_context_rules.py` 共 17 项通过；Ruff 检查及 prompts.py 格式检查通过。验证使用离线响应，没有测定真实模型审核效果。

## 问题演化与表述调整绑定（2026-11-03）

- `num_evolutions=0` 同时跳过演化和表述调整，内部质检后直接生成答案；大于 0 时完成对应轮次的演化，再调整一次表述。初始生成及内部质检仍保留任务规则。
- 新增 `synthesis_generation.py`，从固定的 DeepEval 4.3.2 同步/异步 Context 编排方法适配条件，不修改包文件。关闭时不调用演化方法或表述调整模板，进度条也不包含这两步。
- 问题编号初始化移入独立 `_prepare_query` 钩子，关闭演化后，答案事件、同文本问题编号和中间结果仍正确保存。
- 回归覆盖同步/异步 0、1、2 轮配置、关闭时禁止调用演化/表述调整方法、答案使用质检后的原问题、开启后仅调整一次、多个 Context 的证据隔离，以及阶段日志和落盘。
- 项目 `.venv/bin/python -m pytest -q`：89 项通过；Ruff 检查和本次 9 个相关代码/测试文件的格式检查通过。模型请求使用离线响应。

## 表述调整和答案生成接入完整 Context（2026-11-03）

- 两阶段模板统一使用 Role、Example、Rules、Output 分区，显式展示完整 Context 和当前查询。表述调整返回 input JSON，答案生成返回纯文本。
- 表述调整保留原信息需求、来源范围和文档中可解析的指代；答案使用独立 ANSWER_RULES，应用任务中相关的事实、来源及语言约束，不把出题偏好当作再次生成问题的指令。
- 新增 `synthesis_context.py`，在同步生成调用及异步 Context 任务内保留证据和 task，完成或异常后恢复作用域。保留原有 ContextAwareQualityMixin 的显式质检传参。
- 验证同步/异步多个 Context 包含相同查询时，评分、改写、表述调整和答案均收到对应证据；验证配置规则在上述阶段保持来源范围。
- 当时 `num_evolutions=0` 仍执行表述调整；此行为现已被上方“问题演化与表述调整绑定”改造取代。该次测试覆盖了当时 0/1 轮演化、日志、问题编号及答案落盘。
- 项目 `.venv/bin/python -m pytest -q`：87 项通过；Ruff 检查及本次 7 个相关代码/测试文件的格式检查通过。模型请求使用离线响应，不代表真实模型生成质量已测定。

## 内部质检与文档上下文标准统一（2026-11-02）

- 初始通用规则明确允许问题或指令式查询，并要求指代能在提供的文档证据中明确解析。
- 内部评分及低分改写使用相同 GENERAL_RULES，并接收完整 Context 和当前 task（语言、题型偏好及匹配的文档/Context 规则）。评分不再要求脱离文档独立理解，改写保留原信息需求和来源范围。
- 新增 `synthesis_quality.py`，为同步/异步质量循环显式传入 Context，保持原有轮次、提前结束、used_source_files 及末轮改写行为；不修改已安装的 DeepEval 源码。
- 最终审核的 self_containment 统一解释为文档范围内的指代清晰度，阈值不变；移除仅凭“上文/the above text”等措辞就拒绝的本地判断。无证据、无依据数字/单位、引用失效和真实歧义仍受到检查。
- 回归验证覆盖同步/异步各演化路径中的每次评分/改写均携带证据和任务、不同 Context 中相同问题不串用证据、文档规则不泄漏、来源标签保留，以及文档内指代可通过审核而低清晰度仍被拒绝。
- `ruler2`：`python -m pytest -q` 83 项通过；`ruff check src tests` 通过；7 个本次相关代码/测试文件的格式检查通过。
- 更新了 README、配置注释及完整 Prompt 对照文档。本次未调用远程模型；测试验证传参、编排和规则判定，不代表真实模型评分效果已经测定。

## 阶段日志与中间结果落盘（当前版本）

- 默认 Context 生成替换嵌套进度条，输出每条问题的内部评分次数/分数、改写、演化、表述调整、答案生成、候选保存、最终审核、去重和最终统计。
- 运行目录包含追加的 `run.log` / `events.jsonl`、原子更新的 `progress.json`；记录操作开始、完成/失败及耗时，不输出定时等待日志。
- 初始问题、内部质量检查结果和逐条生成的 QA 随完成写入 `intermediate/<context_id>/`，即使后续评分请求失败也保留初始问题。并发生成保留问题编号；同文本问题也能区分。
- 评分达到阈值数、完整候选数、去重前暂通过数及最终通过数分别统计；尚未知的数量保存为 `null`。检查用中间文件与续跑检查点分开，追加历史使用 `attempt_id` 区分。
- 真实 DeepEval 流程替换模型网络请求的回归测试覆盖：实际返回问题数少于上限、低分改写/多次评分、始终低分仍保留候选、并发问题归属、所有阶段内容落盘、后续失败保留初始内容；另覆盖活动操作快照、中断清理、追加续跑及最终计数。
- `ruler2` 环境：`python -m pytest -q` 53 项通过；`ruff check src tests` 和 `ruff format --check src tests` 通过，24 个 Python 文件格式正确。
- 实际 CLI 离线演练目录：`data/output/progress_demo/run_20261031T012708Z_eba35724/`。1 条占位候选，1 条待审，0 条自动通过；可检查 `run.log`、`events.jsonl` 和 `progress.json`。演练不调用模型，不产生真实 QA。
- 本次未调用远程模型；实际服务响应时间与生成质量仍需正式任务验证。

## 未知模型费用 TypeError 修复

- 在 `ruler2` 环境离线复现了 `deepseek-v4.1-flash` 无价格时，DeepEval 执行 `self.synthesis_cost += None` 触发的 `TypeError`。
- 合成器在生成或内部审核模型缺少价格时保持费用累计器为 `None`，包括 DeepEval 每次生成重置费用的情况；批次合成费用保存为 JSON `null`，续跑也不把未知费用记成 0。
- 回归测试保留真实 DeepEval 生成、过滤、演化、答案生成流程，仅替换模型请求；覆盖生成/审核模型分别已知或未知价格的四种组合、重复调用重置及同步辅助方法。
- 异常消息立即写入 manifest，终端保留 traceback；Ctrl+C 保存 `interrupted` 状态，CLI 返回 130，成功检查点保留。
- `/Users/nnxs/opt/anaconda3/envs/ruler2/bin/python -m pytest -q`：49 项测试通过。
- 同一解释器运行 `ruff check src tests` 和 `ruff format --check src tests`：通过，21 个 Python 文件格式正确。
- 本次未调用远程模型，未验证实际 API 返回、正式生成质量或审核通过率。

## 全文 Context 与配置整理验证（2026-10-30）

- `python -m pytest -q`：42 项测试通过。
- `python -m ruff check src tests`：通过。
- `python -m ruff format --check src tests`：21 个 Python 文件格式通过。
- 当前默认配置：`context.strategy=whole_document`，`max_tokens=6000`，每个 Context 最多生成 10 条候选；两个 YAML 的 `entities`、`relations` 均为空。
- `.env` / `.env.example` 统一提供生成、Judge、Embedding 模型名称、API Key、服务地址；已有 `.env` 赋值保留。YAML 不再包含模型名称，旧字段会明确提示迁移。
- 实际预处理：5 份文档、41 个 Chunk、5 个全文 Context、0 个跨文档 Context；产物保存在 `data/prepared_whole_document/`。
- 全文证据不调用召回 Embedding，`index.json` 的 `backend=none`。测试逐字符检查每篇非空白原文均进入 Context，保留表格、限制说明及准确的来源位置。
- 验证了全文超过 Token 上限直接报错，不截断、不跳过文档；全文策略不受分块数量上限影响。
- 验证了 DeepEval 入口收到全部全文片段和多 QA 数量参数；该适配测试替换远程生成方法，没有真实模型调用。
- 完整全文离线演练：5 条占位候选、5 条待审、0 条重复、0 条拒绝、0 条自动通过；支持从批次快照续跑。
- 配置测试包含 `.env` 自动加载、系统环境变量优先、Embedding 模型加载、YAML 旧模型字段迁移、缺少模型的提前报错，以及可选服务地址留空时使用 SDK 默认地址。
- 原有分块、跨文档来源验证、质量门禁、去重和导出测试继续通过；测试夹具显式配置领域实体与关系，不依赖生产 YAML 中的领域词。

全文离线批次：`data/output/run_20261030T120758Z_51cdd49c/`。

默认配置下理论上最多生成约 50 条正式候选，但真实生成数量、不同问题的覆盖及审核通过率尚未通过远程模型验证。离线演练每个 Context 只构造一条占位记录，不受正式生成数量参数影响。

## 先前分块策略验证（历史批次）

- `python -m pytest -q`：29 项测试通过。
- `python -m ruff check src tests`：通过。
- `python -m ruff format --check src tests`：20 个 Python 文件格式通过。
- `python -m pip check`：无依赖冲突。
- 实际输入解析：5 份文档、41 个 Chunk、31 个 Context，其中 5 个跨文档 Context。
- 五文档完整离线演练成功；31 条抽取占位候选，25 条待审、6 条重复、0 条自动通过、0 条拒绝。
- 测试中验证了离线断点续跑、配置变更拒绝续跑、独立复验、JSONL/JSON/CSV/DeepEval Golden 导出，以及真实 DeepEval 4.3.2 对象及两种入口参数契约。

完整离线批次：

`data/output/run_20261030T112939Z_0ebbe263/`

查看 `summary.json`、`review_queue.jsonl`、`duplicates.jsonl` 和 `manifest.json`。这是离线工程演练，正式通过集为空符合质量门禁设计。占位问题可能重复或对应不同抽取文本，相关原因仅用于测试审核队列和去重机制。

## 未完成的外部验证

先前离线验证未配置 `OPENAI_API_KEY`；本次修复验证也未调用远程 DeepEval 合成、LLM Judge 或 OpenAI Embedding，未产生经过模型审查的正式 QA 数据。因此，本验证不能证明模型实际生成质量、真实通过率或真实 API 成本。配置 `.env` 后，按 README 运行小批正式任务并人工抽查，再扩大生成规模。

`docs` 模式的适配契约已测试；完整文档直连执行还需要安装 `.[docs]` 可选依赖与配置模型凭据。默认 `contexts` 模式的本地预处理、CLI、导出及演练链路已经在本环境实际运行。

系统 Python 3.9 的 DeepEval 导入失败已通过切换 Python 3.11 解决。环境版本锁定在 `requirements.lock.txt`；工程声明 `requires-python >= 3.11`。
