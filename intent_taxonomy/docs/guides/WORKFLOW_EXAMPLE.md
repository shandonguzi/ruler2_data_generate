# Sample

下面以英文知识问答为例，说明主流程的「原子意图分析」与迭代中的「语义分类与任务匹配」各自做什么，以及第二轮为什么需要再次分类。

这是教学用的模拟过程，不是模型实测结果。簇编号、分类名称和体系版本仅用于说明；实际聚类受整批语料、向量和参数影响。例子假设材料类问句有至少 5 条有效支持，并且新增候选通过审核；不表示一条未匹配样本就会自动新增类别。

新增的完整 JSON 均区分模型消息与本地函数参数/返回值。为完整列出数组且避免用省略号代替真实字段，第 1、4、5、6、11、12 章使用独立小规模样本；它们不替换主线 20 条原句的模拟账单。模型字段值与向量值是教学模拟，本地校验及导出结果由现有函数生成。

文中各阶段提供默认 Prompt 的文本文件链接与稳定标识。默认正文位于 prompts/defaults/，页面保存的自定义正文位于 config/prompts/；模板目录见 [catalog.yaml](../../prompts/catalog.yaml)，完整用法见 [Prompt 文件管理](PROMPT_MANAGEMENT.md)。实际请求按标识读取正文，不再从 Python 常量内维护指令。

模型输出 JSON 的字段顺序统一要求：先理由、解释和证据，再置信度及其他信息，最后分类或操作选项。此约束作用于模型生成，不要求本地序列化保持同样顺序。

完整请求由两部分组成：**系统 Prompt + 程序生成的本次输入 JSON**。同一阶段通常每轮复用同一模板，变化的是原子文本、候选叶子、子主题摘要和判断证据等输入，而不是每轮生成一个新模板。下面的链接指向默认模板文件；如改过模板，以页面当前保存值为准。

当前主流程仅维护一套 BERTopic 层级树：叶子审核、局部细分后重建树，逐层命名和审核，再围绕叶子归属迭代。旧的独立领域归纳与任务表维护链路不再用于本流程。

下面的公式是**无失败重试、无格式修复重试、无 JSON 模式降级、未复用缓存时的逻辑调用次数**，用于理解流程，不是固定账单。页面统计的是已经返回结果或错误的实际接口调用，失败请求和重试也计数；正在等待响应的调用尚未计入。

- 同时并发 4 次，仍然计 4 次请求，不会因为并发就只算 1 次。
- 一批输入对应一次模型请求，不等于每个输入都单独请求；不同阶段的批处理方式不同。
- Embedding 是独立模型接口，不计入各阶段注明的对话 LLM 次数。任务统计会同时按 LLM / Embedding 分组展示。
- 中文/英文分词、停用词、UMAP、HDBSCAN、c-TF-IDF、原生层级重建、文件保存和覆盖率统计都是本地计算，不请求 LLM。

## 1. 输入：先有原句，没有预制分类体系

假设导入的表格包含：

| 原句编号 | 原始句子 |
|---|---|
| r1 | Name the traditional clothing in eastern Algeria and explain what it is made of. |
| r2 | What do women traditionally wear in eastern Algeria? |
| r3 | What is Bazeen in Libyan cuisine? |
| r4 | What fabric is traditional clothing in eastern Algeria made from? |
| … | 更多服饰、材料、饮食、历史等问句，其中包含足够的材料类表达 |

重点跟踪 r1。它包含两个请求：询问服饰名称，以及询问该服饰的制作材料。

这些都是有效的知识问答意图，不要求真的调用工具或执行某项操作。

为了在各阶段算清整批请求，本例进一步假设导入 20 条非空原句，提取 25 条明确、不同文本的原子意图；不发生额外提取复核、缓存命中、失败重试或格式降级。后面每个阶段的具体输出仍是模拟条件，不是仅凭“20 条语料”就能预测模型结果。


### 1.1 文件读取的输入与输出

这是独立的单行文件读取示例，不是前文 20 行数据的完整展开。输入 JSON 表示 Excel 中名为 text 的列；输出是 load_excel 返回的 DataFrame 用 to_dict(orient="records") 序列化后的完整结果。这里没有模型请求。

**完整输入 JSON（模拟值）：**

```json
{
  "text": [
    "Name the traditional clothing in eastern Algeria and explain what it is made of."
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
[
  {
    "text": "Name the traditional clothing in eastern Algeria and explain what it is made of."
  }
]
```


## 2. 总览：每一步到底改了什么

本章是流程索引，不执行独立转换，也没有额外模型输入输出。完整数据示例按各阶段放在本章之后；第 7 章说明逐层命名，第 8～11 章说明叶子匹配与迭代，第 12 章给出最终原句多层主题格式。

| 阶段 | 输入 | 本例发生的变化 | 还不会发生什么 |
|---|---|---|---|
| 原子意图分析 | 原始句子 r1 | 拆成 I0、I1；补全 I1 的 it | 不匹配任务类别，不读预制 taxonomy |
| Embedding | I0、I1 的独立文本 | 每条不同文本获得语义向量 | 不生成领域或类别名称 |
| BERTopic 聚类 | 原子文本及向量 | 为意图分配叶子簇或噪声，并生成原生合并树 | 不保证簇内只有一种动作 |
| 簇一致性审核与局部细分 | 簇成员真实样本 | 混合簇拆分，子簇复核，标记支持量 | 不重新拆分原句 |
| 跨簇边界复核 | 相近簇对的样本 | 给出合并、独立或待确认建议 | 当前不自动应用跨簇合并 |
| 逐层命名与合并审核 | BERTopic 树与合格叶子摘要 | 同层并行命名，否定不合理父级，得到审核后的树 | 不独立生成领域树 |
| 第一轮语义分类与任务匹配 | I0、I1 + v1 | I0 已匹配；I1 成为新候选 | 不把新候选立即当成正式任务 |
| 叶子实例泛化与归并 | 相近或消歧冲突的叶子对 | 提案及验证通过后迁移成员、记录旧 ID 映射并更新树 | 不把相似度直接当成合并结论；已完成且输入未变时可整轮跳过 |
| 第一轮叶子更新与树重建 | 未覆盖文本及已有叶子成员 | 聚类形成材料叶子，审核后重建并命名树，得到 v2 | 不把第一轮旧分类记录自动改为 known |
| 第二轮语义分类与任务匹配 | 同一批 I0、I1 + v2 | I1 可以匹配新增任务，变为 known | 不改变原子意图编号；叶子归属变化保存在本轮快照 |
| 最终验证与导出 | 最终体系、分类与原句关联 | 验证归属，按原句汇总最终主题 | 最终文件不混入拆分、参数或审核过程字段 |

## 3. 原子意图分析：一句话拆成哪些独立请求

**Prompt 地址：** [EXTRACT_PROMPT](../../prompts/defaults/atomic_discovery/EXTRACT_PROMPT.txt)，配置键 `atomic_discovery.EXTRACT_PROMPT`。输入是原始 `user_text`，输出 AtomicExtraction；额外的知识问答/指代复核仍用这份模板，并在本次输入中加入上次输出和校验反馈。

**本例输入规模：20 条非空原始句子。输出规模：25 条明确原子意图。** 这里按原句请求，不按提取出的意图数请求。

**本阶段请求次数：** 每条非空原句请求 1 次，触发知识问答/指代复核的原句最多再请求 1 次。

```text
原子意图分析 LLM 次数 = N + E
N = 非空原句数；E = 额外复核的原句数
本例 = 20 + 0 = 20 次
```

空文本不请求模型；这里的 E 是语义复核，网络失败或输出格式不合格引起的重试还需另计。

r1 原文：

```text
Name the traditional clothing in eastern Algeria and explain what it is made of.
```

示意提取结果：

| 项目 | I0：r1:i0 | I1：r1:i1 |
|---|---|---|
| 原文片段 span | Name the traditional clothing in eastern Algeria | explain what it is made of. |
| 独立文本 text | Name the traditional clothing in eastern Algeria | explain what the traditional clothing in eastern Algeria is made of. |
| 状态 | valid | valid |
| context_indices | [] | [0] |
| 补全依据 | 直接表达 | it 指向前一个意图中的传统服饰 |

后续 Embedding 使用 text，而不是只有 span。否则 I1 只剩下“它是什么做的”，不知道“它”指什么，也会失去 eastern Algeria 这个背景。

这里保留地区信息，不猜具体服装名称，更不会回答问句。两条意图各自记录来源、对象和动作；初始提取不输出 slots，地区值保留在原子文本中。

**本阶段输出标签：** I0、I1 都有自己的 intent_id 和提取状态 valid，I1 另有 context_indices=[0]；还没有本轮任务匹配的 task_id 和 known/discovery 标签。

valid 只说明这是可理解的请求，不代表分类体系里已有匹配类别。因此，同一条意图之后可以同时具有“提取有效”和“分类未覆盖”这两个事实。

**此时只确定“要问什么”，还没有确定“属于哪一个任务类别”。**


初始提取在同一请求中完成拆分和语义归纳，返回原子文本、原文片段、object、action、状态、上下文依据及动作关系，不生成 `slots`。金额、人物、地点等必要信息保留在原子文本中。已有语义字段在后续匹配时直接复用，不再逐条请求解析。

### 3.1 原子提取：完整请求与响应

system 消息为 EXTRACT_PROMPT；以下是 r1 的完整 user 消息和 AtomicExtraction 响应，不包含 HTTP 响应信封。relations 为空表示模型没有额外声明执行顺序，不影响第二条意图的 context_indices。程序生成的 intent_id 不属于这份模型响应。

**完整输入 JSON（模拟值）：**

