"""Lightweight professional Windows launcher for QTsys."""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, W, X, Y, BooleanVar, StringVar, Tk, messagebox
from tkinter import ttk


ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]


def read_config_value(name: str, default: str) -> str:
    config_path = ROOT / "config.py"
    try:
        text = config_path.read_text(encoding="utf-8")
    except Exception:
        return default
    match = re.search(rf"{re.escape(name)}\s*=\s*([\"'])(.*?)\1", text)
    return match.group(2) if match else default


VERSION = read_config_value("VERSION", "26.5.14")
REPO_URL = read_config_value("REPO_URL", "https://github.com/AlanWPY/QTsys.git")
PORT = int(read_config_value("PORT", "8000"))

COLORS = {
    "bg": "#07111f",
    "panel": "#0b1628",
    "panel_2": "#111f35",
    "card": "#10243d",
    "card_2": "#0d1b30",
    "line": "#24415f",
    "text": "#e8f2ff",
    "muted": "#8ea4bd",
    "primary": "#00d4ff",
    "primary_2": "#1477ff",
    "success": "#28d17c",
    "warning": "#ffb020",
    "danger": "#ff5263",
}


class TerminalLauncher:
    def __init__(self, root: Tk):
        self.root = root
        self.backend_process: subprocess.Popen | None = None
        self.settings_vars: dict[str, StringVar] = {}
        self.use_mysql_var = BooleanVar(value=False)
        self.status_var = StringVar(value="启动器正在初始化...")
        self.backend_var = StringVar(value="检测中")
        self.version_var = StringVar(value=f"v{VERSION}")
        self.update_var = StringVar(value="尚未检查更新")
        self.health_var = StringVar(value="等待健康检查")

        self.root.title(f"QTsys 金融终端启动器 v{VERSION}")
        self.root.geometry("1160x760")
        self.root.minsize(1040, 700)
        self.root.configure(bg=COLORS["bg"])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.configure_style()
        self.build_ui()
        self.run_bg(self.initial_load)

    def configure_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=COLORS["bg"], foreground=COLORS["text"], fieldbackground="#09182b", bordercolor=COLORS["line"])
        style.configure("Root.TFrame", background=COLORS["bg"])
        style.configure("Panel.TFrame", background=COLORS["panel"])
        style.configure("Card.TFrame", background=COLORS["card"])
        style.configure("SubCard.TFrame", background=COLORS["card_2"])
        style.configure("TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=COLORS["bg"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Panel.TLabel", background=COLORS["panel"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 10))
        style.configure("PanelMuted.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Card.TLabel", background=COLORS["card"], foreground=COLORS["text"], font=("Microsoft YaHei UI", 10))
        style.configure("CardMuted.TLabel", background=COLORS["card"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("Brand.TLabel", background=COLORS["bg"], foreground=COLORS["text"], font=("Bahnschrift SemiBold", 26))
        style.configure("Ticker.TLabel", background=COLORS["bg"], foreground=COLORS["primary"], font=("Consolas", 10, "bold"))
        style.configure("Hero.TLabel", background=COLORS["panel"], foreground="#ffffff", font=("Microsoft YaHei UI", 21, "bold"))
        style.configure("HeroSmall.TLabel", background=COLORS["panel"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 10))
        style.configure("KpiValue.TLabel", background=COLORS["card"], foreground="#ffffff", font=("Consolas", 17, "bold"))
        style.configure("KpiName.TLabel", background=COLORS["card"], foreground=COLORS["muted"], font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 10), borderwidth=0)
        style.configure("Primary.TButton", background=COLORS["primary_2"], foreground="white")
        style.configure("Success.TButton", background="#158f55", foreground="white")
        style.configure("Danger.TButton", background="#b42337", foreground="white")
        style.configure("Ghost.TButton", background=COLORS["card"], foreground=COLORS["text"])
        style.map("TButton", background=[("active", COLORS["panel_2"]), ("disabled", "#27364a")], foreground=[("disabled", COLORS["muted"])])
        style.map("Primary.TButton", background=[("active", COLORS["primary"]), ("disabled", "#27364a")])
        style.configure("TEntry", fieldbackground="#081426", foreground=COLORS["text"], insertcolor=COLORS["text"], padding=9)
        style.configure("TCheckbutton", background=COLORS["panel"], foreground=COLORS["text"])
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=COLORS["panel"], foreground=COLORS["muted"], padding=(22, 11), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", COLORS["card"])], foreground=[("selected", COLORS["text"])])
        style.configure("Vertical.TScrollbar", background=COLORS["panel_2"], troughcolor=COLORS["bg"], arrowcolor=COLORS["muted"])

    def build_ui(self):
        outer = ttk.Frame(self.root, style="Root.TFrame", padding=24)
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer, style="Root.TFrame")
        header.pack(fill=X)
        brand = ttk.Frame(header, style="Root.TFrame")
        brand.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(brand, text="QTsys Terminal", style="Brand.TLabel").pack(anchor=W)
        ttk.Label(brand, text="INSTITUTIONAL QUANT RESEARCH 路 BACKTEST 路 FACTOR LAB", style="Ticker.TLabel").pack(anchor=W, pady=(2, 0))
        ttk.Label(header, textvariable=self.version_var, style="Ticker.TLabel").pack(side=RIGHT, anchor="ne")

        hero = ttk.Frame(outer, style="Panel.TFrame", padding=24)
        hero.pack(fill=X, pady=(22, 18))
        left_panel = ttk.Frame(hero, style="Panel.TFrame")
        left_panel.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(left_panel, text="专业量化分析工作台", style="Hero.TLabel").pack(anchor=W)
        ttk.Label(
            left_panel,
            text="启动后端、打开 WebUI、加密配置密钥、测试数据源与模型连接、执行 GitHub 更新。",
            style="HeroSmall.TLabel",
        ).pack(anchor=W, pady=(8, 0))
        ttk.Label(left_panel, textvariable=self.status_var, style="Panel.TLabel").pack(anchor=W, pady=(16, 0))

        actions = ttk.Frame(hero, style="Panel.TFrame")
        actions.pack(side=RIGHT, padx=(22, 0))
        ttk.Button(actions, text="启动并打开 WebUI", style="Primary.TButton", command=self.start_system).pack(fill=X, pady=(0, 9))
        ttk.Button(actions, text="仅打开 WebUI", style="Ghost.TButton", command=lambda: webbrowser.open(self.web_url())).pack(fill=X, pady=(0, 9))
        ttk.Button(actions, text="停止后端", style="Danger.TButton", command=self.stop_backend).pack(fill=X)

        kpis = ttk.Frame(outer, style="Root.TFrame")
        kpis.pack(fill=X, pady=(0, 18))
        self.kpi(kpis, "后端状态", self.backend_var, 0)
        self.kpi(kpis, "访问地址", StringVar(value=self.web_url()), 1)
        self.kpi(kpis, "研究协议", StringVar(value="真实数据 / 下一开盘 / 成本约束"), 2)
        self.kpi(kpis, "仓库", StringVar(value=REPO_URL.replace("https://github.com/", "")), 3)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=BOTH, expand=True)
        self.build_settings_tab(notebook)
        self.build_update_tab(notebook)
        self.build_health_tab(notebook)
        self.build_log_tab(notebook)

    def kpi(self, parent, title: str, value_var: StringVar, column: int):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
        parent.columnconfigure(column, weight=1)
        ttk.Label(card, text=title, style="KpiName.TLabel").pack(anchor=W)
        ttk.Label(card, textvariable=value_var, style="KpiValue.TLabel").pack(anchor=W, pady=(7, 0))

    def build_settings_tab(self, notebook: ttk.Notebook):
        tab = ttk.Frame(notebook, style="Root.TFrame", padding=18)
        notebook.add(tab, text="配置与连接测试")
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)

        ai_card = self.section(tab, "AI 大模型", "用于策略助手、因子解释、新闻总结和研究假设生成。", 0, 0)
        self.field(ai_card, "接口地址", "llm_base_url", "例如：https://code.newcli.com/claude/aws")
        self.field(ai_card, "模型名称", "llm_model", "例如：claude-sonnet-4-5")
        self.field(ai_card, "API Key", "llm_api_key", "留空则不覆盖已保存密钥", secret=True)
        ttk.Button(ai_card, text="测试大模型", style="Ghost.TButton", command=self.test_llm).pack(anchor=W, pady=(8, 0))

        data_card = self.section(tab, "Tushare 数据源", "用于真实行情、指数成分、日线、财务与因子验证。", 0, 1)
        self.field(data_card, "Tushare Token", "tushare_token", "留空则不覆盖已保存 Token", secret=True)
        ttk.Button(data_card, text="测试 Tushare", style="Ghost.TButton", command=self.test_tushare).pack(anchor=W, pady=(8, 0))

        mysql_card = self.section(tab, "MySQL 缓存", "可选。用于行情缓存、因子看板加速和长任务数据沉淀。", 1, 0, columnspan=2)
        mysql_grid = ttk.Frame(mysql_card, style="Panel.TFrame")
        mysql_grid.pack(fill=X)
        self.field(mysql_grid, "主机", "mysql_host", "127.0.0.1", row=0, column=0)
        self.field(mysql_grid, "端口", "mysql_port", "3306", row=0, column=1)
        self.field(mysql_grid, "用户名", "mysql_user", "root", row=1, column=0)
        self.field(mysql_grid, "数据库", "mysql_database", "qtsys", row=1, column=1)
        self.field(mysql_grid, "密码", "mysql_password", "留空则不覆盖已保存密码", row=2, column=0, secret=True)
        ttk.Checkbutton(mysql_card, text="启用 MySQL 缓存", variable=self.use_mysql_var).pack(anchor=W, pady=(8, 0))

        actions = ttk.Frame(tab, style="Root.TFrame")
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        ttk.Button(actions, text="保存配置", style="Success.TButton", command=self.save_settings).pack(side=LEFT)
        ttk.Button(actions, text="测试 MySQL", style="Ghost.TButton", command=self.test_mysql).pack(side=LEFT, padx=(10, 0))
        ttk.Button(actions, text="重新读取配置", style="Ghost.TButton", command=lambda: self.run_bg(self.load_settings)).pack(side=LEFT, padx=(10, 0))

    def build_update_tab(self, notebook: ttk.Notebook):
        tab = ttk.Frame(notebook, style="Root.TFrame", padding=18)
        notebook.add(tab, text="检查更新")
        panel = ttk.Frame(tab, style="Panel.TFrame", padding=22)
        panel.pack(fill=X)
        ttk.Label(panel, text="GitHub 更新中心", style="Hero.TLabel").pack(anchor=W)
        ttk.Label(panel, text="对比远程版本号和 commit；更新前会自动停止后端服务。", style="HeroSmall.TLabel").pack(anchor=W, pady=(8, 12))
        ttk.Label(panel, textvariable=self.update_var, style="Panel.TLabel").pack(anchor=W, pady=(0, 14))
        row = ttk.Frame(panel, style="Panel.TFrame")
        row.pack(fill=X)
        ttk.Button(row, text="检查更新", style="Ghost.TButton", command=self.check_update).pack(side=LEFT)
        ttk.Button(row, text="开始更新", style="Success.TButton", command=self.do_update).pack(side=LEFT, padx=(10, 0))

    def build_health_tab(self, notebook: ttk.Notebook):
        tab = ttk.Frame(notebook, style="Root.TFrame", padding=18)
        notebook.add(tab, text="系统健康")
        panel = ttk.Frame(tab, style="Panel.TFrame", padding=22)
        panel.pack(fill=BOTH, expand=True)
        ttk.Label(panel, text="发布前健康检查", style="Hero.TLabel").pack(anchor=W)
        ttk.Label(panel, text="运行 Python 编译、反未来函数、执行器一致性、安全扫描、后端和前端冒烟。", style="HeroSmall.TLabel").pack(anchor=W, pady=(8, 12))
        ttk.Label(panel, textvariable=self.health_var, style="Panel.TLabel").pack(anchor=W, pady=(0, 12))
        ttk.Button(panel, text="运行健康检查", style="Primary.TButton", command=self.run_health_check).pack(anchor=W)

    def build_log_tab(self, notebook: ttk.Notebook):
        import tkinter as tk

        tab = ttk.Frame(notebook, style="Root.TFrame", padding=18)
        notebook.add(tab, text="运行日志")
        frame = ttk.Frame(tab, style="Panel.TFrame", padding=12)
        frame.pack(fill=BOTH, expand=True)
        scrollbar = ttk.Scrollbar(frame, orient=VERTICAL)
        scrollbar.pack(side=RIGHT, fill=Y)
        self.log_text = tk.Text(
            frame,
            bg="#04101f",
            fg="#d8ecff",
            insertbackground=COLORS["text"],
            relief="flat",
            wrap="word",
            yscrollcommand=scrollbar.set,
            font=("Consolas", 10),
        )
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.config(command=self.log_text.yview)
        self.log("启动器已就绪。")

    def section(self, parent, title: str, subtitle: str, row: int, column: int, columnspan: int = 1):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=18)
        frame.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", padx=(0 if column == 0 else 10, 0), pady=(0, 10))
        ttk.Label(frame, text=title, style="Hero.TLabel").pack(anchor=W)
        ttk.Label(frame, text=subtitle, style="HeroSmall.TLabel").pack(anchor=W, pady=(4, 12))
        return frame

    def field(self, parent, label: str, key: str, placeholder: str = "", *, row: int | None = None, column: int = 0, secret: bool = False):
        value_var = self.settings_vars.setdefault(key, StringVar())
        wrapper = ttk.Frame(parent, style="Panel.TFrame")
        if row is None:
            wrapper.pack(fill=X, pady=(0, 10))
        else:
            wrapper.grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 12, 0), pady=(0, 10))
            parent.columnconfigure(column, weight=1)
        ttk.Label(wrapper, text=label, style="Panel.TLabel").pack(anchor=W)
        ttk.Entry(wrapper, textvariable=value_var, show="●" if secret else "").pack(fill=X, pady=(4, 0))
        if placeholder:
            ttk.Label(wrapper, text=placeholder, style="PanelMuted.TLabel").pack(anchor=W, pady=(3, 0))

    def web_url(self) -> str:
        return f"http://127.0.0.1:{PORT}"

    def log(self, message: str):
        timestamp = time.strftime("%H:%M:%S")
        if hasattr(self, "log_text"):
            self.log_text.insert(END, f"[{timestamp}] {message}\n")
            self.log_text.see(END)
        self.status_var.set(message)

    def run_bg(self, func, *args):
        def runner():
            try:
                func(*args)
            except Exception as exc:
                self.root.after(0, lambda: self.show_error(str(exc)))

        threading.Thread(target=runner, daemon=True).start()

    def show_error(self, message: str):
        self.log(f"错误：{message}")
        messagebox.showerror("QTsys 金融终端启动器", message)

    def project_python_cmd(self) -> list[str]:
        for candidate in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / "venv" / "Scripts" / "python.exe"):
            if candidate.exists():
                return [str(candidate)]
        python_launcher = shutil.which("py")
        if python_launcher:
            return [python_launcher, "-3"]
        return ["python"]

    def run_project_python(self, code: str, args: list[str] | None = None, timeout: int = 90) -> dict:
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        process = subprocess.run(
            self.project_python_cmd() + ["-c", code, *(args or [])],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        if process.returncode != 0:
            raise RuntimeError((process.stderr or process.stdout or f"Python exited with code {process.returncode}").strip())
        output = (process.stdout or "").strip()
        return json.loads(output.splitlines()[-1]) if output else {}

    def is_port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            return sock.connect_ex(("127.0.0.1", PORT)) == 0

    def refresh_backend_state(self):
        self.backend_var.set("运行中" if self.is_port_open() else "未启动")

    def initial_load(self):
        self.load_settings()
        self.root.after(0, self.refresh_backend_state)
        self.root.after(0, lambda: self.log("启动器初始化完成。"))

    def load_settings(self):
        result = self.run_project_python(r'''
import asyncio, json
async def main():
    from database.connection import get_session_factory, init_db
    from services.settings_service import get_or_create_settings
    await init_db()
    async with get_session_factory()() as db:
        settings = await get_or_create_settings(db)
        print(json.dumps({
            "values": {
                "llm_base_url": settings.llm_base_url or "",
                "llm_model": settings.llm_model or "",
                "mysql_host": settings.mysql_host or "",
                "mysql_port": str(settings.mysql_port or 3306),
                "mysql_user": settings.mysql_user or "",
                "mysql_database": settings.mysql_database or "qtsys",
            },
            "secret_flags": {
                "tushare_token": bool(settings.tushare_token),
                "llm_api_key": bool(settings.llm_api_key),
                "mysql_password": bool(settings.mysql_password),
            },
            "use_mysql": bool(settings.use_mysql),
        }, ensure_ascii=False))
asyncio.run(main())
''')

        def apply_values():
            for key, value in (result.get("values") or {}).items():
                self.settings_vars.setdefault(key, StringVar()).set(value)
            for key, exists in (result.get("secret_flags") or {}).items():
                self.settings_vars.setdefault(key, StringVar()).set("")
                if exists:
                    self.log(f"{key} 已保存；输入新值才会覆盖。")
            self.use_mysql_var.set(bool(result.get("use_mysql")))

        self.root.after(0, apply_values)

    def collect_payload(self) -> dict:
        payload = {
            "llm_base_url": self.settings_vars["llm_base_url"].get().strip(),
            "llm_model": self.settings_vars["llm_model"].get().strip(),
            "mysql_host": self.settings_vars["mysql_host"].get().strip(),
            "mysql_port": int(self.settings_vars["mysql_port"].get().strip() or 3306),
            "mysql_user": self.settings_vars["mysql_user"].get().strip(),
            "mysql_database": self.settings_vars["mysql_database"].get().strip() or "qtsys",
            "use_mysql": 1 if self.use_mysql_var.get() else 0,
        }
        for key in ("tushare_token", "llm_api_key", "mysql_password"):
            value = self.settings_vars[key].get().strip()
            if value:
                payload[key] = value
        return payload

    def save_settings(self):
        self.run_bg(self.save_settings_worker)

    def save_settings_worker(self):
        payload = json.dumps(self.collect_payload(), ensure_ascii=False)
        self.run_project_python(r'''
import asyncio, json, sys
payload = json.loads(sys.argv[1])
async def main():
    from database.connection import get_session_factory, init_db
    from services.settings_service import apply_settings_update, get_or_create_settings
    await init_db()
    async with get_session_factory()() as db:
        settings = await get_or_create_settings(db)
        apply_settings_update(settings, payload)
        await db.commit()
    print(json.dumps({"success": True}, ensure_ascii=False))
asyncio.run(main())
''', [payload], timeout=90)
        self.root.after(0, lambda: self.log("配置已保存到本地加密数据库。"))
        self.root.after(0, lambda: messagebox.showinfo("QTsys 金融终端启动器", "配置已保存。密钥字段已使用系统加密存储。"))

    def test_tushare(self):
        self.run_bg(self.test_tushare_worker)

    def test_tushare_worker(self):
        result = self.run_project_python(r'''
import asyncio, json, sys
token = sys.argv[1].strip()
async def saved_token():
    from database.connection import get_session_factory, init_db
    from services.settings_service import get_or_create_settings
    await init_db()
    async with get_session_factory()() as db:
        settings = await get_or_create_settings(db)
        return settings.tushare_token or ""
if not token:
    token = asyncio.run(saved_token())
if not token:
    raise SystemExit("请先输入或保存 Tushare Token。")
from data.tushare_client import TushareClient
client = TushareClient(token)
df = client.get_stock_basic()
if df is None or df.empty:
    raise SystemExit("Tushare 测试失败：未返回股票基础数据。")
print(json.dumps({"success": True, "count": int(len(df))}, ensure_ascii=False))
''', [self.settings_vars["tushare_token"].get().strip()], timeout=120)
        self.root.after(0, lambda: messagebox.showinfo("QTsys 金融终端启动器", f"Tushare 测试成功，获取 {result.get('count', 0)} 条股票基础数据。"))
        self.root.after(0, lambda: self.log("Tushare 连接测试成功。"))

    def test_mysql(self):
        self.run_bg(self.test_mysql_worker)

    def test_mysql_worker(self):
        payload = json.dumps({
            "host": self.settings_vars["mysql_host"].get().strip() or "127.0.0.1",
            "port": int(self.settings_vars["mysql_port"].get().strip() or 3306),
            "user": self.settings_vars["mysql_user"].get().strip(),
            "password": self.settings_vars["mysql_password"].get().strip(),
            "database": self.settings_vars["mysql_database"].get().strip() or "qtsys",
        }, ensure_ascii=False)
        self.run_project_python(r'''
import asyncio, json, sys
payload = json.loads(sys.argv[1])
async def fill_password():
    if payload.get("password"):
        return
    from database.connection import get_session_factory, init_db
    from services.settings_service import get_or_create_settings
    await init_db()
    async with get_session_factory()() as db:
        settings = await get_or_create_settings(db)
        payload["password"] = settings.mysql_password or ""
asyncio.run(fill_password())
if not payload.get("user"):
    raise SystemExit("请填写 MySQL 用户名。")
import pymysql
conn = pymysql.connect(host=payload["host"], port=int(payload["port"]), user=payload["user"], password=payload.get("password") or "", database=payload["database"], charset="utf8mb4", connect_timeout=8)
try:
    with conn.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
finally:
    conn.close()
print(json.dumps({"success": True}, ensure_ascii=False))
''', [payload], timeout=90)
        self.root.after(0, lambda: messagebox.showinfo("QTsys 金融终端启动器", "MySQL 连接测试成功。"))
        self.root.after(0, lambda: self.log("MySQL 连接测试成功。"))

    def test_llm(self):
        self.run_bg(self.test_llm_worker)

    def test_llm_worker(self):
        payload = json.dumps({
            "api_key": self.settings_vars["llm_api_key"].get().strip(),
            "base_url": self.settings_vars["llm_base_url"].get().strip(),
            "model": self.settings_vars["llm_model"].get().strip(),
        }, ensure_ascii=False)
        result = self.run_project_python(r'''
import asyncio, json, sys
payload = json.loads(sys.argv[1])
async def main():
    from database.connection import get_session_factory, init_db
    from services.settings_service import get_or_create_settings
    from services.llm_gateway import chat_complete_text, normalize_base_url
    await init_db()
    async with get_session_factory()() as db:
        settings = await get_or_create_settings(db)
        api_key = payload.get("api_key") or settings.llm_api_key or ""
        base_url = payload.get("base_url") or settings.llm_base_url or ""
        model = payload.get("model") or settings.llm_model or ""
    if not api_key or not base_url or not model:
        raise SystemExit("请先填写或保存完整的 LLM API Key、接口地址和模型。")
    result = await chat_complete_text(api_key=api_key, base_url=base_url, model=model, messages=[{"role": "user", "content": "Reply with OK only."}], temperature=0.1, max_tokens=16)
    print(json.dumps({"success": True, "model": result.get("model") or model, "preview": (result.get("content") or "")[:80], "base_url": normalize_base_url(base_url)}, ensure_ascii=False))
asyncio.run(main())
''', [payload], timeout=150)
        self.root.after(0, lambda: self.settings_vars["llm_base_url"].set(result.get("base_url") or ""))
        self.root.after(0, lambda: messagebox.showinfo("QTsys 金融终端启动器", f"大模型测试成功。\n模型：{result.get('model')}\n响应：{result.get('preview')}"))
        self.root.after(0, lambda: self.log("大模型连接测试成功。"))

    def run_health_check(self):
        self.run_bg(self.run_health_check_worker)

    def run_health_check_worker(self):
        self.root.after(0, lambda: self.health_var.set("健康检查运行中..."))
        command = self.project_python_cmd() + ["scripts/health_check.py"]
        process = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=300, encoding="utf-8", errors="replace")
        output = (process.stdout or process.stderr or "").strip()
        self.root.after(0, lambda: self.log(output[-1200:] if output else "健康检查无输出。"))
        if process.returncode == 0:
            self.root.after(0, lambda: self.health_var.set("健康检查通过"))
            self.root.after(0, lambda: messagebox.showinfo("QTsys 金融终端启动器", "健康检查通过。"))
        else:
            self.root.after(0, lambda: self.health_var.set("健康检查失败，请查看日志"))
            raise RuntimeError(output or "健康检查失败")

    def start_system(self):
        self.run_bg(self.start_system_worker)

    def start_system_worker(self):
        self.root.after(0, lambda: self.log("正在启动后端服务..."))
        if self.is_port_open():
            self.root.after(0, lambda: self.log("后端已在运行，直接打开 WebUI。"))
            self.root.after(0, self.refresh_backend_state)
            self.root.after(0, lambda: webbrowser.open(self.web_url()))
            return
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.backend_process = subprocess.Popen(
            self.project_python_cmd() + ["main.py"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.is_port_open():
                self.root.after(0, self.refresh_backend_state)
                self.root.after(0, lambda: self.log(f"后端启动成功，PID {self.backend_process.pid}。"))
                self.root.after(0, lambda: webbrowser.open(self.web_url()))
                return
            if self.backend_process.poll() is not None:
                raise RuntimeError("后端进程已退出，请在命令行运行 python main.py 查看详细错误。")
            time.sleep(0.8)
        raise TimeoutError("后端启动超时，请检查端口 8000 是否被占用或依赖是否完整。")

    def stop_backend(self):
        self.run_bg(self.stop_backend_worker)

    def stop_backend_worker(self):
        stopped = False
        if self.backend_process and self.backend_process.poll() is None:
            self.backend_process.terminate()
            try:
                self.backend_process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                self.backend_process.kill()
            stopped = True
        if os.name == "nt" and self.is_port_open():
            powershell = "$conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; $owners = $conns | Select-Object -ExpandProperty OwningProcess -Unique; foreach ($owner in $owners) { Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue }"
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", powershell], capture_output=True, timeout=15)
            stopped = True
        self.root.after(0, self.refresh_backend_state)
        self.root.after(0, lambda: self.log("后端已停止。" if stopped else "当前没有检测到后端进程。"))

    def check_update(self):
        self.run_bg(self.check_update_worker)

    def check_update_worker(self):
        result = self.run_project_python(r'''
import json, re
from updater import check_update, _get_branch, _run_git
info = check_update()
remote_version = ""
branch = _get_branch()
rc, content, _ = _run_git("show", f"origin/{branch}:config.py", timeout=10)
if rc == 0:
    match = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', content)
    if match:
        remote_version = match.group(1)
info["remote_version"] = remote_version
print(json.dumps(info, ensure_ascii=False))
''', timeout=90)
        message = f"本地版本 v{VERSION}"
        if result.get("remote_version"):
            message += f"；远程版本 v{result['remote_version']}"
        if result.get("error"):
            message += f"；检查失败：{result['error']}"
        elif result.get("has_update"):
            message += f"；落后 {result.get('behind', 0)} 个 commit，可更新。"
        else:
            message += "；当前已是最新。"
        self.root.after(0, lambda: self.update_var.set(message))
        self.root.after(0, lambda: self.log(message))

    def do_update(self):
        if self.is_port_open() and not messagebox.askyesno("QTsys 金融终端启动器", "更新前需要停止后端服务。是否继续？"):
            return
        self.run_bg(self.do_update_worker)

    def do_update_worker(self):
        if self.is_port_open():
            self.stop_backend_worker()
        result = self.run_project_python(r'''
import json
from updater import do_update
print(json.dumps(do_update(), ensure_ascii=False))
''', timeout=180)
        if result.get("success"):
            self.root.after(0, lambda: messagebox.showinfo("QTsys 金融终端启动器", result.get("message") or "项目更新完成。"))
            self.root.after(0, lambda: self.log(result.get("message") or "项目更新完成。"))
        else:
            raise RuntimeError(result.get("message") or "项目更新失败。")

    def on_close(self):
        self.root.destroy()


def main():
    root = Tk()
    TerminalLauncher(root)
    root.mainloop()


if __name__ == "__main__":
    main()

