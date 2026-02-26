"""新闻API接口"""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from pydantic import BaseModel
from typing import Optional
from database.connection import get_db
from database.models import NewsArticle, Settings

router = APIRouter(prefix="/api/news", tags=["news"])


# ===== 获取新闻列表 =====

@router.get("")
async def list_news(
    source: Optional[str] = None,
    sentiment: Optional[str] = None,
    sector: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    db: AsyncSession = Depends(get_db),
):
    query = select(NewsArticle).order_by(desc(NewsArticle.publish_time))
    if source:
        query = query.where(NewsArticle.source == source)
    if sentiment:
        query = query.where(NewsArticle.sentiment_label == sentiment)
    if keyword:
        query = query.where(NewsArticle.title.contains(keyword))
    query = query.limit(limit)
    result = await db.execute(query)
    articles = result.scalars().all()

    # 如果按板块过滤，在Python层做(JSON字段)
    items = []
    for a in articles:
        if sector and sector not in (a.sectors or []):
            continue
        items.append(_article_to_dict(a, include_content=False))
    return items


# ===== 刷新新闻 =====

@router.post("/refresh")
async def refresh_news(db: AsyncSession = Depends(get_db)):
    """抓取最新新闻并分析情感"""
    from news.scraper import scrape_all
    from news.analyzer import analyze_sentiment

    items = await scrape_all()
    saved = 0
    for item in items:
        existing = await db.execute(
            select(NewsArticle).where(NewsArticle.title == item.title)
        )
        if existing.scalar_one_or_none():
            continue

        sr = analyze_sentiment(item.title, item.summary)
        article = NewsArticle(
            title=item.title, url=item.url,
            source=item.source,
            publish_time=item.publish_time,
            summary=item.summary,
            content=item.content,
            category=item.category,
            sentiment_score=sr.score,
            sentiment_label=sr.label,
            impact_level=sr.impact_level,
            sectors=sr.sectors,
            keywords_hit=sr.keywords_hit,
            sentiment_reason=sr.reason,
        )
        db.add(article)
        saved += 1

    await db.commit()
    return {
        "message": f"已抓取{len(items)}条, 新增{saved}条",
        "total_fetched": len(items),
        "new_saved": saved,
    }


# ===== 市场情绪汇总 (必须在 /{news_id} 之前) =====

@router.get("/sentiment/summary")
async def sentiment_summary(db: AsyncSession = Depends(get_db)):
    """获取最近新闻的市场情绪汇总"""
    from news.analyzer import aggregate_sentiment

    result = await db.execute(
        select(NewsArticle)
        .order_by(desc(NewsArticle.publish_time))
        .limit(100)
    )
    articles = result.scalars().all()
    sentiments = [
        {
            "label": a.sentiment_label,
            "score": a.sentiment_score,
            "sectors": a.sectors or [],
        }
        for a in articles
    ]
    return aggregate_sentiment(sentiments)


# ===== 获取单条新闻详情 =====

@router.get("/{news_id}")
async def get_news_detail(news_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(NewsArticle).where(NewsArticle.id == news_id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="新闻不存在")

    if not article.content and article.url:
        from news.scraper import fetch_article_content
        content = await fetch_article_content(article.url)
        if content:
            article.content = content
            await db.commit()

    return _article_to_dict(article, include_content=True)


# ===== LLM深度分析 =====

@router.post("/{news_id}/analyze_llm")
async def llm_analyze(news_id: int, db: AsyncSession = Depends(get_db)):
    """使用LLM对单条新闻进行深度分析"""
    result = await db.execute(select(NewsArticle).where(NewsArticle.id == news_id))
    article = result.scalar_one_or_none()
    if not article:
        raise HTTPException(status_code=404, detail="新闻不存在")

    settings_r = await db.execute(select(Settings).where(Settings.id == 1))
    settings = settings_r.scalar_one_or_none()
    if not settings or not settings.llm_api_key or not settings.llm_base_url:
        raise HTTPException(status_code=400, detail="请先在设置页面配置LLM API")

    from news.analyzer import analyze_with_llm
    llm_result = await analyze_with_llm(
        article.title, article.summary or "",
        settings.llm_api_key, settings.llm_base_url,
        settings.llm_model or "gpt-3.5-turbo",
    )
    if not llm_result:
        raise HTTPException(status_code=500, detail="LLM分析失败")

    if "score" in llm_result:
        article.sentiment_score = float(llm_result["score"])
    if "sentiment" in llm_result:
        article.sentiment_label = llm_result["sentiment"]
    if "impact_level" in llm_result:
        article.impact_level = llm_result["impact_level"]
    if "sectors" in llm_result:
        article.sectors = llm_result["sectors"]
    if "reason" in llm_result:
        article.sentiment_reason = llm_result["reason"]
    await db.commit()

    return llm_result


# ===== 辅助函数 =====

def _article_to_dict(a: NewsArticle, include_content=False) -> dict:
    d = {
        "id": a.id,
        "title": a.title,
        "url": a.url,
        "source": a.source,
        "publish_time": a.publish_time or "",
        "summary": a.summary or "",
        "category": a.category or "",
        "sentiment_score": a.sentiment_score,
        "sentiment_label": a.sentiment_label,
        "impact_level": a.impact_level,
        "sectors": a.sectors or [],
        "keywords_hit": a.keywords_hit or [],
        "sentiment_reason": a.sentiment_reason or "",
        "created_at": a.created_at.isoformat() if a.created_at else "",
    }
    if include_content:
        d["content"] = a.content or ""
    return d
