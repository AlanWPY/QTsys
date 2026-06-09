"""数据库连接与 schema 初始化。"""
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import event, text
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
    if database_url.startswith("sqlite+"):
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_async_engine(database_url, **kwargs)
    if database_url.startswith("sqlite+"):
        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
    return engine


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
            FactorMiningSession, FactorMiningCandidate, FactorMiningTrialLog, FactorCorrelationCluster,
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
            ("factor_mining_sessions", "protocol_version", "VARCHAR(40) DEFAULT 'legacy_unverified'"),
            ("factor_mining_sessions", "research_mode", "VARCHAR(40) DEFAULT 'professional'"),
            ("factor_mining_sessions", "factor_themes", "JSON DEFAULT '[]'"),
            ("factor_mining_sessions", "neutralize", "VARCHAR(40) DEFAULT 'rank'"),
            ("factor_mining_sessions", "walk_forward_windows", "INTEGER DEFAULT 3"),
            ("factor_mining_sessions", "embargo_days", "INTEGER DEFAULT 5"),
            ("factor_mining_sessions", "max_trials", "INTEGER DEFAULT 0"),
            ("factor_mining_sessions", "capacity_limit_pct", "FLOAT DEFAULT 0.10"),
            ("factor_mining_sessions", "min_dsr", "FLOAT DEFAULT -0.25"),
            ("factor_mining_candidates", "protocol_version", "VARCHAR(40) DEFAULT 'legacy_unverified'"),
            ("factor_mining_candidates", "theme", "VARCHAR(80) DEFAULT ''"),
            ("factor_mining_candidates", "hypothesis", "TEXT DEFAULT ''"),
            ("factor_mining_candidates", "preprocessing", "JSON DEFAULT '{}'"),
            ("factor_mining_candidates", "significance", "JSON DEFAULT '{}'"),
            ("factor_mining_candidates", "overfit_risk", "JSON DEFAULT '{}'"),
            ("factor_mining_candidates", "capacity", "JSON DEFAULT '{}'"),
            ("factor_mining_candidates", "robustness", "JSON DEFAULT '{}'"),
            ("factor_mining_candidates", "fingerprint", "JSON DEFAULT '{}'"),
            ("factor_mining_candidates", "correlation_cluster", "VARCHAR(80) DEFAULT ''"),
            ("factor_mining_candidates", "revalidation_status", "VARCHAR(40) DEFAULT ''"),
            ("factor_mining_candidates", "is_pinned", "INTEGER DEFAULT 0"),
            ("factor_mining_candidates", "is_deleted", "INTEGER DEFAULT 0"),
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
    await _seed_builtin_factors()


async def _seed_builtin_factors():
    """确保系统预置因子在启动后可直接使用。"""
    from sqlalchemy import select
    from database.models import Factor
    from factor.builtin_factors import BUILTIN_FACTORS, normalize_category

    async with get_session_factory()() as session:
        changed = False
        for name, info in BUILTIN_FACTORS.items():
            result = await session.execute(select(Factor).where(Factor.name == name))
            existing = result.scalar_one_or_none()
            next_values = {
                "description": info["description"],
                "expression": info["expression"],
                "category": normalize_category(info["category"]),
                "source": "builtin",
            }
            if existing:
                for field, value in next_values.items():
                    if getattr(existing, field) != value:
                        setattr(existing, field, value)
                        changed = True
                continue
            session.add(Factor(name=name, **next_values))
            changed = True
        if changed:
            await session.commit()


async def init_external_db(database_url: str):
    engine = _create_engine(database_url)
    try:
        await _init_schema(engine)
    finally:
        await engine.dispose()
