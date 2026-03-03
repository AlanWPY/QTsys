"""DB Manager 主窗口"""
import time
import pymysql
from PyQt6.QtWidgets import (
    QMainWindow, QSplitter, QVBoxLayout, QHBoxLayout,
    QWidget, QPushButton, QStatusBar, QMenuBar, QDialog,
    QTextEdit, QDialogButtonBox,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from db_manager.db_tree import DbTree
from db_manager.sql_editor import SqlEditor
from db_manager.result_table import ResultTable

HELP_SQL = """== 常用 MySQL 语句示例 ==

-- 查看所有数据库
SHOW DATABASES;

-- 切换数据库
USE qtsys;

-- 查看当前数据库所有表
SHOW TABLES;

-- 查看表结构
DESCRIBE table_name;

-- 查询数据 (前100行)
SELECT * FROM table_name LIMIT 100;

-- 条件查询
SELECT * FROM qtsys_daily_quotes
WHERE ts_code = '000001.SZ' AND trade_date >= '20240101'
ORDER BY trade_date DESC LIMIT 50;

-- 聚合统计
SELECT ts_code, COUNT(*) AS cnt, MIN(trade_date) AS first_date, MAX(trade_date) AS last_date
FROM qtsys_daily_quotes GROUP BY ts_code;

-- 创建表
CREATE TABLE my_table (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    value DECIMAL(10,2) DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 插入数据
INSERT INTO my_table (name, value) VALUES ('test', 3.14);

-- 更新数据
UPDATE my_table SET value = 6.28 WHERE name = 'test';

-- 删除数据
DELETE FROM my_table WHERE id = 1;

-- 删除表
DROP TABLE IF EXISTS my_table;

-- 查看表索引
SHOW INDEX FROM table_name;

-- 添加索引
ALTER TABLE table_name ADD INDEX idx_name (column_name);

-- 查看表大小
SELECT table_name, ROUND(data_length/1024/1024, 2) AS size_mb
FROM information_schema.tables WHERE table_schema = DATABASE();

-- 查看连接状态
SHOW PROCESSLIST;

-- 查看变量
SHOW VARIABLES LIKE '%max_connections%';
"""


class HelpDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SQL 帮助")
        self.resize(600, 500)
        layout = QVBoxLayout(self)
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(HELP_SQL)
        layout.addWidget(text)
        btn = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btn.rejected.connect(self.close)
        layout.addWidget(btn)


class MainWindow(QMainWindow):
    def __init__(self, conn_params: dict):
        super().__init__()
        self.conn_params = conn_params
        self.setWindowTitle("QTsys DB Manager")
        self.resize(1200, 750)
        self._build_menu()
        self._build_ui()
        self.db_tree.set_connection(conn_params)

    def _build_menu(self):
        menubar = self.menuBar()
        # 数据库菜单
        db_menu = menubar.addMenu("数据库")
        db_menu.addAction("刷新连接", self._refresh_tree)
        db_menu.addAction("新建数据库...", self._create_database)
        db_menu.addSeparator()
        db_menu.addAction("退出", self.close)
        # 帮助菜单
        help_menu = menubar.addMenu("帮助")
        help_menu.addAction("SQL 示例", self._show_help)
        help_menu.addAction("关于", self._show_about)

    def _refresh_tree(self):
        self.db_tree.refresh()

    def _create_database(self):
        from PyQt6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建数据库", "数据库名称:")
        if ok and name.strip():
            self.execute_raw_sql(f"CREATE DATABASE IF NOT EXISTS `{name.strip()}` CHARACTER SET utf8mb4")
            self.db_tree.refresh()

    def _show_help(self):
        HelpDialog(self).exec()

    def _show_about(self):
        from PyQt6.QtWidgets import QMessageBox
        QMessageBox.about(self, "关于", "QTsys DB Manager\n\nMySQL 数据库管理工具\n配合 QTsys 量化系统使用")

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(4, 4, 4, 4)

        splitter_h = QSplitter(Qt.Orientation.Horizontal)

        # 左侧: 数据库树
        self.db_tree = DbTree()
        splitter_h.addWidget(self.db_tree)

        # 右侧: 编辑器 + 结果
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)

        # 工具栏
        toolbar = QHBoxLayout()
        exec_btn = QPushButton("执行 (Ctrl+Enter)")
        exec_btn.clicked.connect(self.execute_sql)
        clear_btn = QPushButton("清空")
        clear_btn.clicked.connect(lambda: self.editor.clear())
        help_btn = QPushButton("SQL 帮助")
        help_btn.clicked.connect(self._show_help)
        toolbar.addWidget(exec_btn)
        toolbar.addWidget(clear_btn)
        toolbar.addWidget(help_btn)
        toolbar.addStretch()
        right_layout.addLayout(toolbar)

        splitter_v = QSplitter(Qt.Orientation.Vertical)
        self.editor = SqlEditor(self)
        self.result = ResultTable()
        splitter_v.addWidget(self.editor)
        splitter_v.addWidget(self.result)
        splitter_v.setSizes([250, 450])
        right_layout.addWidget(splitter_v)

        splitter_h.addWidget(right)
        splitter_h.setSizes([250, 950])
        main_layout.addWidget(splitter_h)

        self.setStatusBar(QStatusBar())
        self._update_status("已连接")

    def _update_status(self, msg):
        p = self.conn_params
        info = f"{p['user']}@{p['host']}:{p['port']}/{p['database']}"
        self.statusBar().showMessage(f"{msg} | {info}")

    def execute_sql(self):
        sql = self.editor.toPlainText().strip()
        if not sql:
            return
        self.editor.add_history(sql)
        statements = [s.strip() for s in sql.split(";") if s.strip()]
        for stmt in statements:
            self._run_one(stmt)

    def execute_raw_sql(self, sql: str):
        self.editor.setPlainText(sql)
        self._run_one(sql)

    def _run_one(self, sql: str):
        t0 = time.time()
        try:
            conn = pymysql.connect(**self.conn_params, connect_timeout=10)
            cur = conn.cursor()
            cur.execute(sql)
            if cur.description:
                headers = [d[0] for d in cur.description]
                rows = cur.fetchall()
                self.result.set_data(headers, list(rows))
                elapsed = time.time() - t0
                self._update_status(f"{len(rows)} 行 | {elapsed:.3f}s")
            else:
                conn.commit()
                affected = cur.rowcount
                elapsed = time.time() - t0
                self.result.show_message(f"执行成功\n\n影响 {affected} 行\n耗时 {elapsed:.3f}s")
                self._update_status(f"OK, 影响 {affected} 行 | {elapsed:.3f}s")
                self.db_tree.refresh()
            conn.close()
        except Exception as e:
            elapsed = time.time() - t0
            self.result.show_error(f"SQL 执行错误\n\n{type(e).__name__}: {e}\n\n语句: {sql}\n耗时: {elapsed:.3f}s")
            self._update_status("执行出错")
