"""结果表格 + 错误文本 + 分页 + 复制/导出"""
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTableView, QStackedWidget,
    QPushButton, QLabel, QMenu, QApplication, QFileDialog, QTextEdit,
)
from PyQt6.QtGui import QStandardItemModel, QStandardItem
from PyQt6.QtCore import Qt
import csv


class ResultTable(QWidget):
    PAGE_SIZE = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self._all_rows = []
        self._headers = []
        self._page = 0
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.stack = QStackedWidget()

        # 页面0: 表格
        table_widget = QWidget()
        table_layout = QVBoxLayout(table_widget)
        table_layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableView()
        self.table.setSortingEnabled(True)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.model = QStandardItemModel()
        self.table.setModel(self.model)
        table_layout.addWidget(self.table)

        # 分页栏
        pager = QHBoxLayout()
        self.prev_btn = QPushButton("上一页")
        self.prev_btn.clicked.connect(self._prev_page)
        self.next_btn = QPushButton("下一页")
        self.next_btn.clicked.connect(self._next_page)
        self.page_label = QLabel("0 行")
        pager.addWidget(self.prev_btn)
        pager.addWidget(self.page_label)
        pager.addWidget(self.next_btn)
        pager.addStretch()
        table_layout.addLayout(pager)
        self.stack.addWidget(table_widget)

        # 页面1: 错误/消息文本
        self.error_text = QTextEdit()
        self.error_text.setReadOnly(True)
        self.stack.addWidget(self.error_text)

        layout.addWidget(self.stack)

    def set_data(self, headers: list, rows: list):
        self._headers = headers
        self._all_rows = rows
        self._page = 0
        self._render_page()
        self.stack.setCurrentIndex(0)

    def show_error(self, msg: str):
        self.error_text.setStyleSheet("color: #f38ba8; font-size: 14px; padding: 8px;")
        self.error_text.setText(msg)
        self.stack.setCurrentIndex(1)

    def show_message(self, msg: str):
        self.error_text.setStyleSheet("color: #a6e3a1; font-size: 14px; padding: 8px;")
        self.error_text.setText(msg)
        self.stack.setCurrentIndex(1)

    def _render_page(self):
        self.model.clear()
        self.model.setHorizontalHeaderLabels(self._headers)
        start = self._page * self.PAGE_SIZE
        end = start + self.PAGE_SIZE
        for row in self._all_rows[start:end]:
            items = [QStandardItem(str(v) if v is not None else "NULL") for v in row]
            self.model.appendRow(items)
        total = len(self._all_rows)
        pages = (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE if total else 1
        self.page_label.setText(f"{total} 行 | 第 {self._page+1}/{pages} 页")
        self.prev_btn.setEnabled(self._page > 0)
        self.next_btn.setEnabled(end < total)

    def _prev_page(self):
        if self._page > 0:
            self._page -= 1
            self._render_page()

    def _next_page(self):
        if (self._page + 1) * self.PAGE_SIZE < len(self._all_rows):
            self._page += 1
            self._render_page()

    def _context_menu(self, pos):
        menu = QMenu(self)
        menu.addAction("复制选中", self._copy_selection)
        menu.addAction("导出 CSV", self._export_csv)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy_selection(self):
        indexes = self.table.selectionModel().selectedIndexes()
        if not indexes:
            return
        rows_dict = {}
        for idx in indexes:
            rows_dict.setdefault(idx.row(), []).append(idx.data() or "")
        lines = ["\t".join(cells) for _, cells in sorted(rows_dict.items())]
        QApplication.clipboard().setText("\n".join(lines))

    def _export_csv(self):
        if not self._all_rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", "", "CSV (*.csv)")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(self._headers)
            w.writerows(self._all_rows)
