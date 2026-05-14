# 量化研究 Skill 配置

本项目已为 Codex 环境安装并配置以下金融/量化研究类 skill。安装后的 skill 需要重启 Codex 才会出现在会话可用技能列表中；项目内已同步将核心研究约束写入 AI 策略助手提示词。

## 已安装 Skill

| Skill | 来源 | 用途 |
|---|---|---|
| `quantitative-research` | `omer-metin/skills-for-antigravity@quantitative-research` | 机构级回测、Alpha 研究、walk-forward、过拟合防护、交易成本建模 |
| `trading-quant` | `lanyasheng/trading-quant@trading-quant` | 多源行情、A 股/美股/港股/商品市场扫描、资金面和技术面诊断 |
| `market-data` | `eng0ai/eng0-template-skills@market-data` | 美股 OHLCV、公司信息、新闻情绪和全球市场补充数据 |

## 已写入系统的研究约束

- 策略与因子只能使用真实数据源；无数据时必须返回明确错误，不允许模拟占位。
- 收盘价信号默认下一交易日执行，避免未来函数。
- 回测必须包含手续费、印花税、滑点、仓位上限、最小交易单位和成交失败场景。
- AI 只负责生成研究假设和策略草稿；有效性必须由 QTsys 的真实回测、IC/IR、样本外验证和基准超额收益确认。
- Sharpe 过高、交易次数过少、样本内外衰减过大时必须提示过拟合风险。
- 多因子策略优先使用可解释的 rank/标准化/等权组合，避免不可解释黑箱权重。

## 配置文件

- `config/quant_research_skills.json`：记录已安装 skill、适用模块和量化研究 guardrails。
- `services/strategy_ai_service.py`：AI 策略助手已追加机构级量化研究约束。

