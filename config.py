"""QTsys 配置模块。"""
import os
import shutil
import sqlite3

VERSION = "26.05.01"
REPO_URL = "https://github.com/AlanWPY/QTsys.git"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
RUNTIME_DATA_DIR = os.path.join(RUNTIME_DIR, "data")
os.makedirs(RUNTIME_DATA_DIR, exist_ok=True)

LEGACY_DB_PATH = os.path.join(BASE_DIR, 'qtsys.db')
PRIMARY_DB_PATH = os.path.join(RUNTIME_DATA_DIR, 'qtsys.db')
RECOVERED_DB_PATH = os.path.join(RUNTIME_DATA_DIR, 'qtsys_live.db')
COPY_TEST_DB_PATH = os.path.join(RUNTIME_DATA_DIR, 'qtsys_copy_test.db')


def _sqlite_healthy(path: str) -> bool:
    if not os.path.exists(path) or not os.path.isfile(path):
        return False
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute('PRAGMA integrity_check;')
        row = cur.fetchone()
        conn.close()
        return bool(row) and row[0] == 'ok'
    except Exception:
        return False


if os.path.exists(LEGACY_DB_PATH) and not os.path.exists(PRIMARY_DB_PATH):
    try:
        os.replace(LEGACY_DB_PATH, PRIMARY_DB_PATH)
    except OSError:
        pass

if _sqlite_healthy(PRIMARY_DB_PATH):
    DB_PATH = PRIMARY_DB_PATH
elif _sqlite_healthy(RECOVERED_DB_PATH):
    DB_PATH = RECOVERED_DB_PATH
elif _sqlite_healthy(COPY_TEST_DB_PATH):
    try:
        shutil.copy2(COPY_TEST_DB_PATH, RECOVERED_DB_PATH)
        DB_PATH = RECOVERED_DB_PATH
    except OSError:
        DB_PATH = COPY_TEST_DB_PATH
elif _sqlite_healthy(LEGACY_DB_PATH):
    DB_PATH = LEGACY_DB_PATH
else:
    DB_PATH = RECOVERED_DB_PATH

DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
DATABASE_URL_SYNC = f"sqlite:///{DB_PATH}"

LEGACY_CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
CACHE_DIR = os.path.join(RUNTIME_DATA_DIR, "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

if os.path.isdir(LEGACY_CACHE_DIR) and not os.listdir(CACHE_DIR):
    try:
        for file_name in os.listdir(LEGACY_CACHE_DIR):
            src_path = os.path.join(LEGACY_CACHE_DIR, file_name)
            dst_path = os.path.join(CACHE_DIR, file_name)
            if os.path.isfile(src_path) and not os.path.exists(dst_path):
                shutil.copy2(src_path, dst_path)
    except OSError:
        pass

DEFAULT_CASH = 1_000_000.0
DEFAULT_COMMISSION = 0.0003
DEFAULT_STAMP_TAX = 0.001
DEFAULT_SLIPPAGE = 0.002
DEFAULT_VOLUME_LIMIT = 0.25


def build_mysql_url(host, port, user, password, database, async_mode=True):
    driver = "mysql+aiomysql" if async_mode else "mysql+pymysql"
    return f"{driver}://{user}:{password}@{host}:{port}/{database}?charset=utf8mb4"


HOST = "0.0.0.0"
PORT = 8000
