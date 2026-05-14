"""Compatibility entry for the QTsys professional launcher."""
from __future__ import annotations

from launcher.light_app import TerminalLauncher, main


__all__ = ["TerminalLauncher", "main"]


if __name__ == "__main__":
    main()
