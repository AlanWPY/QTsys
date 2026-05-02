"""Factor mining API."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from services.factor_mining_service import (
    get_factor_mining_options,
    get_mining_research_report,
    get_streaming_mining_results,
    get_streaming_mining_status,
    revalidate_mining_candidate,
    run_factor_mining_workflow,
    save_mined_factor,
    start_streaming_mining_session,
    stop_streaming_mining_session,
)

router = APIRouter(prefix="/api/factor_mining", tags=["factor_mining"])


class FactorMiningRequest(BaseModel):
    universe_type: str = "system"
    universe_code: str = "000016.SH"
    custom_pool_id: Optional[int] = None
    start_date: str
    end_date: str
    max_stocks: int = Field(default=50, ge=5, le=500)
    candidate_count: int = Field(default=12, ge=3, le=40)
    gp_generations: int = Field(default=2, ge=0, le=8)
    gp_population: int = Field(default=12, ge=0, le=40)
    select_pct: float = Field(default=0.1, ge=0.02, le=0.5)
    rebalance_days: int = Field(default=5, ge=1, le=60)
    max_depth: int = Field(default=4, ge=2, le=6)
    max_expression_length: int = Field(default=600, ge=120, le=1200)
    auto_stop_candidates: int = Field(default=0, ge=0, le=500)
    protocol_version: str = "v4"
    research_mode: str = "professional"
    factor_themes: list[str] = Field(default_factory=list)
    neutralize: str = "rank_zscore"
    walk_forward_windows: int = Field(default=3, ge=2, le=8)
    embargo_days: int = Field(default=5, ge=0, le=30)
    max_trials: int = Field(default=0, ge=0, le=100000)
    capacity_limit_pct: float = Field(default=0.10, ge=0.01, le=0.50)
    min_dsr: float = Field(default=-0.25, ge=-5.0, le=5.0)


class SaveMinedFactorRequest(BaseModel):
    name: str
    description: str = ""
    expression: str
    candidate_id: Optional[int] = None


@router.get("/options")
async def options(db: AsyncSession = Depends(get_db)):
    return await get_factor_mining_options(db)


@router.post("/run")
async def run_mining(req: FactorMiningRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await run_factor_mining_workflow(
            db,
            universe_type=req.universe_type,
            universe_code=req.universe_code,
            custom_pool_id=req.custom_pool_id,
            start_date=req.start_date,
            end_date=req.end_date,
            max_stocks=req.max_stocks,
            candidate_count=req.candidate_count,
            gp_generations=req.gp_generations,
            gp_population=req.gp_population,
            select_pct=req.select_pct,
            rebalance_days=req.rebalance_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/sessions")
async def start_session(req: FactorMiningRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await start_streaming_mining_session(db, req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/sessions/{session_id}/status")
async def session_status(session_id: str, db: AsyncSession = Depends(get_db)):
    try:
        return await get_streaming_mining_status(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/sessions/{session_id}/results")
async def session_results(
    session_id: str,
    after_id: int = 0,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    try:
        return await get_streaming_mining_results(db, session_id, after_id=after_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str, db: AsyncSession = Depends(get_db)):
    try:
        return await stop_streaming_mining_session(db, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/save_factor")
async def save_factor(req: SaveMinedFactorRequest, db: AsyncSession = Depends(get_db)):
    try:
        return await save_mined_factor(db, req.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/candidates/{candidate_id}/revalidate")
async def revalidate_candidate(candidate_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await revalidate_mining_candidate(db, candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/candidates/{candidate_id}/strict_revalidate")
async def strict_revalidate_candidate(candidate_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await revalidate_mining_candidate(db, candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/research_report/{candidate_id}")
async def research_report(candidate_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await get_mining_research_report(db, candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
