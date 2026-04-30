"""数据库连接与 schema 初始化。"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import text
from config import DATABASE_URL
from database.secret_crypto import encrypt_secret, is_encrypted_secret
from logging_config import get_logger

logger = get_logger("qtsys.db")

_engine = None
_session_factory = None


class Base(DeclarativeBase):
    pass


def _create_engine(database_url: str):
    kwargs = {"echo": False}
    if database_url.startswith("mysql+"):
        kwargs["pool_pre_ping"] = True
    return create_async_engine(database_url, **kwargs)


def get_engine():
    global _engine
    if _engine is None:
        _engine = _create_engine(DATABASE_URL)
    return _engine


def get_session_factory():
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


def switch_to_mysql(mysql_url: str):
    """兼容旧接口：不再运行时切换 ORM 主库。"""
    logger.warning("switch_to_mysql() 已弃用；ORM 主库保持为 SQLite")


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


async def _init_schema(engine):
    async with engine.begin() as conn:
        from database.models import (
            Settings, Strategy, BacktestResult, Factor, FactorResult,
            FactorMiningSession, FactorMiningCandidate,
            FactorBacktestResult, NewsArticle, StockPool,
            DailyQuote, DailyBasic, IndexDaily, TradeCalendar,
        )
        await conn.run_sync(Base.metadata.create_all)
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
        try:
            rows = (
                await conn.execute(
                    text(
                        "SELECT id, tushare_token, llm_api_key, mysql_password "
                        "FROM settings"
                    )
                )
            ).mappings().all()
            for row in rows:
                updates = {}
                for field in ("tushare_token", "llm_api_key", "mysql_password"):
                    value = row.get(field) or ""
                    if value and not is_encrypted_secret(value):
                        updates[field] = encrypt_secret(value)
                if updates:
                    set_clause = ", ".join(f"{field} = :{field}" for field in updates)
                    await conn.execute(
                        text(f"UPDATE settings SET {set_clause} WHERE id = :id"),
                        {"id": row["id"], **updates},
                    )
        except Exception:
            logger.exception("敏感配置明文迁移失败")


async def init_db():
    await _init_schema(get_engine())


async def init_external_db(database_url: str):
    engine = _create_engine(database_url)
    try:
        await _init_schema(engine)
    finally:
        await engine.dispose()
