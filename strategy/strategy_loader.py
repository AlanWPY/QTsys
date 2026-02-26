"""动态加载用户策略代码"""
import re
import types
from typing import Callable, Tuple


FORBIDDEN_IMPORTS = [
    "os", "sys", "subprocess", "shutil", "pathlib",
    "socket", "http", "urllib", "requests",
    "importlib", "ctypes", "signal",
]

# 禁止访问的双下划线属性(可用于沙箱逃逸)
FORBIDDEN_DUNDER = re.compile(r'__\w+__')


def load_strategy(code: str) -> Tuple[Callable, Callable]:
    """加载用户策略代码,返回 (initialize, handle_data) 函数"""
    # 基本安全检查
    for mod in FORBIDDEN_IMPORTS:
        if f"import {mod}" in code or f"from {mod}" in code:
            raise ValueError(f"禁止导入模块: {mod}")

    if "__import__" in code or "eval(" in code or "exec(" in code:
        raise ValueError("禁止使用 __import__, eval, exec")

    # 检查双下划线属性访问(阻止 __subclasses__, __mro__, __globals__ 等)
    for match in FORBIDDEN_DUNDER.finditer(code):
        attr = match.group()
        # 允许 __init__ 和 __name__ (常见无害用法)
        if attr not in ("__init__", "__name__"):
            raise ValueError(f"禁止访问双下划线属性: {attr}")

    # 创建受限命名空间 - 移除 getattr/setattr/hasattr 防止对象图遍历
    namespace = {
        "__builtins__": {
            "range": range, "len": len, "int": int, "float": float,
            "str": str, "bool": bool, "list": list, "dict": dict,
            "tuple": tuple, "set": set, "abs": abs, "max": max,
            "min": min, "sum": sum, "round": round, "sorted": sorted,
            "enumerate": enumerate, "zip": zip, "map": map,
            "filter": filter, "print": print, "isinstance": isinstance,
            "any": any, "all": all, "reversed": reversed,
            "Exception": Exception, "ValueError": ValueError,
            "TypeError": TypeError, "KeyError": KeyError,
            "IndexError": IndexError, "RuntimeError": RuntimeError,
            "True": True, "False": False, "None": None,
        }
    }

    # 注入允许的库
    import numpy as np
    import pandas as pd
    namespace["np"] = np
    namespace["pd"] = pd
    namespace["numpy"] = np
    namespace["pandas"] = pd

    try:
        exec(code, namespace)
    except Exception as e:
        raise ValueError(f"策略代码执行错误: {str(e)}")

    init_func = namespace.get("initialize")
    handle_func = namespace.get("handle_data")

    if not callable(handle_func):
        raise ValueError("策略必须定义 handle_data(context) 函数")

    if init_func is None:
        init_func = lambda ctx: None

    return init_func, handle_func
