"""策略基类与API函数 - 聚宽风格"""


def initialize(context):
    """策略初始化 - 用户需重写"""
    pass


def handle_data(context):
    """每日调用 - 用户需重写"""
    pass


# 以下为策略中可用的API函数说明
# context.order(ts_code, amount)           - 按股数下单(正买负卖)
# context.order_target_percent(ts_code, pct) - 目标仓位百分比
# context.order_value(ts_code, value)      - 按金额下单
# context.get_price(ts_code)               - 获取当前价格
# context.get_history(ts_code, count, field) - 获取历史数据
# context.positions                        - 当前持仓字典
# context.cash                             - 可用资金
# context.portfolio_value                  - 总资产
# context.log(msg)                         - 输出日志
