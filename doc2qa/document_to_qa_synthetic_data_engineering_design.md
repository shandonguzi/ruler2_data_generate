# 文档驱动的 QA 自动合成工程技术设计

> 版本：v1.0\
> 日期：2026-10-30\
> 技术基线：DeepEval Golden Synthesizer\
> 目标：输入一个或多个文档，自动生成有证据、有来源、可审核、可去重的单轮
> QA 数据集。

------------------------------------------------------------------------

## 1. 背景与目标

本方案建设一个通用的 **Document-to-QA Synthetic Data
Pipeline**。系统接收 PDF、DOCX、TXT、Markdown
等文档，自动完成内容解析、切分、上下文组织、问题生成、答案生成、质量校验、去重和数据集导出。

DeepEval 的 `Synthesizer` 是本工程的 QA
生成核心组件之一，但不把整个工程等同于一次
`generate_goldens_from_docs()` 调用。工程需要在 DeepEval
前后增加文档管理、证据追踪、质量门禁和数据集治理能力。

### 1.1 目标

1.  支持单文档和多文档输入。
2.  支持单文档内 QA，也支持有证据支撑的跨文档、多跳 QA。
3.  生成自然、明确、可独立理解的问题，以及有来源依据的标准答案。
4.  每条 QA 都能追溯到原始文件、章节、页码和证据片段。
5.  自动识别低质量问题、无依据答案、冲突答案和重复问题。
6.  支持按主题、问题类型、难度、语言和来源文件控制生成配比。
7.  输出 JSONL、JSON、CSV 等可用于训练或评测的数据格式。

### 1.2 非目标

-   不把生成的 `expected_output` 当作被测模型的 `actual_output`。
-   不默认把文档之外的常识补充进答案。
-   不将合成 QA
    直接视为人工标注的高可信黄金集；重要数据仍需抽样人工审核。
-   不负责判断文档中陈述的现实世界信息是否仍然有效。系统只保证 QA
    忠实反映输入资料，并保留资料的时间和免责声明。

### 1.3 核心原则

-   **Evidence-first**：先确定证据，再生成问题和答案。
-   **Traceable**：保留文档、Chunk、Context、QA 之间的完整关系。
-   **Separation of concerns**：文档解析、上下文构建、QA
    合成、质量校验、导出独立解耦。
-   **Quality gates**：DeepEval
    的过滤分数不是最终验收标准，仍需工程侧答案校验和去重。
-   **Reproducible**：保存模型、提示词、配置、文档版本和运行批次。

------------------------------------------------------------------------

## 2. 总体架构

``` mermaid
flowchart TD
    A[文档输入：PDF / DOCX / TXT / MD] --> B[文件登记与版本管理]
    B --> C[解析与文本规范化]
    C --> D[结构化切分 Chunk]
    D --> E[Embedding 与索引]
    E --> F[Context Builder]
    F --> G[DeepEval Synthesizer]
    G --> H[QA 结构化与来源回填]
    H --> I[答案证据校验 / 问题质量评估]
    I --> J[重复检测与多样性控制]
    J --> K{质量门禁}
    K -->|通过| L[QA Dataset Store]
    K -->|拒绝 / 待审核| M[Review Queue]
    L --> N[JSONL / JSON / CSV / DeepEval Dataset]
```

### 2.1 模块职责

  ----------------------------------------------------------------------------------------
  模块              主要职责                           输入              输出
  ----------------- ---------------------------------- ----------------- -----------------
  Document Manager  文件登记、哈希、版本、来源元数据   原始文件          Document 元数据

  Parser            PDF/DOCX/TXT/MD                    原始文件          规范化文档
                    解析，保留标题、表格、页码                           

  Chunker           结构化切分、重叠、定位信息维护     规范化文档        Chunk 列表

  Embedder / Index  生成向量、建立相似检索索引         Chunk             向量索引

  Context Builder   选择、组合相关                     Chunk / 索引      Context 列表
                    Chunk，控制跨文档关联                                

  Synthesizer       调用 DeepEval 生成 QA              Context           Golden 列表
  Adapter                                              或文档路径        

  QA Validator      问题、答案、证据一致性校验         QA + Evidence     质量评分和结论

  Deduplicator      精确去重、语义去重                 候选 QA           去重后 QA

  Dataset Manager   数据集版本、审核状态、导出         合格 QA           JSONL / JSON /
                                                                         CSV
  ----------------------------------------------------------------------------------------

------------------------------------------------------------------------

## 3. DeepEval 能力与工程接入方式

DeepEval Golden Synthesizer 提供两种与本项目最相关的入口：

  ------------------------------------------------------------------------------------------------------------------------------------------------
  方法                                 输入                          DeepEval 自动完成的工作    适用情况
  ------------------------------------ ----------------------------- -------------------------- --------------------------------------------------
  `generate_goldens_from_docs()`       `document_paths: list[str]`   文档解析、Chunk、Context   原始文档已经准备好，希望快速启动
                                                                     构建、QA 合成              

  `generate_goldens_from_contexts()`   `contexts: list[list[str]]`   直接基于给定 Context 合成  希望自己控制切分、跨文档组合、表格处理和来源追踪
                                                                     QA                         
  ------------------------------------------------------------------------------------------------------------------------------------------------

