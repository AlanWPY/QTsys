"""数据库连接"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from config import DATABASE_URL


engine = create_async_engine(DATABASE_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with async_session() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        from database.models import Settings, Strategy, BacktestResult, Factor, FactorResult, NewsArticle
        await conn.run_sync(Base.metadata.create_all)
        # 兼容旧库: 添加 benchmark_curve 列
        try:
            await conn.execute(
                text("ALTER TABLE backtest_results ADD COLUMN benchmark_curve JSON DEFAULT '[]'")
            )
        except Exception:
            pass  # 列已存在则忽略
        # 兼容旧库: 添加 LLM 配置列
        for col in ['llm_api_key', 'llm_base_url', 'llm_model']:
            try:
                await conn.execute(
                    text(f"ALTER TABLE settings ADD COLUMN {col} VARCHAR(500) DEFAULT ''")
                )
            except Exception:
                pass
