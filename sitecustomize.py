import sys


for _name in ("pyarrow", "numexpr", "bottleneck"):
    sys.modules.setdefault(_name, None)