官方文档说明，`generate_goldens_from_docs()` 比
`generate_goldens_from_contexts()` 多一个 Context Construction
步骤；前者在内部构造 Context 后，再进入相同的 Golden 生成流程。参见
[DeepEval Golden
Synthesizer](https://deepeval.com/docs/golden-synthesizer)、[Generate
From
Documents](https://deepeval.com/docs/synthesizer-generate-from-docs)、[Generate
From
Contexts](https://deepeval.com/docs/synthesizer-generate-from-contexts)。

### 3.1 `generate_goldens_from_docs()` 的处理流程

DeepEval 文档路径模式的上下文构建大致包括：

1.  **Document Parsing**：使用 token-based splitter 将文档切成
    Chunk，并生成 Embedding。
2.  **Context Selection**：从索引中选择 Chunk，使用 LLM
    按清晰度、深度、结构和相关性进行质量评分。
3.  **Context Grouping**：根据向量余弦相似度，把相关 Chunk 组合成
    Context。
4.  **Golden Generation**：基于 Context 生成问题和可选的
    `expected_output`。

其 Golden 生成流程包括：

1.  Input Generation：生成候选问题。
2.  Filtration：评估问题的自包含性和清晰度。
3.  Evolution：改写问题，提升复杂度和真实感。
4.  Expected Output Generation：生成标准答案。
5.  Styling：按配置调整问题和答案的格式、任务风格。

注意：官方将 Expected Output Generation 描述为发生在最终 Styling
前的步骤；它不是被测应用的真实回答生成。

### 3.2 `generate_goldens_from_docs()` 参数

核心参数：

  -------------------------------------------------------------------------------------------------------------
  参数                            含义                    官方文档中的默认值 / 说明
  ------------------------------- ----------------------- -----------------------------------------------------
  `document_paths`                文档路径列表            必填；支持
                                                          `.txt`、`.docx`、`.pdf`、`.md`、`.markdown`、`.mdx`

  `include_expected_output`       是否生成标准答案        `True`

  `max_goldens_per_context`       每个 Context            `2`
                                  最多生成多少条 Golden   

  `context_construction_config`   Context 构建配置        `ContextConstructionConfig`
  -------------------------------------------------------------------------------------------------------------

`ContextConstructionConfig` 中值得关注的参数：

  参数                             含义                                        官方文档默认值
  -------------------------------- ------------------------------- --------------------------
  `chunk_size`                     Chunk token 数                                        1024
  `chunk_overlap`                  相邻 Chunk 重叠 token 数                                 0
  `min_contexts_per_document`      每篇文档最少 Context 数                                  1
  `max_contexts_per_document`      每篇文档最多 Context 数                                  3
  `min_context_length`             Context 最少包含的 Chunk 数                              1
  `max_context_length`             Context 最多包含的 Chunk 数                              3
  `context_quality_threshold`      Context 质量阈值                                       0.5
  `context_similarity_threshold`   Chunk 组合相似度阈值                                   0.5
  `max_retries`                    Context 选择或组合的重试次数                             3
  `embedder`                       Embedding 模型                    `text-embedding-3-small`
  `allow_cross_file_contexts`      是否允许 Context 跨文件                            `False`
  `max_files_per_context`          一个 Context 最多使用的文件数                            3
  `target_files_per_context`       一个 Context 固定使用的文件数                       `None`

**重要工程注意事项：**

-   `allow_cross_file_contexts` 默认是 `False`。如果希望生成跨文档
    QA，不能忽略这一点；需要显式开启，或使用自定义 Context Builder。
-   语义相似度不等于事实关联。仅凭 Embedding
    相似度可能把同主题但无逻辑关系的段落放在一起，因此生产系统应增加实体、章节、主题和来源约束。
-   文档模式会自动选择和组合
    Chunk，适合快速启动；如果要求稳定覆盖指定主题、表格行、跨文档关系或精确复现，建议使用自定义切分与
    Context Builder，再调用 `generate_goldens_from_contexts()`。
-   官方说明 Context
    构建会从索引中随机选择节点。因此，如果业务要求严格可复现，应在工程侧固定文档版本、Chunk
    ID、Context 清单和生成批次，不要只依赖自动构建结果。
-   `context_quality_threshold` 和 `context_similarity_threshold`
    未达阈值时，DeepEval 会按配置重试；重试后仍可能采用评分最高的候选
    Context。工程侧仍应做最终质量门禁。

### 3.3 `generate_goldens_from_contexts()` 参数

输入结构为：

``` python
contexts = [
    ["同一个主题下的文本片段 A", "相关文本片段 B"],
    ["另一个主题下的文本片段 C"],
]
```

核心参数：

  -----------------------------------------------------------------------
  参数                                含义
  ----------------------------------- -----------------------------------
  `contexts`                          Context 列表；每个 Context
                                      是字符串列表

  `include_expected_output`           是否生成 `expected_output`，默认
                                      `True`

  `max_goldens_per_context`           每个 Context 最多生成多少条
                                      Golden，默认 `2`

  `source_files`                      Context
                                      来源文件标识列表，长度必须与
                                      `contexts` 相同
  -----------------------------------------------------------------------

示例：

``` python
from deepeval.synthesizer import Synthesizer

synthesizer = Synthesizer()

contexts = [
    [
        "Metro Line 3's Phase 4 part 2 ends at Adly Mansour.",
        "The LRT page says Adly Mansour connects LRT, Metro Line 3, railway, SuperJet and BRT."
    ]
]

goldens = synthesizer.generate_goldens_from_contexts(
    contexts=contexts,
    source_files=["cairo_metro_en.txt|cairo_light_electric_train_en.txt"],
    include_expected_output=True,
    max_goldens_per_context=2,
)
```

`source_files`
适合提供来源文件级信息，但不能替代工程自己的证据映射。生产系统仍应维护
`context_id -> chunk_ids -> document_id`
的关系，不能只保存一个拼接后的文件名。

### 3.4 DeepEval 生成质量配置

DeepEval 支持三个主要生成配置：

-   `FiltrationConfig`：过滤候选问题。官方介绍的评分维度是
    Self-containment 和 Clarity。
-   `EvolutionConfig`：控制问题演化类型及演化次数。
-   `StylingConfig`：控制任务、场景、问题格式和答案格式。

对于文档问答，建议优先使用保持 Context 可回答性的演化类型：

-   `Evolution.MULTICONTEXT`
-   `Evolution.CONCRETIZING`
-   `Evolution.CONSTRAINED`
-   `Evolution.COMPARATIVE`

官方特别提示：七类 Evolution 中，并非全部都保证演化后的问题仍然能由原
Context 回答；以上四类被明确列为保持 Context
约束的类型。若目标是生成有证据的 QA，不建议无差别使用所有 Evolution。

另外，DeepEval
的输入过滤在多次重试后仍可能保留评分最高的候选，即使它低于阈值。因此，工程侧需要再次过滤，而不能把
DeepEval 的过滤结果直接当作最终验收结果。

### 3.5 DeepEval 的边界

1.  单轮 Golden 生成 `input`，并可生成 `expected_output`；不会生成
    `actual_output`。
2.  多轮 Conversational Golden 不会自动生成真实对话
    `turns`；若需要对话轨迹，应进一步使用 Conversation Simulator。
3.  `expected_output`
    是合成的标准答案，不是人工真值；必须结合证据做后验校验。
4.  DeepEval
    自带的过滤主要覆盖输入问题质量，不等于完整的答案正确性、证据完整性和重复度评估。
5.  DeepEval
    文档模式支持的文件类型有限；HTML、CSV、JSON、数据库等输入可由工程自定义
    Parser 转成 Context 后走 `generate_goldens_from_contexts()`。

------------------------------------------------------------------------

## 4. 推荐工程流程

建议将整个流程拆成 8 个阶段。

### 阶段 1：文档接入与登记

每个文件接入后生成 `document_id`，保存：

-   原始文件名、文件类型、文件大小。
-   文件 SHA256，用于识别重复文件。
-   文档来源 URL / 系统 / 上传批次。
-   语言、主题、版本、生效日期或编译日期（如有）。
-   解析状态、解析器版本、接入时间。

同一文件内容的重复上传不应重复进入合成流程；同名但内容不同的文件应视为不同版本。

### 阶段 2：文档解析与规范化

按文件类型选择 Parser：

  文件类型     推荐处理
  ------------ ------------------------------------------
  PDF          提取文本、页码、标题；扫描 PDF 另加 OCR
  DOCX         保留标题层级、段落、表格
  TXT / MD     保留段落、标题和列表
  HTML         去除导航和广告等噪声，保留正文和标题
  CSV / XLSX   以表格为单位解析，保留列名、行号和单元格
  JSON         按对象路径和字段结构拆分

规范化原则：

-   清理重复空白、页眉页脚、无意义控制字符。
-   不要把表格直接压平成无列名的文本。
-   保留原文中的单位、币种、日期、否定词、限定词和免责声明。
-   不要自动修正文档内部矛盾；应记录冲突，供 QA
    生成阶段避让或显式生成"文档如何描述"的问题。
-   每段文本都应保留回溯到原始文件的位置。

### 阶段 3：Chunk 切分

推荐优先按文档结构切分，而不是只按固定字符数切分：

1.  先按标题、章节、段落、列表和表格拆分。
2.  超长段落再按 token 长度切分。
3.  对连续文本使用适量 overlap。
4.  表格尽量以完整行或完整表格片段为 Chunk，重复带上列名。
5.  每个 Chunk 记录原文偏移、页码、标题路径和内容哈希。

Chunk 数据结构建议：

``` json
{
  "chunk_id": "chunk_xxx",
  "document_id": "doc_xxx",
  "text": "原始文本片段",
  "section_path": ["Metro", "Tickets"],
  "page_start": 12,
  "page_end": 12,
  "char_start": 1200,
  "char_end": 1850,
  "content_hash": "sha256:...",
  "language": "en"
}
```

初始参数可从 500--800 tokens、50--100 tokens overlap
开始测试。该范围是工程起始建议，不是 DeepEval
的官方最优参数；最终应根据文档长度、表格比例和问题质量评估调整。

### 阶段 4：Context 构建

Context 是 QA 合成的直接证据单元。建议支持三种模式：

  ----------------------------------------------------------------------------------
  模式                    说明                    推荐场景
  ----------------------- ----------------------- ----------------------------------
  Single-chunk            一个 Chunk 构成一个     定义、单一事实、单条规则
                          Context                 

  Same-document           同一文档内多个相关      跨段落事实、表格说明与注释
  multi-chunk             Chunk                   

  Cross-document          多个文档的相关 Chunk    多系统对比、跨文档关系、多跳问题
  multi-chunk             组合                    
  ----------------------------------------------------------------------------------

#### 4.1 Context 构建策略

推荐使用以下组合，而不是单纯依赖向量相似度：

1.  **主题召回**：根据 Embedding 找候选 Chunk。
2.  **实体约束**：检查候选 Chunk
    是否包含相同实体、地点、产品、线路或概念。
3.  **结构约束**：优先组合同一章节、相邻段落、表格与其注释。
4.  **关系约束**：只有存在明确关系时才组合跨文档内容，例如"换乘""包含""对比""前置条件"。
5.  **长度约束**：Context 不宜过长，避免问题生成时忽略局部证据。
6.  **证据可回答性**：组合后的 Context
    应能支持至少一个明确、可验证的问题。

#### 4.2 跨文档 Context

跨文档 QA 不等于把多份文档放在同一个 Prompt 中。必须明确每个 Chunk
的来源，并检查它们之间的关系。

建议每个 Context 包含：

``` json
{
  "context_id": "ctx_xxx",
  "chunk_ids": ["chunk_a", "chunk_b"],
  "document_ids": ["doc_a", "doc_b"],
  "relation_type": "transport_interchange",
  "topic": "Adly Mansour interchange",
  "context_text": "组合后的证据文本"
}
```

当使用 DeepEval 文档模式时，显式设置
`allow_cross_file_contexts=True`。当需要更可控的跨文档逻辑时，自己生成
Context 清单，再调用 `generate_goldens_from_contexts()`。

### 阶段 5：QA 生成

建议将生成拆成两个逻辑任务：

**A. Question Generation**

输入：一个 Context + 生成约束。

输出：一个或多个候选问题。

问题类型建议覆盖：

-   事实查询：是什么、在哪里、何时。
-   数值查询：数量、价格、比例、时长。
-   列表查询：有哪些、包含哪些。
-   条件查询：在什么条件下、满足什么要求。
-   比较查询：A 与 B 的区别。
-   多跳查询：需要组合两个或多个证据片段。
-   表格查询：根据表格中的行列信息回答。

问题必须： - 可以独立理解，不依赖"上文""这段文字"等隐式指代。 -
明确实体和查询范围。 - 不要求输入资料之外的信息。 -
避免把答案直接写进问题。 -
不将资料中的计划、预测、历史状态误写成当前事实。

**B. Answer Generation**

输入：问题 + 对应证据 Context。

输出：标准答案 + 证据引用。

答案必须： - 只使用输入证据。 -
保留单位、币种、日期、适用范围和限制条件。 -
多个来源共同支持时，保留所有来源。 -
资料没有提供答案时，标记为不可回答并丢弃，不允许补充常识猜测。 -
文档存在冲突时，不擅自选择一个版本；应拒绝生成，或明确回答"不同来源分别如何描述"。

### 阶段 6：质量校验

DeepEval 的问题过滤只是第一层。建议额外增加以下质量门禁：

  --------------------------------------------------------------------------------------
  校验维度                检查内容                               建议处理
  ----------------------- -------------------------------------- -----------------------
  Question Clarity        问题是否清晰、无歧义                   低分拒绝

  Self-containment        是否脱离原文仍能理解                   不通过拒绝

  Answerability           证据是否足以回答问题                   不可回答拒绝

  Answer Groundedness     答案中的每个事实是否能被证据支持       无依据事实拒绝

  Evidence Coverage       是否引用了回答所需的全部证据           缺失证据拒绝或重生成

  Completeness            是否遗漏问题要求的关键信息             不完整重生成

  Consistency             数字、日期、实体、单位是否与原文一致   不一致拒绝

  Ambiguity               是否存在多个合理答案或来源冲突         拒绝或标记待审核
  --------------------------------------------------------------------------------------

建议使用两种互补校验：

-   **确定性校验**：数字、日期、单位、币种、实体名、表格值、证据引用 ID
    是否能在原文中定位。
-   **LLM Judge
    校验**：问题是否自然、答案是否完整、证据是否真正支持答案、是否存在隐含推断。

Judge 输入应包含 Question、Answer、Evidence 原文，而不是只看 Question 和
Answer。Judge 输出结构化 JSON，例如：

``` json
{
  "answerable": true,
  "groundedness_score": 0.96,
  "completeness_score": 0.92,
  "unsupported_claims": [],
  "evidence_quotes": ["原文中的支持句"],
  "decision": "pass"
}
```

初始门槛可以设为：问题质量分数不低于 0.70、答案 Groundedness 不低于
0.85、Evidence Coverage 不低于
0.90；任何无法定位的关键数字或实体直接拒绝。这些是项目启动时的建议阈值，不是
DeepEval 官方标准，应通过人工抽检校准。

### 阶段 7：重复检测与多样性控制

重复检测至少分三层：

1.  **精确去重**：问题文本规范化（大小写、空白、标点）后计算 Hash。
2.  **语义候选召回**：对问题计算 Embedding，检索相似问题。
3.  **语义判重**：对高相似候选用 LLM
    判断是否"问法不同但答案和信息需求相同"。

建议初始策略：

-   相同规范化文本：直接去重。
-   Embedding Cosine Similarity ≥ 0.92：进入重复候选集。
-   0.85--0.92：交给 LLM 判重。
-   低于 0.85：暂不按重复处理。

这些相似度仅是初始经验阈值，需要按语种、Embedding
模型和领域数据调参。尤其是数字查询，两个问题可能只差一个线路号或日期，不能因为句子相似就误删。

多样性控制建议：

-   按文档、主题、实体、QA 类型统计数量。
-   为每个 Context 设置生成上限。
-   控制简单事实题、列表题、比较题和多跳题的比例。
-   同一事实可以有不同自然表达，但不要把同一个事实无限改写成近义问题。
-   保留被判为重复的记录及其主记录 ID，方便审计。

### 阶段 8：数据集入库与导出

每条 QA 保存以下信息：

-   `question`：问题。
-   `answer`：标准答案。
-   `evidence`：证据片段及其来源位置。
-   `source_document_ids`：来源文档。
-   `qa_type` / `difficulty` / `language`：数据分类。
-   `quality_scores`：各质量维度分数。
-   `dedup_group_id`：重复簇 ID。
-   `generation_metadata`：模型、Prompt 版本、DeepEval 配置、运行批次。
-   `review_status`：自动通过、人工待审、拒绝。

------------------------------------------------------------------------

## 5. 推荐数据结构

### 5.1 QA Golden Schema

建议以 JSONL 作为主存储格式，每行一条 QA：

``` json
{
  "qa_id": "qa_000001",
  "question": "How many stations are listed for Cairo Metro Line 1?",
  "answer": "The source lists 35 stations: 30 surface stations and 5 underground stations.",
  "language": "en",
  "qa_type": "numeric_fact",
  "difficulty": "easy",
  "source_document_ids": ["cairo_metro_en"],
  "evidence": [
    {
      "chunk_id": "chunk_metro_line_1",
      "document_id": "cairo_metro_en",
      "section_path": ["Metro", "Line 1"],
      "page": null,
      "quote": "The portal lists 35 stations: 30 surface and 5 underground."
    }
  ],
  "quality_scores": {
    "question_clarity": 0.96,
    "self_containment": 1.0,
    "answer_groundedness": 0.98,
    "evidence_coverage": 1.0,
    "completeness": 0.95
  },
  "generation_metadata": {
    "engine": "deepeval",
    "method": "generate_goldens_from_contexts",
    "model": "configured_model_name",
    "prompt_version": "qa_v1",
    "run_id": "run_20261030_001"
  },
  "review_status": "auto_pass"
}
```

### 5.2 关系表

生产系统建议逻辑上维护以下实体关系：

``` text
Document 1 ---- N Chunk
Chunk    N ---- N Context
Context  1 ---- N Golden
Golden   N ---- N Evidence
Golden   N ---- 1 DedupGroup
Run      1 ---- N Golden
```

即使第一版使用 SQLite 或 JSONL，也应保留这些 ID
和关系字段，避免后续无法追踪来源。

------------------------------------------------------------------------

## 6. 工程目录建议

``` text
doc2qa/
├── configs/
│   ├── default.yaml
│   └── transport_qa.yaml
├── data/
│   ├── raw/
│   ├── normalized/
│   ├── chunks/
│   └── output/
├── src/
│   ├── document/
│   │   ├── manager.py
│   │   ├── parsers.py
│   │   └── normalizer.py
│   ├── chunking/
│   │   ├── chunker.py
│   │   └── table_chunker.py
│   ├── context/
│   │   ├── embedder.py
│   │   ├── retriever.py
│   │   └── builder.py
│   ├── synthesis/
│   │   ├── deepeval_adapter.py
│   │   ├── prompts.py
│   │   └── mapper.py
│   ├── quality/
│   │   ├── question_judge.py
│   │   ├── answer_judge.py
│   │   ├── evidence_checker.py
│   │   └── deduplicator.py
│   ├── dataset/
│   │   ├── schema.py
│   │   ├── repository.py
│   │   └── exporter.py
│   └── pipeline.py
├── tests/
│   ├── test_parser.py
│   ├── test_chunker.py
│   ├── test_context_builder.py
│   ├── test_synthesis.py
│   └── test_quality.py
├── requirements.txt
└── README.md
```

### 6.1 依赖建议

DeepEval 文档模式需要额外安装文档处理和向量存储依赖：

``` bash
pip install deepeval
pip install chromadb langchain-core langchain-community langchain-text-splitters
```

建议在项目中锁定经过验证的 DeepEval 和依赖版本。模型、Embedding 和 API
Key 通过环境变量或密钥管理服务配置，不要写入代码仓库。

------------------------------------------------------------------------

## 7. DeepEval Adapter 参考实现

以下代码展示文档直连模式。适合先验证流程，不代表完整生产实现。

``` python
import os
from pathlib import Path

from deepeval.synthesizer import Synthesizer, Evolution
from deepeval.synthesizer.config import (
    ContextConstructionConfig,
    FiltrationConfig,
    EvolutionConfig,
    StylingConfig,
)


def build_synthesizer() -> Synthesizer:
    filtration = FiltrationConfig(
        synthetic_input_quality_threshold=0.65,
        max_quality_retries=3,
    )

    # 只选择文档约束下较容易保持可回答性的演化类型
    evolution = EvolutionConfig(
        evolutions={
            Evolution.MULTICONTEXT: 0.35,
            Evolution.CONCRETIZING: 0.25,
            Evolution.CONSTRAINED: 0.20,
            Evolution.COMPARATIVE: 0.20,
        },
        num_evolutions=1,
    )

    styling = StylingConfig(
        task=(
            "Generate single-turn factual QA test cases grounded only "
            "in the supplied document evidence."
        ),
        scenario=(
            "Users ask natural questions about information contained "
            "in a document collection."
        ),
        input_format=(
            "Write a clear, self-contained natural-language question. "
            "Do not refer to 'the text above'."
        ),
        expected_output_format=(
            "Give a concise factual answer. Preserve units, dates, "
            "scope, and caveats from the source."
        ),
    )

    return Synthesizer(
        model=os.getenv("DEEPEVAL_MODEL", "gpt-5.4"),
        async_mode=True,
        max_concurrent=8,
        filtration_config=filtration,
        evolution_config=evolution,
        styling_config=styling,
        cost_tracking=True,
    )


def generate_from_documents(document_paths: list[str]):
    context_config = ContextConstructionConfig(
        chunk_size=800,
        chunk_overlap=80,
        min_contexts_per_document=1,
        max_contexts_per_document=5,
        min_context_length=1,
        max_context_length=3,
        context_quality_threshold=0.55,
        context_similarity_threshold=0.55,
        max_retries=3,
        allow_cross_file_contexts=True,
        max_files_per_context=3,
    )

    synthesizer = build_synthesizer()

    goldens = synthesizer.generate_goldens_from_docs(
        document_paths=document_paths,
        include_expected_output=True,
        max_goldens_per_context=2,
        context_construction_config=context_config,
    )

    # DeepEval 提供 DataFrame 视图，便于后续映射和质量处理
    df = synthesizer.to_pandas()
    return goldens, df


if __name__ == "__main__":
    paths = [
        "data/raw/cairo_railway_en.txt",
        "data/raw/cairo_monorail_en.txt",
        "data/raw/cairo_metro_en.txt",
        "data/raw/cairo_light_electric_train_en.txt",
        "data/raw/cairo_bus_rapid_transit_en.txt",
    ]

    existing_paths = [p for p in paths if Path(p).exists()]
    if not existing_paths:
        raise RuntimeError("No input documents found")

    goldens, dataframe = generate_from_documents(existing_paths)
    print(dataframe.head())
```

注意：

-   示例中的模型名称、切分长度、质量阈值和并发数是可配置的起始值，不是通用最优值。
-   该示例开启了跨文件 Context；如果不需要跨文档问题，应关闭该选项。
-   DeepEval 当前文档的 `Synthesizer` 默认并发上限为
    100；生产环境建议结合模型服务限流、预算和任务大小设置较低的并发值。
-   如果没有使用 OpenAI API Key，官方文档提示需要同时配置自定义 LLM
    和自定义 Embedding 模型。
-   真实运行时应把原始文件路径替换成文件登记模块返回的路径，并将生成结果交给
    QA Validator，而不是直接导出为最终数据集。

------------------------------------------------------------------------

## 8. 自定义 Context 后调用 `generate_goldens_from_contexts()`

当需要精细控制表格、跨文件关联或数据覆盖时，推荐工程侧自己构建 Context：

``` python
from deepeval.synthesizer import Synthesizer

# 每个内部 list 是一个 Context，字符串是该 Context 中的证据片段
contexts = [
    [
        "Metro Line 3 Phase 4 part 2 ends at Adly Mansour.",
        "The LRT page says Adly Mansour connects LRT, Metro Line 3, railway, SuperJet and BRT."
    ],
    [
        "The BRT source says Al-Talbiya appears twice in its station table.",
        "The source says the displayed entries and its stated 49-station project count do not match."
    ],
]

# 每个 Context 对应一个来源描述；长度必须与 contexts 相同
source_files = [
    "cairo_metro_en.txt|cairo_light_electric_train_en.txt",
    "cairo_bus_rapid_transit_en.txt",
]

synthesizer = build_synthesizer()

goldens = synthesizer.generate_goldens_from_contexts(
    contexts=contexts,
    source_files=source_files,
    include_expected_output=True,
    max_goldens_per_context=2,
)

df = synthesizer.to_pandas()
```

生产实现中，`contexts` 不应由人工硬编码，而应由 `ContextBuilder` 从
Chunk 库中动态构建。示例仅展示 DeepEval 所需的数据形状。

------------------------------------------------------------------------

## 9. 贯穿示例：开罗交通资料

本项目可用以下五份资料验证多文档输入能力：

  -------------------------------------------------------------------------------------------------
  文件                                  内容范围                     可生成的 QA 类型
  ------------------------------------- ---------------------------- ------------------------------
  `cairo_railway_en.txt`                铁路目的地票价、购票渠道     数值查询、渠道查询

  `cairo_monorail_en.txt`               单轨线路、车站、换乘关系     线路信息、站点查询、换乘查询

  `cairo_metro_en.txt`                  地铁线路、车站、票价、订阅   列表、数值、票价、规则查询

  `cairo_light_electric_train_en.txt`   LRT                          线路、票价、状态查询
                                        线路、车站、票价和建设阶段   

  `cairo_bus_rapid_transit_en.txt`      BRT 线路描述、站点表、票价   数值、站点、来源限制查询
  -------------------------------------------------------------------------------------------------

### 9.1 示例 QA

以下是根据输入资料手工整理的示例，用于说明目标格式，不代表 DeepEval
已经实际生成或验证了这些记录。

**示例 A：单文档数值题**

-   Question：How many stations are listed for Cairo Metro Line 1?
-   Answer：The source lists 35 stations: 30 surface stations and 5
    underground stations.
-   Evidence：`cairo_metro_en.txt`，Line 1 段落。

**示例 B：跨文档换乘题**

-   Question：According to the supplied sources, which metro line
    reaches Adly Mansour, and what additional transport connections does
    the LRT page list there?
-   Answer：The Metro Line 3 description lists Adly Mansour as its
    terminus. The LRT page lists connections there to the LRT, Metro
    Line 3, railway, SuperJet and BRT.
-   Evidence：`cairo_metro_en.txt` 的 Line 3 Phase 4 part
    2；`cairo_light_electric_train_en.txt` 的 Adly Mansour 换乘说明。

**示例 C：来源限制题**

-   Question：What caveat does the BRT source give about its displayed
    station table?
-   Answer：Al-Talbiya appears twice, and the number of displayed
    entries does not match the stated 49-station project count. The
    displayed row order should not be treated as a verified route
    sequence.
-   Evidence：`cairo_bus_rapid_transit_en.txt` 的 station table 注释。

**示例 D：避免无依据补全**

-   Question：Does the railway fare table specify the currency?
-   Answer：No. The source explicitly says that the fare table does not
    specify a currency.
-   Evidence：`cairo_railway_en.txt` 的票价表说明。

这些资料同时包含计划、建设中项目、来源表格不一致和"不是实时班次"等限定信息。生成时应保留这类限定，不能将其改写为已经实时运营或经过独立核实的事实。

### 9.2 对该样例的覆盖策略

建议至少覆盖：

-   每份文档内部的直接事实 QA。
-   地铁、LRT、BRT、单轨之间有明确证据的换乘和关联 QA。
-   票价、站数、线路长度等数值 QA。
-   站点列表和线路端点 QA。
-   文档中的限制、数据缺失和口径说明 QA。

跨文档问题应由 Context Builder
显式组合证据，不应要求模型凭常识推断交通网络关系。

------------------------------------------------------------------------

## 10. 数据导出与 DeepEval 评测衔接

### 10.1 导出格式

推荐：

-   **JSONL**：主数据格式，适合批量处理和后续训练。
-   **JSON**：适合完整数据集归档。
-   **CSV**：适合人工查看；Evidence 字段建议序列化为 JSON 字符串。
-   **DeepEval EvaluationDataset**：需要直接进入 DeepEval 评测时使用。

DeepEval 支持 `synthesizer.to_pandas()` 查看生成结果，也支持
`synthesizer.save_as(file_type="json" 或 "csv", directory=...)`
本地保存。其官方示例还展示了将 `synthesizer.synthetic_goldens` 传入
`EvaluationDataset` 的方式。

### 10.2 评测时字段区分

  -----------------------------------------------------------------------
  字段                    含义                    来源
  ----------------------- ----------------------- -----------------------
  `input` / `question`    用户问题                Synthesizer

  `expected_output` /     标准答案                Synthesizer + 后验校验
  `answer`                                        

  `context`               生成时使用的上下文      Context Builder /
                                                  Synthesizer

  `actual_output`         被测应用实际回答        运行待测模型后产生

  `retrieval_context`     被测 RAG                RAG 应用运行时产生
                          系统实际检索到的内容    
  -----------------------------------------------------------------------

不要把 `expected_output` 和 `actual_output` 混用。

------------------------------------------------------------------------

## 11. 运行管理与可观测性

每次生成任务创建一个 `run_id`，至少记录：

-   输入文件 ID、版本和 SHA256。
-   Parser、Chunker、Embedding 模型及版本。
-   Chunk 配置、Context 配置。
-   DeepEval 版本、生成模型、Judge 模型。
-   Prompt 版本、Evolution 分布和次数。
-   输入 Context 数、候选 QA 数、各环节拒绝数。
-   生成 Token、调用次数、耗时和成本。
-   最终通过数、重复数、人工审核数。

建议建立以下监控指标：

  指标                      作用
  ------------------------- ----------------------------------
  Parse Success Rate        判断文档解析是否稳定
  Context Acceptance Rate   判断文档切分和上下文构建是否有效
  QA Pass Rate              判断候选 QA 的整体质量
  Unsupported Answer Rate   监控答案无依据问题
  Duplicate Rate            监控问题重复程度
  Cross-document QA Ratio   监控跨文档问题覆盖
  QA per Document / Topic   监控数据分布是否失衡
  Cost per Accepted QA      评估合成成本

------------------------------------------------------------------------

## 12. API / CLI 设计建议

第一版可以只提供 CLI，后续再封装服务接口。

### CLI

``` bash
# 自动解析文档并生成 QA
python -m doc2qa generate \
  --input ./data/raw \
  --mode docs \
  --output ./data/output/run_001.jsonl

# 使用自定义 Context 构建逻辑
python -m doc2qa generate \
  --input ./data/raw \
  --mode contexts \
  --cross-document true \
  --output ./data/output/run_002.jsonl

# 对已生成的数据做质量校验和去重
python -m doc2qa validate \
  --input ./data/output/run_001.jsonl \
  --output ./data/output/run_001_validated.jsonl
```

### 服务接口（后续）

  API                                      作用
  ---------------------------------------- ------------------
  `POST /documents`                        上传或登记文档
  `POST /jobs/qa-generation`               创建 QA 合成任务
  `GET /jobs/{job_id}`                     查询任务进度
  `GET /datasets/{dataset_id}`             查看数据集信息
  `POST /datasets/{dataset_id}/validate`   触发校验
  `GET /datasets/{dataset_id}/export`      导出数据集

------------------------------------------------------------------------

## 13. 测试策略

上线前至少准备以下测试：

1.  **Parser 测试**：不同格式、乱码、空文件、扫描 PDF、复杂表格。
2.  **Chunk 测试**：标题与正文不分离、表格列名不丢失、页码定位正确。
3.  **Context 测试**：无关文档不被错误组合，跨文档证据来源完整。
4.  **Synthesis 测试**：问题可独立理解，答案不超出证据。
5.  **Quality 测试**：数字、日期、单位、实体和限定词能被检查。
6.  **Dedup 测试**：近义问题能识别；仅实体或数字不同的问题不会误删。
7.  **Regression 测试**：Prompt、模型或 DeepEval
    版本变化后，对固定文档集重新生成并比较质量指标。

建议建立一小批人工审核的固定文档和 QA
样本作为回归集。模型或配置升级时，比较通过率、答案支持率、重复率和数据分布变化。

------------------------------------------------------------------------

## 14. 分阶段落地计划

  ---------------------------------------------------------------------------------------------
  阶段                    建设内容                                 验收标准
  ----------------------- ---------------------------------------- ----------------------------
  P0：PoC                 DeepEval docs 模式，输入多个             能跑通端到端并导出 JSON
                          TXT/PDF，生成 QA                         

  P1：可用版本            文档登记、结构化 Chunk、Context          每条 QA 可追溯到原文
                          Builder、来源映射                        

  P2：质量控制            Question/Answer                          无依据答案和明显重复被拦截
                          Judge、确定性证据校验、去重              

  P3：跨文档能力          实体关系、跨文件 Context、Multi-context  能稳定生成有多来源证据的 QA
                          QA                                       

  P4：生产化              任务管理、成本监控、数据版本、人工审核   可重复运行、可审计、可回滚
  ---------------------------------------------------------------------------------------------

### 推荐优先级

第一版不建议同时建设复杂的知识图谱或完整的多模态解析平台。推荐优先顺序：

1.  先用 `generate_goldens_from_docs()` 验证多文档到 QA 的基本链路。
2.  增加文档 ID、Chunk ID 和 Evidence 回溯。
3.  将 Context 构建逐步迁移到工程侧，使用
    `generate_goldens_from_contexts()`。
4.  增加答案证据校验和语义去重。
5.  最后再做多语言、复杂表格、多跳推理和在线服务化。

------------------------------------------------------------------------

## 15. 关键设计结论

1.  **DeepEval 是合成引擎，不是完整的数据生产平台。** 文档解析、Context
    构建、QA 生成可借助
    DeepEval；来源追踪、答案校验、去重、分布控制和审核应由工程补齐。
2.  **两个入口不是二选一的对立方案。** `docs` 适合快速启动；`contexts`
    适合对输入证据和跨文档关系做精细控制。生产系统可以同时保留两种模式。
3.  **跨文档 QA 必须显式设计。** DeepEval 文档模式默认不允许跨文件
    Context；需要开启 `allow_cross_file_contexts`，或由自定义 Context
    Builder 负责。
4.  **答案质量不能只看问题过滤分数。**
    必须单独校验答案是否被证据支持、是否完整、是否保留原文限定。
5.  **每条 QA 都应带 Evidence。**
    只有问题、答案而没有证据定位的数据，不适合作为高可信 Golden
    Dataset。
6.  **自动生成后仍需抽样人工审核。**
    合成数据用于扩大覆盖和建立初始评测集，不能替代所有人工标注。

------------------------------------------------------------------------

## 16. 官方参考资料

1.  DeepEval --- Golden Synthesizer\
    https://deepeval.com/docs/golden-synthesizer

2.  DeepEval --- Generate Goldens From Documents\
    https://deepeval.com/docs/synthesizer-generate-from-docs

3.  DeepEval --- Generate Goldens From Contexts\
    https://deepeval.com/docs/synthesizer-generate-from-contexts

4.  DeepEval --- Synthetic Data Generation Introduction\
    https://deepeval.com/docs/synthetic-data-generation-introduction

> 说明：DeepEval
> API、模型默认值和配置项可能随版本变化。正式实施时应以项目锁定版本的
> API 文档和集成测试为准。
