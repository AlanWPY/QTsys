# QTsys

QTsys 是一个面向量化研究与回测的本地化系统，集成了数据获取、策略回测、因子研究、因子看板、组合分析与新闻跟踪等功能。

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 启动系统

```bash
.\.venv\Scripts\python.exe main.py
```

启动后访问：

- 本地地址：`http://127.0.0.1:8000`
- 局域网地址：`http://0.0.0.0:8000`

### 3. 在 VSCode 内实时预览页面

项目已预置 Live Server 工作区配置，前端可直接在 VSCode 内左右分栏调试：

1. 在 VSCode 中打开本项目根目录 `QTsys`
2. 首次按 `Ctrl+Shift+P`，执行 `Python: Select Interpreter`，选择 `.venv\Scripts\python.exe`
3. 执行任务 `Terminal -> Run Task -> QTsys: 启动后端`
4. 在资源管理器中选中根目录 `index.html`
5. 点击右下角 `Go Live`
6. 按 `Ctrl+Shift+P`，执行 `Simple Browser: Show`
7. 输入 `http://127.0.0.1:5500/index.html`
8. 将 Simple Browser 标签拖到右侧编辑器组，即可左侧看代码、右侧看页面

说明：

- Live Server 端口固定为 `5500`
- `/api` 请求已自动代理到后端 `http://127.0.0.1:8000`
- 若修改前端 `static/index.html`，右侧预览会自动刷新
- 工作区默认解释器为 `.venv\Scripts\python.exe`

### 为什么使用 `.venv`

项目依赖包含 `fastapi`、`sqlalchemy`、`pydantic`、`numpy` 等固定版本。若安装到全局或 Anaconda base 环境，容易出现：

- 缺包，例如 `ModuleNotFoundError: sqlalchemy`
- 版本冲突，影响其他项目
- VSCode 任务、终端、调试器使用了不同解释器

因此本项目默认使用仓库内独立虚拟环境 `.venv`

### 4. 首次使用建议顺序

1. 进入“设置”页配置 `Tushare Token`
2. 如需使用因子看板，配置 MySQL 连接
3. 进入“因子看板”验证数据库连接
4. 选择股票池与时间区间后启动分析

## 核心功能

- 策略管理：创建、编辑、保存与回测交易策略
- AI 策略助手：对话式生成量化策略，并直接保存到策略库
- 回测分析：收益、回撤、风险指标、归因与组合分析
- 因子研究：因子表达式、工作流、Alpha191 模板加载
- 因子看板：Alpha191 批量回测、收益总览、分位收益曲线、因子详情
- 股票池管理：系统股票池 + 自定义股票池
- 数据缓存：本地缓存与 MySQL 缓存协同，支持增量更新
- 新闻模块：新闻抓取、情绪分析与事件跟踪

## 因子看板

因子看板用于评估 Alpha191 因子在真实市场数据上的当前有效性，核心特性包括：

- 使用真实 Tushare 数据，不使用模拟行情
- 支持系统股票池：中证500、沪深300、上证50、中证1000
- 支持自定义股票池：搜索股票、命名保存、重复复用
- 支持自定义回测时间区间
- 支持中途停止分析
- 支持失败因子重试
- 支持结果落库与历史批次复用
- 支持查看因子详情、公式、经济含义、持仓与多分位曲线

详细流程见 `docs/factor_board_guide.md`

## 配置说明

### 基础配置

| 项目 | 作用 | 是否必需 |
|---|---|---|
| `Tushare Token` | 获取行情、指数成分、基础证券数据 | 是 |
| `LLM API Key / Base URL / Model` | AI 策略助手、因子挖掘、新闻分析 | 使用 AI 功能时必需 |
| SQLite | 系统主库，保存设置、业务元数据 | 是 |
| MySQL | 因子看板行情缓存与分析结果库 | 因子看板必需 |

### 因子看板配置要点

- 系统主库固定为 SQLite
- MySQL 主要用于因子看板缓存和分析结果存储
- 若未配置 `Tushare Token`，因子看板无法启动真实分析
- 若未配置 MySQL，因子看板无法保存分析结果

## 目录结构

- `main.py`：FastAPI 入口
- `api/`：HTTP 路由层
- `services/`：业务编排层
- `data/`：Tushare 客户端、缓存与数据访问
- `database/`：数据库连接、模型与结果管理
- `factor/`：Alpha191 公式、引擎、因子分析逻辑
- `engine/`：回测和组合分析引擎
- `static/`：前端页面
- `docs/`：项目说明与专项文档
- `scripts/`：维护与辅助脚本

更详细说明见 `docs/project_structure.md`

## 文档索引

- `docs/README.md`
- `docs/quick_start.md`
- `docs/project_structure.md`
- `docs/factor_board_guide.md`
- `docs/ai_strategy_assistant.md`
- `docs/delivery_summary.md`

## 常见问题

### 1. 因子看板启动后没有结果

优先检查：

- 是否已配置有效的 `Tushare Token`
- 是否已保存 MySQL 连接信息
- MySQL 中是否成功创建结果表
- 选择的股票池是否有效

### 2. 首次分析速度较慢

首次运行需要补齐历史行情缓存。后续相同股票池和时间范围会优先复用缓存或仅做增量更新。

### 3. 分析过程中出现失败因子

系统会保留已完成结果，并在因子看板中展示失败因子列表。修复配置或数据问题后可直接使用“重试失败因子”。

### 4. 页面能打开但分析无法启动

通常是以下原因之一：

- `Tushare Token` 未保存
- MySQL 连接失败
- 自定义股票池为空
- 当前已有分析任务正在运行

### 5. AI 策略助手无法生成策略

优先检查：

- 设置页是否已保存 `API Key`、`接口地址`、`模型名称`
- 所填地址是否是可访问的 OpenAI 兼容 `chat/completions` 接口或其根路径
- 当前网络是否允许访问该模型服务
- 返回错误是否提示 404/403：这通常说明地址路径不兼容，而不是系统内部错误

针对本次 FoxCode / NewCLI 渠道的实测可用配置，见 `docs/ai_strategy_assistant.md`
