from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.data_cache import DataCache, make_mysql_conn
from data.tushare_client import TushareClient
from database.connection import get_session_factory
from factor.factor_backtest import run_selection_backtest
from factor.factor_engine import FactorEngine
from services.backtest_service import resolve_backtest_universe
from services.settings_service import get_or_create_settings


def parse_joinquant_transactions(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path, encoding="gbk")
    records: list[dict] = []
    for _, row in frame.iterrows():
        symbol_text = str(row.iloc[3] or "")
        match = re.search(r"\((\d{6})\.X(SHG|SHE)\)", symbol_text)
        if not match:
            continue
        shares_match = re.search(r"-?\d+", str(row.iloc[6] or ""))
        if not shares_match:
            continue
        code = match.group(1) + (".SH" if match.group(2) == "SHG" else ".SZ")
        action = "buy" if str(row.iloc[4]).strip() == "买" else "sell"
        records.append(
            {
                "date": str(row.iloc[0]).strip(),
                "action": action,
                "code": code,
                "shares": abs(int(shares_match.group(0))),
                "price": float(row.iloc[7]),
                "amount": float(row.iloc[8]),
                "fee": float(row.iloc[12]),
            }
        )
    return pd.DataFrame(records)


def _records(frame: pd.DataFrame, date: str) -> list[dict]:
    if frame.empty:
        return []
    return frame[frame["date"] == date][["action", "code", "shares", "price", "fee"]].to_dict("records")


def _codes(records: list[dict]) -> list[str]:
    return [str(item.get("code") or "") for item in records if item.get("code")]


def _first_day_diagnostic(jq_day: list[dict], local_day: list[dict]) -> dict:
    jq_codes = set(_codes(jq_day))
    local_codes = set(_codes(local_day))
    return {
        "missing_in_local": sorted(jq_codes - local_codes),
        "extra_in_local": sorted(local_codes - jq_codes),
        "common": sorted(jq_codes & local_codes),
        "note": (
            "If prices and fees match but stock sets differ, the dominant cause is usually a different "
            "point-in-time universe or a different factor-data window. Regenerate JoinQuant code after "
            "this fix: QTsys now embeds the same fixed universe snapshot used by local backtests."
        ),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Compare QTsys factor execution with JoinQuant transaction CSV.")
    parser.add_argument("--csv", default=str(ROOT / "test-results" / "transaction.csv"))
    parser.add_argument("--expression", default="-std(returns, 20)")
    parser.add_argument("--direction", default="bottom", choices=["top", "bottom"])
    parser.add_argument("--universe-code", default="000016.SH")
    parser.add_argument("--universe-name", default="上证50")
    parser.add_argument("--start", default="20240501")
    parser.add_argument("--end", default="20260514")
    parser.add_argument("--select-pct", type=float, default=0.1)
    parser.add_argument("--rebalance-days", type=int, default=10)
    args = parser.parse_args()

    jq = parse_joinquant_transactions(Path(args.csv))
    if jq.empty:
        print("No JoinQuant transactions parsed.")
        return

    async with get_session_factory()() as db:
        settings = await get_or_create_settings(db)
        resolved = await resolve_backtest_universe(
            db,
            settings,
            universe_type="system",
            universe_code=args.universe_code,
            universe_name=args.universe_name,
            as_of_date=args.start,
        )
        cache = DataCache(TushareClient(settings.tushare_token), mysql_conn=make_mysql_conn(settings))
        engine = FactorEngine(cache)
        result = run_selection_backtest(
            cache,
            engine,
            args.expression,
            resolved["codes"],
            args.start,
            args.end,
            args.direction,
            args.select_pct,
            args.rebalance_days,
            1_000_000,
            args.universe_code,
            settings.commission_rate,
            settings.stamp_tax_rate,
            settings.slippage,
            0.12,
            0.95,
        )

        local = pd.DataFrame(result.get("trades", []))
        print("=== Config ===")
        print(
            {
                "expression": args.expression,
                "direction": args.direction,
                "universe": args.universe_code,
                "universe_as_of": resolved.get("universe_as_of_date"),
                "start": args.start,
                "end": args.end,
                "select_pct": args.select_pct,
                "rebalance_days": args.rebalance_days,
            }
        )
        print("\n=== Metrics ===")
        print(result.get("metrics"))
        print("\n=== First day ===")
        first_date = str(jq.iloc[0]["date"])
        jq_first = _records(jq, first_date)
        local_first = _records(local, first_date)
        print("JoinQuant:", jq_first)
        print("Local:", local_first)
        print("Diagnostic:", _first_day_diagnostic(jq_first, local_first))
        print("\n=== First differing trade day ===")
        all_dates = sorted(set(jq["date"]).union(set(local["date"] if not local.empty else [])))
        for date in all_dates:
            jq_day = _records(jq, date)
            local_day = _records(local, date)
            if jq_day != local_day:
                print(date)
                print("JoinQuant:", jq_day[:12])
                print("Local:", local_day[:12])
                break


if __name__ == "__main__":
    asyncio.run(main())
