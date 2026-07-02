# -*- coding: utf-8 -*-
"""Test all optimization changes."""
from __future__ import annotations
import sys, os, subprocess, inspect, importlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
import pandas as pd

PASS, FAIL = [], []

def _check(name, ok, detail=""):
    sym = "OK  " if ok else "FAIL"
    print(f"  [{sym}] {name}" + (f" ({detail})" if not ok and detail else ""))
    (PASS if ok else FAIL).append(name)

# 1. Compile check
print("=== 1. Compile Check ===")
for m in ["engine/backtest_engine.py","factor/factor_engine.py","data/data_cache.py","engine/metrics.py"]:
    r = subprocess.run([sys.executable, "-m", "py_compile", m], capture_output=True, text=True)
    _check(f"py_compile {m}", r.returncode == 0, r.stderr.strip())

# 2. AST security
print("\n=== 2. AST Safety (factor_engine) ===")
from factor.factor_engine import _ast_safe_check

for expr in ["close.rolling(5).mean()", "mean(close,20)/mean(close,60)-1", "rank(close)", "(close-low)/(high-low+1e-9)"]:
    _check(f"safe:  {expr[:45]}", _ast_safe_check(expr))

for expr in ["getattr(close,'__class__')", "__import__('os')", "eval('1+1')", "globals()['os']", "exec('pass')", "open('/etc/passwd')", "locals()", "vars(close)"]:
    _check(f"block: {expr[:45]}", not _ast_safe_check(expr))

# 3. Factor expression evaluation
print("\n=== 3. Factor Expressions ===")
from factor.factor_engine import FactorEngine

class FakeCache:
    def get_daily(self, *a, **kw): return pd.DataFrame()
    def get_daily_basic(self, *a, **kw): return pd.DataFrame()

fe = FactorEngine(FakeCache())
n = 60
dates = pd.date_range("2023-01-01", periods=n, freq="B")
c = pd.Series(np.cumsum(np.random.randn(n)) + 100, index=dates)
o = c.shift(1).fillna(c.iloc[0])
h, l, v = c * 1.02, c * 0.98, pd.Series(np.random.randint(1000,10000,n).astype(float), index=dates)

r_evil = fe._eval_expression("eval('1+1')", c, h, l, v, o)
_check("dangerous expr blocked", r_evil is None)

r_ma = fe._eval_expression("mean(close, 5)", c, h, l, v, o)
_check("mean(close,5) returns Series", isinstance(r_ma, pd.Series))
_check("mean(close,5) last value finite", r_ma is not None and np.isfinite(r_ma.dropna().iloc[-1]))

r_sma = fe._eval_expression("sma(close, 3, 1)", c, h, l, v, o)
_check("sma(close,3,1) returns Series", isinstance(r_sma, pd.Series))

r_rank = fe._eval_expression("rank(close)", c, h, l, v, o)
_check("rank(close) returns Series", isinstance(r_rank, pd.Series))
_check("rank(close) values in [0,1]", r_rank is not None and r_rank.dropna().between(0, 1).all())

# 4. Metrics
print("\n=== 4. Metrics ===")
from engine.metrics import calc_metrics

rets = [0.01]*50 + [-0.02]*20 + [0.01]*30
final = float(1_000_000 * np.prod([1+r for r in rets]))
m = calc_metrics(rets, [], 1_000_000, final)
_check("max_drawdown_duration present", "max_drawdown_duration" in m)
_check("max_drawdown_duration == 50 (never recovers)", m.get("max_drawdown_duration") == 50)
_check("max_drawdown < 0", m.get("max_drawdown", 0) < 0)
_check("sharpe_ratio present", "sharpe_ratio" in m)

# 5. DataCache structure
print("\n=== 5. DataCache Structure ===")
# Mock tushare (not installed in test VM)
import types
ts_mock = types.ModuleType("tushare")
ts_mock.pro_api = lambda *a, **kw: None
sys.modules.setdefault("tushare", ts_mock)
dc_mod = importlib.import_module("data.data_cache")
DC = dc_mod.DataCache
_check("_get_daily_core exists", hasattr(DC, "_get_daily_core"))
_check("_mysql_read has lock", "with self._mysql_lock" in inspect.getsource(DC._mysql_read))
_check("_mysql_write has lock", "with self._mysql_lock" in inspect.getsource(DC._mysql_write))
_check("get_daily uses _get_daily_core", "_get_daily_core" in inspect.getsource(DC.get_daily))
_check("get_daily refactored (<30 lines)", inspect.getsource(DC.get_daily).count("\n") < 30)

# 6. BacktestEngine parallel load
print("\n=== 6. BacktestEngine Parallel Load ===")
be_mod = importlib.import_module("engine.backtest_engine")
BE = be_mod.BacktestEngine
run_src = inspect.getsource(BE.run)
_check("ThreadPoolExecutor in run()", "ThreadPoolExecutor" in run_src)
_check("to_dict records vectorized", 'to_dict("records")' in run_src)
_check("iterrows removed from run()", "iterrows()" not in run_src)

# Summary
print(f"\n{'='*45}")
print(f"Passed: {len(PASS)}  Failed: {len(FAIL)}")
if FAIL:
    for f in FAIL:
        print(f"  - {f}")
    sys.exit(1)
else:
    print("All optimization tests passed.")
