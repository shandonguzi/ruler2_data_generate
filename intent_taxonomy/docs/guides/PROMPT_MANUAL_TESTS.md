> 当前本地测试配置已切换到 DeepSeek：`.env` 使用 `EVAL_VLLM_BASE_URL`、`EVAL_VLLM_MODEL`、`EVAL_VLLM_API_KEY`；同时兼容旧的 INTENT_MANUAL_TEST_* 变量。DeepSeek 关闭思考使用 `thinking.type=disabled`，不发送 DashScope 的 enable_thinking 字段。

> 当前默认测试集已切换为真实报告挑选的30条，见 [真实测试集](REAL_PROMPT_TESTS.md)。以下34条人工示例请添加 `--dataset examples` 使用。

# Prompt 手动小批量测试

覆盖全部17个已注册Prompt（含可选Skill路由），各2条人工挑选的英文代表用例，共34条。用例用于检查判断边界，不作为Prompt内的固定规则。通过自动检查不等于模型准确率达标，仍需人工审核理由。

## 配置和请求数量

配置自动读取项目根目录 `.env`。设置 `INTENT_MANUAL_TEST_ENABLE_THINKING=false`，请求显式传入 `extra_body={"enable_thinking": false}`。

默认仅预览，实际0次；加 `--run` 才请求。每个用例1次，不失败重试，每次最多3次。全覆盖需34次，默认分12批，无Embedding或其他隐式请求。

```bash
# 列出用例，0次
.venv/bin/python scripts/manual_prompt_suite.py --list
# 预览第1批，0次
.venv/bin/python scripts/manual_prompt_suite.py --batch 1
# 手动执行第1批，3次
.venv/bin/python scripts/manual_prompt_suite.py --batch 1 --run
# 完整分组的保留/拆分两例，2次
.venv/bin/python scripts/manual_prompt_suite.py --prompt cluster_refinement.SPLIT_PROMPT --examples 2 --run
```

使用当前生效的Prompt（含页面覆盖版本）。结果保存到 `data/reports/prompt_tests/<时间>_<ID>.json`，包含输入、实际模板、原始输出、解析结果、预期不符字段、Token及耗时，不记录API Key。失败也保存，不自动补发请求。

## 用例目录

| Prompt | 用例 | 检查重点 | 请求次数 |
| --- | --- | --- | --- |
| `atomic_discovery.EXTRACT_PROMPT` | `single` | 单意图不补造额外动作 | 1 |
| `atomic_discovery.EXTRACT_PROMPT` | `multi` | 显式打开与缴费分别保留 | 1 |
| `atomic_discovery.REVIEW_PROMPT` | `same` | 同类请求可统一 | 1 |
| `atomic_discovery.REVIEW_PROMPT` | `mixed` | 观察模型是否区分材料与过程 | 1 |
| `atomic_discovery.PAIR_PROMPT` | `same` | 检查证据引用和跨簇关系 | 1 |
| `atomic_discovery.PAIR_PROMPT` | `different` | 检查证据引用和跨簇关系 | 1 |
| `cluster_refinement.SPLIT_PROMPT` | `retain` | 允许推翻旧mixed结论，不强制拆分 | 1 |
| `cluster_refinement.SPLIT_PROMPT` | `split` | 允许推翻旧mixed结论，不强制拆分 | 1 |
| `hierarchy_review.LEAF_PROMPT` | `singleton` | 单样本语义明确仍可命名 | 1 |
| `hierarchy_review.LEAF_PROMPT` | `missing_context` | 缺少上下文不可猜测 | 1 |
| `hierarchy_review.NODE_PROMPT` | `related` | 共用父级，不合并叶子 | 1 |
| `hierarchy_review.NODE_PROMPT` | `unrelated` | 避免空泛共同父级 | 1 |
| `leaf_merge.MERGE_PROMPT` | `normalize` | 近义描述统一 | 1 |
| `leaf_merge.MERGE_PROMPT` | `ancestor` | 相关但不同的叶子共用祖先 | 1 |
| `leaf_merge.VERIFY_PROMPT` | `valid_parent` | 复核共同祖先覆盖双方 | 1 |
| `leaf_merge.VERIFY_PROMPT` | `wrong_merge` | 拒绝不能覆盖制作过程的统一叶子 | 1 |
| `parent_regroup.PROPOSE` | `attach` | 复用已有父主题 | 1 |
| `parent_regroup.PROPOSE` | `independent` | 无合理祖先可保持独立 | 1 |
| `parent_regroup.VERIFY` | `valid` | 独立审核挂载，不能盲信提议 | 1 |
| `parent_regroup.VERIFY` | `invalid` | 独立审核挂载，不能盲信提议 | 1 |
| `classifier.ATOMIC_CLASSIFY_SYSTEM` | `knowledge` | 知识问答是有效语义 | 1 |
| `classifier.ATOMIC_CLASSIFY_SYSTEM` | `operation` | 解析动作并保留时间 | 1 |
| `classifier.PAIRWISE_SYSTEM` | `match` | 候选任务匹配 | 1 |
| `classifier.PAIRWISE_SYSTEM` | `reject` | 不强行匹配无关任务 | 1 |
| `classifier.FINAL_MATCH_SYSTEM` | `known` | 只选候选中的任务 | 1 |
| `classifier.FINAL_MATCH_SYSTEM` | `uncovered` | 无候选时返回未覆盖 | 1 |
| `scene_mapping.MAP_PROMPT` | `knowledge` | 完整36项场景中选真实ID | 1 |
| `scene_mapping.MAP_PROMPT` | `alarm` | 操作类场景匹配，人工核对具体选择 | 1 |
| `scene_mapping.NAME_PROMPT` | `knowledge` | 只生成任务名称，不重新选择S4 | 1 |
| `scene_mapping.NAME_PROMPT` | `alarm` | 只生成任务名称，不重新选择S4 | 1 |
| `skill_router.JUDGE_SYSTEM` | `match` | 可选Skill路由，未启用时也可单测 | 1 |
| `skill_router.JUDGE_SYSTEM` | `reject` | 可选Skill路由，未启用时也可单测 | 1 |
| `skill_router.FALLBACK_SYSTEM` | `match` | 可选Skill路由，未启用时也可单测 | 1 |
| `skill_router.FALLBACK_SYSTEM` | `reject` | 可选Skill路由，未启用时也可单测 | 1 |

场景关联使用完整S4目录；自动检查ID有效性，具体选择需要人工核对。名称和语义解析不强制固定措辞。Skill路由为可选功能，使用小型示例Registry。此工具测试单次Prompt响应，不修改已有运行结果，也不替代端到端验收。
