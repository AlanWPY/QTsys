"""连接对话框"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QLineEdit,
    QSpinBox, QPushButton, QHBoxLayout, QLabel, QMessageBox,
)
from PyQt6.QtCore import Qt
import pymysql


class ConnectionDialog(QDialog):
    def __init__(self, parent=None, creds=None):
        super().__init__(parent)
        self.setWindowTitle("连接 MySQL")
        self.setMinimumWidth(380)
        self.result_creds = None
        self._build_ui(creds)

    def _build_ui(self, creds):
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.host_edit = QLineEdit(creds.get("host", "127.0.0.1") if creds else "127.0.0.1")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(creds.get("port", 3306) if creds else 3306)
        self.user_edit = QLineEdit(creds.get("user", "root") if creds else "root")
        self.pass_edit = QLineEdit(creds.get("password", "") if creds else "")
        self.pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.db_edit = QLineEdit(creds.get("database", "qtsys") if creds else "qtsys")

        form.addRow("主机:", self.host_edit)
        form.addRow("端口:", self.port_spin)
        form.addRow("用户:", self.user_edit)
        form.addRow("密码:", self.pass_edit)
        form.addRow("数据库:", self.db_edit)
        layout.addLayout(form)

        self.status_label = QLabel("")
        layout.addWidget(self.status_label)

        btns = QHBoxLayout()
        test_btn = QPushButton("测试连接")
        test_btn.clicked.connect(self._test)
        save_btn = QPushButton("保存并连接")
        save_btn.clicked.connect(self._save)
        btns.addWidget(test_btn)
        btns.addWidget(save_btn)
        layout.addLayout(btns)

    def _get_params(self):
        return dict(
            host=self.host_edit.text(),
            port=self.port_spin.value(),
            user=self.user_edit.text(),
            password=self.pass_edit.text(),
            database=self.db_edit.text(),
        )

    def _ensure_db(self, p):
        """如果数据库不存在则自动创建"""
        db_name = p["database"]
        no_db = {k: v for k, v in p.items() if k != "database"}
        conn = pymysql.connect(**no_db, connect_timeout=5)
        cur = conn.cursor()
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
        conn.close()

    def _test(self):
        try:
            p = self._get_params()
            self._ensure_db(p)
            conn = pymysql.connect(**p, connect_timeout=5)
            conn.close()
            self.status_label.setText("✓ 连接成功")
            self.status_label.setStyleSheet("color: #a6e3a1;")
        except Exception as e:
            self.status_label.setText(f"✗ {e}")
            self.status_label.setStyleSheet("color: #f38ba8;")

    def _save(self):
        try:
            p = self._get_params()
            self._ensure_db(p)
            conn = pymysql.connect(**p, connect_timeout=5)
            conn.close()
            from db_manager.fingerprint import save_credentials
            save_credentials(**p)
            self.result_creds = p
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "连接失败", str(e))
