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
- 因子策略联动：策略代码与 AI 都可直接调用因子库，构建多因子策略
- 回测分析：收益、回撤、风险指标、归因与组合分析
- 回测工作台：历史筛选、横向对比、组合分析与结果导出
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

## 回测与组合分析工作流

推荐使用顺序：

1. 在“策略”页创建或让 AI 生成策略草稿
2. 在“回测”页选择：
   - 策略
   - 回测区间
   - 股票池模式（系统股票池 / 自定义股票池 / 代码列表）
3. 运行回测后，在结果页查看：
   - 收益曲线与基准对比
   - 风险指标
   - 交易记录
   - 归因分析
   - 回测洞察
4. 点击进入“历史”页，执行：
   - 搜索、筛选、排序历史结果
   - 收藏重点结果
   - 为结果添加复盘备注
   - 选择多个结果进行横向对比
   - 导出对比 CSV
5. 在“历史”页选择 2 个及以上结果，点击“送入组合分析”
6. 在“组合分析”页查看：
   - 策略相关性矩阵
   - 权重分配结果
   - 组合净值曲线
   - 组合诊断（平均相关性、有效策略数、最大单策略权重等）
   - 导出组合 CSV / JSON

## AI 策略助手工作流

1. 在“策略”页输入自然语言需求
2. AI 会结合系统模板、风控约束和可选市场新闻生成策略
3. 若选择直接保存，策略会自动落库并联动到回测页
4. 回测页会继承策略、股票池上下文与相关配置
5. 可继续进入“历史”页和“组合分析”页完成研究闭环

## 因子与策略联动

系统已支持在策略中直接调用因子库：

- `context.get_factor('因子名', ts_code)`：获取当前交易日该股票的因子值
- `context.get_factor_history('因子名', ts_code, count)`：获取因子历史序列
- `context.list_factors(keyword='')`：查看当前可用因子目录

典型用法：

```python
def initialize(context):
    context.target_pct = 0.2

def handle_data(context):
    for ts_code in context.universe:
        momentum = context.get_factor('momentum_20', ts_code)
        ma_hist = context.get_factor_history('ma_position', ts_code, 5)
        if len(ma_hist) < 3:
            continue
        pos = context.positions.get(ts_code)
        has_position = pos is not None and pos.amount > 0
        if momentum > 0.02 and ma_hist[-1] >= 0.5 and not has_position:
            context.order_target_percent(ts_code, context.target_pct)
        elif has_position and (momentum < -0.02 or ma_hist[-1] < 0.25):
            context.order(ts_code, -pos.amount)
```

AI 策略助手也会读取当前因子目录，并优先生成可调用 `context.get_factor(...)` / `context.get_factor_history(...)` 的多因子策略。

现在有两条联动入口：

- 在“因子研究”页选择单个因子后，可直接点击“发送到 AI 策略助手”或在评价结果里点击“生成策略草稿”
- 在“因子看板”页查看 Alpha191 研究结果后，也可把当前因子、研究区间、股票池和评价摘要直接发送给 AI

这两个入口都会把当前因子表达式、说明、研究区间、股票池和可用回测上下文一并带到“策略”页，并自动触发一次策略草稿生成。

组合分析当前支持的权重方法：

- `等权 (1/N)`
- `逆方差`
- `最大 Sharpe`
- `风险平价`

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
