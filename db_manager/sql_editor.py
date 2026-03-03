"""SQL 编辑器 + 语法高亮"""
from PyQt6.QtWidgets import QPlainTextEdit
from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont
from PyQt6.QtCore import Qt, QRegularExpression

SQL_KEYWORDS = (
    "SELECT|FROM|WHERE|INSERT|UPDATE|DELETE|CREATE|DROP|ALTER|TABLE|INDEX|"
    "INTO|VALUES|SET|JOIN|LEFT|RIGHT|INNER|OUTER|ON|AND|OR|NOT|IN|IS|NULL|"
    "LIKE|BETWEEN|ORDER|BY|GROUP|HAVING|LIMIT|OFFSET|AS|DISTINCT|COUNT|"
    "SUM|AVG|MAX|MIN|UNION|ALL|EXISTS|CASE|WHEN|THEN|ELSE|END|"
    "PRIMARY|KEY|FOREIGN|REFERENCES|DEFAULT|AUTO_INCREMENT|"
    "SHOW|DATABASES|TABLES|DESCRIBE|USE|EXPLAIN|TRUNCATE|GRANT|REVOKE"
)


class SqlHighlighter(QSyntaxHighlighter):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules = []

        kw_fmt = QTextCharFormat()
        kw_fmt.setForeground(QColor("#89b4fa"))
        kw_fmt.setFontWeight(QFont.Weight.Bold)
        self._rules.append((QRegularExpression(
            f"\\b({SQL_KEYWORDS})\\b",
            QRegularExpression.PatternOption.CaseInsensitiveOption
        ), kw_fmt))

        str_fmt = QTextCharFormat()
        str_fmt.setForeground(QColor("#a6e3a1"))
        self._rules.append((QRegularExpression(r"'[^']*'"), str_fmt))

        num_fmt = QTextCharFormat()
        num_fmt.setForeground(QColor("#fab387"))
        self._rules.append((QRegularExpression(r"\b\d+\.?\d*\b"), num_fmt))

        cmt_fmt = QTextCharFormat()
        cmt_fmt.setForeground(QColor("#6c7086"))
        self._rules.append((QRegularExpression(r"--[^\n]*"), cmt_fmt))

    def highlightBlock(self, text):
        for pattern, fmt in self._rules:
            it = pattern.globalMatch(text)
            while it.hasNext():
                m = it.next()
                self.setFormat(m.capturedStart(), m.capturedLength(), fmt)


class SqlEditor(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPlaceholderText("输入 SQL 语句... (Ctrl+Enter 执行)")
        self._highlighter = SqlHighlighter(self.document())
        self._history = []
        self._hist_idx = -1

    def add_history(self, sql: str):
        if sql and (not self._history or self._history[-1] != sql):
            self._history.append(sql)
        self._hist_idx = len(self._history)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Return and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.parent().execute_sql()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() == Qt.Key.Key_Up and self._history:
                self._hist_idx = max(0, self._hist_idx - 1)
                self.setPlainText(self._history[self._hist_idx])
                return
            if event.key() == Qt.Key.Key_Down and self._history:
                self._hist_idx = min(len(self._history), self._hist_idx + 1)
                if self._hist_idx < len(self._history):
                    self.setPlainText(self._history[self._hist_idx])
                else:
                    self.clear()
                return
        super().keyPressEvent(event)
