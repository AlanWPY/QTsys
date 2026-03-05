# QTsys 高级因子分析功能使用指南

## 新增功能概览

系统已添加5个专业量化分析功能，通过API调用使用。

---

## 1. 因子相关性分析

**API端点**: `POST /api/advanced_analysis/correlation`

**功能**: 计算多个因子之间的相关系数矩阵，避免选择高度相关的因子

**请求示例**:
```json
{
  "expressions": [
    "close / delay(close, 20) - 1",
    "std(returns, 20)",
    "corr(close, volume, 10)"
  ],
  "universe": ["000001.SZ", "000002.SZ", "600036.SH"],
  "start_date": "20230101",
  "end_date": "20231231"
}
```

**返回**: 相关性矩阵和统计摘要

---

## 2. 因子组合优化

**API端点**: `POST /api/advanced_analysis/combine`

**功能**: 使用IC加权或自定义权重组合多个因子

**请求示例**:
```json
{
  "expressions": [
    "close / delay(close, 20) - 1",
    "std(returns, 20)"
  ],
  "weights": null,  // null表示自动IC加权
  "universe": ["000001.SZ", "000002.SZ"],
  "start_date": "20230101",
  "end_date": "20231231"
}
```

**返回**: 组合因子表达式、权重和性能指标

---

## 3. 因子衰减分析

**API端点**: `POST /api/advanced_analysis/decay`

**功能**: 监控因子IC随时间的变化，判断因子是否失效

**请求示例**:
```json
{
  "expression": "close / delay(close, 20) - 1",
  "universe": ["000001.SZ", "000002.SZ"],
  "start_date": "20230101",
  "end_date": "20231231",
  "window_days": 60
}
```

**返回**: 滚动窗口IC序列和趋势判断

---

## 4. 行业中性化

**API端点**: `POST /api/advanced_analysis/neutralize`

**功能**: 对因子进行行业中性化处理

**请求示例**:
```json
{
  "expressions": ["close / delay(close, 20) - 1"],
  "universe": ["000001.SZ"],
  "start_date": "20230101",
  "end_date": "20231231"
}
```

**返回**: 中性化后的因子表达式

---

## 5. 因子归因分析

**API端点**: `POST /api/advanced_analysis/attribution`

**功能**: 分解组合因子中每个子因子的收益贡献

**请求示例**:
```json
{
  "expressions": [
    "close / delay(close, 20) - 1",
    "std(returns, 20)"
  ],
  "weights": [0.6, 0.4],
  "universe": ["000001.SZ", "000002.SZ"],
  "start_date": "20230101",
  "end_date": "20231231"
}
```

**返回**: 每个因子的贡献度和最佳/最差因子

---

## Alpha191批量测试

**API端点**: `POST /api/alpha191/batch_test`

**功能**: 批量测试多个Alpha191因子，快速筛选最优因子

**请求示例**:
```json
{
  "factor_numbers": [1, 2, 3, 4, 5],
  "universe": ["000001.SZ", "000002.SZ"],
  "start_date": "20230101",
  "end_date": "20231231",
  "groups": 5,
  "forward_days": 5
}
```

**返回**: 按IC_IR排序的因子性能列表

---

## 使用建议

1. **因子筛选流程**:
   - 使用批量测试筛选高IC_IR的Alpha191因子
   - 使用相关性分析剔除高度相关的因子
   - 使用衰减分析确认因子稳定性

2. **因子组合**:
   - 选择3-5个低相关性的优质因子
   - 使用IC加权或等权组合
   - 通过归因分析优化权重

3. **风险控制**:
   - 对所有因子进行行业中性化
   - 定期监控因子衰减情况
   - 及时更新失效因子

---

## 完整的191个Alpha因子

系统已包含全部191个Alpha191因子，可通过以下方式使用：

1. **单个加载**: 因子工作流页面 → 加载模板 → Alpha191因子 → 输入编号(1-191)
2. **批量测试**: 调用 `/api/alpha191/batch_test` API
3. **查看列表**: 调用 `/api/alpha191/list` API

---

## 技术支持

所有API均支持异步处理，适合大规模因子研究。建议使用Postman或Python requests库进行API调用。
