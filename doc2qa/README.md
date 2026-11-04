# DeepEval 文档 QA 自动合成工程

基于 `document_to_qa_synthetic_data_engineering_design.md` 实现，直接使用目录中的 `cairo_transportation/` 五份资料。默认将每篇文档的全部正文组织为一个 Context，再调用 DeepEval `generate_goldens_from_contexts()` 生成多个单轮 QA；也保留片段策略和 `docs` 直连入口。

## 快速开始

使用 **Python 3.11 或以上**。本工程固定 DeepEval **4.3.2**。系统自带的 Python 3.9 无法导入该版本中的部分类型定义，不能作为正式运行环境。

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements.txt
```

本次工作区已经建立 `.venv`，可以直接使用 `.venv/bin/python`。`requirements.lock.txt` 保存实际验证环境的完整依赖版本；要重建同版本环境，先安装此锁定文件，再运行 `python -m pip install --no-deps -e .`。该锁定文件对应 Python 3.11；`docs` 模式的可选依赖另行安装。

先检查原始资料、分块和全文 Context，不需要 API Key：

```bash
python -m doc2qa prepare --config configs/transport_qa.yaml --output data/prepared
```

执行离线工程演练：

```bash
python -m doc2qa generate --config configs/transport_qa.yaml --smoke
```

`--smoke` 从输入证据构造抽取式占位样本，仅验证解析、映射、校验、去重、落盘和导出链路。记录标记 `engine=offline_smoke`、`not_model_generated=true`，去重后保存在待审或重复队列；正式 `dataset.jsonl` 为空是预期行为。**这些样本不是 DeepEval 生成或 LLM 审核过的 QA。**

## HTML 流程与生成效果观察台

直接打开项目根目录的 [output_viewer.html](output_viewer.html)，即可查看当前流程及生成 HTML 时最新批次的离线快照，不需要启动服务或安装前端依赖。

- 点击「选择 output 文件夹」，可以选择一个具体批次，或选择包含多个批次的 `data/output` 父目录。父目录默认展示最近更新的批次，可通过下拉框切换。
- 查看问答、原始维度分数、已有引用和完整 Context；支持按状态、题型、来源筛选，搜索问题/答案，按评分或生成时间排序，导出筛选结果。
- 「分布与批次」「初始问题」「运行记录」「产物文件」分别展示统计、生成前的问题、运行事件和只读原始文件。旧批次保留旧评分维度，并标明与当前 Prompt 版本的区别。
- 尚未导出的运行中批次会合并生成及审核检查点，保留未审核候选；界面会标记为中间快照。目录连接成功后可手动刷新或每 10 秒刷新。浏览器不支持持续目录访问时，使用「兼容导入文件夹」重新选择来更新。

也可从指定 output 目录重新生成一个内嵌数据的 HTML 文件，便于直接打开或离线传阅：

```bash
# 默认选择 data/output 中最近更新的批次，更新根目录 output_viewer.html。
python3 scripts/build_output_viewer.py
# 指定某个批次及 HTML 保存位置；仅使用 Python 3.11+ 标准库。
python3 scripts/build_output_viewer.py \
  --output data/output/run_你的批次目录 \
  --html /tmp/qa_report.html
```

页面模板是 `tools/output_viewer.html`，构建脚本只读取已知产物，不调用模型、不修改批次文件。内嵌数据是生成时的快照；需要最新内容时连接目录刷新，或重新运行构建脚本。

## 配置分工与全文策略

- `.env`：API Key、服务地址、生成模型 `DEEPEVAL_MODEL`、审核模型 `DEEPEVAL_JUDGE_MODEL`、向量模型 `DEEPEVAL_EMBEDDING_MODEL` 和遥测设置。参照带完整注释的 `.env.example`。
- `configs/default.yaml`：按输入输出、证据切分、Context 构建、QA 生成、质量门禁分组，每个参数均有注释。YAML 不再接受模型名称，旧配置会给出对应环境变量的迁移提示。
- `configs/transport_qa.yaml`：继承通用配置，只保留交通资料任务相关设置；`entities`、`relations` 均为空。

从项目根目录运行时自动加载该目录的 `.env`，系统环境变量优先。离线预处理和演练无需模型凭据；正式生成缺少必需的模型名称或 Key 会提前报错。批次 `config.json` / `manifest.json` 保存解析后的模型名称以便复现，不保存 Key。

当前默认设置：

```yaml
context:
  strategy: whole_document
  max_tokens: 6000
  entities: []
  relations: []
