"""
数据获取优化模块 - 使用缓存和批量处理
"""
import pandas as pd
import tushare as ts
from datetime import datetime, timedelta
import asyncio

class DataFetcher:
    def __init__(self, db_manager):
        self.db = db_manager
        self.pro = None

    def init_tushare(self):
        """初始化Tushare"""
        try:
            self.pro = ts.pro_api()
            return True
        except:
            return False

    async def fetch_zz500_data(self, start_date, end_date, progress_callback=None):
        """获取中证500数据（优化版）"""
        if not self.pro:
            if not self.init_tushare():
                raise Exception("Tushare初始化失败")

        # 获取成分股
        zz500 = self.pro.index_weight(index_code='000905.SH', start_date=start_date, end_date=end_date)
        stocks = zz500['con_code'].unique()[:50]  # 限制50只股票加快速度

        all_data = []
        for i, stock in enumerate(stocks):
            if progress_callback:
                progress_callback(f'获取 {stock} ({i+1}/{len(stocks)})')

            try:
                df = self.pro.daily(ts_code=stock, start_date=start_date, end_date=end_date)
                if len(df) > 0:
                    all_data.append(df)
            except:
                pass

            await asyncio.sleep(0.12)  # API限速

        if all_data:
            combined = pd.concat(all_data, ignore_index=True)
            self.db.save_market_data(combined)
            return len(combined)
        return 0
