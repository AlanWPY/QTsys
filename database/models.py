"""数据库 ORM 模型"""
from datetime import datetime
from sqlalchemy import Column, Integer, String, Float, Text, DateTime, JSON, UniqueConstraint
from database.connection import Base
from database.encrypted_types import EncryptedString


class Settings(Base):
    __tablename__ = "settings"

    id = Column(Integer, primary_key=True, default=1)
    tushare_token = Column(EncryptedString(), default="")
    default_cash = Column(Float, default=1_000_000.0)
    commission_rate = Column(Float, default=0.0003)
    stamp_tax_rate = Column(Float, default=0.001)
    slippage = Column(Float, default=0.002)
    llm_api_key = Column(EncryptedString(), default="")
    llm_base_url = Column(String(500), default="")
    llm_model = Column(String(100), default="")
    mysql_host = Column(String(200), default="")
    mysql_port = Column(Integer, default=3306)
    mysql_user = Column(String(100), default="")
    mysql_password = Column(EncryptedString(), default="")
    mysql_database = Column(String(100), default="qtsys")
    use_mysql = Column(Integer, default=0)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    code = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BacktestResult(Base):
    __tablename__ = "backtest_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_id = Column(Integer, nullable=False)
    strategy_name = Column(String(100), default="")
    start_date = Column(String(10), nullable=False)
    end_date = Column(String(10), nullable=False)
    universe = Column(Text, default="")
    initial_cash = Column(Float, default=1_000_000.0)
    final_value = Column(Float, default=0.0)
    metrics = Column(JSON, default=dict)
    equity_curve = Column(JSON, default=list)
    trades = Column(JSON, default=list)
    daily_returns = Column(JSON, default=list)
    benchmark_curve = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class Factor(Base):
    __tablename__ = "factors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    expression = Column(Text, nullable=False)
    category = Column(String(50), default="custom")
    source = Column(String(20), default="user")
    graph_json = Column(JSON, default=None)
    factor_type = Column(String(20), default="technical")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FactorMiningSession(Base):
    __tablename__ = "factor_mining_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(40), nullable=False, unique=True)
    status = Column(String(20), default="pending")
    phase = Column(String(40), default="queued")
    message = Column(Text, default="")
    universe_type = Column(String(20), default="system")
    universe_code = Column(String(20), default="")
    universe_name = Column(String(100), default="")
    custom_pool_id = Column(Integer, default=None)
    stock_count = Column(Integer, default=0)
    start_date = Column(String(10), nullable=False)
    end_date = Column(String(10), nullable=False)
    params = Column(JSON, default=dict)
    protocol_version = Column(String(40), default="legacy_unverified")
    research_mode = Column(String(40), default="professional")
    factor_themes = Column(JSON, default=list)
    neutralize = Column(String(40), default="rank")
    walk_forward_windows = Column(Integer, default=3)
    embargo_days = Column(Integer, default=5)
    max_trials = Column(Integer, default=0)
    capacity_limit_pct = Column(Float, default=0.10)
    min_dsr = Column(Float, default=-0.25)
    tested_count = Column(Integer, default=0)
    accepted_count = Column(Integer, default=0)
    best_score = Column(Float, default=0.0)
    error = Column(Text, default="")
    started_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    stopped_at = Column(DateTime, default=None)


class FactorMiningCandidate(Base):
    __tablename__ = "factor_mining_candidates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(40), nullable=False)
    expression_hash = Column(String(64), nullable=False)
    name = Column(String(100), nullable=False)
    description = Column(Text, default="")
    expression = Column(Text, nullable=False)
    source = Column(String(40), default="")
    protocol_version = Column(String(40), default="legacy_unverified")
    theme = Column(String(80), default="")
    hypothesis = Column(Text, default="")
    preprocessing = Column(JSON, default=dict)
    significance = Column(JSON, default=dict)
    overfit_risk = Column(JSON, default=dict)
    capacity = Column(JSON, default=dict)
    robustness = Column(JSON, default=dict)
    fingerprint = Column(JSON, default=dict)
    correlation_cluster = Column(String(80), default="")
    revalidation_status = Column(String(40), default="")
    direction = Column(String(10), default="top")
    complexity = Column(Integer, default=0)
    score = Column(Float, default=0.0)
    metrics = Column(JSON, default=dict)
    train_metrics = Column(JSON, default=dict)
    valid_metrics = Column(JSON, default=dict)
    test_metrics = Column(JSON, default=dict)
    backtest_metrics = Column(JSON, default=dict)
    equity_curve = Column(JSON, default=list)
    normalized_curve = Column(JSON, default=list)
    is_pinned = Column(Integer, default=0)
    is_deleted = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("session_id", "expression_hash"),)


