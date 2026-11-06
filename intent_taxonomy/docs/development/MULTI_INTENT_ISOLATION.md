# 多意图隔离

原句仅在原子意图提取时作为整体输入。后续语义解析和任务判断分别处理每个原子意图，避免其他子句的词语影响本意图。

```text
完整原句
  → EXTRACT_PROMPT：提取原子意图、补全指代及共同背景
  → 复用初始提取的对象、动作与参数；缺字段时才用 ATOMIC_CLASSIFY_SYSTEM 补解析
  → 原子文本向量召回当前叶子主题 Top-K
  → 每个候选：PAIRWISE_SYSTEM 判断是否匹配
  → FINAL_MATCH_SYSTEM 最终消歧
```

“打开支付宝，交电费”保留“打开支付宝”和“交电费”两个动作。不同意图不会因为同属一个业务目标而被本地规则合并。

- 原句入口 `IntentClassifier.classify` 使用当前 `atomic_discovery.EXTRACT_PROMPT`，再逐个调用 `classify_atomic`。
- 总流程已经提取了原子意图，迭代时直接调用 `classify_atomic`，不会重新拆句。
- 单意图语义解析仅传入原子文本和已有参数，不召回或预测领域。
- Pairwise 和 Final 仅传入当前意图及候选任务。
- 默认向量召回直接编码独立原子文本，文本不变时跨轮复用。
- 默认 unresolved_only 只匹配未确认、冲突或失效归属；有效已审核归属直接继承，来源标记 audited_cluster 或 retained_assignment。
- 最终消歧的多个冲突候选写入 conflicting_task_ids，交给叶子归并比较，不据此强行选定任务。

实现地址：[classifier.py](../../intent_engine/classification/classifier.py)、[vector_recall.py](../../intent_engine/classification/vector_recall.py)。

分类 checkpoint 使用 `semanticv16`，包含输入文本和顺序、taxonomy 内容、模型、Embedding 与分类配置的指纹。请求失败的结果不写入 checkpoint，下次运行重试。旧分类链路的 checkpoint 不再复用。
