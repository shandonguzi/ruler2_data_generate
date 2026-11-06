# 输入文件与文本列

“分类与迭代”支持 `.xlsx`、`.xls`、`.csv`。第一行为列名，每个后续数据行对应一条原始语料；其他编号或标签列不参与分类。

选择文件后页面自动读取工作表、列名和前 5 条预览。Excel 可切换工作表，再选择文本列；CSV 直接选择文本列。处理条数留空时读取全部数据行，预览不会截断正式输入。空文件、重复表头或不存在的列会明确报错。

```bash
python main.py discover --input questions.xlsx --sheet 验证集 --text-column 会话内容 --iteration-rounds 3
python main.py discover --input questions.csv --text-column text --iteration-scope unresolved_only
```

每条原句可拆为多个原子意图，处理过程保留来源行。最终文件包含原始句子及各层主题，见[完整流程](UNSUPERVISED_DISCOVERY.md)。

## 已有结果与新增文件

新建流程用 `--input`；从已有快照执行单阶段用 `--source`。当前 CLI 将二者设为互斥，不能同时传入。Excel 工作表参数为 `--sheet`，文本列参数为 `--text-column`。

继续迭代中的追加用例、失败续跑及参数限制见 [继续迭代与复用指南](CONTINUATION_AND_REUSE.md)。页面虽然已有追加入口，当前命令参数冲突尚未修复，不应把“已有快照 + 新文件”的组合当作已可用 CLI 命令。