synthesis:
  max_goldens_per_context: 10
```

全文策略把每篇文档的全部正文 Chunk 按原文顺序放进一个 Context，并保留细粒度 Chunk ID、表头和来源限制，供本地校验及 Judge 核对已有引用。当前五篇不同资料对应五个 Context，最多生成约 50 条候选 QA；审核、去重后可能减少。每篇 Context 超过 `max_tokens` 时直接报错，提示提高上限或改用片段策略，不会截断文本或悄悄丢掉该文档。上限统计包含证据标签等开销，不等于模型的总上下文窗口，需为提示词和输出留出空间。

全文策略不做跨文档拼接、不构建召回向量，忽略 `max_chunks`、`max_contexts_per_document`、`cross_document` 等片段组合参数。`index.json` 记录 `backend=none`、空向量。正式 QA 语义去重独立运行，仍可能调用 Embedding API。

切回原有片段策略可在 YAML 设置 `context.strategy: chunks`，或使用 `--context-strategy chunks`。该策略用 `context.embedding: tfidf` 做本地召回，也可设为 `openai` 使用 `.env` 中的向量模型。跨文档组合还需开启 `cross_document` 并满足共享实体、关系词、相似度条件；`entities: []` 尝试自动发现英文实体，`relations: []` 不产生规则型跨文档组合。两者为空不影响单文档 QA 生成。

`question_types` 是每个 Context 都会参考的内容题型集合。初始生成逐条返回 `{"input": "问题", "qa_type": "实际题型"}`，按证据选择有用的类型组合，可多可少，不要求等量，也不强凑证据不支持的类型。`type_weights` 是可选的关注偏好，不是数量配额或 Context 轮转分配。`max_goldens_per_context` 仍是候选数量上限。

## 本地 Prompt 管理

QA 合成阶段的完整模板统一放在 [src/doc2qa/synthesis_templates.py](src/doc2qa/synthesis_templates.py)，来源于 DeepEval 4.3.2，可直接在本地调整，无需修改虚拟环境中的包文件。

| 环节 | 本地模板方法 |
| --- | --- |
| 初始问题生成 | `SynthesizerTemplate.generate_synthetic_inputs` |
| 问题演化 | `EvolutionTemplate`，公共规则为 `base_instruction` |
| 表述调整 | `SynthesizerTemplate.rewrite_evolved_input` |
| 答案生成 | `SynthesizerTemplate.generate_synthetic_expected_output` |

当前启用的四种演化对应 `multi_context_evolution`、`concretizing_evolution`、`constrained_evolution`、`comparative_question_evolution`；其余三种也已迁入本地，便于后续启用。演化权重、语言和题型要求仍在 `src/doc2qa/synthesis.py::build_synthesizer()` 中装配；轮数、质量阈值和重试次数由 YAML 配置控制。

通用文档问题生成规则 `GENERAL_RULES`、最终 QA 审核规则 `JUDGE_RULES` 和判重规则 `DEDUP_RULES` 继续在 [src/doc2qa/prompts.py](src/doc2qa/prompts.py) 管理。`GENERAL_RULES` 由本地模板用于初始问题生成和开启后的表述调整；旧名称 `GENERATION_RULES` 保留为兼容别名。初始生成的 `# Rules` 下，`## General_rules` 展示通用约束，`## Task_specific` 展示当前语言、题型偏好和匹配的 Context 特殊规则。工程不再设置 `input_format`、`scenario`，也不再生成 `generation_settings` 区块。跨文件来源要求在有多个来源标签时单独展示为 `# Source requirements`。完整阶段说明见 [docs/qa_prompt_reference.md](docs/qa_prompt_reference.md)。

