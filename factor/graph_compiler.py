"""因子工作流图编译器 - 将可视化节点图编译为表达式字符串"""
from typing import Any

# ===== 节点类型注册表 =====
# 每个节点: template(表达式模板), inputs(输入端口列表), outputs(输出端口列表), params(参数定义)

NODE_REGISTRY = {
    # --- 数据源节点 ---
    "input_close":    {"template": "close",    "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "CLOSE", "tooltip": "收盘价", "description": "股票收盘价"},
    "input_open":     {"template": "open",     "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "OPEN", "tooltip": "开盘价", "description": "股票开盘价"},
    "input_high":     {"template": "high",     "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "HIGH", "tooltip": "最高价", "description": "股票最高价"},
    "input_low":      {"template": "low",      "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "LOW", "tooltip": "最低价", "description": "股票最低价"},
    "input_volume":   {"template": "volume",   "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "VOLUME", "tooltip": "成交量", "description": "股票成交量"},
    "input_returns":  {"template": "returns",  "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "RET", "tooltip": "每日收益率(收盘/前收盘-1)", "description": "每日收益率，计算公式：收盘价/前一日收盘价-1"},
    "input_vwap": {"template": "vwap", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "VWAP", "tooltip": "均价 - 成交量加权平均价格", "description": "成交量加权平均价格 VWAP = 成交额/成交量"},
    "input_amount": {"template": "amount", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "AMOUNT", "tooltip": "成交额 - 日成交金额", "description": "日成交金额"},
    "input_dtm": {"template": "dtm", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "DTM", "tooltip": "上涨动力", "description": "上涨动力 DTM = (OPEN<=DELAY(OPEN,1)?0:MAX((HIGH-OPEN),(OPEN-DELAY(OPEN,1))))"},
    "input_dbm": {"template": "dbm", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "DBM", "tooltip": "下跌动力", "description": "下跌动力 DBM = (OPEN>=DELAY(OPEN,1)?0:MAX((OPEN-LOW),(OPEN-DELAY(OPEN,1))))"},
    "input_tr": {"template": "tr", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "TR", "tooltip": "真实波幅", "description": "真实波幅 TR = MAX(MAX(HIGH-LOW,ABS(HIGH-DELAY(CLOSE,1))),ABS(LOW-DELAY(CLOSE,1)))"},
    "input_hd": {"template": "hd", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "HD", "tooltip": "最高价变化", "description": "最高价变化 HD = HIGH-DELAY(HIGH,1)"},
    "input_ld": {"template": "ld", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "LD", "tooltip": "最低价变化", "description": "最低价变化 LD = DELAY(LOW,1)-LOW"},
    "input_pe":       {"template": "pe",       "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "PE", "tooltip": "市盈率"},
    "input_pb":       {"template": "pb",       "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "PB", "tooltip": "市净率"},
    "input_ps":       {"template": "ps",       "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "PS", "tooltip": "市销率"},
    "input_total_mv": {"template": "total_mv", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "TOTAL_MV", "tooltip": "总市值"},
    "input_circ_mv":  {"template": "circ_mv",  "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "CIRC_MV", "tooltip": "流通市值"},
    "input_turnover": {"template": "turnover_rate", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "TURNOVER", "tooltip": "换手率"},
    "input_constant": {"template": "{value}",  "inputs": [], "outputs": ["out"], "params": {"value": {"type": "float", "default": 1.0}}, "category": "data", "label": "CONSTANT", "tooltip": "常数值"},

    # --- 数学运算节点 ---
    "math_add":   {"template": "({a} + {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "ADD", "tooltip": "加法运算 a + b"},
    "math_sub":   {"template": "({a} - {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "SUB", "tooltip": "减法运算 a - b"},
    "math_mul":   {"template": "({a} * {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "MUL", "tooltip": "乘法运算 a * b"},
    "math_div":   {"template": "({a} / {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "DIV", "tooltip": "除法运算 a / b"},
    "math_abs":   {"template": "abs({in})",      "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "ABS", "tooltip": "绝对值函数 |A|"},
    "math_log":   {"template": "log({in})",      "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "LOG", "tooltip": "自然对数函数 ln(A)"},
    "math_sqrt":  {"template": "sqrt({in})",     "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "SQRT", "tooltip": "平方根函数 √A"},
    "math_power": {"template": "power({in}, {exp})", "inputs": ["in"], "outputs": ["out"], "params": {"exp": {"type": "float", "default": 2.0}}, "category": "math", "label": "POWER", "tooltip": "幂运算 A^exp"},
    "math_neg":   {"template": "neg({in})",      "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "NEG", "tooltip": "取负运算 -A"},
    "math_max":   {"template": "max({a}, {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "MAX", "tooltip": "最大值 MAX(A,B) - 在A,B中选择最大的数"},
    "math_min":   {"template": "min({a}, {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "MIN", "tooltip": "最小值 MIN(A,B) - 在A,B中选择最小的数"},
    "math_clip":  {"template": "clip({in}, {lower}, {upper})", "inputs": ["in"], "outputs": ["out"], "params": {"lower": {"type": "float", "default": -3.0}, "upper": {"type": "float", "default": 3.0}}, "category": "math", "label": "CLIP", "tooltip": "截断函数 - 将值限制在[lower,upper]范围内"},
    "math_signedpower": {"template": "signedpower({in}, {exp})", "inputs": ["in"], "outputs": ["out"], "params": {"exp": {"type": "float", "default": 2.0}}, "category": "math", "label": "SIGNEDPOWER", "tooltip": "带符号幂运算 - sign(A)*|A|^exp 保留符号的幂运算"},
    "math_sign": {"template": "sign({in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "SIGN", "tooltip": "符号函数 SIGN(A) - 返回1(A>0), 0(A=0), -1(A<0)"},
    "math_floor": {"template": "floor({in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "FLOOR", "tooltip": "向下取整 - 不大于A的最大整数"},
    "math_ceil": {"template": "ceil({in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "CEIL", "tooltip": "向上取整 - 不小于A的最小整数"},
    "math_round": {"template": "round_val({in}, {decimals})", "inputs": ["in"], "outputs": ["out"], "params": {"decimals": {"type": "int", "default": 0}}, "category": "math", "label": "ROUND", "tooltip": "四舍五入 - 保留指定小数位"},

    # --- 时序运算节点 ---
    "ts_mean":      {"template": "mean({series}, {window})",      "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "MEAN", "tooltip": "序列A过去n天均值"},
    "ts_std":       {"template": "std({series}, {window})",       "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "STD", "tooltip": "序列A过去n天标准差"},
    "ts_sum":       {"template": "sum({series}, {window})",       "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "SUM", "tooltip": "序列A过去n天求和"},
    "ts_max":       {"template": "ts_max({series}, {window})",    "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "TSMAX", "tooltip": "序列A过去n天的最大值"},
    "ts_min":       {"template": "ts_min({series}, {window})",    "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "TSMIN", "tooltip": "序列A过去n天的最小值"},
    "ts_rank":      {"template": "ts_rank({series}, {window})",   "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "TSRANK", "tooltip": "序列A的末位值在过去n天的顺序排位"},
    "ts_delay":     {"template": "delay({series}, {periods})",    "inputs": ["series"], "outputs": ["out"], "params": {"periods": {"type": "int", "default": 1}}, "category": "timeseries", "label": "DELAY", "tooltip": "延迟n期 A(i-n)"},
    "ts_delta":     {"template": "delta({series}, {periods})",    "inputs": ["series"], "outputs": ["out"], "params": {"periods": {"type": "int", "default": 1}}, "category": "timeseries", "label": "DELTA", "tooltip": "差分 A(i)-A(i-n)"},
    "ts_pctchange": {"template": "pctchange({series}, {periods})", "inputs": ["series"], "outputs": ["out"], "params": {"periods": {"type": "int", "default": 1}}, "category": "timeseries", "label": "PCTCHANGE", "tooltip": "变化率 (A(i)-A(i-n))/A(i-n)"},
    "ts_corr":      {"template": "corr({a}, {b}, {window})",      "inputs": ["a", "b"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "CORR", "tooltip": "序列A、B过去n天相关系数"},
    "ts_cov":       {"template": "cov({a}, {b}, {window})",       "inputs": ["a", "b"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "COVIANCE", "tooltip": "序列A、B过去n天协方差"},
    "ts_decay":     {"template": "ts_decay({series}, {window})",  "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "DECAY", "tooltip": "线性衰减加权均值"},
    "ts_ewm":       {"template": "ewm({series}, {span})",         "inputs": ["series"], "outputs": ["out"], "params": {"span": {"type": "int", "default": 20}}, "category": "timeseries", "label": "EWM", "tooltip": "指数加权移动平均"},
    "ts_argmax": {"template": "ts_argmax({series}, {window})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "advanced_ts", "label": "TSARGMAX", "tooltip": "窗口内最大值的索引位置(从0开始)"},
    "ts_argmin": {"template": "ts_argmin({series}, {window})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "advanced_ts", "label": "TSARGMIN", "tooltip": "窗口内最小值的索引位置(从0开始)"},
    "ts_product": {"template": "ts_product({series}, {window})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "advanced_ts", "label": "PROD", "tooltip": "序列A过去n天累乘"},
    "ts_highday": {"template": "highday({series}, {window})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "advanced_ts", "label": "HIGHDAY", "tooltip": "计算A前n期时间序列中最大值距离当前时点的间隔"},
    "ts_lowday": {"template": "lowday({series}, {window})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "advanced_ts", "label": "LOWDAY", "tooltip": "计算A前n期时间序列中最小值距离当前时点的间隔"},
    "ts_wma": {"template": "wma({series}, {window})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "advanced_ts", "label": "WMA", "tooltip": "计算A前n期样本加权平均值,权重为0.9^i"},
    "ts_decaylinear": {"template": "decaylinear({series}, {window})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "advanced_ts", "label": "DECAYLINEAR", "tooltip": "对A序列计算移动平均加权,权重对应d,d-1,...,1(权重和为1)"},
    "ts_sma": {"template": "sma({series}, {window}, {weight})", "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}, "weight": {"type": "int", "default": 1}}, "category": "advanced_ts", "label": "SMA", "tooltip": "平滑移动平均 y(i+1)=(A*m+y(i)*(n-m))/n"},
    "stat_regbeta": {"template": "regbeta({x}, {y}, {window})", "inputs": ["x", "y"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "statistics", "label": "REGBETA", "tooltip": "前n期样本A对B做回归所得回归系数"},
    "stat_regresi": {"template": "regresi({x}, {y}, {window})", "inputs": ["x", "y"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "statistics", "label": "REGRESI", "tooltip": "前n期样本A对B做回归所得的残差"},
    "stat_sequence": {"template": "sequence({length})", "inputs": [], "outputs": ["out"], "params": {"length": {"type": "int", "default": 20}}, "category": "statistics", "label": "SEQUENCE", "tooltip": "生成1~n的等差序列"},
    "stat_sumif": {"template": "sumif({series}, {condition}, {window})", "inputs": ["series", "condition"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "statistics", "label": "SUMIF", "tooltip": "对A前n项条件求和,其中condition表示选择条件"},

    # --- 截面运算节点 ---
    "cs_rank":       {"template": "cs_rank({in})",       "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "RANK", "tooltip": "向量A升序排序(截面排名)"},
    "cs_zscore":     {"template": "cs_zscore({in})",     "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "ZSCORE", "tooltip": "截面Z-score标准化"},
    "cs_percentile": {"template": "cs_percentile({in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "PERCENTILE", "tooltip": "截面百分位排名"},
    "cs_demean":     {"template": "cs_demean({in})",     "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "DEMEAN", "tooltip": "截面去均值"},
    "cs_scale": {"template": "scale({in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "SCALE", "tooltip": "截面标准化到和为1"},
    "cs_indneutralize": {"template": "indneutralize({in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "INDNEUTRALIZE", "tooltip": "行业中性化(去除行业均值)"},
    "cs_advm": {"template": "advm({in}, {window})", "inputs": ["in"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "cross_section", "label": "ADVM", "tooltip": "平均日成交额"},

    # --- 条件逻辑节点 ---
    "cond_gt":      {"template": "({a} > {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "GT", "tooltip": "大于 a > b"},
    "cond_lt":      {"template": "({a} < {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "LT", "tooltip": "小于 a < b"},
    "cond_and":     {"template": "({a} & {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "AND", "tooltip": "逻辑与 a & b"},
    "cond_or":      {"template": "({a} | {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "OR", "tooltip": "逻辑或 a || b"},
    "cond_ternary": {"template": "ternary({cond}, {true_val}, {false_val})", "inputs": ["cond", "true_val", "false_val"], "outputs": ["out"], "params": {}, "category": "condition", "label": "TERNARY", "tooltip": "条件选择 A?B:C - 若A成立则为B,否则为C"},
    "cond_gte": {"template": "({a} >= {b})", "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "GTE", "tooltip": "大于等于 a >= b"},
    "cond_lte": {"template": "({a} <= {b})", "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "LTE", "tooltip": "小于等于 a <= b"},
    "cond_eq": {"template": "({a} == {b})", "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "EQ", "tooltip": "等于 a == b"},
    "cond_not": {"template": "(~{in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "condition", "label": "NOT", "tooltip": "逻辑非 ~A"},

    # --- 输出节点 ---
    "output": {"template": "{in}", "inputs": ["in"], "outputs": [], "params": {}, "category": "output", "label": "OUTPUT", "tooltip": "因子输出节点"},
}

# 节点分类颜色
CATEGORY_COLORS = {
    "data": "#3b82f6",
    "math": "#f59e0b",
    "timeseries": "#22c55e",
    "advanced_ts": "#10b981",
    "statistics": "#8b5cf6",
    "cross_section": "#a855f7",
    "condition": "#ef4444",
    "output": "#6366f1",
}


class CompileError(Exception):
    pass


def validate_graph(graph: dict) -> list[str]:
    """验证图结构，返回错误列表"""
    errors = []
    nodes = {n["id"]: n for n in graph.get("nodes", [])}
    edges = graph.get("edges", [])

    if not nodes:
        errors.append("图中没有节点")
        return errors

    # 检查恰好1个output节点
    output_nodes = [n for n in nodes.values() if n["type"] == "output"]
    if len(output_nodes) == 0:
        errors.append("缺少输出节点")
    elif len(output_nodes) > 1:
        errors.append("只能有一个输出节点")

    # 检查节点类型是否合法
    for nid, node in nodes.items():
        if node["type"] not in NODE_REGISTRY:
            errors.append(f"未知节点类型: {node['type']} (节点 {nid})")

    # 构建连接映射: to_port -> from_port
    input_map = {}  # (to_node_id, to_port) -> (from_node_id, from_port)
    for edge in edges:
        from_info = edge["from"]
        to_info = edge["to"]
        key = (to_info["nodeId"], to_info["port"])
        if key in input_map:
            errors.append(f"端口重复连接: {to_info['nodeId']}.{to_info['port']}")
        input_map[key] = (from_info["nodeId"], from_info["port"])

    # 检查所有必需输入端口已连接
    for nid, node in nodes.items():
        ntype = node["type"]
        if ntype not in NODE_REGISTRY:
            continue
        reg = NODE_REGISTRY[ntype]
        for port in reg["inputs"]:
            if (nid, port) not in input_map:
                errors.append(f"未连接的输入: {ntype}.{port} (节点 {nid})")

    # 环检测 (DFS)
    if not errors:
        adj = {nid: [] for nid in nodes}
        for edge in edges:
            adj[edge["from"]["nodeId"]].append(edge["to"]["nodeId"])
        if _has_cycle(adj, nodes):
            errors.append("图中存在环路")

    return errors


def _has_cycle(adj: dict, nodes: dict) -> bool:
    """DFS环检测"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in nodes}

    def dfs(u):
        color[u] = GRAY
        for v in adj.get(u, []):
            if color[v] == GRAY:
                return True
            if color[v] == WHITE and dfs(v):
                return True
        color[u] = BLACK
        return False

    for nid in nodes:
        if color[nid] == WHITE:
            if dfs(nid):
                return True
    return False


def compile_graph(graph: dict) -> dict:
    """编译工作流图为表达式字符串
    返回: {"expression": str, "errors": list[str]}
    """
    errors = validate_graph(graph)
    if errors:
        return {"expression": "", "errors": errors}

    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = graph.get("edges", [])

    # 构建输入映射: (to_node_id, to_port) -> (from_node_id, from_port)
    input_map = {}
    for edge in edges:
        key = (edge["to"]["nodeId"], edge["to"]["port"])
        input_map[key] = (edge["from"]["nodeId"], edge["from"]["port"])

    # 找到output节点
    output_node = None
    for n in nodes.values():
        if n["type"] == "output":
            output_node = n
            break

    try:
        expr = _build_expr(output_node["id"], nodes, input_map)
        return {"expression": expr, "errors": []}
    except CompileError as e:
        return {"expression": "", "errors": [str(e)]}


def _build_expr(node_id: str, nodes: dict, input_map: dict) -> str:
    """递归构建节点表达式"""
    node = nodes[node_id]
    ntype = node["type"]
    reg = NODE_REGISTRY[ntype]
    template = reg["template"]
    params = node.get("params", {})

    # 收集所有替换值
    replacements = {}

    # 1. 递归解析输入端口
    for port in reg["inputs"]:
        key = (node_id, port)
        if key not in input_map:
            raise CompileError(f"未连接: {ntype}.{port}")
        src_node_id, _ = input_map[key]
        replacements[port] = _build_expr(src_node_id, nodes, input_map)

    # 2. 填充参数值
    for pname, pdef in reg["params"].items():
        val = params.get(pname, pdef["default"])
        replacements[pname] = str(val)

    # 3. 替换模板
    expr = template
    for k, v in replacements.items():
        expr = expr.replace("{" + k + "}", v)

    return expr
