"""数据库连接 - 支持 SQLite/MySQL 懒加载切换"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from config import DATABASE_URL
from logging_config import get_logger

logger = get_logger("qtsys.db")

_engine = None
_session_factory = None


class Base(DeclarativeBase):
    pass


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_async_engine(DATABASE_URL, echo=False)
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


def switch_to_mysql(mysql_url: str):
    """切换全局引擎到 MySQL"""
    global _engine, _session_factory
    if _engine:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_engine.dispose())
            else:
                loop.run_until_complete(_engine.dispose())
        except Exception:
            pass
    _engine = create_async_engine(mysql_url, echo=False, pool_pre_ping=True)
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )
    logger.info(f"已切换到 MySQL 引擎")


async def get_db():
    async with get_session_factory()() as session:
        yield session


class _SessionProxy:
    """代理对象，使 async_session() 调用等价于 get_session_factory()()"""
    def __call__(self):
        return get_session_factory()()
    def __aenter__(self, *a):
        return get_session_factory()().__aenter__(*a)

async_session = _SessionProxy()


async def init_db():
    async with get_engine().begin() as conn:
        from database.models import (
            Settings, Strategy, BacktestResult, Factor, FactorResult,
            FactorBacktestResult, NewsArticle,
            DailyQuote, DailyBasic, IndexDaily, TradeCalendar
        )
        await conn.run_sync(Base.metadata.create_all)
        # 兼容旧库迁移
        _migrate_cols = [
            ("backtest_results", "benchmark_curve", "JSON DEFAULT '[]'"),
            ("settings", "llm_api_key", "VARCHAR(500) DEFAULT ''"),
            ("settings", "llm_base_url", "VARCHAR(500) DEFAULT ''"),
            ("settings", "llm_model", "VARCHAR(500) DEFAULT ''"),
            ("settings", "mysql_host", "VARCHAR(200) DEFAULT ''"),
            ("settings", "mysql_port", "INTEGER DEFAULT 3306"),
            ("settings", "mysql_user", "VARCHAR(100) DEFAULT ''"),
            ("settings", "mysql_password", "VARCHAR(200) DEFAULT ''"),
            ("settings", "mysql_database", "VARCHAR(100) DEFAULT 'qtsys'"),
            ("settings", "use_mysql", "INTEGER DEFAULT 0"),
            ("factors", "graph_json", "DEFAULT NULL"),
            ("factors", "factor_type", "DEFAULT 'technical'"),
            ("factors", "updated_at", "DEFAULT NULL"),
        ]
        for table, col, typedef in _migrate_cols:
            try:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
                )
            except Exception:
                pass