表述调整和答案生成同样使用 `# Role`、`# Example`、`# Rules`、`# Output` 分区，均接收完整参考 Context。表述调整还接收当前任务规则，保留原始信息需求，结合证据作最少修改，将依赖 Context 的指代替换为证据中的明确实体或来源名称，保留可独立理解的指令式问法，输出 `{"input": "...", "qa_type": "..."}`。当前精简的答案模板使用自身的 `Answer_requirements` 和 `expected_output_format`，输出一句简洁、完整的答案，不只返回数字、日期或词组；不会插入出题用的 `General_rules` 和 `Task_specific`。`prompts.py` 中的 `ANSWER_RULES` 常量仍保留，但当前答案模板不渲染它。

DeepEval 4.3.2 原本不向表述调整模板传入证据，也不向答案模板传入 task；`synthesis_context.py` 在同步调用和异步 Context 任务内保留这些输入，模板在缺省时读取对应作用域，并在完成或异常后清理。不同 Context 即使包含相同问题也不会共用证据。直接调用本地表述调整模板预览时，请显式传入 `context`。

`synthesis.num_evolutions` 为 `0` 时同时跳过问题演化和表述调整：初始生成 → 答案生成 → 五维 Judge 评分。设为 `1～5` 时，完成对应轮次的演化后再执行一次表述调整，然后生成答案并进行五维 Judge 评分。同步、异步入口都由本地 `synthesis_generation.py::EvolutionStylingMixin` 按此条件编排；关闭时既不构造这两阶段的 prompt，也不调用对应模型或记录对应阶段日志。初始生成和可选表述调整的语言、题型及文档规则继续保留。

内部问题评分和低分改写已移除，包括对应 prompt、模型调用、内部质量阈值和中间评分文件。旧配置中的 `synthesis.input_quality_threshold` 需要删除；`max_retries` 控制请求失败重试（docs 模式也用于 Context 构建重试）。`synthesis_context.py` 的同步 Context 传递由生成入口直接完成，异步任务继续各自保存独立证据作用域。

初始生成要求问题脱离 Context 或前文仍能独立理解：例如使用 “When did the Golden Gate Bridge open?”，而不是 “When did this bridge open?”。实体、必要的来源范围、地点和条件必须写清；问题自身已给出先行词的代词可以使用，不要求答案脱离证据仍可获得，也不能为消除指代而编造名称或把答案写入问题。可选演化和表述调整采用同一约束。Judge 保留 `self_containment` 字段名，但按“仅阅读问题本身能否确定请求及指代”评分，不借助 Context 或候选答案补齐缺失指代。本地证据、表头、数字/单位等检查继续执行。

初始生成的 Role 仅保留基于证据、独立可理解、符合受众和自然措辞的核心要求；示例使用爱因斯坦获诺贝尔奖的原因与年份，问法略偏口语。第一人称可选，不编造个人经历。

`JUDGE_RULES` 按 `question_clarity`、`self_containment`、`completeness`、`answer_correctness`、`personification` 拆成五个独立 prompt，每个只评一个维度，沿用 `# Role`、`# Example`、`# Rules`、`# Output` 格式。`answer_correctness` 合并旧 groundedness 和 evidence_coverage，检查给出的答案是否正确回应 query、符合 Context 且被已有引用支持；`completeness` 单独检查请求的各部分是否答全。`personification` 评价问题是否自然、符合目标受众，不预设行业或场景，也不强制第一人称、方言或用途故事。五份评分规则使用通用表述，示例只保留 Query 与评分；完整性和正确性仍根据实际答案、Context 和已有引用判断。审核继续接收当前 Context 匹配的配置规则及输出语言。

