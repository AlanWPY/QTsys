# QTsys 部署检查清单

## ✅ 核心功能检查

### Alpha191因子系统
- [x] 191个因子全部集成
- [x] 工作流自动生成（160个完整，31个表达式模式）
- [x] 节点类型修复（const → input_constant）
- [x] 端口映射修复（所有节点端口正确）
- [x] 智能回退机制（编译失败自动使用表达式）
- [x] 预览功能正常
- [x] 回测功能正常

### API端点
- [x] `/api/factors/workflow/alpha191_v2/{number}` - 工作流生成
- [x] `/api/alpha191/batch_test` - 批量测试
- [x] `/api/advanced_analysis/correlation` - 相关性分析
- [x] `/api/advanced_analysis/combine` - 组合优化
- [x] `/api/advanced_analysis/decay` - 衰减分析
- [x] `/api/advanced_analysis/neutralize` - 中性化
- [x] `/api/advanced_analysis/attribution` - 归因分析

### 文档
- [x] README.md - 更新Alpha191章节
- [x] QUICK_START.md - 快速使用指南
- [x] ADVANCED_ANALYSIS_GUIDE.md - 高级分析指南
- [x] ALPHA191_COMPLETE_REPORT.md - 完整报告
- [x] TEST_REPORT.md - 测试报告
- [x] CHANGELOG.md - 更新日志

## 🚀 启动前检查

### 环境配置
- [ ] Python 3.8+ 已安装
- [ ] 依赖包已安装（requirements.txt）
- [ ] Tushare Token已配置

### 文件完整性
- [x] factor/expression_to_graph.py
- [x] factor/alpha191_templates.py
- [x] api/routes_alpha191.py
- [x] api/routes_advanced_analysis.py
- [x] static/index.html（v2.2-Fixed）

### 服务器配置
- [x] 禁用自动更新检查
- [x] 关闭reload模式
- [x] 端口8000可用

## 📋 启动步骤

1. **清理缓存**
```bash
cd F:\Github_project\QTsys
find . -type d -name "__pycache__" -exec rm -rf  + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null
```

2. **启动服务器**
```bash
python main.py
```

3. **验证启动**
- 访问 http://localhost:8000
- 检查控制台无错误
- 确认"因子工作流"页面可访问

## ✅ 功能测试

### 基础测试
1. 加载Alpha#1
   - 应显示工作流图
   - 节点横向排列
   - 有OUTPUT节点
   - 无"未知节点类型"错误

2. 加载Alpha#4
   - 应显示"表达式模式"
   - 表达式正确显示
   - 可以预览

3. 预览测试
   - 股票代码：000001.SZ
   - 日期：20230101-20231231
   - 应返回IC值序列

### 高级测试
4. 批量测试（API）
```python
import requests
response = requests.post('http://localhost:8000/api/alpha191/batch_test', json={
    "factor_numbers": [1, 5, 10],
    "universe": ["000001.SZ"],
    "start_date": "20230101",
    "end_date": "20231231"
})
print(response.json())
```

5. 回测测试
   - 选择Alpha#1
   - 运行选股回测
   - 应返回收益曲线

## 🎯 验收标准

### 必须通过
- [x] 所有191个因子可以加载
- [x] 无"未知节点类型"错误
- [x] 预览功能正常
- [x] 回测功能正常
- [x] API端点响应正常

### 性能要求
- [x] 因子加载 < 1秒
- [x] 预览计算 < 10秒
- [x] 回测完成 < 60秒

### 用户体验
- [x] 界面无明显卡顿
- [x] 错误提示清晰
- [x] 操作流程顺畅

## 📝 发布说明

### 版本号
v2.2 - Alpha191完整版（已修复）

### 主要更新
- 修复节点类型错误
- 修复端口映射问题
- 添加智能回退机制
- 完善所有文档

### 已知问题
- 31个复杂因子使用表达式模式（不影响功能）

### 使用建议
- 优先使用有工作流图的160个因子
- 表达式模式因子功能完全正常
- 建议使用批量测试筛选最优因子

---

**检查完成后即可正式发布！**
