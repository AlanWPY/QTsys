# QTsys Alpha191 快速使用指南

## 🚀 快速开始

### 1. 启动系统
```bash
cd F:\Github_project\QTsys
python main.py
```

### 2. 打开浏览器
访问：`http://localhost:8000`

### 3. 加载Alpha因子
1. 点击顶部导航 "因子工作流"
2. 点击 "加载模板" → "Alpha191因子"
3. 输入因子编号（1-191）
4. 点击 "加载"

### 4. 查看工作流
- 工作流图自动显示
- 节点从左到右展示数据流
- 最右侧是OUTPUT输出节点

### 5. 预览因子
1. 设置股票代码（如：000001.SZ）
2. 设置日期范围
3. 点击 "预览"
4. 查看IC值序列

### 6. 运行回测
1. 选择回测模式（选股/技术）
2. 设置股票池
3. 设置参数
4. 点击 "运行回测"

## 📊 高级功能

### 批量测试（API）
```python
import requests

response = requests.post('http://localhost:8000/api/alpha191/batch_test', json={
    "factor_numbers": [1, 2, 3, 4, 5],
    "universe": ["000001.SZ", "000002.SZ"],
    "start_date": "20230101",
    "end_date": "20231231",
    "groups": 5,
    "forward_days": 5
})

results = response.json()
print(f"最佳因子: Alpha#{results['results'][0]['number']}")
```

### 因子相关性分析
```python
response = requests.post('http://localhost:8000/api/advanced_analysis/correlation', json={
    "expressions": [
        "close / delay(close, 20) - 1",
        "std(returns, 20)"
    ],
    "universe": ["000001.SZ"],
    "start_date": "20230101",
    "end_date": "20231231"
})

corr_matrix = response.json()['correlation_matrix']
```

## 💡 使用技巧

1. **因子筛选**：先用批量测试找出IC_IR > 1的因子
2. **去相关**：使用相关性分析，选择相关系数 < 0.5的因子
3. **稳定性**：用衰减分析确认因子长期有效
4. **组合优化**：IC加权组合3-5个因子
5. **风险控制**：行业中性化处理

## ⚠️ 注意事项

1. 首次使用需配置Tushare Token
2. 浏览器缓存问题：按Ctrl+Shift+R强制刷新
3. 服务器重启后需重新加载因子
4. 大规模回测建议使用API异步调用

## 🔧 常见问题

**Q: 工作流图不显示？**
A: 强制刷新浏览器（Ctrl+Shift+R）

**Q: 预览失败？**
A: 检查Tushare Token配置和股票代码格式

**Q: 回测很慢？**
A: 减少股票池数量或缩短日期范围

**Q: 如何保存因子？**
A: 工作流会自动编译为表达式并保存

---

更多详情请查看：
- `IMPLEMENTATION_SUMMARY.md` - 功能总结
- `ADVANCED_ANALYSIS_GUIDE.md` - 高级分析指南
- `ALPHA191_COMPLETE_REPORT.md` - 完整报告