每条 QA 分别调用五次，模型每次只返回 `{"score": 1/2/3}`（表示三个整数类别）。Python 将 1/2/3 映射为 0/0.5/1，各占 20%，总分达到 `quality.overall_threshold`（默认 0.8）通过，不设单项最低分，因此 personification 得 1 分仍可能以总分 0.8 通过。原始类别分保存在 `judge_result`，映射后的五项分数及总分 `overall` 保存在 `quality_scores`。模型不返回判定、原因、引用或题型/难度；原有引用和生成元数据保留。调用失败、缺失或非法分数均待审，可在恢复批次时重试；本地来源规则失败仍拒绝。新 Prompt 版本为 `evidence_qa_v16_concise_generic`；新生成/复验使用新维度，历史批次及仅重跑去重时保留原评分键，不自动迁移。

`qa_type` 仅保留五类，标记信息需求，不限制提问句式。`question_type_rules()` 使用 `## Question_types` 总标题，下设 `###` 题型、选择规则及句式要求，内容使用 `-` 要点列表：

| 标签 | 主要内容 |
| --- | --- |
| factual | 普通事实、人物、地点、时间、原文明示的解释或流程 |
| numeric_fact | 数量、金额、比例、时长及有据的简单计算，包括表格数值 |
| list | 多项内容、名称、步骤或类别的列举 |
| conditional | 具有实质适用条件、资格或分支情况的查询 |
| comparison | 两个或多个对象、选项或时期的明确比较 |

每条只选择最贴切的主标签，优先具体类别；例如表格票价数值查询标为 `numeric_fact`，而不是把整篇 BRT 问题统一标为 `list`。谁/什么/何时/哪里、为什么/如何、是否和指令式提问均可使用，因果、流程和推断须有文档依据。可选演化后的表述调整同时返回最终 `qa_type`；初始标签保存在 `generation_metadata.initial_qa_type`，最终标签写入 QA 并用于 `accepted_per_type` 统计。标签缺失、未知或超出配置范围会明确报错。此逻辑用于新生成批次，已有批次的旧标签仍保留。

### 为文档或 Context 配置特殊规则

直接编辑 [configs/transport_qa.yaml](configs/transport_qa.yaml) 顶部的 `synthesis.document_rules`，无需修改 Python。BRT、地铁、LRT、单轨和铁路五份文档均已配置简短英文规则，每篇三条，重点是本地乘客会问的实际出行问题，以及站名、票价、换乘和运营状态的事实边界。BRT 示例：

```yaml
synthesis:
  document_rules:
    cairo_bus_rapid_transit_en.txt:
      - Ask everyday questions a local passenger might ask about taking Cairo BRT, focusing on fares, station access and transfers rather than project statistics.
      - Use only documented station names, fare bands and connections; do not infer stop order or point-to-point fares.
      - Distinguish reported service hours and frequencies from verified current operations, and keep planned facilities clearly labelled.
    # 其他文档文件名.txt:
    #   - 在这里填写该文档的要求。
  context_rules: {}
```

文件名填写完整名称和扩展名，区分大小写，不写目录；同名文件共用这组规则。每个 `-` 后写一条规则，中英文均可；输出语言仍由 `language` 控制。同一文档拆成多个 Context 时，这些 Context 都会收到文档规则；跨文档 Context 会分别收到各来源的规则，并标明文件名和 `document_id`。未匹配文档专属规则时，仍保留通用规则及语言、题型要求。某个文件填 `[]` 可停用其规则，继承配置时也使用 `[]` 停用对应条目。

若要进一步指定某个 Context，可先运行 `prepare`，从 `contexts.jsonl` 复制实际 `context_id`，再填写：

```yaml
synthesis:
  context_rules:
    ctx_这里替换成实际ID:
      - 本 Context 优先生成当前证据支持的票价问题。
```

文档规则和 Context 规则依次追加，不相互覆盖；重建证据后 Context ID 可能变化，需重新核对。当前语言和题型要求、匹配到的配置规则共同通过 `StylingConfig.task` 放入初始生成的 `## Task_specific`，也传入开启后的表述调整。没有匹配的专属规则时，该区块保留语言和题型要求。问题演化仍使用自己的模板。配置规则属于生成要求，不作为证据正文传入。

