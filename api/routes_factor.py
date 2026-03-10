"""因子挖掘API接口"""
import asyncio
import csv
import io
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from database.models import Settings, Factor, FactorResult
from factor.builtin_factors import BUILTIN_FACTORS
from factor.alpha191_templates import get_alpha191_formula
from factor.expression_to_graph import ExpressionToGraph
from services.factor_service import evaluate_factor_workflow, mine_gp_workflow

router = APIRouter(prefix="/api/factors", tags=["factors"])


class FactorCreate(BaseModel):
    name: str
    description: str = ""
    expression: str
    category: str = "custom"


class FactorEvalRequest(BaseModel):
    factor_id: int
    universe: list[str]
    start_date: str
    end_date: str
    groups: int = 5
    forward_days: int = 5  # 收益预测周期: 1/5/10/20日


class GPMineRequest(BaseModel):
    universe: list[str]
    start_date: str
    end_date: str
    pop_size: int = 30
    generations: int = 10


class LLMMineRequest(BaseModel):
    universe: list[str]
    start_date: str
    end_date: str
    count: int = 5
    hint: str = ""


# ===== 因子CRUD =====

@router.get("")
async def list_factors(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Factor).order_by(Factor.created_at.desc()))
    factors = result.scalars().all()
    return [
        {
            "id": f.id, "name": f.name, "description": f.description,
            "expression": f.expression, "category": f.category,
            "source": f.source,
            "created_at": f.created_at.isoformat() if f.created_at else "",
        }
        for f in factors
    ]


@router.post("")
async def create_factor(data: FactorCreate, db: AsyncSession = Depends(get_db)):
    f = Factor(
        name=data.name, description=data.description,
        expression=data.expression, category=data.category, source="user",
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return {"id": f.id, "name": f.name}


@router.delete("/{factor_id}")
async def delete_factor(factor_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Factor).where(Factor.id == factor_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="因子不存在")
    await db.delete(f)
    await db.commit()
    return {"message": "已删除"}


@router.post("/init_builtin")
async def init_builtin_factors(db: AsyncSession = Depends(get_db)):
    """初始化内置因子"""
    created = 0
    for name, info in BUILTIN_FACTORS.items():
        result = await db.execute(select(Factor).where(Factor.name == name))
        if result.scalar_one_or_none():
            continue
        f = Factor(
            name=name, description=info["description"],
            expression=info["expression"],
            category=info["category"], source="builtin",
        )
        db.add(f)
        created += 1
    await db.commit()
    return {"message": f"已创建{created}个内置因子", "created": created}


# ===== 因子评价 =====

@router.post("/evaluate")
async def evaluate_factor(req: FactorEvalRequest, db: AsyncSession = Depends(get_db)):
    """评价因子效果"""
    try:
        return await evaluate_factor_workflow(
            db,
            factor_id=req.factor_id,
            universe=req.universe,
            start_date=req.start_date,
            end_date=req.end_date,
            groups=req.groups,
            forward_days=req.forward_days,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))

    result = await db.execute(select(Factor).where(Factor.id == req.factor_id))
    factor = result.scalar_one_or_none()
    if not factor:
        raise HTTPException(status_code=404, detail="因子不存在")

    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from data.tushare_client import TushareClient
    from data.data_cache import DataCache
    from factor.factor_engine import FactorEngine

    client = TushareClient(settings.tushare_token)
    from data.data_cache import make_mysql_conn
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    eval_result = await asyncio.to_thread(
        engine.evaluate, factor.expression,
        req.universe, req.start_date, req.end_date, req.groups,
        req.forward_days,
    )

    if "error" in eval_result:
        raise HTTPException(status_code=400, detail=eval_result["error"])

    # 保存结果
    fr = FactorResult(
        factor_id=factor.id, factor_name=factor.name,
        universe=",".join(req.universe),
        start_date=req.start_date, end_date=req.end_date,
        metrics=eval_result["metrics"],
        ic_series=eval_result["ic_series"],
        group_returns=eval_result["group_returns"],
        turnover_series=eval_result["turnover_series"],
        long_short_curve=eval_result["long_short_curve"],
    )
    db.add(fr)
    await db.commit()

    return eval_result