```json
{
  "user_text": "Name the traditional clothing in eastern Algeria and explain what it is made of."
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "原句明确请求服饰名称和制作材料，两条请求可独立回答。",
  "intents": [
    {
      "reason": "原文明确询问传统服饰名称。",
      "span": "Name the traditional clothing in eastern Algeria",
      "text": "Name the traditional clothing in eastern Algeria",
      "context_indices": [],
      "object": "传统服饰",
      "action": "查询名称",
      "status": "valid"
    },
    {
      "reason": "it 指向前一个意图中的 traditional clothing in eastern Algeria。",
      "span": "explain what it is made of.",
      "text": "explain what the traditional clothing in eastern Algeria is made of.",
      "context_indices": [
        0
      ],
      "object": "服饰制作材料",
      "action": "查询材料",
      "status": "valid"
    }
  ],
  "relations": [],
  "status": "valid"
}
```



### 3.2 指代复核分支：完整请求与响应

只有本地 extraction_issues 检查产生反馈时才会调用；这里故意让前一次模型输出留下 it，展示条件分支，不计入主线 E=0 的模拟账单。validation_feedback 由真实校验函数生成，system 仍为 EXTRACT_PROMPT。

**完整输入 JSON（模拟值）：**

```json
{
  "user_text": "Name the traditional clothing in eastern Algeria and explain what it is made of.",
  "previous": {
    "reason": "原句明确请求服饰名称和制作材料，两条请求可独立回答。",
    "intents": [
      {
        "reason": "原文明确询问传统服饰名称。",
        "span": "Name the traditional clothing in eastern Algeria",
        "text": "Name the traditional clothing in eastern Algeria",
        "context_indices": [],
        "object": "传统服饰",
        "action": "查询名称",
        "status": "valid"
      },
      {
        "reason": "it 指向前一个意图中的 traditional clothing in eastern Algeria。",
        "span": "explain what it is made of.",
        "text": "explain what it is made of.",
        "context_indices": [
          0
        ],
        "object": "服饰制作材料",
        "action": "查询材料",
        "status": "valid"
      }
    ],
    "relations": [],
    "status": "valid"
  },
  "validation_feedback": [
    "存在 context_indices，但 text 仍有指代；必须根据原文补全对象，不生成原文没有的内容。"
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "原句明确请求服饰名称和制作材料，两条请求可独立回答。",
  "intents": [
    {
      "reason": "原文明确询问传统服饰名称。",
      "span": "Name the traditional clothing in eastern Algeria",
      "text": "Name the traditional clothing in eastern Algeria",
      "context_indices": [],
      "object": "传统服饰",
      "action": "查询名称",
      "status": "valid"
    },
    {
      "reason": "it 指向前一个意图中的 traditional clothing in eastern Algeria。",
      "span": "explain what it is made of.",
      "text": "explain what the traditional clothing in eastern Algeria is made of.",
      "context_indices": [
        0
      ],
      "object": "服饰制作材料",
      "action": "查询材料",
      "status": "valid"
    }
  ],
  "relations": [],
  "status": "valid"
}
```


## 4. Embedding 与 BERTopic：先发现语义分布

**本阶段没有对话 Prompt。** Embedding 调用向量服务；分词、UMAP、HDBSCAN、c-TF-IDF 和原生层级树在本地计算。

**本例输入规模：由这 20 条原句提取的 25 条不同、有效原子文本。** 不是重新输入 20 条原句，也没有输入候选簇。

**本阶段请求次数：** 对话 LLM 为 0 次。Embedding 另计：完整缓存未命中时为 ceil(U / B)，U 为不同有效原子文本数，B 为向量服务批大小。

```text
本例 U=25、B=64：Embedding 请求 = ceil(25 / 64) = 1 次
BERTopic、分词、UMAP、HDBSCAN、c-TF-IDF、原生层级树：LLM 请求均为 0 次
```

完整向量缓存命中时 Embedding 也为 0 次；向量接口的失败重试单独计入实际调用。

不同的原子文本分别计算向量，相同文本可以共用向量与聚类位置，但各自的来源编号仍保留。

为了展示后续迭代如何处理未覆盖意图，假设本次初始结果如下：

| 原子意图 | 初始聚类结果 |
|---|---|
| I0：询问阿尔及利亚东部传统服饰名称 | 簇 C0 |
| 其他若干服饰名称问句 | 簇 C0 |
| 一些菜肴定义问句 | 也进入簇 C0 |
| I1：询问该服饰的材料 | 噪声，cluster_id = -1 |
| 其他若干材料类问句 | 分散在噪声或待确认簇 |

这里的噪声表示密度聚类没有接纳该样本，**不等于原句无效，也不等于后续一定无法分类**。

BERTopic 还会根据叶子簇生成原生合并树，节点标签主要来自关键词。树深度不等于固定的“领域 / 对象 / 动作”语义层级，且不同分支可以深浅不同。

<img src="assets/WORKFLOW_EXAMPLE/截屏2026-10-31 11.29.16.png" alt="截屏2026-10-31 11.29.16" style="zoom: 33%;" />


### 4.1 向量编码与本地聚类的数据格式

上面的模型 ID 和下面的三维向量均为教学模拟。输入是 SemanticEmbeddings.encode 内传给 embeddings.create 的本批业务参数（本例不配置 dimensions 或 extra_body）；输出是该函数校验并按输入顺序整理后的向量矩阵，不是向量服务原始 HTTP 响应。真实向量维度由所用模型决定。

**完整输入 JSON（模拟值）：**

```json
{
  "model": "example-embedding-model",
  "input": [
    "Name the traditional clothing in eastern Algeria",
    "explain what the traditional clothing in eastern Algeria is made of."
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
[
  [
    0.8,
    0.6,
    0.0
  ],
  [
    0.6,
    0.8,
    0.0
  ]
]
```


### 4.2 本地聚类：完整调用参数与结果

这是独立的两条文本小样本，用于完整展示 cluster_atomic_texts(texts, embeddings, cfg) 的调用参数与 ClusteringResult；不是前文 25 条意图的聚类复现。不同文本仅 2 条，小于门槛，因此代码返回全噪声。结果由现有 build_topic_tree 生成，labels 按输入文本顺序对应。此处不请求 LLM。

**完整输入 JSON（模拟值）：**

```json
{
  "texts": [
    "Name the traditional clothing in eastern Algeria",
    "explain what the traditional clothing in eastern Algeria is made of."
  ],
  "embeddings": [
    [
      0.8,
      0.6,
      0.0
    ],
    [
      0.6,
      0.8,
      0.0
    ]
  ],
  "cfg": {
    "min_cluster_size": 5
  }
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "labels": [
    -1,
    -1
  ],
  "keywords": {},
  "hierarchy": {
    "version": 1,
    "source": "BERTopic.hierarchical_topics",
    "representation": "c_tf_idf",
    "linkage": "average",
    "message": "不同原子文本过少，未生成候选主题",
    "nodes": [],
    "root_ids": [],
    "max_depth": 0,
    "leaf_count": 0,
    "status": "no_topics"
  }
}
```

## 5. 簇一致性审核：检查一个簇里是否混了不同意图

**本阶段 Prompt 地址：**

| 任务 | 默认 Prompt 文件 | 配置键 |
|---|---|---|
| 原始簇与细分后子簇的一致性审核 | [REVIEW_PROMPT](../../prompts/defaults/atomic_discovery/REVIEW_PROMPT.txt) | atomic_discovery.REVIEW_PROMPT |
| 局部向量分组不可靠时，LLM 对全簇样本分组 | [SPLIT_PROMPT](../../prompts/defaults/cluster_refinement/SPLIT_PROMPT.txt) | cluster_refinement.SPLIT_PROMPT |

局部 BERTopic 本身没有 Prompt；原始簇和子簇复核复用同一个 REVIEW_PROMPT，但 members 换成本次要审的簇样本。

**抽样审核：** 每簇先按不同原子文本去重，再根据样本向量与簇中心的相似度排序，交替选择靠近簇中心的代表样本和远离中心的边缘样本，直到达到 `atomic_discovery.review_examples` 上限（当前为 8）或取完全部不同文本。这样既检查簇的主要语义，也关注可能混入其他意图的边缘样本。超过抽样上限时，审核结论只基于所选样本，不代表已逐条检查全簇；未超过上限时则覆盖全部不同文本。

一致性审核只发送一次 members，每条仅含 intent_id 和已补全上下文的 text。不发送 span、raw_text、slots，也不重复附上 review_samples；簇规模、关键词及抽样范围仍作为元信息。中间文件继续保留完整 review_samples，供证据校验和追溯。此精简同样适用于细分后的子簇审核。

**本阶段请求次数：** 无有效历史审核可复用时，初始每簇审核 1 次，每个需模型审核的新增子簇再审核 1 次；符合审核复用条件时不请求。迭代内的单文本簇可按有效语义记录跳过比较，见第 9.2 节。局部向量计算不请求 LLM，启用完整 LLM 分组时每次分组另加 1 次。

```text
簇审核与细分 LLM 次数 = C + A + L
C = 初始候选簇数；A = 细分产生、需要审核的子簇总数；L = LLM 完整分组次数
本例 = 2 个初始簇 + 2 个子簇 + 1 次完整分组 = 5 次
```

**本例输入规模：20 条原句产生 25 条原子意图；其中 23 条进入 2 个初始非噪声簇，另 2 条是噪声，不作为普通簇审核。** 假设 C0 含 18 条不同原子文本，C1 含 5 条；C0 混合，C1 待确认。C0 经一次 LLM 完整分组拆成 C10、C11，各含 9 条不同文本，两个子簇均审核通过。

| 本阶段子任务 | 实际输入规模 | 请求次数 | 每次请求放什么 |
|---|---|---:|---|
| 初始簇审核 | 2 簇：C0 的 18 条、C1 的 5 条原子文本 | 2 | 一次一个簇的样本，本例分别抽取 8 条、全取 5 条 |
| LLM 完整分组 | 1 个混合簇 C0，共 18 条原子文本 | 1 | 一次放入 C0 的全部 18 条及其审核依据 |
| 子簇审核 | 2 个子簇 C10/C11，各 9 条原子文本 | 2 | 一次一个子簇，本例每次抽取 8 条 |
| 合计 | 同一批样本被不同子任务复核，不是新增原句 | **5** | 2 + 1 + 2 |