此配置用于 `mode: contexts`，同时支持 `whole_document` 和 `chunks` 策略。DeepEval 自行构建 Context 的 `docs` 模式暂不支持这些配置，存在非空规则时会提前报错；使用该模式需将对应规则列表设为 `[]` 或选择未配置规则的任务。

`config.json`、`manifest.json` 会保存规则；`events.jsonl` 中每个 Context 的开始事件还会记录实际选中的 `context_rules`，便于核对匹配结果。离线 `--smoke` 记录选择结果，但使用抽取式占位样本。

修改模板时保留方法签名、插值变量，以及各阶段约定的输出字段：初始生成的 `data[].input` / `qa_type`、表述调整的 `input` / `qa_type`、最终 Judge 的 `score`。跨文件生成时保留 `used_source_files`；演化和答案生成返回文本。Python f-string 中的示例 JSON 大括号需写成 `{{` 和 `}}`。

重新启动生成命令即可使用修改后的模板。`build_synthesizer()` 会通过 `install_local_templates()` 将 DeepEval 的模板查找和演化映射注册为本地实现，覆盖同步、异步及 `docs` 转入 Context 后的 QA 合成路径。注册作用于当前 Python 进程内的 Synthesizer；适配版本固定为 4.3.2。

批次 `manifest.json` 的 `prompt_sha256` 包含本地模板、生成/审核/判重规则及 YAML 中的文档/Context 规则；模板源文件也纳入代码指纹。要使用修改后的规则或模板，请重新运行生成命令创建新批次。不带 `--config` 的 `--resume` 沿用批次快照中的规则，不会重读原 YAML；显式传入修改后的配置续跑会被指纹检查拒绝。无需手动提升 `PROMPT_VERSION` 才能检测变更；该常量可用于记录业务版本。

## 真正调用 DeepEval

```bash
# 首次使用且尚无 .env 时复制模板；已有 .env 则直接编辑，保留现有凭据。
cp -n .env.example .env
# 在 .env 中填写 OPENAI_API_KEY，可配置模型及 OPENAI_BASE_URL。
python -m doc2qa generate --config configs/transport_qa.yaml --limit-contexts 3
# 小批量检查质量后，去掉上限执行整个 Context 清单：
python -m doc2qa generate --config configs/transport_qa.yaml
```

`.env.example` 为生成和审核预填 `gpt-4.1-mini`，向量模型预填 `text-embedding-3-small`；实际可用模型取决于账户、服务商和 DeepEval 对模型标识的支持。兼容端点需支持 Chat Completions；启用远程向量功能时还需支持 Embedding API。Judge 和模型判重优先使用 `json_schema`，接口明确不支持该格式时自动依次尝试 `json_object` 和普通文本，在当前 Remote 实例中记住每个模型可用的格式。后两种模式通过 prompt 附带 JSON schema，返回结果仍由本地 Pydantic 严格校验；鉴权、限流和其他参数错误不会触发格式切换。`OPENAI_BASE_URL` 留空时使用 SDK 默认地址。生成、Judge、Embedding 当前共用 API Key 与服务地址。

`--limit-contexts 3` 只生成 Context 清单的前 3 项，不是每篇 3 项，也不是只生成 3 条 QA。默认全文策略下对应前 3 篇资料；片段策略下可能都来自同一篇。程序仍先完成全部输入的预处理，因此该参数不会限制片段策略预处理阶段的 Embedding 调用。离线演练每个选中的 Context 仅构造一条占位候选。

断点续跑使用命令输出中的批次目录：

```bash
python -m doc2qa generate --resume data/output/<run_id>
```

CLI 自动读取批次配置、已解析的模型名称与 smoke/limit 设置；无 `--config` 的续跑沿用快照模型，当前环境继续提供凭据和服务地址。输入内容、路径、配置、代码、提示词或依赖改变后拒绝复用原批次。Context 生成与 QA Judge 的成功结果分别检查点保存；生成失败或 Judge 网络错误可以续跑。LLM 生成具有随机性，固定 seed 不承诺逐字复现；文档快照、Context 清单和检查点可以精确回放。并发运行同一 `run_id` 不受支持，请为并发任务创建独立批次。

