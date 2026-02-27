"""QTsys - 量化金融分析系统 FastAPI入口"""
import sys
import os
import asyncio
from contextlib import asynccontextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from logging_config import setup_logging, get_logger
setup_logging()
logger = get_logger("qtsys.main")

from config import VERSION
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
from api.routes_ws import router as ws_router
from api.routes_factor_backtest import router as factor_backtest_router
from api.routes_quality import router as quality_router

STATIC_DIR = os.path.join(BASE_DIR, "static")

# 新闻自动刷新任务引用
_news_refresh_task = None


async def _auto_refresh_news():
    """后台定时刷新新闻 - 交易时段5分钟，其他时段15分钟"""
    from datetime import datetime
    from database.connection import async_session
    logger.info("新闻自动刷新任务已启动")
    while True:
        try:
            now = datetime.now()
            hour = now.hour
            minute = now.minute
            # 交易时段 9:00-15:30 缩短为5分钟
            is_trading = (hour == 9 and minute >= 0) or (10 <= hour <= 14) or (hour == 15 and minute <= 30)
            interval = 300 if is_trading else 900  # 5min vs 15min

            from news.scraper import scrape_all
            from news.analyzer import analyze_sentiment
            from database.models import NewsArticle
            from sqlalchemy import select

            items = await scrape_all()
            if items:
                async with async_session() as db:
                    saved = 0
                    for item in items:
                        existing = await db.execute(
                            select(NewsArticle).where(NewsArticle.title == item.title)
                        )
                        if existing.scalar_one_or_none():
                            continue
                        sr = analyze_sentiment(item.title, item.summary)
                        article = NewsArticle(
                            title=item.title, url=item.url, source=item.source,
                            publish_time=item.publish_time, summary=item.summary,
                            content=item.content, category=item.category,
                            sentiment_score=sr.score, sentiment_label=sr.label,
                            impact_level=sr.impact_level, sectors=sr.sectors,
                            keywords_hit=sr.keywords_hit, sentiment_reason=sr.reason,
                        )
                        db.add(article)
                        saved += 1
                    await db.commit()
                    logger.info(f"自动刷新: 抓取{len(items)}条, 新增{saved}条")
        except Exception:
            logger.exception("新闻自动刷新异常")
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _news_refresh_task
    logger.info(f"QTsys v{VERSION} 启动中...")
    await init_db()
    logger.info("数据库初始化完成")
    _news_refresh_task = asyncio.create_task(_auto_refresh_news())
    yield
    if _news_refresh_task:
        _news_refresh_task.cancel()
        logger.info("新闻自动刷新任务已停止")
    logger.info("QTsys 已关闭")

app = FastAPI(title="QTsys", description="量化交易回测系统", lifespan=lifespan)

app.include_router(settings_router)
app.include_router(data_router)
app.include_router(strategy_router)
app.include_router(backtest_router)
app.include_router(factor_router)
app.include_router(news_router)
app.include_router(market_router)
app.include_router(ws_router)
app.include_router(factor_backtest_router)
app.include_router(quality_router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/system/version")
async def get_version():
    """获取系统版本和本地commit信息（不联网）"""
    from updater import get_local_info
    return get_local_info()


@app.post("/api/system/check_update")
async def check_for_update():
    """检查远程是否有更新（联网fetch）"""
    from updater import check_update
    return await asyncio.to_thread(check_update)


@app.post("/api/system/update")
async def trigger_update():
    """手动触发更新"""
    from updater import check_update, do_update
    info = await asyncio.to_thread(check_update)
    if info["error"]:
        return {"success": False, "message": info["error"]}
    if not info["has_update"]:
        return {"success": True, "message": "已是最新版本"}
    result = await asyncio.to_thread(do_update)
    return result


if __name__ == "__main__":
    # 启动前自动检查更新（仅直接运行时执行，reload子进程不执行）
    from updater import auto_update_on_startup
    auto_update_on_startup()

    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
