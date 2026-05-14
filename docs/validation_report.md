# QTsys 真实性与可用性验证报告

更新时间：2026-05-01

## 本轮重点结论

- 已修复单股票因子表达式 `rank/cs_rank/cs_zscore/scale/indneutralize` 使用全样本未来数据的问题。
- 已修复系统股票池回测默认使用“当前最新指数成分股”导致的幸存者偏差风险；现在按回测起点前可获得的成分股快照解析。
- 已修复因子看板分组收益同日收盘价建仓的问题；现在按信号日后一交易日开盘建仓，并在下一调仓执行日开盘调仓。
- 已修复因子 IC 计算中零方差样本触发 `numpy invalid value encountered in divide` 的问题。
- 已清理因子 API 中已被服务层替代的不可达旧代码。

## 回测统一口径

- 策略在交易日 `T` 使用截至 `T` 的历史数据形成信号。
- 订单在下一交易日开盘执行，执行时考虑滑点、手续费、印花税、涨跌停、成交量限制和 A 股 100 股交易单位。
- 因子挖掘与因子看板均不得使用模拟行情或未来价格作为可交易信号。
- 旧版本保存的因子挖掘结果建议重新验证后再用于策略或外部平台复测。
- 因子挖掘新口径为 `Institutional Factor Lab v4`：候选生成只使用训练/验证信息，最终展示只使用测试集样本外曲线；测试集收益、测试 IC、测试 IR 不得参与候选打分或参数选择。
- v4 候选必须保存研究主题、经济学假设、预处理口径、统计显著性、DSR/PBO 风险、embargo walk-forward 稳健性、容量评分、因子指纹、相关性簇和 JoinQuant 复验说明。
- v4 展示逻辑不再只展示“严格通过”结果；完成真实样本外评估的候选均会入库并按 `institutional_pass`、`research_candidate`、`evaluated_weak` 分层，避免用户长时间只看到 0 个结果，同时保持结果真实性。

## 已执行验证

```powershell
.\.venv\Scripts\python.exe -m py_compile <项目全部非虚拟环境 Python 文件>
.\.venv\Scripts\python.exe scripts\validate_factor_no_lookahead.py
.\.venv\Scripts\python.exe scripts\security_check.py
.\.venv\Scripts\python.exe scripts\health_check.py
```

结果：

- Python 全量编译通过。
- 反未来函数/执行口径检查通过。
- 明文密钥扫描未发现明显敏感信息。
- 后端接口 `/api/factors`、`/api/factor_mining/options`、`/api/backtest/universe_options` 返回 `200`。
- 前端页面 `/`、`/?page=factor`、`/?page=factormining`、`/?page=factorboard`、`/?page=backtest`、`/?page=strategy` 无 Babel/React 控制台错误。
- 系统健康接口 `/api/system/health` 可用于检查版本、数据库、依赖、配置状态和后台任务状态，且不返回任何密钥明文。

## 发布前建议

1. 使用有效 Tushare Token 跑一个短周期真实回测，并与外部平台做同股票池、同费用、同调仓日复核。
2. 对旧版本挖掘出的高收益因子重新运行系统内回测，再生成 JoinQuant 代码复测。
3. 发布 GitHub 前再次运行 `scripts/health_check.py`，确认安全扫描、反未来函数检查、后端接口和前端冒烟测试均通过。
4. 确认 `runtime/`、`logs/`、`tmp/`、`data/cache/*.pkl`、本地数据库和密钥文件没有进入 Git 提交列表。

## 2026-05-11 统一执行核心验证

- 新增 `engine/execution_simulator.py`，作为因子挖掘与后续策略回测统一接入的标准执行仿真核心。
- 因子挖掘内部 `_backtest_from_factors` 已改为调用 `CanonicalExecutionSimulator`，不再使用独立手写的简化买卖逻辑。
- 新执行口径覆盖：T 日信号、下一交易日开盘成交、收盘计值、A 股只做多、100 股整数手、佣金/最低佣金/印花税/滑点/过户费、涨跌停拦截、停牌/无量过滤、科创板默认过滤、单票上限和成交量容量约束。
- 新增 `scripts/validate_execution_simulator.py`，验证下一交易日开盘执行、整手撮合、涨停不可买和成交量容量限制。
- `scripts/health_check.py` 已纳入统一执行核心验证项。
- 已执行 `python scripts/health_check.py --skip-frontend`，结果通过：Python 编译、反未来函数、统一执行规则、安全扫描、后端接口冒烟均为 OK。

## 2026-05-11 回测/聚宽一致性增强

- 主策略回测 `BacktestEngine` 已增加拒单追踪，回测结果会返回 `order_rejections` 和 `order_trace`，用于解释未成交、涨跌停、科创板过滤、无行情等差异。
- 新增 `/api/backtest/parity_package/{result_id}`，用于查看本地回测的执行假设、曲线、交易记录、按日期聚合成交和拒单摘要。
- 新增 `/api/backtest/parity_export/{result_id}`，用于下载本地对账 CSV，方便与 JoinQuant 日志逐日核对。
- 因子选股回测 `factor/factor_backtest.py` 已改为使用 `CanonicalExecutionSimulator`，不再使用独立简化口径。
- JoinQuant 因子模板已改为从系统设置/请求参数注入 `commission_rate`、`stamp_tax_rate`、`min_commission`、`slippage`，不再硬编码核心交易成本。
- 回测结果页新增“执行对账”页签和“导出对账”按钮，用户可直接查看执行假设、拒单摘要和订单追踪。

## 2026-05-11 细节加固

- `CanonicalExecutionSimulator` 现在直接返回完整 `daily_returns`，避免因子选股回测从降采样净值曲线反推日收益。
- 因子选股回测保留统一执行器输出的完整日收益序列，提升风险指标、任务结果和后续分析的一致性。
- 回测 CSV 和对账 CSV 增加 UTF-8 BOM，降低 Windows/Excel 打开中文字段时出现乱码的概率。