生成失败时，终端输出完整 traceback，`manifest.json` 立即保存异常类型及消息。Ctrl+C 会将批次标记为 `interrupted`，保留成功检查点，并以退出码 130 结束。修复代码后应重新运行原始生成命令创建新批次；原批次的代码指纹已不同，不能直接 `--resume`。

### 实时阶段与中间输出

默认 `contexts` 生成使用阶段日志替代嵌套百分比进度条。终端依次显示初始问题的实际返回/使用数量、可选的问题演化和表述调整、答案生成、候选保存、最终审核、去重及最终统计。交互终端中，每个操作开始时显示一行，完成后在原行追加 ✅，异常或中断追加 ❌，并保留耗时；并发操作按各自编号独立更新。HTTP 客户端的请求 INFO 日志不再输出，警告和错误保留。输出被重定向到非终端时使用普通追加行，不写入光标控制码。并发任务带 Context ID 和问题编号，例如：

```text
INFO [ctx_...] 初始问题生成完成：返回 15 条，本次使用 15 条；最终合格数量待审核
INFO [ctx_...] 生成答案 2/15：开始 ✅ (8.2s)
INFO [ctx_...] 最终质量审核 70/75：开始 ✅ (0.1s)
INFO [ctx_...] Context 1/5：已保存 15 条完整候选 QA，最终审核待进行
```

运行开始就会输出批次绝对路径。持久化日志和事件仍分别记录每个操作的开始、完成/失败及耗时，不包含终端光标控制码，异常同时记录类型和消息。等待期间不输出定时提示；可从 `progress.json` 的 `active_operations` 查看当前正在执行的操作及开始时间。最终审核通过数在去重前标为“暂通过”。

所有新批次自动保存：

| 文件 | 用途 |
| --- | --- |
| `run.log` | 带时间的可读阶段日志和异常 traceback；续跑追加 |
| `events.jsonl` | 每个阶段的结构化事件，包含时间、attempt ID、Context/问题编号、耗时、模型返回的解析结果、演化/表述调整及答案、最终 Judge 分数；逐条写入 |
| `progress.json` | 原子更新的当前状态，含所有正在进行的操作及各 Context 的生成、审核和最终通过计数；未知数量为 `null` |
| `intermediate/<context_id>/initial_questions.jsonl` | 已返回的初始问题，每条标记是否被本次合成采用 |
| `intermediate/<context_id>/generated_qa.jsonl` | 答案生成后立即保存的问题及答案，标记 `review_status=not_validated` |

中间文件在相应输出完成时创建，手动中断后仍可查看已记录内容；失败/中断前尚未返回的请求没有可保存的输出。中间文件用于检查，不是合格数据，也不作为续跑检查点。续跑保留事件和中间文件历史，以新的 `attempt_id` 区分本次尝试；`progress.json` 展示本次状态，复用的检查点会明确标记。记录模型输出内容，不保存 API Key 或完整请求提示词。

```bash
tail -f data/output/<run_id>/run.log
python -m json.tool data/output/<run_id>/progress.json
```

指定输出文件时也会保存完整的独立批次目录：

```bash
python -m doc2qa generate --config configs/transport_qa.yaml --output data/output/transport.jsonl
```

### DeepEval docs 直连模式

```bash
python -m pip install -e '.[docs]'
python -m doc2qa generate --config configs/transport_qa.yaml --mode docs --context-strategy chunks
```

直连模式由 DeepEval 执行解析、分块及向量召回，不支持本工程的全文策略，需显式选择 `chunks`。为适配本目录短文件，默认每篇取 1 个 Context，不承诺覆盖全文。工程将 Golden 的 Context 对齐回原文，仅接受唯一的精确或空白差异匹配；无法映射的结果保存到 `unaligned_golden_*.json` 待审，并将批次标记为 `partial`。直连模式不提供与自建 Context 相同的实体关系筛选保证，推荐用默认 `contexts` 模式进行可控生产。`--smoke` 和 `--limit-contexts` 只适用于 `contexts`。