最终共有 3 个非噪声簇（C10、C11、C1），另有 2 条噪声意图。当前 review_examples=8，C10、C11 各抽取 8 条，C1 全取 5 条；每簇最多发送 8 条不同文本的审核样本。每簇多个样本放在同一次请求里，不按成员条数分别请求。

“最多拆分 2 轮”不等于“只额外调用 2 次”，每轮可能产生多个子簇。若局部向量分组已可靠则 L 可为 0，实际仍受每分支轮数、完整分组大小和总拆分预算限制。

LLM 审核 C0，发现里面既有服饰名称查询，也有菜肴定义查询，不能只因都在问文化知识就认为属于同一个原子意图类别。

流程尝试对 C0 局部细分，再审核子簇。假设得到：

```text
C0：混合簇
├── C10：传统服饰名称查询 → 审核通过
└── C11：菜肴定义查询     → 审核通过
```

**本阶段对 r1 的输出：**

| 原子意图 | 聚类/审核标签 | 此时还没有的标签 |
|---|---|---|
| I0 / r1:i0 | 原生簇 C10；簇审核通过；提取状态仍为 valid | 本轮逐条匹配后的 known/discovery |
| I1 / r1:i1 | cluster_id=-1，噪声；提取状态仍为 valid | 不能把噪声直接当成 invalid，也尚无材料任务匹配 |

此时 I0 的聚类归属由 C0 变为 C10，原子意图 text 与编号不变。原生层级树和相邻簇关系会相应重建。

I1 在本例中仍然是噪声，没有因为审核 C0 就自动获得类别。语义明确的单样本则可以保留为低支持候选，但不能声称已经证明该类别稳定。

### 5.1 一致性审核：完整请求与响应

**每簇最多抽取 8 条不同原子意图文本**，由 `atomic_discovery.review_examples=8` 控制。先按 text 去重，再按与簇中心的向量相似度排序，交替选择中心代表样本和远离中心的边缘样本，直到取满 8 条或全部取完。

- 不超过 8 条不同文本：全部发送，review_scope 为 all_unique_texts。
- 超过 8 条不同文本：只发送抽取的 8 条，review_scope 为 sampled。审核结论仅基于抽样，不代表已经逐条检查全簇。
- 一簇的所选样本合并到一次请求中，每条只含 intent_id 和 text；sample_count、unique_text_count 仍记录该簇总规模，不是发送样本数量。

主线 C0 有 18 条不同文本，因此本次审核只抽取 8 条；后续若触发 5.2 的 LLM 完整分组，则发送全部 18 条，不受此抽样上限限制。下面的独立示例只有 4 条不同文本，所以全部发送，不需要凑满 8 条。

下面是独立的 4 条样本演示，不替代主线 C0 的 18 条样本和调用账单。system 为 REVIEW_PROMPT。实际初始簇请求仅通过 members 发送一次样本，每条仅含 intent_id 和 text；review_samples 保留在中间文件中。

**完整输入 JSON（模拟值）：**

```json
{
  "cluster_id": 0,
  "sample_count": 4,
  "unique_text_count": 4,
  "keywords": [
    "traditional",
    "clothing",
    "cuisine"
  ],
  "review_scope": "all_unique_texts",
  "members": [
    {
      "intent_id": "demo1:i0",
      "text": "Name the traditional clothing in eastern Algeria."
    },
    {
      "intent_id": "demo2:i0",
      "text": "What do women traditionally wear in eastern Algeria?"
    },
    {
      "intent_id": "demo3:i0",
      "text": "What is Bazeen in Libyan cuisine?"
    },
    {
      "intent_id": "demo4:i0",
      "text": "What is couscous?"
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "两条样本询问服饰名称，另两条询问菜肴定义，回答目标不同。",
  "boundary_ids": [],
  "split_groups": [
    {
      "label": "传统服饰名称查询",
      "evidence_ids": [
        "demo1:i0",
        "demo2:i0"
      ]
    },
    {
      "label": "传统菜肴定义查询",
      "evidence_ids": [
        "demo3:i0",
        "demo4:i0"
      ]
    }
  ],
  "object": "传统服饰与传统菜肴",
  "action": "查询名称与查询定义",
  "semantic_clear": true,
  "verdict": "mixed"
}
```


### 5.2 LLM 完整分组：完整请求与响应

仅在局部向量分组不可靠且未超完整分组预算时调用 SPLIT_PROMPT。与抽样审核不同，这里发送待分组的全部不同原子文本；每个输入 ID 必须完整、不重复地出现在 groups 或 uncertain_ids 中。

**调用条件：必须先由一致性审核判为 mixed，再尝试局部 BERTopic；只有未得到可靠分组时，才回退到 LLM 完整分组。** 当前“未得到可靠分组”的具体判定是以下任一情况：

| 局部聚类结果 | 是否尝试 LLM 完整分组 |
|---|---|
| 非噪声分组少于 2 个，包括全部为噪声或只有一个有效分组 | 是 |
| 不同原子文本至少 4 条，且非噪声分组中超过一半为单样本组 | 是，记录 singleton_dominated |
| 局部聚类报错、返回样本数不匹配或向量不可用 | 是，记录错误 |
| 至少 2 个非噪声分组，且未触发上述碎片化条件 | 否，直接采用局部分组并审核子簇 |

“可靠”在此表示通过上述程序检查，并不代表分组语义已经确认；局部分组和 LLM 分组产生的子簇都还需要一致性审核。

回退还必须满足预算：不同原子文本至少 2 条、最多 refinement_llm_limit 条（默认 80）；分支未达到 refinement_rounds（默认 2），且总尝试未达到 refinement_max_splits（默认 64）。不满足时停止当前分支并保留待确认结果，不强行分组。审核为 consistent 或 uncertain 的簇不会仅因规模大而进入此回退。

```text
簇审核 = mixed
  → 局部 BERTopic
    → 得到可靠分组：直接审核子簇
    → 未得到可靠分组：检查完整分组与细分预算
      → 预算允许：LLM 完整分组 → 审核子簇
      → 预算不允许：停止当前分支，保留待确认
```


样本仅发送 intent_id 和已补全上下文的 text，不发送 raw_text、span 或 slots；同时保留前次簇审核 review 作为拆分依据。完整原文和原始片段仍保存在中间文件中用于追溯。初始提取不生成 slots。

**完整输入 JSON（模拟值）：**

```json
{
  "members": [
    {
      "intent_id": "demo1:i0",
      "text": "Name the traditional clothing in eastern Algeria."
    },
    {
      "intent_id": "demo2:i0",
      "text": "What do women traditionally wear in eastern Algeria?"
    },
    {
      "intent_id": "demo3:i0",
      "text": "What is Bazeen in Libyan cuisine?"
    },
    {
      "intent_id": "demo4:i0",
      "text": "What is couscous?"
    }
  ],
  "review": {
    "reason": "两条样本询问服饰名称，另两条询问菜肴定义，回答目标不同。",
    "boundary_ids": [],
    "split_groups": [
      {
        "label": "传统服饰名称查询",
        "evidence_ids": [
          "demo1:i0",
          "demo2:i0"
        ]
      },
      {
        "label": "传统菜肴定义查询",
        "evidence_ids": [
          "demo3:i0",
          "demo4:i0"
        ]
      }
    ],
    "object": "传统服饰与传统菜肴",
    "action": "查询名称与查询定义",
    "semantic_clear": true,
    "verdict": "mixed"
  }
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "groups": [
    {
      "label": "传统服饰名称查询",
      "evidence_ids": [
        "demo1:i0",
        "demo2:i0"
      ]
    },
    {
      "label": "传统菜肴定义查询",
      "evidence_ids": [
        "demo3:i0",
        "demo4:i0"
      ]
    }
  ],
  "uncertain_ids": []
}
```



### 5.3 细分后子簇复核：完整请求与响应

沿用 5.1 的独立四样本演示。system 仍为 REVIEW_PROMPT，但本次只审服饰子簇，实际请求另外携带 parent_cluster_id 和 refinement_round。两条样本可被判断语义一致；是否达到后续候选支持门槛仍由程序单独判断。

**完整输入 JSON（模拟值）：**

```json
{
  "cluster_id": 10,
  "parent_cluster_id": 0,
  "refinement_round": 1,
  "sample_count": 2,
  "unique_text_count": 2,
  "keywords": [
    "传统服饰名称查询"
  ],
  "review_scope": "all_unique_texts",
  "members": [
    {
      "intent_id": "demo1:i0",
      "text": "Name the traditional clothing in eastern Algeria."
    },
    {
      "intent_id": "demo2:i0",
      "text": "What do women traditionally wear in eastern Algeria?"
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "两条样本均询问阿尔及利亚东部传统服饰的名称。",
  "boundary_ids": [],
  "split_groups": [],
  "object": "传统服饰",
  "action": "查询名称",
  "semantic_clear": true,
  "verdict": "consistent"
}
```


## 6. 跨簇边界复核：检查相近簇是否重复

**Prompt 地址：** [PAIR_PROMPT](../../prompts/defaults/atomic_discovery/PAIR_PROMPT.txt)，配置键 `atomic_discovery.PAIR_PROMPT`。每次输入 left/right 两组真实样本，不是把整句所有意图揉在一起判断。

**实际发送的是两个簇的真实文本样本，不是只发送 BERTopic 的主题名称或关键词。** 每个样本目前包含：

| 输入字段 | 实际内容 |
|---|---|
| intent_id | 可供审核引用的原子意图 ID |
| text | 独立、必要时已补全上下文的原子意图文本 |
| span | 对应的原文连续片段 |
| raw_text | 该意图来源的完整原句 |

这些字段放入 left/right 两个样本数组。因此虽然按原子意图抽样，当前请求中也确实携带其完整原句；text、span 和 raw_text 可能重复，不能把“16 条样本”直接理解为“16 条短句的 Token”。此复核请求本身不发送整个 BERTopic 主题树，也不只依靠关键词判断类别相同。

