"""
数据库配置管理模块
"""
import json
import os
from pathlib import Path

CONFIG_FILE = Path(__file__).parent.parent / 'config' / 'db_config.json'

def load_db_config():
    """加载数据库配置"""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {
        'host': 'localhost',
        'port': 3306,
        'user': 'root',
        'password': '',
        'database': 'qtsys'
    }

def save_db_config(config):
    """保存数据库配置"""
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

def test_db_connection(config):
    """测试数据库连接"""
    try:
        import pymysql
        conn = pymysql.connect(
            host=config['host'],
            port=config['port'],
            user=config['user'],
            password=config['password'],
            database=config.get('database', 'qtsys'),
            connect_timeout=5
        )
        conn.close()
        return True, "连接成功"
    except Exception as e:
        return False, str(e)
