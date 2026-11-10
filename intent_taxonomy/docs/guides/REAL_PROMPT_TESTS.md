# 真实报告 Prompt 手动测试集

来源：`20261104_222248_4b638fba`、`20261105_195042_057d470e`。共30条，覆盖15个流程Prompt，每项2条；当前默认使用此集。17项人工示例仍可通过 `--dataset examples` 选择。

16条直接重放历史请求的真实输入，保留请求ID、文件行号、输入校验和及历史响应；14条从真实叶子、原子意图和父主题组装，明确标注为新测试，不冒充历史已执行请求。比较来源记录保留原句、原子文本、两版簇ID及最新主题路径；最新路径仅供对照，不冒充历史请求当时的路径。

重点样本：阿联酋/阿曼官方宗教（r344:i0、r1411:i0）；diwaniya社会角色与主要目的（同一原句r21:i0、r21:i1）；沙特女性驾驶和旅行法律地位（r14:i0）；也门弯刀材料（r27:i0）。另保留真实混合小簇、正常簇、匹配/拒绝、父级保留/展开等对照。

历史结果并非标准答案，真实用例的语义判定统一标记为 `needs_review`，结构或ID检查失败才标为error；请对照旧响应和真实文本审核。共用父级、新归并提议没有历史模型答案，输入是实际结果，挂载和统一定义只是待审假设。

Skill路由的两个Prompt在这两次运行中无真实调用，所以真实集不包含它们；不要为了凑满17项而混入虚构输入。S4映射使用真实原子文本和完整项目场景目录，尚需人工核对具体场景选择。

```bash
# 列出真实用例，0次请求
.venv/bin/python scripts/manual_prompt_suite.py --list
# 预览一批，0次
.venv/bin/python scripts/manual_prompt_suite.py --batch 1
# 真正执行一批，3次，不重试
.venv/bin/python scripts/manual_prompt_suite.py --batch 1 --run
# 测试真实叶子关系两例，2次
.venv/bin/python scripts/manual_prompt_suite.py --prompt leaf_merge.MERGE_PROMPT --examples 2 --run
```

全部真实用例共30次请求，分10批；仍建议逐批人工执行。继续使用`.env`中的端点、模型、Key及思考开关，不自动修改开关以绕过服务端限制。请求前打印输入字符数；归并例只含两侧叶子及祖先路径，复核额外提供选中祖先的定义，不含全树或全部父主题。所有输出保存在`data/reports/prompt_tests/`，不改变原流程。历史输入重放是检查新Prompt响应，不是重跑原版本完整流程。

## 挑选目录

| Prompt | 用例 | 来源类型 | 关注点 |
| --- | --- | --- | --- |
| `atomic_discovery.EXTRACT_PROMPT` | `real_multi` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `atomic_discovery.EXTRACT_PROMPT` | `real_single` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `atomic_discovery.PAIR_PROMPT` | `real_keep` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `atomic_discovery.PAIR_PROMPT` | `real_merge` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `atomic_discovery.REVIEW_PROMPT` | `real_consistent` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `atomic_discovery.REVIEW_PROMPT` | `real_mixed` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `classifier.ATOMIC_CLASSIFY_SYSTEM` | `real_material` | assembled_from_real_results | 真实原子意图补解析，核对对象动作是否合理 |
| `classifier.ATOMIC_CLASSIFY_SYSTEM` | `real_singleton` | assembled_from_real_results | 真实原子意图补解析，核对对象动作是否合理 |
| `classifier.FINAL_MATCH_SYSTEM` | `real_ambiguous` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `classifier.FINAL_MATCH_SYSTEM` | `real_known` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `classifier.PAIRWISE_SYSTEM` | `real_False` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `classifier.PAIRWISE_SYSTEM` | `real_True` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `cluster_refinement.SPLIT_PROMPT` | `real_small_cluster` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `cluster_refinement.SPLIT_PROMPT` | `real_two_members` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `hierarchy_review.LEAF_PROMPT` | `real_False` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `hierarchy_review.LEAF_PROMPT` | `real_True` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `hierarchy_review.NODE_PROMPT` | `real_group` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `hierarchy_review.NODE_PROMPT` | `real_separate` | historical_request | 复核真实请求，历史响应仅作对照，不作正确答案 |
| `leaf_merge.MERGE_PROMPT` | `real_diwaniya` | assembled_from_real_results | 同一句拆出社会角色和主要目的：判断近义重复还是相关叶子 |
| `leaf_merge.MERGE_PROMPT` | `real_religions` | assembled_from_real_results | 两个国家官方宗教：判断统一叶子或共用祖先，检查重复类别 |
| `leaf_merge.VERIFY_PROMPT` | `real_diwaniya` | assembled_from_real_results | 真实已有定义作为待审假设，不预设验证通过 |
| `leaf_merge.VERIFY_PROMPT` | `real_religions` | assembled_from_real_results | 真实已有定义作为待审假设，不预设验证通过 |
| `parent_regroup.PROPOSE` | `real_diwaniya` | assembled_from_real_results | 真实叶子与真实父主题：判断挂入或保持独立 |
| `parent_regroup.PROPOSE` | `real_religion` | assembled_from_real_results | 真实叶子与真实父主题：判断挂入或保持独立 |
| `parent_regroup.VERIFY` | `real_diwaniya` | assembled_from_real_results | 待审挂载假设，不将候选召回当作正确归属 |
| `parent_regroup.VERIFY` | `real_religion` | assembled_from_real_results | 待审挂载假设，不将候选召回当作正确归属 |
| `scene_mapping.MAP_PROMPT` | `real_material` | assembled_from_real_results | 从真实完整S4目录选择；具体正确性人工核对 |
| `scene_mapping.MAP_PROMPT` | `real_singleton` | assembled_from_real_results | 从真实完整S4目录选择；具体正确性人工核对 |
| `scene_mapping.NAME_PROMPT` | `real_material` | assembled_from_real_results | 真实原子意图生成简短任务名称 |
| `scene_mapping.NAME_PROMPT` | `real_singleton` | assembled_from_real_results | 真实原子意图生成简短任务名称 |


## 2026-11-06 路径上下文优化

正式请求和本测试集均不再发送 `hierarchy_context` 或全量 `existing_parents`。归并提议只含两侧成员、定义和叶子祖先路径；复核只带提议、两侧成员路径及选中祖先的定义。跨簇成员仅保留intent_id/text。历史跨簇请求按此结构投影，source标为projected_historical_request；来源中的校验和仍对应原请求输入，不冒充投影后的输入校验和。

输入字符变化（不是Token，且不含system模板）：

| 用例 | 修改前 | 修改后 |
| --- | ---: | ---: |
| 归并diwaniya | 114,350 | 994 |
| 归并religions | 114,929 | 1,572 |
| 复核diwaniya | 81,423 | 935 |
| 复核religions | 81,994 | 1,363 |
| 跨簇keep | 4,456 | 1,790 |
| 跨簇merge | 3,026 | 1,389 |

旧测试输出和原始流程报告保留，不会自动更新；重新手动测试时才使用这些输入。
