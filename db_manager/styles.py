"""深色主题 QSS - Catppuccin Mocha 风格"""

DARK_THEME = """
QMainWindow, QDialog {
    background-color: #1e1e2e;
    color: #cdd6f4;
}
QMenuBar {
    background-color: #181825;
    color: #cdd6f4;
    border-bottom: 1px solid #313244;
    padding: 2px;
}
QMenuBar::item:selected {
    background-color: #313244;
    border-radius: 4px;
}
QMenu {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    padding: 4px;
}
QMenu::item {
    padding: 5px 24px;
    border-radius: 3px;
}
QMenu::item:selected {
    background-color: #313244;
}
QMenu::separator {
    height: 1px;
    background-color: #313244;
    margin: 4px 8px;
}
QTreeWidget {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    selection-background-color: #45475a;
    font-family: Consolas, monospace;
    font-size: 13px;
    outline: none;
}
QTreeWidget::item {
    padding: 3px 0;
}
QTreeWidget::item:hover {
    background-color: #1e1e2e;
}
QTreeWidget::item:selected {
    background-color: #45475a;
}
QPlainTextEdit, QTextEdit {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    selection-background-color: #45475a;
    font-family: Consolas, 'Courier New', monospace;
    font-size: 14px;
    padding: 4px;
}
QTableView {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    selection-background-color: #45475a;
    font-family: Consolas, monospace;
    font-size: 13px;
    gridline-color: #313244;
    alternate-background-color: #1a1a2e;
}
QHeaderView::section {
    background-color: #313244;
    color: #cdd6f4;
    border: none;
    border-right: 1px solid #45475a;
    border-bottom: 1px solid #45475a;
    padding: 5px 8px;
    font-weight: bold;
}
QLineEdit, QSpinBox {
    background-color: #181825;
    color: #cdd6f4;
    border: 1px solid #313244;
    border-radius: 4px;
    padding: 5px 8px;
    font-size: 13px;
}
QLineEdit:focus, QSpinBox:focus {
    border-color: #89b4fa;
}
QPushButton {
    background-color: #89b4fa;
    color: #1e1e2e;
    border: none;
    padding: 6px 16px;
    border-radius: 4px;
    font-weight: bold;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #74c7ec;
}
QPushButton:pressed {
    background-color: #89dceb;
}
QSplitter::handle {
    background-color: #313244;
    width: 2px;
    height: 2px;
}
QStatusBar {
    background-color: #181825;
    color: #a6adc8;
    border-top: 1px solid #313244;
    font-size: 12px;
    padding: 2px;
}
QLabel {
    color: #cdd6f4;
}
QTabBar::tab {
    background-color: #313244;
    color: #cdd6f4;
    padding: 6px 12px;
}
QTabBar::tab:selected {
    background-color: #45475a;
}
QDialogButtonBox QPushButton {
    min-width: 80px;
}
QMessageBox {
    background-color: #1e1e2e;
}
QInputDialog {
    background-color: #1e1e2e;
}
QScrollBar:vertical {
    background-color: #181825;
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background-color: #45475a;
    border-radius: 5px;
    min-height: 20px;
}
QScrollBar::handle:vertical:hover {
    background-color: #585b70;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background-color: #181825;
    height: 10px;
    border: none;
}
QScrollBar::handle:horizontal {
    background-color: #45475a;
    border-radius: 5px;
    min-width: 20px;
}
QScrollBar::handle:horizontal:hover {
    background-color: #585b70;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
"""
