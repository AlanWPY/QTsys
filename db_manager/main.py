"""QTsys DB Manager 入口"""
import sys
import os

# 确保项目根目录在 sys.path 中
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 修复 Anaconda 环境下 PyQt6 DLL 加载问题
try:
    import importlib.util
    spec = importlib.util.find_spec("PyQt6")
    if spec and spec.submodule_search_locations:
        qt_bin = os.path.join(spec.submodule_search_locations[0], "Qt6", "bin")
        if os.path.isdir(qt_bin):
            os.add_dll_directory(qt_bin)
            os.environ["PATH"] = qt_bin + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

from PyQt6.QtWidgets import QApplication
from db_manager.styles import DARK_THEME
from db_manager.fingerprint import load_credentials
from db_manager.connection_dialog import ConnectionDialog
from db_manager.app import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK_THEME)

    creds = load_credentials()
    if not creds:
        dlg = ConnectionDialog(creds=None)
        if dlg.exec() != ConnectionDialog.DialogCode.Accepted:
            return
        creds = dlg.result_creds

    # 验证已保存凭据
    import pymysql
    try:
        c = pymysql.connect(**creds, connect_timeout=5)
        c.close()
    except Exception:
        dlg = ConnectionDialog(creds=creds)
        if dlg.exec() != ConnectionDialog.DialogCode.Accepted:
            return
        creds = dlg.result_creds

    win = MainWindow(creds)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
