"""QTsys - 量化交易回测系统 FastAPI入口"""
import sys
import os
from contextlib import asynccontextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from database.connection import init_db
from api.routes_settings import router as settings_router
from api.routes_data import router as data_router
from api.routes_strategy import router as strategy_router
from api.routes_backtest import router as backtest_router
from api.routes_factor import router as factor_router
from api.routes_news import router as news_router
from api.routes_market import router as market_router

STATIC_DIR = os.path.join(BASE_DIR, "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="QTsys", description="量化交易回测系统", lifespan=lifespan)

app.include_router(settings_router)
app.include_router(data_router)
app.include_router(strategy_router)
app.include_router(backtest_router)
app.include_router(factor_router)
app.include_router(news_router)
app.include_router(market_router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
