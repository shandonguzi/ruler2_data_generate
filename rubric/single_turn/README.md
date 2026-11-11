# 单轮 QA 五维评分

面向单轮 QA 的问句改写质检：读取 Excel 中的原 query、原 answer、新 query，由模型逐行给五维分数和理由，再由程序计算 CRITIC 总分和通过／拒绝结果，写入新的 Excel。

1. 准备 `.xlsx` 文件，第一行为列名。例如：

   | 编号 | 原query | 原answer | 新query | 备注 |
   | --- | --- | --- | --- | --- |
   | 1 | 小明昨天买了什么？ | 一本书。 | 请问小明昨天买了什么东西？ | 样例 |

   三个输入列名可在配置中修改。其他列、行顺序、原有单元格内容、公式、常规格式及其他工作表会保留；模型只接收配置指定的三个文本字段。

2. 将 [config.example.yaml](./config.example.yaml) 复制为 `config.yaml`，填写接口、密钥、模型、输入输出路径和列名：

   ```yaml
   api_url: "https://example.com/v1/chat/completions"
   api_key: "填写你的密钥"
   model: "deepseek-v4.1-flash-1017"

   input: "input.xlsx"
   output: "output/评分结果.xlsx"
   columns:
     original_query: "原query"
     original_answer: "原answer"
     new_query: "新query"
   sheet_name: ""
   ```

   `api_url` 使用完整的 Chat Completions 地址。`api_key` 留空时读取环境变量 `OPENAI_API_KEY`。相对路径以配置文件所在目录为起点；上述输入路径即 `rubric/single_turn/input.xlsx`。`sheet_name` 留空处理第一个工作表，也可填写具体表名。输入和输出必须是不同的 `.xlsx` 文件。

3. 在项目根目录运行（Python 3.11）：

   ```powershell
   python -m pip install -r rubric/single_turn/requirements.txt
   python -m rubric.single_turn.evaluate run
   ```

   也可以省略 `run`。若要先检查列名并生成未评分的 Excel，可运行 `python -m rubric.single_turn.evaluate preview`，此命令不调用模型，写入单独的 `<输出文件名不含扩展名>_预览.xlsx`。`--config` 可指定其他配置文件；`--input`、`--output`、`--model`、`--api-url` 可临时覆盖配置，例如：

   ```powershell
   python -m rubric.single_turn.evaluate run --input "D:\数据\待评分.xlsx" --output "D:\数据\已评分.xlsx"
   ```

输出在原有列的右侧追加八列：**语言自然分、表述清晰分、内部逻辑分、原意忠实分、原答案匹配分、CRITIC总分、评测结果、评分说明**。若原表已有同名列，新列使用 `（2）` 等后缀，原列保留。

五维各打 1～3 分，顺序为语言自然、表述清晰、内部逻辑、原意忠实、原答案匹配。固定 CRITIC 权重依次约为 **0.2148、0.1184、0.1803、0.2646、0.2219**；每维按 `(分数−1)/2` 换算后加权，得到 0～1 总分。**任一维为 1 分直接拒绝；否则总分 ≥ 0.80 通过，低于 0.80 拒绝。**严重语病或歧义使内容维无法判断时，内容分写“无法判定”，总分留空并拒绝。程序使用完整精度的冻结权重，0.80 是项目试验阈值。

整行空白会保留并跳过。缺少必填内容的行标为“输入不完整”，不会调用模型；三列中若使用公式，需有 Excel 保存的计算结果。评分失败时会保存包含已完成结果的 Excel，未处理行标为“未评分”；再次运行相同配置会跳过已成功评分的行。

续跑记录保存在输出文件旁的 `<输出文件名不含扩展名>_records/`，包含 `manifest.json`、`model_scores.jsonl` 和 `attempts.jsonl`。输入数据、列映射、模型或评分规则变化时，请换一个输出文件名，以免混用记录。配置中的密钥不会写入评分结果或记录文件。

当前流程只需要三个输入列，无需预评分、good/bad 标签或审核状态，结果直接输出 Excel。此前的 HTML 报告可继续查看。

也支持直接执行 `python rubric/single_turn/evaluate.py run`。配置默认读取本目录的 `config.yaml`，默认输入及输出分别位于本目录的 `input.xlsx` 和 `output/评分结果.xlsx`。

本目录集中保存原五维评分代码（evaluate.py、critic.py、excel_io.py）、HTML 报告（report.py）、配置示例、CSV 转 TXT 工具及离线测试。评分标准、CRITIC 权重和阈值保持原定义。

运行单轮测试：

```bash
python -m pytest -q rubric/single_turn
```

多轮上下文语料的四维质检与自动修复见 [mtqc](../mtqc/README.md)。
