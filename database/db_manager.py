"""因子看板结果数据库管理。"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

import pandas as pd
import pymysql
from sqlalchemy import create_engine

from .db_config import load_db_config


class DatabaseManager:
    def __init__(self, config: Optional[dict] = None):
        self.config = config or load_db_config()
        self.conn = None

    def connect(self):
        self.conn = pymysql.connect(
            host=self.config['host'],
            port=self.config['port'],
            user=self.config['user'],
            password=self.config['password'],
            database=self.config.get('database', 'qtsys'),
            charset='utf8mb4',
            autocommit=False,
        )
        return self.conn

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def _engine(self):
        return create_engine(
            f"mysql+pymysql://{self.config['user']}:{self.config['password']}@{self.config['host']}:{self.config['port']}/{self.config['database']}?charset=utf8mb4"
        )

    def init_tables(self):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS factor_analysis_results (
                id INT AUTO_INCREMENT PRIMARY KEY,
                analysis_batch VARCHAR(32) DEFAULT '',
                factor_name VARCHAR(100) NOT NULL,
                min_quantile_excess_return DECIMAL(12,6),
                max_quantile_excess_return DECIMAL(12,6),
                benchmark_return DECIMAL(12,6),
                min_quantile_excess DECIMAL(12,6),
                max_quantile_excess DECIMAL(12,6),
                min_quantile_turnover DECIMAL(12,6),
                max_quantile_turnover DECIMAL(12,6),
                ic_mean DECIMAL(12,8),
                ir_value DECIMAL(12,8),
                coverage_ratio DECIMAL(12,6),
                universe_size INT DEFAULT 0,
                rebalance_count INT DEFAULT 0,
                latest_trade_date DATE NULL,
                analysis_date DATETIME NOT NULL,
                start_date DATE,
                end_date DATE,
                backtest_days INT DEFAULT 730,
                universe_type VARCHAR(20) DEFAULT 'system',
                universe_code VARCHAR(50) DEFAULT '',
                universe_name VARCHAR(120) DEFAULT '',
                INDEX idx_batch (analysis_batch),
                INDEX idx_factor (factor_name),
                INDEX idx_date (analysis_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS factor_daily_holdings (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                factor_name VARCHAR(100) NOT NULL,
                trade_date DATE NOT NULL,
                ts_code VARCHAR(20) NOT NULL,
                factor_value DECIMAL(20,8),
                quantile TINYINT,
                weight DECIMAL(12,8),
                UNIQUE KEY uk_factor_date_code_q (factor_name, trade_date, ts_code, quantile),
                INDEX idx_factor_date (factor_name, trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS factor_daily_returns (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                factor_name VARCHAR(100) NOT NULL,
                trade_date DATE NOT NULL,
                quantile TINYINT NOT NULL,
                portfolio_value DECIMAL(20,8),
                daily_return DECIMAL(12,8),
                cumulative_return DECIMAL(12,8),
                UNIQUE KEY uk_factor_date_q (factor_name, trade_date, quantile),
                INDEX idx_factor_date (factor_name, trade_date)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )

        migrations = [
            "ALTER TABLE factor_analysis_results ADD COLUMN analysis_batch VARCHAR(32) DEFAULT ''",
            "ALTER TABLE factor_analysis_results ADD COLUMN universe_type VARCHAR(20) DEFAULT 'system'",
            "ALTER TABLE factor_analysis_results ADD COLUMN universe_code VARCHAR(50) DEFAULT ''",
            "ALTER TABLE factor_analysis_results ADD COLUMN universe_name VARCHAR(120) DEFAULT ''",
            "ALTER TABLE factor_analysis_results ADD COLUMN min_quantile_excess_return DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN max_quantile_excess_return DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN benchmark_return DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN min_quantile_excess DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN max_quantile_excess DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN min_quantile_turnover DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN max_quantile_turnover DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN ic_mean DECIMAL(12,8) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN ir_value DECIMAL(12,8) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN coverage_ratio DECIMAL(12,6) DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN universe_size INT DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN rebalance_count INT DEFAULT 0",
            "ALTER TABLE factor_analysis_results ADD COLUMN latest_trade_date DATE NULL",
            "ALTER TABLE factor_analysis_results ADD COLUMN start_date DATE NULL",
            "ALTER TABLE factor_analysis_results ADD COLUMN end_date DATE NULL",
            "ALTER TABLE factor_analysis_results ADD COLUMN backtest_days INT DEFAULT 730",
            "ALTER TABLE factor_daily_holdings ADD UNIQUE KEY uk_factor_date_code_q (factor_name, trade_date, ts_code, quantile)",
            "ALTER TABLE factor_daily_returns ADD COLUMN portfolio_value DECIMAL(20,8) DEFAULT 0",
            "ALTER TABLE factor_daily_returns ADD COLUMN daily_return DECIMAL(12,8) DEFAULT 0",
            "ALTER TABLE factor_daily_returns ADD COLUMN cumulative_return DECIMAL(12,8) DEFAULT 0",
            "ALTER TABLE factor_daily_returns ADD UNIQUE KEY uk_factor_date_q (factor_name, trade_date, quantile)",
        ]
        for sql in migrations:
            try:
                cursor.execute(sql)
            except Exception:
                pass

        self.conn.commit()
        cursor.close()

    def replace_factor_daily_data(self, factor_name: str):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM factor_daily_holdings WHERE factor_name = %s", (factor_name,))
        cursor.execute("DELETE FROM factor_daily_returns WHERE factor_name = %s", (factor_name,))
        self.conn.commit()
        cursor.close()

    def save_factor_result(self, result: dict, analysis_batch: str):
        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM factor_analysis_results WHERE analysis_batch = %s AND factor_name = %s",
            (analysis_batch, result['factor_name']),
        )
        cursor.execute(
            """
            INSERT INTO factor_analysis_results
            (
                analysis_batch, factor_name, min_quantile_excess_return, max_quantile_excess_return,
                benchmark_return, min_quantile_excess, max_quantile_excess,
                min_quantile_turnover, max_quantile_turnover, ic_mean, ir_value,
                coverage_ratio, universe_size, rebalance_count, latest_trade_date,
                analysis_date, start_date, end_date, backtest_days,
                universe_type, universe_code, universe_name
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                analysis_batch,
                result['factor_name'],
                result['min_q_return'],
                result['max_q_return'],
                result['benchmark_return'],
                result['min_q_excess'],
                result['max_q_excess'],
                result['min_q_turnover'],
                result['max_q_turnover'],
                result['ic_mean'],
                result['ir_value'],
                result.get('coverage_ratio', 0),
                result.get('universe_size', 0),
                result.get('rebalance_count', 0),
                result.get('latest_trade_date') or None,
                datetime.now(),
                result['start_date'],
                result['end_date'],
                result.get('backtest_days', 730),
                result.get('universe_type', 'system'),
                result.get('universe_code', ''),
                result.get('universe_name', ''),
            ),
        )
        self.conn.commit()
        cursor.close()

    def get_batch_result_count(self, analysis_batch: str) -> int:
        engine = self._engine()
        query = """
            SELECT COUNT(DISTINCT factor_name) AS factor_count
            FROM factor_analysis_results
            WHERE analysis_batch = %s
        """
        df = pd.read_sql(query, engine, params=(analysis_batch,))
        if df.empty:
            return 0
        return int(df.iloc[0].get("factor_count") or 0)

    def save_daily_holdings(self, factor_name, holdings_data):
        cursor = self.conn.cursor()
        sql = """
            INSERT INTO factor_daily_holdings
            (factor_name, trade_date, ts_code, factor_value, quantile, weight)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE factor_value=VALUES(factor_value), weight=VALUES(weight)
        """
        rows = [
            (factor_name, item['date'], item['stock'], item['factor_value'], item['quantile'], item['weight'])
            for item in holdings_data
        ]
        if rows:
            cursor.executemany(sql, rows)
            self.conn.commit()
        cursor.close()

    def save_daily_returns(self, factor_name, returns_data):
        cursor = self.conn.cursor()
        sql = """
            INSERT INTO factor_daily_returns
            (factor_name, trade_date, quantile, portfolio_value, daily_return, cumulative_return)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                portfolio_value=VALUES(portfolio_value),
                daily_return=VALUES(daily_return),
                cumulative_return=VALUES(cumulative_return)
        """
        rows = [
            (factor_name, item['date'], item['quantile'], item['portfolio_value'], item['daily_return'], item['cumulative_return'])
            for item in returns_data
        ]
        if rows:
            cursor.executemany(sql, rows)
            self.conn.commit()
        cursor.close()

    def get_latest_analysis_results(self):
        engine = self._engine()
        query = """
            SELECT factor_name, min_quantile_excess_return, max_quantile_excess_return,
                   benchmark_return, min_quantile_excess, max_quantile_excess,
                   min_quantile_turnover, max_quantile_turnover, ic_mean, ir_value,
                   coverage_ratio, universe_size, rebalance_count, latest_trade_date,
                   analysis_date, backtest_days, analysis_batch,
                   universe_type, universe_code, universe_name
            FROM factor_analysis_results
            WHERE analysis_batch = (
                SELECT analysis_batch
                FROM factor_analysis_results
                WHERE analysis_batch <> ''
                ORDER BY analysis_date DESC, id DESC
                LIMIT 1
            )
            ORDER BY max_quantile_excess DESC, ic_mean DESC, factor_name ASC
        """
        return pd.read_sql(query, engine)

    def get_latest_batch_summary(self):
        engine = self._engine()
        query = """
            SELECT analysis_batch,
                   MAX(analysis_date) AS analysis_date,
                   MIN(start_date) AS start_date,
                   MAX(end_date) AS end_date,
                   MAX(backtest_days) AS backtest_days,
                   MAX(universe_type) AS universe_type,
                   MAX(universe_code) AS universe_code,
                   MAX(universe_name) AS universe_name,
                   COUNT(DISTINCT factor_name) AS factor_count
            FROM factor_analysis_results
            WHERE analysis_batch = (
                SELECT analysis_batch
                FROM factor_analysis_results
                WHERE analysis_batch <> ''
                ORDER BY analysis_date DESC, id DESC
                LIMIT 1
            )
            GROUP BY analysis_batch
            LIMIT 1
        """
        df = pd.read_sql(query, engine)
        return df.iloc[0].to_dict() if not df.empty else None

    def get_factor_latest_result(self, factor_name: str):
        engine = self._engine()
        query = """
            SELECT factor_name, min_quantile_excess_return, max_quantile_excess_return,
                   benchmark_return, min_quantile_excess, max_quantile_excess,
                   min_quantile_turnover, max_quantile_turnover, ic_mean, ir_value,
                   coverage_ratio, universe_size, rebalance_count, latest_trade_date,
                   analysis_date, backtest_days, analysis_batch,
                   universe_type, universe_code, universe_name
            FROM factor_analysis_results
            WHERE factor_name = %s
            ORDER BY analysis_date DESC, id DESC
            LIMIT 1
        """
        df = pd.read_sql(query, engine, params=(factor_name,))
        return df.iloc[0].to_dict() if not df.empty else None

    def get_factor_daily_returns(self, factor_name):
        engine = self._engine()
        query = """
            SELECT trade_date, quantile, portfolio_value, daily_return, cumulative_return
            FROM factor_daily_returns
            WHERE factor_name = %s
            ORDER BY trade_date, quantile
        """
        return pd.read_sql(query, engine, params=(factor_name,))

    def get_factor_holdings(self, factor_name, trade_date=None):
        engine = self._engine()
        if trade_date:
            query = """
                SELECT trade_date, ts_code, factor_value, quantile, weight
                FROM factor_daily_holdings
                WHERE factor_name = %s AND trade_date = %s
                ORDER BY quantile, factor_value DESC
            """
            return pd.read_sql(query, engine, params=(factor_name, trade_date))
        query = """
            SELECT trade_date, ts_code, factor_value, quantile, weight
            FROM factor_daily_holdings
            WHERE factor_name = %s
            ORDER BY trade_date DESC, quantile, factor_value DESC
        """
        return pd.read_sql(query, engine, params=(factor_name,))
