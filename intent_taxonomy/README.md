# Intent Taxonomy

从原始语料发现原子意图，通过 BERTopic 聚类和 LLM 审核生成多层主题树，再迭代匹配未覆盖意图。

每次运行拥有独立的分类体系，保存在流程阶段结果中；无需预制或全局分类文件。

## 启动

```bash
uv sync
uv run python bootstrap.py
uv run python web_server.py
```

在页面“模型配置”设置对话与 Embedding 服务，并使用检测按钮验证。进入“分类与迭代”，导入 Excel 或 CSV，选择工作表、文本列和迭代设置后运行。

命令行也可运行：

```bash
uv run python main.py discover --input input.xlsx --text-column 用户提问 --iteration-rounds 3
uv run python main.py discover --input input.csv --text-column text --iteration-scope unresolved_only
uv run python main.py discover --source data/reports/<run_id>/stages/domains.json --stage iterate --iteration-rounds 3
```

## 流程与结果

原句 → 原子意图分析 → Embedding / BERTopic → 簇一致性审核与混合簇细分 → 重建主题树 → 自下而上逐层命名审核 → 叶子主题匹配及迭代 → 导出。

- 流程总览展示整体覆盖、审核状态和迭代历史；阶段结果切换对应的数据快照。
- 每次流程保存在 `data/reports/<run_id>/`，包含 `workflow.json`、`stages/`、`process/`、`results/` 和模型请求记录 `requests/`。
- 最终 `sentence_results.xlsx`、`sentence_results.json` 保存原始句子与各层主题，不包含中间参数和审核过程。
- 模型请求支持阶段审查、父子簇递归审查、输入输出展开及分页；任务历史重启后保留。
- Prompt 正文位于 `prompts/defaults/`，页面自定义保存到 `config/prompts/`。
- 初始提取同时生成对象、动作；默认 `unresolved_only` 从首轮起匹配噪声、普通待确认簇及其他未确认归属，继承有效的已审核归属，`all` 则重新检查全部归属。
- 手动整理层级会生成派生流程，不覆盖来源结果。

## 文档

- [完整流程与配置](docs/guides/UNSUPERVISED_DISCOVERY.md)
- [各阶段完整示例与请求次数](docs/guides/WORKFLOW_EXAMPLE.md)
- [继续迭代、失败续跑与结果复用](docs/guides/CONTINUATION_AND_REUSE.md)
- [Prompt 管理与请求审查](docs/guides/PROMPT_MANAGEMENT.md)
- [文件、工作表与文本列](docs/guides/INPUT_FILES.md)
- [模型参数与 Token 统计](docs/guides/MODEL_REQUESTS.md)
- [升级与历史数据](docs/guides/MIGRATION.md)
- [源码目录结构](docs/development/DIRECTORY_STRUCTURE.md)
- [全部文档](docs/README.md)

## 开发检查

```bash
uv run python -m pytest -q
```

分类、聚类和审核的过程数据均归属所选流程。共享检查点和向量缓存按实际输入、模型和体系内容隔离，不代表不同流程共享分类标签。
