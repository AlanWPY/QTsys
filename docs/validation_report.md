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