## 复验和导出

```bash
python -m doc2qa validate \
  --input data/output/<run_id>/candidates.jsonl \
  --output data/output/revalidated

# 只进行确定性校验；通过规则的记录仍然待审：
python -m doc2qa validate \
  --input data/output/<run_id>/candidates.jsonl \
  --output data/output/revalidated_offline --offline
```

默认在输入 JSONL 同目录读取 `documents.jsonl`、`chunks.jsonl`、`contexts.jsonl`；移动了候选文件时用 `--run-dir` 指定原始批次快照。复验默认复用批次质量配置，同时用当前环境中的模型名称覆盖快照名称，也可用 `--config` 明确指定新的质量阈值。复验不会仅凭导入记录自带的分数直接通过。复验输出目录同时保存 `run.log`、`events.jsonl`、`progress.json`，逐条记录审核状态、耗时及分数；`scored_candidates.jsonl` 单独保存去重前的完整评分，向量服务失败也不会丢失。接口格式切换和请求失败的具体错误写入日志。建议使用独立输出目录保留原批次。

只重跑去重，复用已完成的五维评分：

```bash
python -m doc2qa dedup \
  --run-dir data/output/run_20261103T134402Z_9e1da937 \
  --output data/output/dedup_only

# 仅精确去重，不调用 Embedding 或模型：
python -m doc2qa dedup \
  --run-dir data/output/run_20261103T134402Z_9e1da937 \
  --output data/output/dedup_exact_only --exact-only
```

`dedup` 默认优先读取批次内的 `scored_candidates.jsonl`；不存在时读取 `checkpoints/*.validated.jsonl`，按原批次候选顺序处理。也可通过 `--input <已评分JSONL>` 显式选择文件。它不生成问答、不调用五维评分，只执行精确去重，以及开启后的向量召回和模型判重；模型名称采用当前 `.env`，去重阈值默认沿用批次配置，可用 `--config` 覆盖。请使用去重前快照，去重后已带有重复标记或语义去重错误的输入会明确报错。输出目录须与源批次不同，保存去重结果、评分快照、日志及 `deduplication_manifest.json`。完整语义去重需要有权限的 Embedding 服务。Embedding 请求每批最多 20 条，75 条问题会拆为 20/20/20/15；程序按输入顺序合并向量并累计用量。

每批主要产物：

| 文件 | 内容 |
| --- | --- |
| `documents.jsonl` | 文件 SHA256、别名、来源 URL、编译日期及规范化文本快照 |
| `chunks.jsonl` | 稳定 ID、标题路径、字符定位、PDF 页码、表格列名 |
| `contexts.jsonl` | 主证据、限定说明证据、关联类型和共享实体 |
| `index.json` | 片段策略的 TF-IDF / OpenAI 向量；全文策略为 `backend=none`、空向量 |
| `candidates.jsonl` | 全部候选，保留状态及原因 |
| `dataset.jsonl` / `.json` / `.csv` | 仅自动通过的数据 |
| `review_queue.jsonl` | 未完成 Judge、歧义、冲突或 Judge 调用异常的记录 |
| `rejected.jsonl` | 无依据数字、单位、引用失效或质量分数不足等记录 |
| `duplicates.jsonl` | 重复记录、主记录 ID 和重复簇 ID |
| `deepeval_goldens.json` | 可构造 `EvaluationDataset` 的本地 Golden 记录 |
| `manifest.json` / `summary.json` | 输入版本、配置、依赖、提示词哈希、统计、用量、费用与错误 |

字符偏移指向 `documents.jsonl` 中的规范化文本，而不是原始 PDF/DOCX 的字节。TXT 保留行列与内容，换行统一为 LF，去除 BOM/NUL；PDF 保存页码，扫描页明确报错要求 OCR；DOCX 按正文顺序保存段落和表格，不能可靠提供页码。表格分段保留整行并重复携带列名，不声称支持扫描 OCR 或复杂合并单元格还原。

