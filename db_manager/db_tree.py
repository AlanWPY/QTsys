"""数据库/表树形浏览器"""
from PyQt6.QtWidgets import QTreeWidget, QTreeWidgetItem, QMenu, QInputDialog, QMessageBox
from PyQt6.QtCore import Qt
import pymysql


class DbTree(QTreeWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHeaderLabel("数据库")
        self.conn_params = None
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.itemExpanded.connect(self._on_expand)
        self.itemDoubleClicked.connect(self._on_double_click)

    def set_connection(self, params: dict):
        self.conn_params = params
        self.refresh()

    def _conn(self):
        return pymysql.connect(**self.conn_params, connect_timeout=5)

    def refresh(self):
        self.clear()
        if not self.conn_params:
            return
        try:
            with self._conn() as c:
                cur = c.cursor()
                cur.execute("SHOW DATABASES")
                for (db,) in cur.fetchall():
                    item = QTreeWidgetItem(self, [db])
                    item.setData(0, Qt.ItemDataRole.UserRole, {"type": "database", "name": db})
                    item.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        except Exception as e:
            QTreeWidgetItem(self, [f"错误: {e}"])

    def _on_expand(self, item):
        info = item.data(0, Qt.ItemDataRole.UserRole)
        if not info or item.childCount() > 0:
            return
        if info["type"] == "database":
            self._load_tables(item, info["name"])
        elif info["type"] == "table":
            self._load_columns(item, info["db"], info["name"])

    def _load_tables(self, parent, db_name):
        try:
            with self._conn() as c:
                cur = c.cursor()
                cur.execute(f"SHOW TABLES FROM `{db_name}`")
                for (tbl,) in cur.fetchall():
                    child = QTreeWidgetItem(parent, [tbl])
                    child.setData(0, Qt.ItemDataRole.UserRole, {"type": "table", "db": db_name, "name": tbl})
                    child.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
        except Exception:
            pass

    def _load_columns(self, parent, db_name, tbl_name):
        try:
            with self._conn() as c:
                cur = c.cursor()
                cur.execute(f"DESCRIBE `{db_name}`.`{tbl_name}`")
                for row in cur.fetchall():
                    col_text = f"{row[0]} ({row[1]})"
                    child = QTreeWidgetItem(parent, [col_text])
                    child.setData(0, Qt.ItemDataRole.UserRole, {"type": "column"})
        except Exception:
            pass

    def _on_double_click(self, item, column):
        info = item.data(0, Qt.ItemDataRole.UserRole)
        if not info or info["type"] != "table":
            return
        main_win = self.window()
        if hasattr(main_win, "execute_raw_sql"):
            sql = f"SELECT * FROM `{info['db']}`.`{info['name']}` LIMIT 200"
            main_win.execute_raw_sql(sql)

    def _context_menu(self, pos):
        item = self.itemAt(pos)
        menu = QMenu(self)
        if not item:
            menu.addAction("刷新", self.refresh)
            menu.addAction("新建数据库...", self._create_database)
            menu.exec(self.viewport().mapToGlobal(pos))
            return
        info = item.data(0, Qt.ItemDataRole.UserRole)
        if not info:
            return
        if info["type"] == "database":
            menu.addAction("查看所有表", lambda: self._expand_db(item))
            menu.addAction("新建表...", lambda: self._create_table(info["name"]))
            menu.addSeparator()
            menu.addAction("删除数据库", lambda: self._drop_database(info["name"]))
            menu.addSeparator()
            menu.addAction("刷新", self.refresh)
        elif info["type"] == "table":
            menu.addAction("查看前200行", lambda: self._on_double_click(item, 0))
            menu.addAction("查看表结构", lambda: self._show_structure(info))
            menu.addAction("行数统计", lambda: self._count_rows(info))
            menu.addAction("导出表", lambda: self._export_table(info))
            menu.addAction("清空表", lambda: self._truncate_table(info))
            menu.addSeparator()
            menu.addAction("删除表", lambda: self._drop_table(info))
        menu.exec(self.viewport().mapToGlobal(pos))

    def _count_rows(self, info):
        try:
            with self._conn() as c:
                cur = c.cursor()
                cur.execute(f"SELECT COUNT(*) FROM `{info['db']}`.`{info['name']}`")
                count = cur.fetchone()[0]
            main_win = self.window()
            if hasattr(main_win, "statusBar"):
                main_win.statusBar().showMessage(f"{info['name']}: {count} 行")
        except Exception:
            pass

    def _export_table(self, info):
        main_win = self.window()
        if hasattr(main_win, "execute_raw_sql"):
            sql = f"SELECT * FROM `{info['db']}`.`{info['name']}`"
            main_win.execute_raw_sql(sql)

    def _expand_db(self, item):
        self.expandItem(item)

    def _show_structure(self, info):
        main_win = self.window()
        if hasattr(main_win, "execute_raw_sql"):
            main_win.execute_raw_sql(f"DESCRIBE `{info['db']}`.`{info['name']}`")

    def _create_database(self):
        name, ok = QInputDialog.getText(self, "新建数据库", "数据库名称:")
        if not ok or not name.strip():
            return
        try:
            with self._conn() as c:
                c.cursor().execute(f"CREATE DATABASE IF NOT EXISTS `{name.strip()}` CHARACTER SET utf8mb4")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _create_table(self, db_name):
        name, ok = QInputDialog.getText(self, "新建表", "表名:")
        if not ok or not name.strip():
            return
        main_win = self.window()
        if hasattr(main_win, "editor"):
            sql = f"CREATE TABLE `{db_name}`.`{name.strip()}` (\n    id INT AUTO_INCREMENT PRIMARY KEY,\n    name VARCHAR(100),\n    created_at DATETIME DEFAULT CURRENT_TIMESTAMP\n) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;"
            main_win.editor.setPlainText(sql)

    def _drop_database(self, db_name):
        ret = QMessageBox.warning(self, "确认删除",
            f"确定要删除数据库 `{db_name}` 吗？\n此操作不可恢复！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            with self._conn() as c:
                c.cursor().execute(f"DROP DATABASE `{db_name}`")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _drop_table(self, info):
        ret = QMessageBox.warning(self, "确认删除",
            f"确定要删除表 `{info['db']}`.`{info['name']}` 吗？\n此操作不可恢复！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            with self._conn() as c:
                c.cursor().execute(f"DROP TABLE `{info['db']}`.`{info['name']}`")
            self.refresh()
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))

    def _truncate_table(self, info):
        ret = QMessageBox.warning(self, "确认清空",
            f"确定要清空表 `{info['name']}` 的所有数据吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        try:
            with self._conn() as c:
                c.cursor().execute(f"TRUNCATE TABLE `{info['db']}`.`{info['name']}`")
            main_win = self.window()
            if hasattr(main_win, "statusBar"):
                main_win.statusBar().showMessage(f"已清空 {info['name']}")
        except Exception as e:
            QMessageBox.critical(self, "错误", str(e))
