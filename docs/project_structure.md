# 项目结构

## 顶层目录

- `main.py`：FastAPI 应用入口，负责注册路由与静态页面
- `requirements.txt`：Python 依赖
- `README.md`：项目总览与快速开始
- `docs/`：文档目录

## 后端模块

- `api/`：HTTP 路由层，负责参数接收、校验与接口组织
- `services/`：业务编排层，负责调度数据、分析和状态更新
- `data/`：数据访问层，包含 Tushare 客户端、缓存与行情读取
- `database/`：数据库连接、ORM 模型、结果存取
- `factor/`：Alpha191 因子公式、计算引擎、因子看板分析逻辑
- `engine/`：回测、组合分析与统计计算
- `strategy/`：策略脚本与相关功能
- `news/`：新闻抓取、分析与处理

## 前端模块

- `static/index.html`：主前端页面，包含多个功能页签
- `static/lib/`：前端依赖库

## 因子看板相关核心文件

- `api/routes_factor_board.py`：因子看板接口、状态管理、股票池接口
- `services/factor_board_service.py`：分析任务编排、缓存复用、增量更新、并行分析
- `factor/factor_board_analyzer.py`：单因子回测、IC、分位收益与换手计算
- `database/db_manager.py`：因子结果、持仓、日收益的落库和查询
- `static/index.html`：因子看板前端页面与详情弹窗

## 运行时目录

- `runtime/`：运行期数据
- `logs/`：日志输出
- `tmp/`：临时文件

## 维护建议

- 新接口优先放在 `api/` + `services/` 的清晰分层中
- 因子看板相关逻辑尽量集中在 `routes_factor_board.py`、`factor_board_service.py` 与 `factor_board_analyzer.py`
- 真实数据、缓存和落库逻辑不要混入前端层