class FactorMiningTrialLog(Base):
    __tablename__ = "factor_mining_trial_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(40), nullable=False)
    expression_hash = Column(String(64), nullable=False)
    name = Column(String(100), default="")
    source = Column(String(40), default="")
    theme = Column(String(80), default="")
    expression = Column(Text, default="")
    stage = Column(String(40), default="")
    score = Column(Float, default=0.0)
    metrics = Column(JSON, default=dict)
    rejection_reasons = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("session_id", "expression_hash", "stage"),)


class FactorCorrelationCluster(Base):
    __tablename__ = "factor_correlation_clusters"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String(40), nullable=False)
    cluster_key = Column(String(80), nullable=False)
    representative_candidate_id = Column(Integer, default=None)
    members = Column(JSON, default=list)
    max_abs_corr = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (UniqueConstraint("session_id", "cluster_key"),)


class FactorResult(Base):
    __tablename__ = "factor_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    factor_id = Column(Integer, nullable=False)
    factor_name = Column(String(100), default="")
    universe = Column(Text, default="")
    start_date = Column(String(10), nullable=False)
    end_date = Column(String(10), nullable=False)
    metrics = Column(JSON, default=dict)
    ic_series = Column(JSON, default=list)
    group_returns = Column(JSON, default=list)
    turnover_series = Column(JSON, default=list)
    long_short_curve = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class FactorBacktestResult(Base):
    __tablename__ = "factor_backtest_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    factor_id = Column(Integer, nullable=False)
    backtest_type = Column(String(20), default="selection")
    params = Column(JSON, default=dict)
    metrics = Column(JSON, default=dict)
    equity_curve = Column(JSON, default=list)
    trades = Column(JSON, default=list)
    stock_pool_history = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(500), nullable=False)
    url = Column(String(1000), default="")
    source = Column(String(50), default="")
    publish_time = Column(String(20), default="")
    summary = Column(Text, default="")
    content = Column(Text, default="")
    category = Column(String(50), default="")
    sentiment_score = Column(Float, default=0.0)
    sentiment_label = Column(String(10), default="中性")
    impact_level = Column(String(10), default="低")
    sectors = Column(JSON, default=list)
    keywords_hit = Column(JSON, default=list)
    sentiment_reason = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class StockPool(Base):
    __tablename__ = "stock_pools"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    description = Column(Text, default="")
    pool_type = Column(String(20), default="custom")
    index_code = Column(String(20), default="")
    stock_items = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyQuote(Base):
    __tablename__ = "qtsys_daily_quotes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False)
    trade_date = Column(String(10), nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    vol = Column(Float)
    amount = Column(Float)
    pct_chg = Column(Float)
    __table_args__ = (UniqueConstraint("ts_code", "trade_date"),)


class DailyBasic(Base):
    __tablename__ = "qtsys_daily_basic"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False)
    trade_date = Column(String(10), nullable=False)
    pe = Column(Float)
    pb = Column(Float)
    ps = Column(Float)
    total_mv = Column(Float)
    circ_mv = Column(Float)
    turnover_rate = Column(Float)
    __table_args__ = (UniqueConstraint("ts_code", "trade_date"),)


class IndexDaily(Base):
    __tablename__ = "qtsys_index_daily"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts_code = Column(String(20), nullable=False)
    trade_date = Column(String(10), nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    vol = Column(Float)
    __table_args__ = (UniqueConstraint("ts_code", "trade_date"),)


class TradeCalendar(Base):
    __tablename__ = "qtsys_trade_calendar"
    id = Column(Integer, primary_key=True, autoincrement=True)
    cal_date = Column(String(10), nullable=False, unique=True)
    is_open = Column(Integer, default=1)