# ===== 遗传算法挖掘 =====

@router.post("/mine/gp")
async def mine_gp(req: GPMineRequest, db: AsyncSession = Depends(get_db)):
    """遗传算法因子挖掘"""
    try:
        return await mine_gp_workflow(
            db,
            universe=req.universe,
            start_date=req.start_date,
            end_date=req.end_date,
            pop_size=req.pop_size,
            generations=req.generations,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from data.tushare_client import TushareClient
    from data.data_cache import DataCache
    from factor.factor_engine import FactorEngine
    from factor.genetic import run_gp

    client = TushareClient(settings.tushare_token)
    from data.data_cache import make_mysql_conn
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    results = await asyncio.to_thread(
        run_gp, engine, req.universe,
        req.start_date, req.end_date,
        req.pop_size, req.generations,
    )

    # 自动保存发现的因子
    saved = []
    for i, r in enumerate(results):
        name = f"GP因子_{i+1}"
        f = Factor(
            name=name, description=f"遗传算法挖掘 (适应度={r['fitness']})",
            expression=r["expression"],
            category="GP挖掘", source="gp",
        )
        db.add(f)
        saved.append({"name": name, **r})
    await db.commit()

    return {"factors": saved}


# ===== LLM挖掘 =====

@router.post("/mine/llm")
async def mine_llm(req: LLMMineRequest, db: AsyncSession = Depends(get_db)):
    """LLM因子挖掘"""
    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings:
        raise HTTPException(status_code=400, detail="请先配置设置")
    if not settings.llm_api_key or not settings.llm_base_url:
        raise HTTPException(status_code=400, detail="请先在设置页面配置LLM API Key和地址")

    from factor.llm_miner import call_llm, parse_llm_response, FACTOR_MINING_PROMPT

    extra = f"\n5. 额外提示: {req.hint}" if req.hint else ""
    prompt = FACTOR_MINING_PROMPT.format(count=req.count, extra_hint=extra)

    response = await call_llm(
        settings.llm_api_key, settings.llm_base_url,
        settings.llm_model or "gpt-3.5-turbo", prompt,
    )

    if not response:
        raise HTTPException(status_code=500, detail="LLM调用失败，请检查API配置")

    factors = parse_llm_response(response)
    if not factors:
        raise HTTPException(status_code=500, detail="LLM返回格式解析失败")

    # 保存到数据库
    saved = []
    for item in factors:
        f = Factor(
            name=item["name"], description=item["description"],
            expression=item["expression"],
            category="LLM挖掘", source="llm",
        )
        db.add(f)
        saved.append(item)
    await db.commit()

    return {"factors": saved}


# ===== 因子结果导出 =====

@router.get("/export/{result_id}")
async def export_factor_result(result_id: int, db: AsyncSession = Depends(get_db)):
    """导出因子评估结果为CSV"""
    result = await db.execute(
        select(FactorResult).where(FactorResult.id == result_id)
    )
    r = result.scalar_one_or_none()
    if not r:
        raise HTTPException(status_code=404, detail="因子结果不存在")

    output = io.StringIO()

    # 写入指标摘要
    output.write("# 因子评价指标\n")
    metrics = r.metrics or {}
    for k, v in metrics.items():
        output.write(f"{k},{v}\n")
    output.write("\n")

    # 写入IC序列
    output.write("# IC序列\n")
    writer = csv.DictWriter(output, fieldnames=["date", "ic"])
    writer.writeheader()
    for row in (r.ic_series or []):
        writer.writerow(row)
    output.write("\n")

    # 写入分组收益
    output.write("# 分组收益\n")
    gr_fields = ["group", "label", "cumulative_return", "annual_return"]
    writer2 = csv.DictWriter(output, fieldnames=gr_fields)
    writer2.writeheader()
    for row in (r.group_returns or []):
        writer2.writerow({k: row.get(k, "") for k in gr_fields})
    output.write("\n")

    # 写入多空曲线
    output.write("# 多空曲线\n")
    writer3 = csv.DictWriter(output, fieldnames=["date", "value"])
    writer3.writeheader()
    for row in (r.long_short_curve or []):
        writer3.writerow(row)

    output.seek(0)
    filename = f"factor_{result_id}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ===== 工作流 API =====

class WorkflowCompileRequest(BaseModel):
    graph: dict

class WorkflowPreviewRequest(BaseModel):
    graph: dict
    ts_code: str
    start_date: str
    end_date: str

class WorkflowSaveRequest(BaseModel):
    name: str
    description: str = ""
    graph: dict
    factor_type: str = "technical"
    category: str = "workflow"


@router.post("/workflow/compile")
async def compile_workflow(req: WorkflowCompileRequest):
    """编译工作流图为表达式"""
    from factor.graph_compiler import compile_graph
    result = compile_graph(req.graph)
    return result


@router.post("/workflow/preview")
async def preview_workflow(req: WorkflowPreviewRequest, db: AsyncSession = Depends(get_db)):
    """单股票因子值预览"""
    from factor.graph_compiler import compile_graph
    compiled = compile_graph(req.graph)
    if compiled["errors"]:
        raise HTTPException(status_code=400, detail=compiled["errors"][0])

    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.tushare_token:
        raise HTTPException(status_code=400, detail="请先配置Tushare Token")

    from data.tushare_client import TushareClient
    from data.data_cache import DataCache
    from factor.factor_engine import FactorEngine

    client = TushareClient(settings.tushare_token)
    from data.data_cache import make_mysql_conn
    cache = DataCache(client, mysql_conn=make_mysql_conn(settings))
    engine = FactorEngine(cache)

    fv = await asyncio.to_thread(
        engine.compute_factor_values,
        compiled["expression"], req.ts_code,
        req.start_date, req.end_date,
    )
    if fv is None:
        raise HTTPException(status_code=400, detail="因子计算失败")

    data = []
    for dt, val in fv.dropna().items():
        date_str = dt.strftime("%Y%m%d") if hasattr(dt, "strftime") else str(dt)
        data.append({"date": date_str, "value": round(float(val), 6)})

    return {"expression": compiled["expression"], "data": data}


@router.post("/workflow/save")
async def save_workflow(req: WorkflowSaveRequest, db: AsyncSession = Depends(get_db)):
    """编译并保存工作流因子"""
    from factor.graph_compiler import compile_graph
    compiled = compile_graph(req.graph)
    if compiled["errors"]:
        raise HTTPException(status_code=400, detail=compiled["errors"][0])

    f = Factor(
        name=req.name, description=req.description,
        expression=compiled["expression"],
        category=req.category, source="workflow",
        graph_json=req.graph, factor_type=req.factor_type,
    )
    db.add(f)
    await db.commit()
    await db.refresh(f)
    return {"id": f.id, "name": f.name, "expression": compiled["expression"]}


@router.put("/workflow/{factor_id}")
async def update_workflow(factor_id: int, req: WorkflowSaveRequest, db: AsyncSession = Depends(get_db)):
    """更新工作流因子"""
    from factor.graph_compiler import compile_graph
    result = await db.execute(select(Factor).where(Factor.id == factor_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="因子不存在")

    compiled = compile_graph(req.graph)
    if compiled["errors"]:
        raise HTTPException(status_code=400, detail=compiled["errors"][0])

    f.name = req.name
    f.description = req.description
    f.expression = compiled["expression"]
    f.graph_json = req.graph
    f.factor_type = req.factor_type
    f.category = req.category
    await db.commit()
    return {"id": f.id, "expression": compiled["expression"]}


@router.get("/{factor_id}/graph")
async def get_factor_graph(factor_id: int, db: AsyncSession = Depends(get_db)):
    """加载工作流JSON"""
    result = await db.execute(select(Factor).where(Factor.id == factor_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status_code=404, detail="因子不存在")
    return {
        "id": f.id, "name": f.name,
        "expression": f.expression,
        "graph_json": f.graph_json,
        "factor_type": getattr(f, 'factor_type', 'technical'),
    }


@router.get("/workflow/templates")
async def get_workflow_templates():
    """内置工作流模板"""
    from factor.graph_compiler import NODE_REGISTRY, CATEGORY_COLORS
    templates = [
        {
            "name": "动量因子",
            "description": "close / delay(close, 20) - 1",
            "graph": _make_template_momentum(),
        },
        {
            "name": "波动率因子",
            "description": "std(returns, 20)",
            "graph": _make_template_volatility(),
        },
        {
            "name": "价值因子",
            "description": "1 / pe",
            "graph": _make_template_value(),
        },
        {
            "name": "反转因子",
            "description": "-1 * (close / delay(close, 5) - 1)",
            "graph": _make_template_reversal(),
        },
        {
            "name": "量价背离",
            "description": "corr(close, volume, 10)",
            "graph": _make_template_vol_price(),
        },
    ]
    node_registry = {
        k: {"inputs": v["inputs"], "outputs": v["outputs"],
             "params": v["params"], "category": v["category"], "label": v["label"],
             "tooltip": v.get("tooltip", "")}
        for k, v in NODE_REGISTRY.items()
    }
    return {"templates": templates, "node_registry": node_registry, "category_colors": CATEGORY_COLORS}


@router.get("/workflow/alpha191_v2/{number}")
async def get_alpha191_template_v2(number: int):
    """获取 Alpha191 因子模板 V2（带工作流图）"""
    if number < 1 or number > 191:
        raise HTTPException(status_code=400, detail="编号必须在 1-191 之间")

    formula = get_alpha191_formula(number)
    if not formula:
        raise HTTPException(status_code=404, detail=f"Alpha#{number} 暂未实现")

    parser = ExpressionToGraph()
    graph = parser.parse(formula)

    return {
        "name": f"Alpha#{number}",
        "description": f"Alpha191 第{number}号因子",
        "expression": formula,
        "category": "Alpha191",
        "graph": graph
    }


@router.get("/workflow/alpha191/{number}")
async def get_alpha191_template(number: int):
    """获取 Alpha191 因子模板（自动生成工作流图）"""
    import logging
    logger = logging.getLogger("qtsys.api")

    logger.info(f"=== Alpha191 API called with number={number} ===")

    if number < 1 or number > 191:
        raise HTTPException(status_code=400, detail="编号必须在 1-191 之间")

    formula = get_alpha191_formula(number)
    logger.info(f"Formula: {formula[:50]}")

    if not formula:
        raise HTTPException(status_code=404, detail=f"Alpha#{number} 暂未实现")

    parser = ExpressionToGraph()
    graph = parser.parse(formula)
    logger.info(f"Graph generated: {len(graph['nodes'])} nodes, {len(graph['edges'])} edges")

    result = {
        "name": f"Alpha#{number}",
        "description": f"Alpha191 第{number}号因子",
        "expression": formula,
        "category": "Alpha191",
        "graph": graph
    }

    logger.info(f"Result keys: {list(result.keys())}")
    logger.info(f"Has graph: {'graph' in result}")

    return result


def _make_template_momentum():
    """动量因子模板: close / delay(close, 20) - 1"""
    return {
        "nodes": [
            {"id": "n1", "type": "input_close", "x": 50, "y": 100, "params": {}},
            {"id": "n2", "type": "ts_delay", "x": 250, "y": 200, "params": {"periods": 20}},
            {"id": "n3", "type": "math_div", "x": 450, "y": 150, "params": {}},
            {"id": "n4", "type": "input_constant", "x": 250, "y": 350, "params": {"value": 1.0}},
            {"id": "n5", "type": "math_sub", "x": 650, "y": 200, "params": {}},
            {"id": "n6", "type": "output", "x": 850, "y": 200, "params": {}},
        ],
        "edges": [
            {"id": "e1", "from": {"nodeId": "n1", "port": "out"}, "to": {"nodeId": "n2", "port": "series"}},
            {"id": "e2", "from": {"nodeId": "n1", "port": "out"}, "to": {"nodeId": "n3", "port": "a"}},
            {"id": "e3", "from": {"nodeId": "n2", "port": "out"}, "to": {"nodeId": "n3", "port": "b"}},
            {"id": "e4", "from": {"nodeId": "n3", "port": "out"}, "to": {"nodeId": "n5", "port": "a"}},
            {"id": "e5", "from": {"nodeId": "n4", "port": "out"}, "to": {"nodeId": "n5", "port": "b"}},
            {"id": "e6", "from": {"nodeId": "n5", "port": "out"}, "to": {"nodeId": "n6", "port": "in"}},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
    }


def _make_template_value():
    return {
        "nodes": [
            {"id": "n1", "type": "input_constant", "x": 50, "y": 100, "params": {"value": 1.0}},
            {"id": "n2", "type": "input_pe", "x": 50, "y": 250, "params": {}},
            {"id": "n3", "type": "math_div", "x": 300, "y": 150, "params": {}},
            {"id": "n4", "type": "output", "x": 550, "y": 150, "params": {}},
        ],
        "edges": [
            {"id": "e1", "from": {"nodeId": "n1", "port": "out"}, "to": {"nodeId": "n3", "port": "a"}},
            {"id": "e2", "from": {"nodeId": "n2", "port": "out"}, "to": {"nodeId": "n3", "port": "b"}},
            {"id": "e3", "from": {"nodeId": "n3", "port": "out"}, "to": {"nodeId": "n4", "port": "in"}},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
    }


def _make_template_reversal():
    return {
        "nodes": [
            {"id": "n1", "type": "input_close", "x": 50, "y": 150, "params": {}},
            {"id": "n2", "type": "ts_pctchange", "x": 300, "y": 150, "params": {"periods": 5}},
            {"id": "n3", "type": "math_neg", "x": 550, "y": 150, "params": {}},
            {"id": "n4", "type": "output", "x": 750, "y": 150, "params": {}},
        ],
        "edges": [
            {"id": "e1", "from": {"nodeId": "n1", "port": "out"}, "to": {"nodeId": "n2", "port": "series"}},
            {"id": "e2", "from": {"nodeId": "n2", "port": "out"}, "to": {"nodeId": "n3", "port": "in"}},
            {"id": "e3", "from": {"nodeId": "n3", "port": "out"}, "to": {"nodeId": "n4", "port": "in"}},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
    }

def _make_template_volatility():
    return {
        "nodes": [
            {"id": "n1", "type": "input_returns", "x": 50, "y": 150, "params": {}},
            {"id": "n2", "type": "ts_std", "x": 300, "y": 150, "params": {"window": 20}},
            {"id": "n3", "type": "output", "x": 550, "y": 150, "params": {}},
        ],
        "edges": [
            {"id": "e1", "from": {"nodeId": "n1", "port": "out"}, "to": {"nodeId": "n2", "port": "series"}},
            {"id": "e2", "from": {"nodeId": "n2", "port": "out"}, "to": {"nodeId": "n3", "port": "in"}},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
    }

def _make_template_vol_price():
    return {
        "nodes": [
            {"id": "n1", "type": "input_close", "x": 50, "y": 100, "params": {}},
            {"id": "n2", "type": "input_volume", "x": 50, "y": 250, "params": {}},
            {"id": "n3", "type": "ts_corr", "x": 300, "y": 150, "params": {"window": 10}},
            {"id": "n4", "type": "output", "x": 550, "y": 150, "params": {}},
        ],
        "edges": [
            {"id": "e1", "from": {"nodeId": "n1", "port": "out"}, "to": {"nodeId": "n3", "port": "a"}},
            {"id": "e2", "from": {"nodeId": "n2", "port": "out"}, "to": {"nodeId": "n3", "port": "b"}},
            {"id": "e3", "from": {"nodeId": "n3", "port": "out"}, "to": {"nodeId": "n4", "port": "in"}},
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 1.0},
    }