**大量语料时的单次输入上限按抽样条数控制：** 当前 `atomic_discovery.review_examples=8`，每簇先按不同原子文本去重，再选靠近中心的代表与远离中心的边缘样本，最多 8 条；一个簇有几百条意图也不会全量发入。一次复核两个簇，通常为至多 8+8=16 条样本。例如 1000 条原句聚成 30 个簇时，每个簇对请求仍只发送两侧抽样；总原句数主要影响分布、簇数和总请求量，不会直接变成一次输入 1000 句。

不过当前限制的是**样本数量，不是输入 Token 总量**，长原句和重复字段仍可能造成上下文过大。`max_tokens=1000` 限的是复核输出长度，不会截断输入。后续若要稳妥处理长文本，应增加输入 Token 预算和动态抽样/压缩证据，不能仅靠固定样本数保证不超上下文，也不宜只用主题词代替真实样本。

**本阶段请求次数：** 每个实际相邻簇对请求 1 次，合计 P 次。P 是程序去重后挑选的相邻对数，不是默认遍历所有簇的两两组合。

**不是簇数平方次 LLM 请求。** 当前实现按簇中心的向量相似度，为每个非噪声簇选择最相近的 K 个其他簇，然后将 A→B、B→A 合并为同一个无向簇对。每个去重后的簇对只请求一次。

