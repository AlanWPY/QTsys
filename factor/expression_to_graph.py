"""表达式转工作流图 - 自动将Alpha191表达式转换为可视化节点图"""
import re
from typing import Dict, List

class ExpressionToGraph:
    def __init__(self):
        self.node_counter = 0
        self.nodes = []
        self.edges = []

    def parse(self, expression: str) -> Dict:
        self.node_counter = 0
        self.nodes = []
        self.edges = []
        root_id = self._parse_expr(expression)
        output_id = self._add_node("OUTPUT", "output", {})
        self._add_edge(root_id, "out", output_id, "in")
        self._auto_layout()
        return {"nodes": self.nodes, "edges": self.edges}

    def _parse_expr(self, expr: str) -> str:
        expr = expr.strip()
        if not expr: return self._add_node("0", "input_constant", {"value": 0})

        # 去除最外层括号
        while expr.startswith('(') and self._matching_paren(expr, 0) == len(expr) - 1:
            expr = expr[1:-1].strip()

        # 常量
        try:
            val = float(expr)
            return self._add_node(str(val), "input_constant", {"value": val})
        except: pass

        # 数据源
        sources = {'close':'input_close','open':'input_open','high':'input_high',
                   'low':'input_low','volume':'input_volume','vwap':'input_vwap',
                   'amount':'input_amount','returns':'input_returns'}
        if expr in sources:
            return self._add_node(expr.upper(), sources[expr], {})

        # 函数调用
        func_match = re.match(r'^(\w+)\((.*)\)$', expr, re.DOTALL)
        if func_match:
            return self._parse_function(func_match.group(1), func_match.group(2))

        # 二元运算符（从低优先级到高优先级）
        for ops in [['=='], ['>', '<', '>=', '<='], ['+', '-'], ['*', '/'], ['**']]:
            pos = self._find_op(expr, ops)
            if pos >= 0:
                op = next(o for o in ops if expr[pos:pos+len(o)] == o)
                left = expr[:pos].strip()
                right = expr[pos+len(op):].strip()
                return self._parse_binary(op, left, right)

        # 一元负号
        if expr.startswith('-'):
            inner = self._parse_expr(expr[1:])
            node_id = self._add_node("NEG", "math_neg", {})
            self._add_edge(inner, "out", node_id, "in")
            return node_id

        return self._add_node("?", "input_constant", {"value": 0})

    def _parse_function(self, func: str, args_str: str) -> str:
        args = self._split_args(args_str)

        func_map = {
            'rank':('cs_rank',[]),'delta':('ts_delta',['window']),'delay':('ts_delay',['window']),
            'corr':('ts_corr',['window']),'std':('ts_std',['window']),'mean':('ts_mean',['window']),
            'sum':('ts_sum',['window']),'max':('ts_max',['window']),'min':('ts_min',['window']),
            'ts_rank':('ts_rank',['window']),'ts_max':('ts_max',['window']),'ts_min':('ts_min',['window']),
            'ts_argmax':('ts_argmax',['window']),'ts_argmin':('ts_argmin',['window']),
            'ts_product':('ts_product',['window']),'signedpower':('math_signedpower',['exp']),
            'power':('math_power',['exp']),'abs':('math_abs',[]),'log':('math_log',[]),
            'sqrt':('math_sqrt',[]),'sign':('math_sign',[]),'ternary':('cond_ternary',[]),
            'where':('cond_ternary',[]),'sma':('ts_sma',['window','weight']),
            'wma':('ts_wma',['window']),'decaylinear':('ts_decaylinear',['window']),
            'scale':('cs_scale',[]),'highday':('ts_highday',['window']),'lowday':('ts_lowday',['window']),
            'regbeta':('stat_regbeta',['window']),'regresi':('stat_regresi',['window']),
        }

        if func not in func_map:
            return self._add_node(func, "input_constant", {"value": 0})

        node_type, param_names = func_map[func]
        arg_ids = []
        params = {}

        for i, arg in enumerate(args):
            if i < len(args) - len(param_names):
                arg_ids.append(self._parse_expr(arg))
            else:
                param_idx = i - (len(args) - len(param_names))
                try:
                    params[param_names[param_idx]] = float(arg)
                except:
                    params[param_names[param_idx]] = 1

        node_id = self._add_node(func.upper(), node_type, params)

        # 根据节点类型确定输入端口名称
        input_port_map = {
            'ts_delta': ['series'],
            'ts_delay': ['series'],
            'ts_corr': ['a', 'b'],
            'ts_std': ['series'],
            'ts_mean': ['series'],
            'ts_sum': ['series'],
            'ts_max': ['series'],
            'ts_min': ['series'],
            'ts_rank': ['series'],
            'ts_argmax': ['series'],
            'ts_argmin': ['series'],
            'ts_product': ['series'],
            'ts_sma': ['series'],
            'ts_wma': ['series'],
            'ts_decaylinear': ['series'],
            'ts_highday': ['series'],
            'ts_lowday': ['series'],
            'stat_regbeta': ['x', 'y'],
            'stat_regresi': ['x', 'y'],
            'cond_ternary': ['cond', 'true_val', 'false_val'],
        }

        input_names = input_port_map.get(node_type, ['in'])

        for i, arg_id in enumerate(arg_ids):
            inp = input_names[i] if i < len(input_names) else 'in'
            self._add_edge(arg_id, "out", node_id, inp)

        return node_id

    def _parse_binary(self, op: str, left: str, right: str) -> str:
        op_map = {'+':'math_add','-':'math_sub','*':'math_mul','/':'math_div',
                  '>':'cond_gt','<':'cond_lt','>=':'cond_gte','<=':'cond_lte',
                  '==':'cond_eq','**':'math_power'}
        node_id = self._add_node(op, op_map.get(op, 'math_add'), {})
        left_id = self._parse_expr(left)
        right_id = self._parse_expr(right)
        self._add_edge(left_id, "out", node_id, "a")
        self._add_edge(right_id, "out", node_id, "b")
        return node_id

    def _find_op(self, expr: str, ops: List[str]) -> int:
        depth = 0
        for i in range(len(expr) - 1, -1, -1):
            if expr[i] == ')': depth += 1
            elif expr[i] == '(': depth -= 1
            if depth == 0:
                for op in ops:
                    if expr[i:i+len(op)] == op:
                        if op in ['+','-'] and i > 0 and expr[i-1] in 'eE':
                            continue
                        return i
        return -1

    def _split_args(self, args_str: str) -> List[str]:
        args, current, depth = [], "", 0
        for char in args_str:
            if char == ',' and depth == 0:
                if current.strip(): args.append(current.strip())
                current = ""
            else:
                if char == '(': depth += 1
                elif char == ')': depth -= 1
                current += char
        if current.strip(): args.append(current.strip())
        return args

    def _matching_paren(self, s: str, start: int) -> int:
        depth = 1
        for i in range(start + 1, len(s)):
            if s[i] == '(': depth += 1
            elif s[i] == ')': depth -= 1
            if depth == 0: return i
        return -1

    def _add_node(self, label: str, node_type: str, params: Dict) -> str:
        node_id = f"n{self.node_counter}"
        self.node_counter += 1
        self.nodes.append({
            "id": node_id,
            "type": node_type,
            "x": 0,
            "y": 0,
            "params": params
        })
        return node_id

    def _add_edge(self, source: str, source_handle: str, target: str, target_handle: str):
        self.edges.append({
            "id": f"e{len(self.edges)}",
            "from": {"nodeId": source, "port": source_handle},
            "to": {"nodeId": target, "port": target_handle}
        })

    def _auto_layout(self):
        """改进的树状布局算法"""
        levels = {}
        def calc_level(nid, visited=None):
            if visited is None: visited = set()
            if nid in visited: return 0
            visited.add(nid)
            incoming = [e for e in self.edges if e['to']['nodeId'] == nid]
            return max([calc_level(e['from']['nodeId'], visited) for e in incoming], default=-1) + 1

        for node in self.nodes:
            levels[node['id']] = calc_level(node['id'])

        level_groups = {}
        for nid, lv in levels.items():
            level_groups.setdefault(lv, []).append(nid)

        x_spacing = 250
        y_spacing = 120
        for lv, nids in level_groups.items():
            y_offset = -(len(nids) - 1) * y_spacing / 2
            for i, nid in enumerate(nids):
                node = next(n for n in self.nodes if n['id'] == nid)
                node['x'] = lv * x_spacing + 50
                node['y'] = y_offset + i * y_spacing + 200
