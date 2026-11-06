# 目录结构

项目保留可直接运行的 Python 包布局，不需要先安装为 wheel。

| 位置 | 职责 |
|---|---|
| `intent_engine/entrypoints/` | CLI 参数与命令调度、工作空间初始化 |
| `intent_engine/web/` | FastAPI 路由、后台任务管理 |
| `intent_engine/atomic/` | BERTopic 原生树与逐层语义审核（hierarchy_review.py）； 原子意图提取、Embedding 接入、BERTopic、局部细分和层级树 |
| `intent_engine/classification/` | 原子意图语义解析、叶子主题召回与匹配；可选 Skill 路由 |
| `intent_engine/workflows/` | 总流程、候选体系迭代、运行清单、产物读取、诊断、删除、可视化数据 |
| `intent_engine/taxonomies/` | 流程分类体系的数据操作与编辑工具 |
| `intent_engine/exporting/` | 过程报告、最终原句结果、语义主题映射 |
| `prompts/` | 模板目录元数据与独立默认 Prompt 文本文件；自定义文件保存在 config/prompts/ |
| `intent_engine/settings/` | YAML 配置、模型设置、Prompt 文件解析和聚类设置 |
| `intent_engine/infrastructure/` | 模型请求、Token 统计、进度协议、输入文件、检查点和通用函数 |
| `intent_engine/models.py` | 各模块共用的 Pydantic 数据模型 |
| `intent_engine/paths.py` | 项目根路径，避免入口文件移动后相对路径失效 |
| `web/index.html` | 桌面控制台页面 |
| `web/static/js/` | 控制台、模型/Prompt/聚类设置、可视化和清理交互 |
| `web/static/css/` | 页面样式 |
| `web/static/favicon.svg` | 页面图标 |
| `scripts/` | Windows / PowerShell / Shell 启动、安装辅助和本地检查脚本 |
| `requirements/` | 基础、Web 和可选聚类依赖清单 |
| `tests/` | 按上述职责分组；集成测试放在 integration |
| `docs/guides/` | 使用说明 |
| `docs/development/` | 开发与协议说明 |
| `docs/reports/` | 样本分析报告 |

## 启动方式

原有命令继续有效：

```bash
python main.py --help
python bootstrap.py
python web_server.py
```

也可以直接运行对应包入口：

```bash
python -m intent_engine.entrypoints.cli --help
python -m intent_engine.entrypoints.bootstrap
python -m intent_engine.web.app
```

根目录 `main.py`、`web_server.py`、`bootstrap.py` 仅负责转发。`web_server:app` 的 ASGI 导入保持可用；旧模块导入 `main.build_parser` 等入口符号继续指向同一个实现，避免维护两套状态。Windows 启动入口 `start_web.bat`、`start_web.ps1`、`setup_windows.bat` 和 `run.sh` 也保留为兼容转发，实际逻辑位于 scripts。直接运行 scripts 下的启动脚本时，会先定位项目根目录。

CLI 默认配置始终定位项目根目录的 `config/config.yaml`；显式传入 `--config` 时按调用者指定的文件读取。Web 子进程仍在项目根目录启动。

## 依赖与前端资源

标准依赖文件为 `requirements/base.txt`、`requirements/web.txt`、`requirements/clustering.txt`。根目录旧文件仅包含 `-r` 引用，旧安装命令继续有效。pyproject 中的依赖声明保留，uv 环境无需重建。

静态资源 URL 使用 `/static/js/…`、`/static/css/…` 和 `/static/favicon.svg`，由 `web/static` 提供；API URL 不变。升级后需重启 Web 服务并刷新页面。

## 源码导入与持久化兼容

内部 Python 导入改成按职责分组的绝对路径，例如：

```python
from intent_engine.atomic.atomic_discovery import run_atomic_discovery
from intent_engine.settings.config import load_config
from intent_engine.workflows.workflow_artifacts import list_workflows
from intent_engine.web.app import app
```

自行编写的扩展若导入旧的扁平源码路径，需要更新对应 import。没有引入全局 import hook 或为每个旧文件保留一套重复实现。

Prompt 保持稳定标识，例如 atomic_discovery.EXTRACT_PROMPT；正文保存在 prompts/defaults/，页面修改保存在 config/prompts/，目录元数据为 prompts/catalog.yaml。Python 仅通过 prompt_ref 引用，运行时按 key 解析，不再从模块扫描正文。详见[文件管理](../guides/PROMPT_MANAGEMENT.md)。

## 数据目录

- `data/reports/<运行编号>/`：stages / process / results 及 workflow.json；兼容旧平铺报告。
- `data/sample/`：用户提供的样本与待分析结果。
- `data/checkpoints/`：共享缓存与检查点。
- `data/uploads/`：页面导入的输入文件。

各流程独立保存分类体系，已移除全局分类文件。输入、样本与已有流程报告保持原位置。

## 验证

```bash
python -m pytest -q
```

可按目录运行某类测试，例如 `python -m pytest tests/workflows -q`。
