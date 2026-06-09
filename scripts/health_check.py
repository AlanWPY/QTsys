"""QTsys release health check.

This script is intentionally read-only for repo-tracked files. It validates
syntax, anti-lookahead invariants, obvious secrets, backend reachability, and
frontend smoke pages.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)
BACKEND_URL = "http://127.0.0.1:8000"
SKIP_PARTS = {".git", ".venv", "__pycache__", "runtime", "logs", "tmp", "test-results"}


def _run(cmd: list[str], *, timeout: int = 180) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode == 0, proc.stdout.strip()
    except Exception as exc:
        return False, str(exc)


def _url_status(url: str, timeout: int = 15) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status == 200, str(response.status)
    except Exception as exc:
        return False, str(exc)


def _iter_py_files() -> list[str]:
    files = []
    for path in ROOT.rglob("*.py"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        files.append(str(path))
    return files


def _start_backend_if_needed() -> subprocess.Popen | None:
    ok, _ = _url_status(f"{BACKEND_URL}/api/system/version", timeout=3)
    if ok:
        return None
    proc = subprocess.Popen(
        [str(PYTHON), "main.py"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 25
    while time.time() < deadline:
        ok, _ = _url_status(f"{BACKEND_URL}/api/system/version", timeout=3)
        if ok:
            return proc
        time.sleep(1)
    return proc


def check_py_compile() -> tuple[bool, str]:
    files = _iter_py_files()
    if not files:
        return False, "No Python files found"
    return _run([str(PYTHON), "-m", "py_compile", *files], timeout=180)


def check_script(script_name: str) -> tuple[bool, str]:
    return _run([str(PYTHON), str(ROOT / "scripts" / script_name)], timeout=180)


def check_backend() -> tuple[bool, str]:
    endpoints = [
        "/",
        "/api/system/health",
        "/api/factors",
        "/api/backtest/universe_options",
        "/api/factor_mining/options",
    ]
    failures = []
    for endpoint in endpoints:
        ok, message = _url_status(BACKEND_URL + endpoint)
        if not ok:
            failures.append(f"{endpoint}: {message}")
    return not failures, "\n".join(failures) if failures else "backend endpoints ok"


def check_frontend() -> tuple[bool, str]:
    code = r"""
from playwright.sync_api import sync_playwright
pages = ['/', '/?page=strategy', '/?page=backtest', '/?page=factor', '/?page=factormining', '/?page=factorboard', '/?page=settings']
errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    for path in pages:
        page = context.new_page()
        page.on('pageerror', lambda exc, path=path: errors.append(f'{path}: {exc}'))
        page.on(
            'console',
            lambda msg, path=path: errors.append(f'{path} console {msg.type}: {msg.text}')
            if msg.type == 'error' and 'deoptimised the styling of /Inline Babel script' not in msg.text
            else None,
        )
        resp = page.goto('http://127.0.0.1:8000' + path, wait_until='domcontentloaded', timeout=60000)
        page.wait_for_timeout(1500)
        body = page.locator('body').inner_text(timeout=15000)
        if not resp or resp.status != 200:
            errors.append(f'{path}: status {resp.status if resp else None}')
        if not body.strip():
            errors.append(f'{path}: empty body')
        page.close()
    browser.close()
if errors:
    print('\n'.join(errors))
    raise SystemExit(1)
print('frontend smoke passed')
"""
    return _run([str(PYTHON), "-c", code], timeout=180)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run QTsys health checks.")
    parser.add_argument("--skip-frontend", action="store_true", help="Skip Playwright frontend smoke test.")
    args = parser.parse_args()

    started_proc = _start_backend_if_needed()
    checks = [
        ("Python compile", check_py_compile),
        ("Encoding guard", lambda: check_script("check_encoding.py")),
        ("Factor expression operators", lambda: check_script("validate_factor_expression_operators.py")),
        ("No-lookahead invariants", lambda: check_script("validate_factor_no_lookahead.py")),
        ("Canonical execution invariants", lambda: check_script("validate_execution_simulator.py")),
        ("Secret scan", lambda: check_script("security_check.py")),
        ("Backend smoke", check_backend),
    ]
    if not args.skip_frontend:
        checks.append(("Frontend smoke", check_frontend))

    failed = []
    print("QTsys health check\n" + "=" * 20)
    for name, fn in checks:
        ok, output = fn()
        print(f"[{'OK' if ok else 'FAIL'}] {name}")
        if output:
            for line in output.splitlines()[:12]:
                print(f"  {line}")
        if not ok:
            failed.append(name)

    if started_proc is not None:
        started_proc.terminate()
        try:
            started_proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            started_proc.kill()

    if failed:
        print("\nFailed checks: " + ", ".join(failed))
        return 1
    print("\nAll health checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
