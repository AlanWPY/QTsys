"""因子目录服务：统一为策略、AI、前端提供因子库摘要。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Factor
from factor.builtin_factors import BUILTIN_FACTORS


def _normalize_factor_item(item: dict) -> dict:
    return {
        "id": item.get("id"),
        "name": str(item.get("name") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "expression": str(item.get("expression") or "").strip(),
        "category": str(item.get("category") or "custom").strip() or "custom",
        "source": str(item.get("source") or "user").strip() or "user",
    }


async def load_factor_catalog(db: AsyncSession) -> list[dict]:
    """加载完整因子目录，优先数据库，缺失时补入内置因子。"""
    result = await db.execute(select(Factor).order_by(Factor.created_at.desc(), Factor.id.desc()))
    rows = result.scalars().all()
    catalog = []
    seen_names = set()

    for factor in rows:
        item = _normalize_factor_item(
            {
                "id": factor.id,
                "name": factor.name,
                "description": factor.description,
                "expression": factor.expression,
                "category": factor.category,
                "source": factor.source,
            }
        )
        if not item["name"]:
            continue
        catalog.append(item)
        seen_names.add(item["name"])

    builtin_id_seed = -1
    for name, info in BUILTIN_FACTORS.items():
        if name in seen_names:
            continue
        catalog.append(
            _normalize_factor_item(
                {
                    "id": builtin_id_seed,
                    "name": name,
                    "description": info.get("description", ""),
                    "expression": info.get("expression", f"builtin:{name}"),
                    "category": info.get("category", "builtin"),
                    "source": "builtin",
                }
            )
        )
        builtin_id_seed -= 1

    return catalog


async def load_factor_catalog_snapshot(db: AsyncSession, limit: int = 24) -> dict:
    catalog = await load_factor_catalog(db)
    trimmed = catalog[: max(1, limit)]
    return {
        "count": len(catalog),
        "items": trimmed,
    }