- K 由 [config/config.yaml](../../config/config.yaml) 中的 `atomic_discovery.neighbor_top_k` 控制，默认 3。
- C 为本次计算邻接关系的非噪声簇数，噪声不是一个待复核的普通簇。
- 具体选择与去重实现见 [neighbor_pairs](../../intent_engine/atomic/atomic_discovery.py#L441)。

```text
P ≤ min(C × K, C × (C − 1) / 2)
默认 K=3：P ≤ min(3C, C × (C − 1) / 2)
```

| 非噪声簇数 C | 全量两两比较 | 当前默认 K=3 的 LLM 基础请求上限 |
|---|---:|---:|
| 3 | 3 | 3 |
| 10 | 45 | 30 |
| 100 | 4950 | 300 |

实际 P 取决于邻接关系去重后的结果，可能低于上限。K 固定时，请求上限随簇数线性增加；若把 K 调大到覆盖所有其他簇，就会退化为全量两两比较。这里不含失败重试，实际接口调用可能更多。

**平方级的是当前本地相似度矩阵计算。** C×C 的簇中心比较属于本地运算，不请求 LLM；不能把这部分计算量当成接口次数。并行只改变执行方式，不减少 P。

**本例输入规模：来自同批 20 条原句的 3 个非噪声簇，分别有 9、9、5 条不同原子文本。** 默认 K=3，实际其他簇只有 2 个，去重后复核全部 3 个簇对：

| 簇对 | 本例一次请求中的输入 | LLM 请求次数 |
|---|---|---:|
| C10 ↔ C11 | 左簇 8 条 + 右簇 8 条审核样本 | 1 |
| C10 ↔ C1 | 左簇 8 条 + 右簇 5 条审核样本 | 1 |
| C11 ↔ C1 | 左簇 8 条 + 右簇 5 条审核样本 | 1 |
| 合计 | **3 个簇对**；同一样本可能出现在不同簇对请求中 | **3** |

并行执行仍然计 3 次。一次只发送左右两个簇的审核样本，每侧最多 `review_examples=8` 条，不足则全部选取。因此一次请求最多 **左侧 8 条＋右侧 8 条，共 16 条**，不会将所有原句发进去。

在其他运行中，如果另有 C12 也询问传统服饰名称，LLM 比较 C10、C12 的真实样本，判断：

- 仅表达方式不同，可能建议合并。
- 一个询问名称、一个询问穿着场合，可能保持独立。
- 证据不足，则标为待确认。

**当前这一步只记录建议，不直接合并簇。** 后面的体系归一可能合并重复任务，那是分类体系层面的修改，不等于应用了这里的原生聚类合并建议。


### 6.1 簇对复核：完整请求与响应

system 为 PAIR_PROMPT。这里继续使用第 5 章独立演示的两组样本，分别表示服饰簇和菜肴簇；并非主线各 9 条样本的省略写法。left_cluster_id/right_cluster_id 由程序写入报告，不在本次模型输入或模型响应中。

**完整输入 JSON（模拟值）：**

```json
{
  "left": [
    {
      "intent_id": "demo1:i0",
      "text": "Name the traditional clothing in eastern Algeria.",
      "span": "Name the traditional clothing in eastern Algeria.",
      "raw_text": "Name the traditional clothing in eastern Algeria."
    },
    {
      "intent_id": "demo2:i0",
      "text": "What do women traditionally wear in eastern Algeria?",
      "span": "What do women traditionally wear in eastern Algeria?",
      "raw_text": "What do women traditionally wear in eastern Algeria?"
    }
  ],
  "right": [
    {
      "intent_id": "demo3:i0",
      "text": "What is Bazeen in Libyan cuisine?",
      "span": "What is Bazeen in Libyan cuisine?",
      "raw_text": "What is Bazeen in Libyan cuisine?"
    },
    {
      "intent_id": "demo4:i0",
      "text": "What is couscous?",
      "span": "What is couscous?",
      "raw_text": "What is couscous?"
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "左侧询问传统服饰名称，右侧询问菜肴定义，对象和回答结果均不同。",
  "left_evidence_ids": [
    "demo1:i0",
    "demo2:i0"
  ],
  "right_evidence_ids": [
    "demo3:i0",
    "demo4:i0"
  ],
  "decision": "keep"
}
```


## 7. BERTopic 层级命名与合并审核：只维护一套树

**Prompt 地址：** 叶子使用 [LEAF_PROMPT](../../prompts/defaults/hierarchy_review/LEAF_PROMPT.txt)，配置键 `hierarchy_review.LEAF_PROMPT`；父节点使用 [NODE_PROMPT](../../prompts/defaults/hierarchy_review/NODE_PROMPT.txt)，配置键 `hierarchy_review.NODE_PROMPT`。调度实现为 [review_hierarchy](../../intent_engine/atomic/hierarchy_review.py)。不再一次性将全部合格簇汇总给领域归纳模型，也不独立生成“领域 → 任务”树。

BERTopic 在最终叶子簇上生成原生合并树。先审核叶子节点，再从最深层向根推进；同一个原生深度的节点按 Workers 并发请求，整层结束后才处理上一层。不同深度有依赖，不会同时审核未准备好的父子节点。

### 7.1 叶子命名：输入、输出与请求次数

首次命名时每个合格叶子请求一次；已审核的单文本或多文本叶子若完整成员、审核、样本、Prompt 和模型配置未变则复用，不再请求。语义明确且命名有效即可准入；单文本只标记低支持，不能仅因样本少而否决。输入审核或单样本语义摘要及最多 8 条不同原子文本，每条只有 text，不发送 intent_id；审核摘要只包含 reason、object、action、semantic_clear 和 verdict，不发送证据 ID、boundary_ids 或 split_groups。不把全簇原句重新送入；簇审核不合格的叶子不请求命名，也不进入最终导出。

```json
{
  "node_id": "topic:10",
  "review": {
    "reason": "样本均询问传统服饰名称",
    "object": "传统服饰",
    "action": "查询名称",
    "verdict": "consistent"
  },
  "members": [
    {
      "text": "Name the traditional clothing in eastern Algeria."
    }
  ],
  "kind": "leaf"
}
```

```json
{
  "reason": "请求的对象和回答目标一致",
  "description": "询问某地区或文化中的传统服饰名称，不包含材料查询",
  "label": "传统服饰名称查询",
  "semantic_issue": "none",
  "semantic_clear": true
}
```

**本例输入规模与调用数：** 原始 20 条语句产生 25 条原子意图。细分后 C10、C11 各有 9 条不同文本，通过簇审核；C1 的 5 条仍待确认。因此合格叶子 E=2，各抽取 8 条，一次一个叶子，共 2 次对话 LLM 请求，而不是按 16 条样本请求 16 次。

### 7.2 父节点：判断子主题是否适合共同归组

父节点只接收当前子分支的名称、定义、审核理由和叶子数量摘要，不发送全部原子文本。沿用原来归纳领域的判断原则：看知识主题或业务对象，不因都是“查询名称”而把服饰与航天合并。

```json
{
  "node_id": "merge:20",
  "children": [
    {"description": "询问传统服饰名称", "reason": "服饰主题", "node_id": "topic:10", "leaf_count": 1, "label": "传统服饰名称查询"},
    {"description": "询问传统菜肴是什么", "reason": "饮食主题", "node_id": "topic:11", "leaf_count": 1, "label": "传统菜肴定义查询"}
  ],
  "kind": "parent"
}
```

```json
{
  "reason": "两个子主题可以作为传统文化知识的不同分支，具体动作与对象仍需分开",
  "description": "介绍传统服饰与饮食等文化事物，保留下层具体意图",
  "label": "传统文化知识",
  "decision": "group"
}
```

- group：保留这个父节点，使用新名称和定义；不合并叶子意图或样本。
- separate：删除本次父级组合，子分支提升为独立分支。
- uncertain 或请求失败：同样不强行保留这个父节点；保留子分支并记录原因或错误。

若上一级仍需判断是否归组，将使用提升后的子分支摘要。每个请求最多 `hierarchy_child_limit=16` 个子节点摘要；超过上限就保留分支并记录 child_summary_limit，不把所有簇塞进一个请求。

**本例父节点输入规模与调用数：** 假设 C10、C11 是同一原生父节点的两个子节点，父节点 P=1，传入 2 条子主题摘要，请求 1 次。更高父节点若包含尚未通过审核的 C1，则不请求、不强行命名。此阶段合计 E+P=2+1=3 次对话 LLM。

一般情况下，C 个叶子的完整二叉树有 C−1 个原生父节点，审核请求不超过合格叶子数加可审核父节点数；并发只缩短等待时间，不减少请求数。子分支被否定时可能产生多根森林，每条路径仍有确定顺序。

### 7.3 本阶段 r1 的标签与查找适配格式

I0 若在 C10，得到路径“传统文化知识 → 传统服饰名称查询”。I1 仍是噪声，不会因其他叶子命名成功就自动归属。

为复用语义解析和向量召回，程序从这套树派生 candidate_taxonomy：domain 是叶子祖先路径，task 是叶子。它是查找适配表，不是另一套由 LLM 独立维护的分类结构。

```json
{
  "version": 1,
  "domains": [{"description": "介绍传统服饰与饮食等文化事物", "name": "传统文化知识"}],
  "tasks": [
    {"description": "询问传统服饰名称", "task_id": "T1", "positive_examples": ["Name the traditional clothing in eastern Algeria."], "domain": "传统文化知识", "object": "传统服饰", "action": "查询名称"},
    {"description": "询问传统菜肴是什么", "task_id": "T2", "positive_examples": ["What is Bazeen?"], "domain": "传统文化知识", "object": "传统菜肴", "action": "查询定义"}
  ]
}
```

T1/T2 为教学简写；真实 ID 带流程编号和叶子簇编号，task_topic_nodes 保存任务与叶子节点的对应关系。多级祖先按路径组织，不强制恰好两层。bertopic_hierarchy 保留原生树，topic_hierarchy 保存审核后用于展示和导出的树。

## 8. 第一轮：召回叶子主题并判断意图归属

原子意图文本、编号、对象和动作来自初始提取，字段齐全时不再请求语义解析。默认增量模式从本轮起继承已审核簇的归属；噪声、普通待确认簇等未确认归属的意图，以及必要的失效归属校验进入向量 Top-K 召回、逐候选判断及最终消歧。选择全量模式时全部意图进入匹配，但也复用已有语义字段。不预测领域，祖先路径从树中取得。

| 子任务 | 输入 | Prompt 或实现地址 | 请求次数 |
|---|---|---|---|
| 缺字段时补解析 | 1 条原子文本；新数据直接复用主流程对象、动作 | [ATOMIC_CLASSIFY_SYSTEM](../../prompts/defaults/classifier/ATOMIC_CLASSIFY_SYSTEM.txt) | 新数据 0 次；缺字段时每条 1 次 |
| 向量召回 | 原子文本、叶子定义和代表样本 | [VectorTaskRecall](../../intent_engine/classification/vector_recall.py) | 无对话 LLM；未缓存文本按批请求 Embedding |
| 逐候选判断 | 1 条意图及 1 个候选任务 | [PAIRWISE_SYSTEM](../../prompts/defaults/classifier/PAIRWISE_SYSTEM.txt) | 每条意图 K 次，K≤candidate_top_k |
| 最终消歧 | 1 条意图及通过判断的候选列表 | [FINAL_MATCH_SYSTEM](../../prompts/defaults/classifier/FINAL_MATCH_SYSTEM.txt) | F=0 或 1 次 |

本例 v1 有 2 个叶子任务。以下逐候选示例以显式全量匹配或归属失效的分支展示：I0 召回 2 个候选并最终匹配 T1；I1 没有材料主题，2 个候选均不匹配。默认增量模式下，I0 若已在审核簇 C10 中则直接继承 T1，不发这组匹配请求；如果 I1 是噪声或属于普通待确认簇，就进入召回。

| 意图 | 第一轮结果 | 后续去向 |
|---|---|---|
| I0：查询服饰名称 | known，匹配 T1 | 保持现有叶子归属 |
| I1：查询服饰制作材料 | discovery，无任务匹配 | 进入第 9 章未覆盖文本池 |

**本轮请求次数与规模：** 新流程语义字段齐全时，全量匹配模式下 1 条原句的 2 条意图，共 (2+1)+(2+0)=5 次对话 LLM；25 条意图各召回 2 个任务、18 条进入消歧，则为 25×2+18=68 次。默认增量模式从首轮就复用已审核簇归属：若本例 I0 已覆盖、只有 I1 未确认归属，则只匹配 I1，召回 2 个候选均不匹配时为 2 次。旧数据缺失语义字段的意图另加每条 1 次补解析。这是模拟值，实际以阶段 Token 和请求记录为准。原子文本不变时复用向量；叶子或祖先定义变化时更新相应主题向量。


**按输入输出阅读：** 8.1 仅在缺少字段时补解析；8.2 本地召回候选；8.3 对每个候选分别判断；存在通过候选时才执行 8.4 最终消歧。下面的示例分别标明跟踪 I0 还是 I1，避免把两条意图的请求误认为同一次调用。

### 8.1 按需补解析：旧文件缺少语义字段时的完整输入与输出

以下模型响应示例按 Prompt 要求展示：先给 reason 等依据，再给 confidence，最后给分类或匹配结论。程序解析后的 Pydantic 对象若调用 model_dump()，会按模型字段声明顺序重新排列；该顺序不等于模型原始响应的生成顺序。

新流程已在初始提取中生成对象和动作，本步骤为 0 次请求，直接使用保存的字段。只有旧文件或失效语义字段缺失时，才使用 ATOMIC_CLASSIFY_SYSTEM 补解析；下面展示这一回退分支的完整输入输出，不代表每轮必定调用。字段齐全时不为 slots 额外发起请求。值为模拟值，不是运行日志。

**完整输入 JSON（模拟值）：**

```json
{
  "atomic_text": "explain what the traditional clothing in eastern Algeria is made of.",
  "slots": {}
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "当前独立文本明确询问服饰的制作材料。",
  "confidence": 0.98,
  "object": "服饰制作材料",
  "action": "查询材料",
  "slots": {
    "region": "eastern Algeria"
  }
}
```


### 8.2 向量召回：完整本地输入与输出

**本步骤不调用对话 LLM。** 它分为向量编码和本地检索：原子文本、叶子定义等文本的向量未缓存时，请求 Embedding 模型接口；缓存命中时直接复用向量。随后在本地计算余弦相似度，选出 Top-K 叶子任务。Embedding 请求单独统计，不计为对话 LLM 请求；相似度计算和 Top-K 选择不请求任何模型。

召回只提供候选，不负责判定匹配。真正由对话 LLM 判断候选能否满足意图的是 **8.3 逐候选判断**。

程序调用 `VectorTaskRecall.recall(taxonomy, text, top_k)`，从当前全部叶子中选候选。这不是一次对话 LLM 请求：taxonomy 在本地用于建立索引，向量服务接收的是编码文本。以下完整列出 v1 的两个任务；模拟召回顺序为 T1、T2。返回的是 TaskDef 列表，不包含相似度分数。domain 字段是祖先路径的兼容存储，不参与领域预测或预筛选。

**完整输入 JSON（函数参数，模拟值）：**

```json
{
  "taxonomy": {
    "version": 1,
    "domains": [
      {
        "name": "传统文化知识",
        "description": "祖先主题路径的兼容查找字段",
        "parent_domain": ""
      }
    ],
    "tasks": [
      {
        "task_id": "T1",
        "domain": "传统文化知识",
        "object": "传统服饰",
        "action": "查询名称",
        "description": "回答传统服饰的名称",
        "positive_examples": [
          "Name the traditional clothing in eastern Algeria."
        ],
        "boundary_notes": [
          "不回答制作材料"
        ],
        "domain_aliases": [],
        "object_aliases": [],
        "action_aliases": [],
        "subtasks": [],
        "created_in_version": 1
      },
      {
        "task_id": "T2",
        "domain": "传统文化知识",
        "object": "传统菜肴",
        "action": "查询定义",
        "description": "解释传统菜肴是什么",
        "positive_examples": [
          "What is Bazeen in Libyan cuisine?"
        ],
        "boundary_notes": [
          "不回答服饰知识"
        ],
        "domain_aliases": [],
        "object_aliases": [],
        "action_aliases": [],
        "subtasks": [],
        "created_in_version": 1
      }
    ]
  },
  "text": "explain what the traditional clothing in eastern Algeria is made of.",
  "top_k": 5
}
```

**完整输出 JSON（TaskDef 列表，模拟排序）：**

```json
[
  {
    "task_id": "T1",
    "domain": "传统文化知识",
    "object": "传统服饰",
    "action": "查询名称",
    "description": "回答传统服饰的名称",
    "positive_examples": [
      "Name the traditional clothing in eastern Algeria."
    ],
    "boundary_notes": [
      "不回答制作材料"
    ],
    "domain_aliases": [],
    "object_aliases": [],
    "action_aliases": [],
    "subtasks": [],
    "created_in_version": 1
  },
  {
    "task_id": "T2",
    "domain": "传统文化知识",
    "object": "传统菜肴",
    "action": "查询定义",
    "description": "解释传统菜肴是什么",
    "positive_examples": [
      "What is Bazeen in Libyan cuisine?"
    ],
    "boundary_notes": [
      "不回答服饰知识"
    ],
    "domain_aliases": [],
    "object_aliases": [],
    "action_aliases": [],
    "subtasks": [],
    "created_in_version": 1
  }
]
```

召回表示“值得进一步比较”，不代表匹配成功。即使名称任务排在材料问句的候选首位，也必须经下一步判断；不能根据 Top-1 直接写 known。

### 8.3 逐候选判断：完整输入与输出

system 为 PAIRWISE_SYSTEM；一次只比较一条原子意图和一个召回叶子。以下展示 I1 与 T1 不匹配的请求；其他召回叶子分别请求。candidate_task.topic_path 来自查找适配表的祖先路径，不是模型预测的领域。

**完整输入 JSON（模拟值）：**

```json
{
  "intent_text": "explain what the traditional clothing in eastern Algeria is made of.",
  "atomic_intent": {
    "object": "服饰制作材料",
    "action": "查询材料",
    "span": "explain what the traditional clothing in eastern Algeria is made of.",
    "slots": {
      "region": "eastern Algeria"
    },
    "confidence": 0.98
  },
  "candidate_task": {
    "task_id": "T1",
    "topic_path": [
      "传统文化知识"
    ],
    "object": "传统服饰",
    "action": "查询名称",
    "description": "回答传统服饰的名称",
    "object_aliases": [],
    "action_aliases": [],
    "subtasks": [],
    "positive_examples": [
      "What do women traditionally wear in eastern Algeria?"
    ],
    "boundary_notes": [
      "不回答制作材料"
    ]
  }
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "请求查询制作材料，候选只回答名称，履约结果不同。",
  "confidence": 0.97,
  "task_id": "T1",
  "match": false
}
```


### 8.4 最终消歧：完整输入与输出

system 为 FINAL_MATCH_SYSTEM。此例跟踪 I0：假设 T1 已通过单候选判断，shortlist 只有 T1，默认仍执行最终消歧。I1 若所有候选都未通过，则直接保留 discovery，不产生最终消歧请求。

**完整输入 JSON（模拟值）：**

```json
{
  "intent_text": "Name the traditional clothing in eastern Algeria",
  "atomic_intent": {
    "object": "传统服饰",
    "action": "查询名称",
    "span": "Name the traditional clothing in eastern Algeria",
    "slots": {
      "region": "eastern Algeria"
    },
    "confidence": 0.98
  },
  "shortlist": [
    {
      "task": {
        "task_id": "T1",
        "topic_path": [
          "传统文化知识"
        ],
        "object": "传统服饰",
        "action": "查询名称",
        "description": "回答传统服饰的名称",
        "object_aliases": [],
        "action_aliases": [],
        "subtasks": [],
        "positive_examples": [
          "What do women traditionally wear in eastern Algeria?"
        ],
        "boundary_notes": [
          "不回答制作材料"
        ]
      },
      "pairwise_match": true,
      "pairwise_confidence": 0.97,
      "pairwise_reason": "请求与候选叶子的回答目标相同。"
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "该候选叶子与当前原子意图履约等价。",
  "confidence": 0.97,
  "candidate_task_ids": [
    "T1"
  ],
  "selected_task_id": "T1",
  "status": "known"
}
```

## 9. 未覆盖意图聚类、成员复核与树重建

当前主流程调用 [run_hierarchy_iterations](../../intent_engine/workflows/hierarchy_iteration.py)，不再走独立领域新增、跨领域归一、任务表合并的旧维护链路。

**本章输入来自第 8 章的逐意图匹配结果，以及当前树、叶子成员和已缓存向量。输出是更新后的成员归属、审核后的同一棵树和由树派生的叶子适配表。** 下面按实际子步骤列出完整输入输出；本章不是把所有数据一次发送给一个模型。第 9.1～9.5 节使用独立的小规模材料示例，避免省略数组成员；不改变前述整批请求次数假设。

1. known 意图进入匹配到的现有叶子；discovery 意图进入未覆盖文本池，复用向量做 BERTopic 聚类。ambiguous 保留原簇，不强行迁移。
2. 未覆盖文本聚类形成新叶子或噪声；已有叶子编号保持稳定，新叶子使用新的簇编号。新的任务仍须通过簇一致性审核及节点命名。
3. 成员有更新时先检查簇审核复用条件；单文本簇用本地语义检查，其余未命中复用的簇才抽样最多 8 条重新审核。混合簇按第 5 章规则细分。
4. 基于最终叶子成员重建原生 BERTopic 树，再按第 7 章自下而上处理；有效指纹匹配的叶子名称直接复用，其余叶子命名和父主题审核按需请求。
5. 从同一棵树派生查找适配表；继续执行第 9.6 节的叶子实例泛化与归并，成功合并后更新成员、树及适配表。保存版本变化、树前后快照和成员路径。迭代准备阶段也执行归并。

**本例规模与请求次数：** 假设第一轮有 7 条未覆盖意图，其中 5 条材料问句形成一个新叶子，另外 2 条仍为噪声；原有待确认簇成员被重新分类后不再单独保留。最终有 3 个叶子；仅在这 3 个叶子均未命中簇审核复用、且各有至少两条不同文本时，一致性审核才是 3 次。若 3 个叶子均未命中命名复用且 2 个父节点均符合条件，则命名与父审核为 5 次。叶子归并另外按实际请求计数：未缓存候选对先提案 1 次，提案为 merge 时再验证 1 次，成功应用后还可能增加树重建后的父审核。本地聚类、向量重用、原生树拓扑重建和适配表生成不请求对话 LLM；重建后的节点审核另计。发生混合簇细分时另外按 C+A+L 计算，不能直接套用固定 3 次。

新树示意（仅为可能结果，不保证模型固定生成这个层数）：

```text
传统文化知识
├── 服饰知识
│   ├── 传统服饰名称查询（T1）
│   └── 服饰制作材料查询（T3）
└── 传统菜肴定义查询（T2）
```

**r1 在本阶段后：** I0 的叶子仍为 T1，但祖先路径变为“传统文化知识 → 服饰知识 → 传统服饰名称查询”；I1 的材料文本被新聚类接受后获得材料叶子。原子文本及编号没有变化。第一轮匹配记录仍属于 v1，不会被改写成 v2 的匹配结果。


### 9.1 未覆盖文本聚类：完整本地输入与输出

以下为独立的 5 条 discovery 文本演示，与上文整批 25 条意图的账单区分。输入是 cluster_atomic_texts(texts, embeddings, cfg) 的函数参数；输出是 ClusteringResult 的 JSON 表示。向量、簇分配和关键词为模拟值，未实际运行 BERTopic。原始局部标签 0 会被迭代器映射为新的全局簇编号，例如 12，避免覆盖已有叶子。此步骤不请求对话 LLM。

**完整输入 JSON（模拟值）：**

```json
{
  "texts": [
    "What is traditional clothing made of?",
    "Which fabric is used for traditional clothing?",
    "Name the materials used in traditional outfits.",
    "What are traditional garments made from?",
    "Explain the fabric used in traditional dress."
  ],
  "embeddings": [
    [
      1.0,
      0.0,
      0.0
    ],
    [
      0.99,
      0.1,
      0.0
    ],
    [
      0.98,
      0.2,
      0.0
    ],
    [
      0.97,
      0.24,
      0.0
    ],
    [
      0.96,
      0.28,
      0.0
    ]
  ],
  "cfg": {
    "min_cluster_size": 5,
    "topic_language": "english"
  }
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "labels": [
    0,
    0,
    0,
    0,
    0
  ],
  "keywords": {
    "0": [
      "clothing",
      "fabric"
    ]
  },
  "hierarchy": {
    "version": 1,
    "source": "BERTopic.hierarchical_topics",
    "representation": "c_tf_idf",
    "linkage": "average",
    "status": "single_topic",
    "message": "",
    "nodes": [
      {
        "node_id": "topic:0",
        "topic_id": 0,
        "label": "clothing · fabric",
        "keywords": [
          "clothing",
          "fabric"
        ],
        "children": [],
        "is_leaf": true,
        "distance": null,
        "parent_id": null,
        "depth": 0,
        "leaf_topic_ids": [
          0
        ]
      }
    ],
    "root_ids": [
      "topic:0"
    ],
    "max_depth": 1,
    "leaf_count": 1
  }
}
```


### 9.2 成员一致性复核：完整输入与输出

迭代成员复核和局部细分后的子簇复核优先复用有效的历史簇审核，并对单文本簇跳过模型比较。例如本轮有 10 个待复核簇，其中 6 个为单文本簇，若其余 4 簇均未命中审核缓存，则基础调用为 4 次；有缓存时还会减少。这 6 个单文本簇的对象、动作来自已保存的有效语义记录，不要求本轮额外逐意图解析。重复文本不会增加不同文本数。叶子命名审核仍单独计数，网络或结构校验重试另计；初始发现阶段的审核不受此规则影响。

system 为 REVIEW_PROMPT。成员更新后，仅对未命中审核复用且至少包含两条不同文本的叶子请求一致性审核；单文本簇执行本地语义检查。本次展示材料叶子。默认每簇最多抽样 8 条，本例只有 5 条，全部发送。输入由真实 consistency_review_payload 构造，只保留精简 members 和簇元信息。

**完整输入 JSON（模拟值）：**

```json
{
  "cluster_id": 12,
  "sample_count": 5,
  "unique_text_count": 5,
  "keywords": [
    "clothing",
    "fabric"
  ],
  "review_scope": "all_unique_texts",
  "members": [
    {
      "intent_id": "demo_material_1:i0",
      "text": "What is traditional clothing made of?"
    },
    {
      "intent_id": "demo_material_2:i0",
      "text": "Which fabric is used for traditional clothing?"
    },
    {
      "intent_id": "demo_material_3:i0",
      "text": "Name the materials used in traditional outfits."
    },
    {
      "intent_id": "demo_material_4:i0",
      "text": "What are traditional garments made from?"
    },
    {
      "intent_id": "demo_material_5:i0",
      "text": "Explain the fabric used in traditional dress."
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "样本均询问传统服饰使用的材料，具体措辞不改变回答目标。",
  "boundary_ids": [],
  "object": "服饰制作材料",
  "action": "查询材料",
  "split_groups": [],
  "verdict": "consistent",
  "semantic_clear": true
}
```


### 9.3 叶子命名审核：完整输入与输出

重建时复用上一版已审核且未变化的单文本和多文本叶子名称、描述与理由；新叶子、旧记录缺少新版指纹、定义或模型配置变化时仍请求。复用记录标为 `reused_singleton` 或 `reused_leaf`，不生成虚构请求。比如 10 个合格叶子中有 6 个满足复用条件，则叶子命名基础请求为 4 次；父节点审核另外计数。单子分支父节点继续直接展开、不请求模型。

树重建后，system 使用 [hierarchy_review.LEAF_PROMPT](../../prompts/defaults/hierarchy_review/LEAF_PROMPT.txt)，kind=leaf；下面的输入由真实 review_hierarchy 构造。输出为 LeafNamingDecision，semantic_clear=true 表示语义与命名可用；程序据此批准，支持量另外标记。

**完整输入 JSON（模拟值）：**

```json
{
  "node_id": "topic:12",
  "kind": "leaf",
  "review": {
    "reason": "样本均询问传统服饰使用的材料，具体措辞不改变回答目标。",
    "object": "服饰制作材料",
    "action": "查询材料",
    "semantic_clear": true,
    "verdict": "consistent"
  },
  "members": [
    {
      "text": "What is traditional clothing made of?"
    },
    {
      "text": "Which fabric is used for traditional clothing?"
    },
    {
      "text": "Name the materials used in traditional outfits."
    },
    {
      "text": "What are traditional garments made from?"
    },
    {
      "text": "Explain the fabric used in traditional dress."
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "样本均询问传统服饰使用的材料，具体措辞不改变回答目标。",
  "description": "回答传统服饰使用的制作材料",
  "label": "服饰制作材料查询",
  "semantic_issue": "none",
  "semantic_clear": true
}
```


### 9.4 父节点审核：完整输入与输出

父节点使用 [NODE_PROMPT](../../prompts/defaults/hierarchy_review/NODE_PROMPT.txt)，kind=parent。父节点在子节点审核完成后请求，children 是已审核的名称、定义、理由与叶子数量；group 只保留共同父节点，不合并叶子。这里是仅有服饰名称和材料两个叶子的独立演示树，不是主线含饮食叶子的完整树。

**完整输入 JSON（模拟值）：**

```json
{
  "node_id": "merge:20",
  "kind": "parent",
  "children": [
    {
      "node_id": "topic:10",
      "label": "传统服饰名称查询",
      "description": "回答传统服饰的名称",
      "reason": "样本明确查询传统服饰名称。",
      "leaf_count": 1
    },
    {
      "node_id": "topic:12",
      "label": "服饰制作材料查询",
      "description": "回答传统服饰使用的制作材料",
      "reason": "样本均询问传统服饰使用的材料，具体措辞不改变回答目标。",
      "leaf_count": 1
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "子主题共同围绕传统服饰，保留名称与材料的独立叶子。",
  "description": "传统服饰名称与制作材料相关知识",
  "label": "服饰知识",
  "decision": "group"
}
```

### 9.5 从同一棵树派生查找适配表：完整本地输入与输出

输入是 derive_taxonomy(data) 使用的一份完整可执行数据对象，candidate_taxonomy.version 为版本底稿。输出列出该函数写入的三个字段，其他输入字段保留不变。结果由真实函数生成，无 LLM 请求。适配表的 domain 只是祖先路径的兼容存储字段，不是另一套领域分类。

**完整输入 JSON（模拟值）：**

```json
{
  "run_id": "example_run",
  "topic_hierarchy": {
    "version": 1,
    "source": "BERTopic+LLM",
    "representation": "reviewed_c_tf_idf",
    "linkage": "average",
    "status": "ready",
    "message": "保留审核通过的父主题；不合理的合并已展开为独立分支",
    "nodes": [
      {
        "node_id": "topic:10",
        "topic_id": 10,
        "label": "传统服饰名称查询",
        "keywords": [
          "clothing",
          "name"
        ],
        "children": [],
        "is_leaf": true,
        "distance": null,
        "parent_id": "merge:20",
        "depth": 1,
        "leaf_topic_ids": [
          10
        ],
        "description": "回答传统服饰的名称",
        "review_reason": "样本明确查询传统服饰名称。",
        "semantic_status": "approved",
        "intent_count": 1,
        "unique_text_count": 1,
        "row_count": 1
      },
      {
        "node_id": "topic:12",
        "topic_id": 12,
        "label": "服饰制作材料查询",
        "keywords": [
          "clothing",
          "fabric"
        ],
        "children": [],
        "is_leaf": true,
        "distance": null,
        "parent_id": "merge:20",
        "depth": 1,
        "leaf_topic_ids": [
          12
        ],
        "description": "回答传统服饰使用的制作材料",
        "review_reason": "样本均询问传统服饰使用的材料，具体措辞不改变回答目标。",
        "semantic_status": "approved",
        "intent_count": 5,
        "unique_text_count": 5,
        "row_count": 5
      },
      {
        "node_id": "merge:20",
        "topic_id": null,
        "label": "服饰知识",
        "keywords": [
          "clothing"
        ],
        "children": [
          "topic:10",
          "topic:12"
        ],
        "is_leaf": false,
        "distance": 0.2,
        "parent_id": null,
        "depth": 0,
        "leaf_topic_ids": [
          10,
          12
        ],
        "description": "传统服饰名称与制作材料相关知识",
        "review_reason": "子主题共同围绕传统服饰，保留名称与材料的独立叶子。",
        "semantic_status": "approved",
        "intent_count": 6,
        "unique_text_count": 6,
        "row_count": 6
      }
    ],
    "root_ids": [
      "merge:20"
    ],
    "max_depth": 2,
    "leaf_count": 2
  },
  "clusters": [
    {
      "cluster_id": 10,
      "keywords": [
        "clothing",
        "name"
      ],
      "sample_count": 1,
      "unique_text_count": 1,
      "review_samples": [
        {
          "intent_id": "demo_clothing:i0",
          "text": "Name the traditional clothing in eastern Algeria",
          "span": "Name the traditional clothing in eastern Algeria",
          "raw_text": "Name the traditional clothing in eastern Algeria"
        }
      ],
      "reviewed_ids": [
        "demo_clothing:i0"
      ],
      "review_scope": "all_unique_texts",
      "review": {
        "verdict": "consistent",
        "semantic_clear": true,
        "object": "传统服饰",
        "action": "查询名称",
        "reason": "样本明确查询传统服饰名称。",
        "split_groups": [],
        "boundary_ids": []
      },
      "hierarchy_eligible": true,
      "topic_pending": false
    },
    {
      "cluster_id": 12,
      "keywords": [
        "clothing",
        "fabric"
      ],
      "sample_count": 5,
      "unique_text_count": 5,
      "review_samples": [
        {
          "intent_id": "demo_material_1:i0",
          "text": "What is traditional clothing made of?",
          "span": "What is traditional clothing made of?",
          "raw_text": "What is traditional clothing made of?"
        },
        {
          "intent_id": "demo_material_2:i0",
          "text": "Which fabric is used for traditional clothing?",
          "span": "Which fabric is used for traditional clothing?",
          "raw_text": "Which fabric is used for traditional clothing?"
        },
        {
          "intent_id": "demo_material_3:i0",
          "text": "Name the materials used in traditional outfits.",
          "span": "Name the materials used in traditional outfits.",
          "raw_text": "Name the materials used in traditional outfits."
        },
        {
          "intent_id": "demo_material_4:i0",
          "text": "What are traditional garments made from?",
          "span": "What are traditional garments made from?",
          "raw_text": "What are traditional garments made from?"
        },
        {
          "intent_id": "demo_material_5:i0",
          "text": "Explain the fabric used in traditional dress.",
          "span": "Explain the fabric used in traditional dress.",
          "raw_text": "Explain the fabric used in traditional dress."
        }
      ],
      "reviewed_ids": [
        "demo_material_1:i0",
        "demo_material_2:i0",
        "demo_material_3:i0",
        "demo_material_4:i0",
        "demo_material_5:i0"
      ],
      "review_scope": "all_unique_texts",
      "review": {
        "verdict": "consistent",
        "semantic_clear": true,
        "object": "服饰制作材料",
        "action": "查询材料",
        "reason": "样本均询问传统服饰使用的材料，具体措辞不改变回答目标。",
        "split_groups": [],
        "boundary_ids": []
      },
      "hierarchy_eligible": true,
      "topic_pending": false
    }
  ],
  "cluster_task_ids": {
    "10": "T1",
    "12": "T3"
  },
  "candidate_taxonomy": {
    "version": 2
  }
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "candidate_taxonomy": {
    "version": 2,
    "domains": [
      {
        "name": "服饰知识",
        "description": "传统服饰名称与制作材料相关知识"
      }
    ],
    "tasks": [
      {
        "task_id": "T1",
        "domain": "服饰知识",
        "object": "传统服饰",
        "action": "查询名称",
        "description": "回答传统服饰的名称",
        "positive_examples": [
          "Name the traditional clothing in eastern Algeria"
        ]
      },
      {
        "task_id": "T3",
        "domain": "服饰知识",
        "object": "服饰制作材料",
        "action": "查询材料",
        "description": "回答传统服饰使用的制作材料",
        "positive_examples": [
          "What is traditional clothing made of?",
          "Which fabric is used for traditional clothing?",
          "Name the materials used in traditional outfits.",
          "What are traditional garments made from?",
          "Explain the fabric used in traditional dress."
        ]
      }
    ]
  },
  "cluster_task_ids": {
    "10": "T1",
    "12": "T3"
  },
  "task_topic_nodes": {
    "T1": "topic:10",
    "T3": "topic:12"
  }
}
```


### 9.6 叶子实例泛化与归并：完整输入与输出

**先判断能否复用整轮完成状态：** 体系、成员、冲突、模型、Prompt、Embedding 设置和候选策略都未变化，且上次已处理完、无错误或延后时，记录 `unchanged_completed_pass`，在向量计算前直接跳过；此分支 LLM=0、Embedding=0。未满足整轮复用时才生成候选，再检查候选对缓存。真实合并、预算不足、共享叶子延后或错误都不能直接视为已处理完。下面的 JSON 是未命中缓存、实际审核一对候选时的请求，不表示每轮必定发生这些调用。

本阶段会实际迁移成员、合并重复叶子。初版命名后执行一次，继续迭代前执行准备归并，每轮末尾再执行；与第 6 章只给建议、第 9.4 节只保留父节点不同。候选来自全局中心近邻（默认每叶 Top 3、相似度至少 0.75），以及最终消歧的冲突任务对；冲突对优先且不受相似度门槛限制。

每侧最多 8 条不同文本、最多 8 条冲突文本；下面为独立双叶模拟示例。先调用 [MERGE_PROMPT](../../prompts/defaults/leaf_merge/MERGE_PROMPT.txt)。

**完整输入 JSON（模拟值）：**

```json
{
  "left": {
    "task_id": "T_clothing_a",
    "label": "传统服饰材料查询",
    "object": "传统服饰",
    "action": "查询材料",
    "description": "询问传统服饰的制作材料",
    "boundary_notes": [],
    "members": [
      {
        "text": "What is traditional clothing made of?"
      }
    ]
  },
  "right": {
    "task_id": "T_clothing_b",
    "label": "传统服装材质查询",
    "object": "传统服装",
    "action": "询问材质",
    "description": "询问传统服装的制作材质",
    "boundary_notes": [],
    "members": [
      {
        "text": "Which fabric is used for traditional clothes?"
      }
    ]
  },
  "conflict_texts": []
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "两侧都要求回答服饰制作材料，服装和服饰是同义对象，地区差异不改变履约。",
  "description": "回答传统服饰使用的制作材料，不回答名称或穿着场合。",
  "label": "传统服饰制作材料查询",
  "object": "传统服饰",
  "action": "查询材料",
  "decision": "merge"
}
```

仅当 decision=merge 时，再调用 [VERIFY_PROMPT](../../prompts/defaults/leaf_merge/VERIFY_PROMPT.txt) 验证给定样本能否被通用定义覆盖。

**完整输入 JSON（模拟值）：**

```json
{
  "definition": {
    "reason": "两侧都要求回答服饰制作材料，服装和服饰是同义对象，地区差异不改变履约。",
    "description": "回答传统服饰使用的制作材料，不回答名称或穿着场合。",
    "label": "传统服饰制作材料查询",
    "object": "传统服饰",
    "action": "查询材料",
    "decision": "merge"
  },
  "left": [
    {
      "text": "What is traditional clothing made of?"
    }
  ],
  "right": [
    {
      "text": "Which fabric is used for traditional clothes?"
    }
  ]
}
```

**完整输出 JSON（模拟值）：**

```json
{
  "reason": "两侧给定样本都可由通用材料查询定义覆盖。",
  "rejected_texts": [],
  "valid": true
}
```

验证通过才提交；同批只提交互不共享叶子的合并，不把 A-B 和 B-C 直接传递合并。默认每次最多审核 64 个新候选对、合并 16 对，超限或重叠候选延后。完整成员、定义、样本、Prompt 和模型身份未变化时复用候选对审核。成功后保留成员较多的规范叶子 ID，记录旧 ID 映射；词面更改不会代替验证。详见[叶子归并指南](UNSUPERVISED_DISCOVERY.md#叶子实例泛化与实际归并)。

## 10. 第二轮：重复第 8、9 章流程

本章按显式选择 `iteration_scope: all` 描述。如果选择默认 `unresolved_only`，从首轮起对噪声及普通待确认簇等未确认归属的意图执行第 8 章；已确认的归属继续复用，新叶子可采用已审核簇的成员关系。叶子定义变化或被删除时补做受影响意图的分类。例如 25 条意图中仅 3 条没有确认归属、其余叶子未失效，则语义字段齐全时补解析为 0 次，逐候选及消歧仅针对这 3 条，不是 25 条；簇与层级审核另外按实际执行量统计。详见[增量迭代配置](UNSUPERVISED_DISCOVERY.md#增量迭代处理未确认归属的意图)。

使用第 9 章更新后的 v2 树和叶子适配表，重复第 8 章的“语义解析 → 向量召回 → 逐候选判断 → 最终消歧”，原子文本和编号不变；对象和动作复用初始解析；只有缺失时补解析，参数不额外请求。输入输出格式与第 8 章一致，不再重复列出 JSON。

本例 v2 新增材料叶子 T3；全量模式会重新匹配 I1。默认增量模式可采用新叶子的已审核成员关系，记录为 audited_cluster，而不虚构逐意图模型分数；存在未解决冲突时仍进入匹配。第一轮历史记录保持原样。I0 仍匹配 T1，展示路径随更新后的树取得。

若仍有未覆盖意图或成员归属发生变化，继续第 9 章聚类、成员复核与树重建；否则跳过这些步骤，叶子归并先检查已完成状态，必要时才生成候选和请求，再进入第 11 章。存在超预算或共享叶子的延后归并时不能报告 no_changes 收敛。每轮请求量仍按第 8、9 章实际执行的步骤统计，不另设“第二轮专用”请求。

## 11. 停止与最终验证

比较当前叶子归属以及树节点的名称、定义、子节点和审核状态。未变化时停止；重复历史状态时报告循环；达到最大轮数时停止但不宣称收敛。模型请求失败不会被当成新意图或“已收敛”。未提交的重建异常会回退到上一版已提交层级。

显式选择全量模式（iteration_scope=all）时，若最后一次更新改变了任务适配表，使用最终版本重新分类全部原子意图。若已使用最终版本完成分类，则复用，不重复请求。

**请求次数与规模：** 本地停止判断 0 次。若 25 条意图需要最终验证，次数为 Σ(P_i+K_i+F_i)，P_i 为缺字段补解析次数；新数据 P_i=0、K≤3 时上限 100 次。版本未变且已有有效结果则 0 次。对应 Prompt 与第 8 章相同；不存在额外一次总审核调用。

增量模式下，最终验证采用同样的噪声重试及叶子失效检查，不再全量重新调用。设符合重试或失效条件的意图为 M 条，则基础分类调用按这 M 条的 Σ(P_i+K_i+F_i)，其中新数据 P_i=0 计算，其余复用或采用已审核成员关系；并在记录中区分 `llm_classification` 与 `audited_cluster` 来源。

最终归属必须通过任务存在、版本、指纹、原句关联、原子文本及重复记录检查，才用于导出。任务映射回同一套审核后的叶子与祖先，不能输出独立领域树的标签。

### 11.1 继续迭代与从断点恢复

普通继续迭代从新运行第 1 轮计数，复用规则仍按第 8、9 章执行。失败任务若使用 --resume-state，则继承快照中的有效已完成轮次，从下一轮继续；带 processing_errors 的尾轮重试。恢复时 --iteration-rounds 是总轮数上限，例如已完成 2 轮、上限 3，则从第 3 轮开始，而不是额外执行 3 轮。达到上限仍会按条件检查准备步骤及最终验证。

--resume-trace 是独立的成功请求回放：只有请求身份匹配且旧成功响应通过当前结构校验才命中；不是按句子相同就无条件复用，也不回放 Embedding。回放或本地复用不产生新服务请求。详细操作、追加用例的入口限制和七类复用条件见 [继续迭代与复用指南](CONTINUATION_AND_REUSE.md)。

## 12. 最终导出：原句及审核后各层主题

最终导出统一来自审核后的 topic_hierarchy，不再提供领域两层导出模式。噪声若最终可靠匹配某个已审核叶子，也可沿该叶子的祖先路径导出；未确定归属不强行填标签。

对 r1 的两个意图，导出按层去重共同祖先：

```json
{
  "records": [
    {
      "raw_text": "Name the traditional clothing in eastern Algeria and explain what it is made of.",
      "topics_by_level": [
        {"level":1,"topics":["传统文化知识"]},
        {"level":2,"topics":["服饰知识"]},
        {"level":3,"topics":["传统服饰名称查询","服饰制作材料查询"]}
      ]
    }
  ]
}
```

最终 JSON/XLSX 仍只包含原句和各层主题。不同分支深度可不同，不生成虚构父主题补齐；全局有更深分支时，较短路径对应的后续层可以为空。一个原句拆出多个意图，可以对应多条分支、同层多个标签。

**本阶段请求次数：** 文件生成、去重与覆盖率统计都是本地计算，LLM=0、Embedding=0。前面各阶段的请求分别见对应章节，层级审核不能再按“全簇摘要集中 1 次”计费。

独立领域归纳、领域召回及两层导出代码已删除。已有原生树可读取和重新导出，但不会从旧领域表虚构新的层级；要补齐命名与父级审核，需要重跑层级阶段。切换到新逻辑不会自动改写历史报告，需重跑层级审核；要补充新发现能力则从聚类或迭代阶段生成派生版本。

## 13. 文件与页面如何核对

- `stages/cluster.json`：初始叶子及原生树。
- `stages/review.json`：一致性审核、局部细分及更新后的原生树。
- `stages/domains.json`：阶段 ID 沿用 domains，内容为逐层命名与合并审核；包含原生 bertopic_hierarchy、审核后 topic_hierarchy、hierarchy_review 以及派生查找表。
- `stages/iterate.json`：每轮匹配记录、树前后快照、成员路径及最终验证。
- `leaf_merging.passes` / `leaf_merging.cache`：归并阶段完成复用、逐对提案验证、预算延后及旧 ID 映射；`unchanged_completed_pass` 表示整轮无需请求。
- 模型请求记录中的 `replayed`：续跑时使用已成功响应的回放命中，不等于再次请求服务。
- `process/discovery.json` 与 `process/review.xlsx`：完整过程与节点审核理由；不是最终业务交付文件。
- `results/sentence_results.json` / `.xlsx`：原句与各层主题。

运行任务仍按迭代轮次折叠，每个阶段可查看请求和 Token。树页面展示 LLM 名称、定义与审核理由，最终导出使用同一套路径。

体系管理中的 Domain/Task 是树的查找适配表，可修改叶子说明、示例和别名；新增/删除主题、修改对象动作或改变祖先结构需通过簇审核及层级阶段执行，避免树和适配表相互冲突。

## 14. 四种“合并”的区别

| 名称 | 是否改变叶子成员 | 当前行为 |
|---|---|---|
| 跨簇边界复核 | 否 | 提供两个叶子是否重复的建议 |
| 父主题 group | 否 | 为子主题保留一个共同上层节点 |
| 叶子实例泛化与归并 | 是 | 两阶段审核通过后，迁移成员、保留规范叶子 ID，并更新旧 ID 映射与树 |
| 迭代匹配与重新聚类 | 是 | 调整叶子归属，发现新叶子，重新审核后重建树 |

领域不再是 BERTopic 之外的另一套生成结构；语义解析不再单独判断领域，匹配叶子后从树取得祖先路径。
