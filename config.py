"""QTsys 配置文件"""
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 数据库
DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(BASE_DIR, 'qtsys.db')}"
DATABASE_URL_SYNC = f"sqlite:///{os.path.join(BASE_DIR, 'qtsys.db')}"

# 缓存目录
CACHE_DIR = os.path.join(BASE_DIR, "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# 回测默认参数
DEFAULT_CASH = 1_000_000.0
DEFAULT_COMMISSION = 0.0003  # 万三佣金
DEFAULT_STAMP_TAX = 0.001   # 千一印花税(卖出)
DEFAULT_SLIPPAGE = 0.002    # 滑点 0.2%
DEFAULT_VOLUME_LIMIT = 0.25 # 成交量限制25%

# 服务器
HOST = "0.0.0.0"
PORT = 8000
