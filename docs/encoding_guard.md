# QTsys 中文编码防护记录

## 问题记录

2026-05-15 修复 `static/index.html` 时出现过一次中文大面积乱码。根因是前端文件中的 UTF-8 中文内容被错误地按 GBK/ANSI 方式读取或写回，形成 mojibake，例如 `因子` 变成 `鍥犲瓙`，部分字符进一步变成 `�` 后不可逆。

## 处理原则

- 所有前端、文档、脚本文件统一使用 UTF-8 编码保存。
- 不使用 PowerShell `Get-Content | Set-Content` 直接重写包含中文的大文件，除非显式指定 UTF-8 且已做差异检查。
- 修改 `static/index.html` 时优先使用 `apply_patch` 或 Python `Path.read_text/write_text(encoding="utf-8")`。
- 提交前运行 `python scripts/check_encoding.py`，确认没有 `�`、`鍥犲瓙`、`璁剧疆`、`鎸栨帢` 等典型乱码标记。
- `scripts/health_check.py` 已接入 Encoding guard，发布前健康检查会自动拦截编码污染。
