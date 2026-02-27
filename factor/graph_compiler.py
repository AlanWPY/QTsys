"""因子工作流图编译器 - 将可视化节点图编译为表达式字符串"""
from typing import Any

# ===== 节点类型注册表 =====
# 每个节点: template(表达式模板), inputs(输入端口列表), outputs(输出端口列表), params(参数定义)

NODE_REGISTRY = {
    # --- 数据源节点 ---
    "input_close":    {"template": "close",    "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "收盘价"},
    "input_open":     {"template": "open",     "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "开盘价"},
    "input_high":     {"template": "high",     "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "最高价"},
    "input_low":      {"template": "low",      "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "最低价"},
    "input_volume":   {"template": "volume",   "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "成交量"},
    "input_returns":  {"template": "returns",  "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "收益率"},
    "input_pe":       {"template": "pe",       "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "PE"},
    "input_pb":       {"template": "pb",       "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "PB"},
    "input_ps":       {"template": "ps",       "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "PS"},
    "input_total_mv": {"template": "total_mv", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "总市值"},
    "input_circ_mv":  {"template": "circ_mv",  "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "流通市值"},
    "input_turnover": {"template": "turnover_rate", "inputs": [], "outputs": ["out"], "params": {}, "category": "data", "label": "换手率"},
    "input_constant": {"template": "{value}",  "inputs": [], "outputs": ["out"], "params": {"value": {"type": "float", "default": 1.0}}, "category": "data", "label": "常数"},

    # --- 数学运算节点 ---
    "math_add":   {"template": "({a} + {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "加法"},
    "math_sub":   {"template": "({a} - {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "减法"},
    "math_mul":   {"template": "({a} * {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "乘法"},
    "math_div":   {"template": "({a} / {b})",   "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "除法"},
    "math_abs":   {"template": "abs({in})",      "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "绝对值"},
    "math_log":   {"template": "log({in})",      "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "对数"},
    "math_sqrt":  {"template": "sqrt({in})",     "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "平方根"},
    "math_power": {"template": "power({in}, {exp})", "inputs": ["in"], "outputs": ["out"], "params": {"exp": {"type": "float", "default": 2.0}}, "category": "math", "label": "幂"},
    "math_neg":   {"template": "neg({in})",      "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "math", "label": "取负"},
    "math_max":   {"template": "max({a}, {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "最大值"},
    "math_min":   {"template": "min({a}, {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "math", "label": "最小值"},
    "math_clip":  {"template": "clip({in}, {lower}, {upper})", "inputs": ["in"], "outputs": ["out"], "params": {"lower": {"type": "float", "default": -3.0}, "upper": {"type": "float", "default": 3.0}}, "category": "math", "label": "截断"},

    # --- 时序运算节点 ---
    "ts_mean":      {"template": "mean({series}, {window})",      "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "均值"},
    "ts_std":       {"template": "std({series}, {window})",       "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "标准差"},
    "ts_sum":       {"template": "sum({series}, {window})",       "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "求和"},
    "ts_max":       {"template": "ts_max({series}, {window})",    "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "最大值"},
    "ts_min":       {"template": "ts_min({series}, {window})",    "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "最小值"},
    "ts_rank":      {"template": "ts_rank({series}, {window})",   "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "排名"},
    "ts_delay":     {"template": "delay({series}, {periods})",    "inputs": ["series"], "outputs": ["out"], "params": {"periods": {"type": "int", "default": 1}}, "category": "timeseries", "label": "延迟"},
    "ts_delta":     {"template": "delta({series}, {periods})",    "inputs": ["series"], "outputs": ["out"], "params": {"periods": {"type": "int", "default": 1}}, "category": "timeseries", "label": "差分"},
    "ts_pctchange": {"template": "pctchange({series}, {periods})", "inputs": ["series"], "outputs": ["out"], "params": {"periods": {"type": "int", "default": 1}}, "category": "timeseries", "label": "变化率"},
    "ts_corr":      {"template": "corr({a}, {b}, {window})",      "inputs": ["a", "b"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "相关系数"},
    "ts_cov":       {"template": "cov({a}, {b}, {window})",       "inputs": ["a", "b"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "协方差"},
    "ts_decay":     {"template": "ts_decay({series}, {window})",  "inputs": ["series"], "outputs": ["out"], "params": {"window": {"type": "int", "default": 20}}, "category": "timeseries", "label": "衰减均值"},
    "ts_ewm":       {"template": "ewm({series}, {span})",         "inputs": ["series"], "outputs": ["out"], "params": {"span": {"type": "int", "default": 20}}, "category": "timeseries", "label": "指数加权"},

    # --- 截面运算节点 ---
    "cs_rank":       {"template": "cs_rank({in})",       "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "截面排名"},
    "cs_zscore":     {"template": "cs_zscore({in})",     "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "截面Z分"},
    "cs_percentile": {"template": "cs_percentile({in})", "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "截面百分位"},
    "cs_demean":     {"template": "cs_demean({in})",     "inputs": ["in"], "outputs": ["out"], "params": {}, "category": "cross_section", "label": "截面去均值"},

    # --- 条件逻辑节点 ---
    "cond_gt":      {"template": "({a} > {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "大于"},
    "cond_lt":      {"template": "({a} < {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "小于"},
    "cond_and":     {"template": "({a} & {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "且"},
    "cond_or":      {"template": "({a} | {b})",  "inputs": ["a", "b"], "outputs": ["out"], "params": {}, "category": "condition", "label": "或"},
    "cond_ternary": {"template": "ternary({cond}, {true_val}, {false_val})", "inputs": ["cond", "true_val", "false_val"], "outputs": ["out"], "params": {}, "category": "condition", "label": "条件选择"},

    # --- 输出节点 ---
    "output": {"template": "{in}", "inputs": ["in"], "outputs": [], "params": {}, "category": "output", "label": "输出"},
}

# 节点分类颜色
CATEGORY_COLORS = {
    "data": "#3b82f6",
    "math": "#f59e0b",
    "timeseries": "#22c55e",
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
