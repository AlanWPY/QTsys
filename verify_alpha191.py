"""验证Alpha191因子工作流图的正确性"""
from factor.expression_to_graph import ExpressionToGraph
from factor.alpha191_templates import ALPHA191_FORMULAS

def verify_graph(number: int, formula: str, graph: dict) -> tuple:
    """验证工作流图是否正确"""
    issues = []

    # 检查基本结构
    if not graph.get('nodes'):
        issues.append("无节点")
    if not graph.get('edges'):
        issues.append("无边")

    # 检查输出节点
    output_nodes = [n for n in graph['nodes'] if n['type'] == 'output_factor']
    if len(output_nodes) != 1:
        issues.append(f"输出节点数量错误: {len(output_nodes)}")

    # 检查孤立节点
    node_ids = {n['id'] for n in graph['nodes']}
    connected = set()
    for e in graph['edges']:
        connected.add(e['source'])
        connected.add(e['target'])
    isolated = node_ids - connected
    if isolated:
        issues.append(f"孤立节点: {len(isolated)}个")

    # 检查边的有效性
    for e in graph['edges']:
        if e['source'] not in node_ids:
            issues.append(f"边源节点不存在: {e['source']}")
        if e['target'] not in node_ids:
            issues.append(f"边目标节点不存在: {e['target']}")

    return len(issues) == 0, issues

parser = ExpressionToGraph()
valid_count = 0
invalid_factors = []

print("开始验证191个Alpha因子工作流图...\n")

for i in range(1, 192):
    formula = ALPHA191_FORMULAS[i]
    try:
        graph = parser.parse(formula)
        is_valid, issues = verify_graph(i, formula, graph)

        if is_valid:
            valid_count += 1
        else:
            invalid_factors.append({
                'number': i,
                'formula': formula[:80] + '...' if len(formula) > 80 else formula,
                'issues': issues,
                'nodes': len(graph['nodes']),
                'edges': len(graph['edges'])
            })
    except Exception as e:
        invalid_factors.append({
            'number': i,
            'formula': formula[:80] + '...' if len(formula) > 80 else formula,
            'issues': [f"解析异常: {str(e)[:50]}"],
            'nodes': 0,
            'edges': 0
        })

print(f"验证完成: {valid_count}/191 个因子工作流正确\n")

if invalid_factors:
    print(f"发现 {len(invalid_factors)} 个问题因子:\n")
    for factor in invalid_factors:
        print(f"Alpha#{factor['number']}:")
        print(f"  公式: {factor['formula']}")
        print(f"  节点数: {factor['nodes']}, 边数: {factor['edges']}")
        print(f"  问题: {', '.join(factor['issues'])}")
        print()
else:
    print("所有因子工作流图验证通过！")