加载 DeepEval 数据集：

```python
from pathlib import Path
from doc2qa.storage import evaluation_dataset

dataset = evaluation_dataset(Path("data/output/<run_id>/deepeval_goldens.json"))
# dataset.goldens 包含 input / expected_output / context。
# 评测时需另行运行被测应用获取 actual_output 和真实 retrieval_context。
```

## 质量门禁

1. **确定性来源检查**：引用 ID、所属文档、字符范围、原文逐字匹配；答案中的数字、显式币种和常见单位需出现在证据中。
2. **独立 LLM Judge**：清晰度、问题脱离上下文后的独立可理解性、答案依据、证据覆盖、完整性分别请求评分，每次只返回一个 1/2/3 类别分。程序映射为 0/0.5/1，五项各占 20%，加权总分 ≥ 0.8 自动通过。已有引用由本地代码核对原文；来源冲突和限制由对应维度评分，不再返回独立判定字段。
3. **多来源核实**：跨文档 QA 必须引用全部主证据文档，且 Judge 确认回答确实需要多个来源；仅附带通用免责声明不足以满足要求。
4. **精确与语义去重**：规范化问题精确判重；相同问题答案冲突时送审。语义向量只召回候选，由 LLM 判定事实需求是否等价；线路数字和已配置实体不同的题不会合并。语义检查失败时相关记录退回待审。
5. **失败关闭**：没有 Judge 分数不会伪造高分；异常、冲突、无效引用不会直接自动通过。

确定性规则无法单独证明语义正确性；LLM Judge 也可能误判。特别是日期、实体归属、单位与数值的绑定、计划状态和隐含冲突需要 Judge 与人工抽查。资料中的现实状态不进行联网事实更新，保留原文口径。DeepEval 返回的合成费用属于估算；Judge/Embedding 记录 Token 用量，费用为 `null`，不猜测服务商报价，也不宣称记录了 DeepEval 内部所有请求的精确 Token 数。

DeepEval 4.3.2 对没有价格数据的 OpenAI 兼容模型返回费用 `None`，原生合成器累计时会触发 `TypeError`。工程已对此兼容：生成模型或内部审核模型缺少价格时禁用合成费用累计，`deepeval_cost_usd` 保存为 `null`，生成继续进行。关闭费用显示本身不能解决该异常。

`question_types` 在每个 Context 中共同作为可选类型，`type_weights` 引导偏好；每条 QA 的类型由生成及可选表述调整返回，Judge 只负责评分，`accepted_per_type` 统计最终逐条标签。每文档 Context 上限和跨文档上限控制覆盖，`summary.json` 展示最终分布。首版不包含在线服务、人工审核 UI、任务队列或知识图谱。

## 开发和验证

```bash
python -m pytest -q
python -m ruff check src tests
python -m ruff format --check src tests
```

测试包含真实五文档回归、重复文件/版本、空文件/乱码、DOCX 表格顺序、扫描 PDF 拒绝、中文切分、共享实体与关系约束、原文偏移、数字/币种幻觉、无效引用、Judge 阈值、跨文档必要性、精确及语义去重、导出、断点续跑和输入变更保护。DeepEval 适配测试使用锁定的真实类及接口、替换网络生成方法；不代表已成功调用远程模型。本次运行结果见 `VERIFICATION.md`。

代码位于 `src/doc2qa/`：`document.py` / `chunking.py` / `context.py` 负责证据，`synthesis.py` 负责 DeepEval，`quality.py` 负责门禁，`pipeline.py` 负责批次与检查点，`storage.py` 负责导出，`cli.py` 提供命令。

接口参考：[DeepEval Contexts](https://deepeval.com/docs/synthesizer-generate-from-contexts)、[DeepEval Documents](https://deepeval.com/docs/synthesizer-generate-from-docs)。实现同时核对了本地安装的 4.3.2 源码。
