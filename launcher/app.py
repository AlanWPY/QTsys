"""Professional desktop launcher for QTsys.

The launcher intentionally uses only the Python standard library plus the
project's existing dependencies. It can be started by double-clicking the root
``QTsys启动器.pyw`` file on Windows.
"""
from __future__ import annotations

import asyncio
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
from dataclasses import dataclass
from pathlib import Path
from tkinter import BOTH, END, LEFT, RIGHT, VERTICAL, W, X, Y, BooleanVar, StringVar, Tk, messagebox
from tkinter import ttk

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import PORT, REPO_URL, VERSION  # noqa: E402


BG = "#0b1220"
PANEL = "#111827"
PANEL_2 = "#172033"
CARD = "#1f2937"
TEXT = "#e5e7eb"
MUTED = "#94a3b8"
PRIMARY = "#38bdf8"
PRIMARY_DARK = "#0ea5e9"
SUCCESS = "#22c55e"
WARNING = "#f59e0b"
DANGER = "#ef4444"
BORDER = "#273449"


@dataclass
class LauncherState:
    backend_process: subprocess.Popen | None = None
    last_backend_pid: int | None = None


class QTsysLauncher:
    def __init__(self, root: Tk):
        self.root = root
        self.state = LauncherState()
        self.status_var = StringVar(value="正在初始化启动器...")
        self.backend_state_var = StringVar(value="检测中")
        self.version_var = StringVar(value=f"本地版本 v{VERSION}")
        self.update_var = StringVar(value="尚未检查更新")
        self.busy_var = BooleanVar(value=False)
        self.settings_vars: dict[str, StringVar] = {}
        self.use_mysql_var = BooleanVar(value=False)
        self.secret_flags: dict[str, bool] = {}

        self.root.title(f"QTsys 启动器 v{VERSION}")
        self.root.geometry("1080x720")
        self.root.minsize(980, 660)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.configure_style()
        self.build_ui()
        self.run_background(self.initial_load)

    def configure_style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=TEXT, fieldbackground=PANEL, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Title.TLabel", background=BG, foreground="#f8fafc", font=("Microsoft YaHei UI", 23, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei UI", 10))
        style.configure("Hero.TLabel", background=PANEL, foreground="#f8fafc", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Kpi.TLabel", background=CARD, foreground="#f8fafc", font=("Microsoft YaHei UI", 17, "bold"))
        style.configure("KpiName.TLabel", background=CARD, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(16, 10), borderwidth=0)
        style.map("TButton", background=[("active", PANEL_2), ("disabled", "#334155")], foreground=[("disabled", "#94a3b8")])
        style.configure("Primary.TButton", background=PRIMARY_DARK, foreground="white")
        style.map("Primary.TButton", background=[("active", PRIMARY), ("disabled", "#334155")])
        style.configure("Success.TButton", background="#16a34a", foreground="white")
        style.map("Success.TButton", background=[("active", SUCCESS), ("disabled", "#334155")])
        style.configure("Danger.TButton", background="#dc2626", foreground="white")
        style.map("Danger.TButton", background=[("active", DANGER), ("disabled", "#334155")])
        style.configure("TEntry", fieldbackground="#0f172a", foreground=TEXT, insertcolor=TEXT, borderwidth=1, padding=8)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT)
        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(18, 10), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", TEXT)])
        style.configure("Vertical.TScrollbar", background=PANEL_2, troughcolor=BG, arrowcolor=MUTED)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill=BOTH, expand=True)

        header = ttk.Frame(outer)
        header.pack(fill=X)
        left = ttk.Frame(header)
        left.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(left, text="QTsys 启动器", style="Title.TLabel").pack(anchor=W)
        ttk.Label(left, text="启动服务、打开 WebUI、配置密钥、测试连接与更新项目的统一入口", style="Subtitle.TLabel").pack(anchor=W, pady=(4, 0))
        ttk.Label(header, textvariable=self.version_var, style="Subtitle.TLabel").pack(side=RIGHT, anchor="ne")

        hero = ttk.Frame(outer, style="Panel.TFrame", padding=22)
        hero.pack(fill=X, pady=(22, 18))
        hero_left = ttk.Frame(hero, style="Panel.TFrame")
        hero_left.pack(side=LEFT, fill=X, expand=True)
        ttk.Label(hero_left, text="一键启动量化分析工作台", style="Hero.TLabel").pack(anchor=W)
        ttk.Label(hero_left, textvariable=self.status_var, style="Panel.TLabel").pack(anchor=W, pady=(8, 0))
        actions = ttk.Frame(hero, style="Panel.TFrame")
        actions.pack(side=RIGHT)
        ttk.Button(actions, text="启动并打开 WebUI", style="Primary.TButton", command=self.start_system).pack(fill=X, pady=(0, 8))
        ttk.Button(actions, text="仅打开 WebUI", command=lambda: webbrowser.open(self.web_url())).pack(fill=X, pady=(0, 8))
        ttk.Button(actions, text="停止后端", style="Danger.TButton", command=self.stop_backend).pack(fill=X)

        kpis = ttk.Frame(outer)
        kpis.pack(fill=X, pady=(0, 18))
        self.kpi(kpis, "后端状态", self.backend_state_var, 0)
        self.kpi(kpis, "访问地址", StringVar(value=self.web_url()), 1)
        self.kpi(kpis, "远程仓库", StringVar(value=REPO_URL.replace("https://github.com/", "")), 2)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill=BOTH, expand=True)
        self.build_settings_tab(notebook)
        self.build_update_tab(notebook)
        self.build_logs_tab(notebook)

    def kpi(self, parent, title: str, value_var: StringVar, column: int):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
        parent.columnconfigure(column, weight=1)
        ttk.Label(card, text=title, style="KpiName.TLabel").pack(anchor=W)
        ttk.Label(card, textvariable=value_var, style="Kpi.TLabel").pack(anchor=W, pady=(6, 0))

    def build_settings_tab(self, notebook: ttk.Notebook):
        tab = ttk.Frame(notebook, padding=18)
        notebook.add(tab, text="配置与测试")

        canvas = ttk.Frame(tab)
        canvas.pack(fill=BOTH, expand=True)

        sections = ttk.Frame(canvas)
        sections.pack(fill=BOTH, expand=True)
        sections.columnconfigure(0, weight=1)
        sections.columnconfigure(1, weight=1)

        ai = self.section(sections, "AI 大模型", "用于策略助手、因子解释和智能分析", 0, 0)
        self.field(ai, "接口地址", "llm_base_url", "例如 https://code.newcli.com/claude/aws")
        self.field(ai, "模型名称", "llm_model", "例如 claude-sonnet-4-5")
        self.field(ai, "API Key", "llm_api_key", "留空则不覆盖已保存密钥", secret=True)
        ttk.Button(ai, text="测试大模型", command=self.test_llm).pack(anchor=W, pady=(10, 0))

        data = self.section(sections, "Tushare", "用于真实行情、指数成分、因子回测数据", 0, 1)
        self.field(data, "Tushare Token", "tushare_token", "留空则不覆盖已保存 Token", secret=True)
        ttk.Button(data, text="测试 Tushare", command=self.test_tushare).pack(anchor=W, pady=(10, 0))

        mysql = self.section(sections, "MySQL 缓存", "可选。用于行情缓存与因子看板加速", 1, 0, columnspan=2)
        grid = ttk.Frame(mysql, style="Panel.TFrame")
        grid.pack(fill=X)
        self.field(grid, "主机", "mysql_host", "127.0.0.1", row=0, column=0)
        self.field(grid, "端口", "mysql_port", "3306", row=0, column=1)
        self.field(grid, "用户名", "mysql_user", "root", row=1, column=0)
        self.field(grid, "数据库", "mysql_database", "qtsys", row=1, column=1)
        self.field(grid, "密码", "mysql_password", "留空则不覆盖已保存密码", row=2, column=0, secret=True)
        ttk.Checkbutton(mysql, text="启用 MySQL 缓存", variable=self.use_mysql_var).pack(anchor=W, pady=(10, 0))

        settings_actions = ttk.Frame(tab)
        settings_actions.pack(fill=X, pady=(14, 0))
        ttk.Button(settings_actions, text="保存配置", style="Success.TButton", command=self.save_settings).pack(side=LEFT)
        ttk.Button(settings_actions, text="测试 MySQL", command=self.test_mysql).pack(side=LEFT, padx=(10, 0))
        ttk.Button(settings_actions, text="重新读取配置", command=lambda: self.run_background(self.load_settings)).pack(side=LEFT, padx=(10, 0))

    def section(self, parent, title: str, subtitle: str, row: int, column: int, columnspan: int = 1):
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=18)
        frame.grid(row=row, column=column, columnspan=columnspan, sticky="nsew", padx=(0 if column == 0 else 10, 0), pady=(0, 10))
        ttk.Label(frame, text=title, style="Hero.TLabel").pack(anchor=W)
        ttk.Label(frame, text=subtitle, style="Panel.TLabel").pack(anchor=W, pady=(4, 12))
        return frame

    def field(self, parent, label: str, key: str, placeholder: str = "", *, row: int | None = None, column: int = 0, secret: bool = False):
        var = self.settings_vars.setdefault(key, StringVar())
        wrapper = ttk.Frame(parent, style="Panel.TFrame")
        if row is None:
            wrapper.pack(fill=X, pady=(0, 10))
        else:
            wrapper.grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 12, 0), pady=(0, 10))
            parent.columnconfigure(column, weight=1)
        ttk.Label(wrapper, text=label, style="Panel.TLabel").pack(anchor=W)
        entry = ttk.Entry(wrapper, textvariable=var, show="•" if secret else "")
        entry.pack(fill=X, pady=(4, 0))
        if placeholder:
            ttk.Label(wrapper, text=placeholder, style="Panel.TLabel").pack(anchor=W, pady=(3, 0))

    def build_update_tab(self, notebook: ttk.Notebook):
        tab = ttk.Frame(notebook, padding=18)
        notebook.add(tab, text="检查更新")

        panel = ttk.Frame(tab, style="Panel.TFrame", padding=22)
        panel.pack(fill=X)
        ttk.Label(panel, text="项目更新", style="Hero.TLabel").pack(anchor=W)
        ttk.Label(panel, text="根据 GitHub 远程仓库版本号和 commit 检查更新。更新前会自动 stash 本地改动，更新后按需安装依赖。", style="Panel.TLabel").pack(anchor=W, pady=(8, 12))
        ttk.Label(panel, textvariable=self.update_var, style="Panel.TLabel").pack(anchor=W, pady=(0, 14))
        row = ttk.Frame(panel, style="Panel.TFrame")
        row.pack(fill=X)
        ttk.Button(row, text="检查更新", command=self.check_update).pack(side=LEFT)
        ttk.Button(row, text="开始更新", style="Success.TButton", command=self.do_update).pack(side=LEFT, padx=(10, 0))

    def build_logs_tab(self, notebook: ttk.Notebook):
        tab = ttk.Frame(notebook, padding=18)
        notebook.add(tab, text="运行日志")
        frame = ttk.Frame(tab, style="Panel.TFrame", padding=12)
        frame.pack(fill=BOTH, expand=True)
        scrollbar = ttk.Scrollbar(frame, orient=VERTICAL)
        scrollbar.pack(side=RIGHT, fill=Y)
        import tkinter as tk
        self.log_text = tk.Text(
            frame,
            bg="#020617",
            fg="#dbeafe",
            insertbackground=TEXT,
            relief="flat",
            wrap="word",
            yscrollcommand=scrollbar.set,
            font=("Consolas", 10),
        )
        self.log_text.pack(side=LEFT, fill=BOTH, expand=True)
        scrollbar.config(command=self.log_text.yview)
        self.log("启动器已就绪。")

    def web_url(self) -> str:
        return f"http://127.0.0.1:{PORT}"

    def log(self, message: str):
        ts = time.strftime("%H:%M:%S")
        if hasattr(self, "log_text"):
            self.log_text.insert(END, f"[{ts}] {message}\n")
            self.log_text.see(END)
        self.status_var.set(message)

    def run_background(self, func, *args):
        def runner():
            try:
                result = func(*args)
                if asyncio.iscoroutine(result):
                    asyncio.run(result)
            except Exception as exc:
                self.root.after(0, lambda: self.show_error(str(exc)))

        threading.Thread(target=runner, daemon=True).start()

    def project_python_cmd(self) -> list[str]:
        candidates = [
            ROOT / ".venv" / "Scripts" / "python.exe",
            ROOT / "venv" / "Scripts" / "python.exe",
        ]
        if not getattr(sys, "frozen", False):
            candidates.insert(0, Path(sys.executable))
        for item in candidates:
            if item.exists():
                return [str(item)]
        py_launcher = shutil.which("py")
        if py_launcher:
            return [py_launcher, "-3"]
        return ["python"]

    def run_project_python(self, code: str, timeout: int = 60) -> dict:
        cmd = self.project_python_cmd() + ["-c", code]
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, timeout=timeout, env=env, encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or f"Python exited with code {proc.returncode}").strip())
        output = (proc.stdout or "").strip()
        if not output:
            return {}
        try:
            return json.loads(output.splitlines()[-1])
        except json.JSONDecodeError:
            return {"message": output}

    def show_error(self, message: str):
        self.log(f"错误：{message}")
        messagebox.showerror("QTsys 启动器", message)

    def is_port_open(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.4)
            return sock.connect_ex(("127.0.0.1", PORT)) == 0

    def refresh_backend_state(self):
        running = self.is_port_open()
        self.backend_state_var.set("运行中" if running else "未启动")
        return running

    def initial_load(self):
        self.root.after(0, lambda: self.log("正在读取本地配置..."))
        asyncio.run(self.load_settings_async())
        self.root.after(0, self.refresh_backend_state)
        self.root.after(0, lambda: self.log("启动器初始化完成。"))

    def load_settings(self):
        asyncio.run(self.load_settings_async())

    async def load_settings_async(self):
        from database.connection import get_session_factory, init_db
        from services.settings_service import get_or_create_settings

        await init_db()
        async with get_session_factory()() as db:
            settings = await get_or_create_settings(db)
            values = {
                "llm_base_url": settings.llm_base_url or "",
                "llm_model": settings.llm_model or "",
                "mysql_host": settings.mysql_host or "",
                "mysql_port": str(settings.mysql_port or 3306),
                "mysql_user": settings.mysql_user or "",
                "mysql_database": settings.mysql_database or "qtsys",
            }
            self.secret_flags = {
                "tushare_token": bool(settings.tushare_token),
                "llm_api_key": bool(settings.llm_api_key),
                "mysql_password": bool(settings.mysql_password),
            }
            use_mysql = bool(settings.use_mysql)

        def apply():
            for key, value in values.items():
                self.settings_vars.setdefault(key, StringVar()).set(value)
            for key, exists in self.secret_flags.items():
                self.settings_vars.setdefault(key, StringVar()).set("")
                if exists:
                    self.log(f"{key} 已保存，输入新值才会覆盖。")
            self.use_mysql_var.set(use_mysql)

        self.root.after(0, apply)

    def collect_settings_payload(self, include_empty_secrets: bool = False) -> dict:
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
            if value or include_empty_secrets:
                payload[key] = value
        return payload

    def save_settings(self):
        self.run_background(self.save_settings_worker)

    def save_settings_worker(self):
        payload = self.collect_settings_payload()
        asyncio.run(self.save_settings_async(payload))
        self.root.after(0, lambda: self.log("配置已保存到本地加密数据库。"))
        self.root.after(0, lambda: messagebox.showinfo("QTsys 启动器", "配置已保存。密钥字段已使用系统加密存储。"))

    async def save_settings_async(self, payload: dict):
        from database.connection import get_session_factory, init_db
        from services.settings_service import apply_settings_update, get_or_create_settings

        await init_db()
        async with get_session_factory()() as db:
            settings = await get_or_create_settings(db)
            apply_settings_update(settings, payload)
            await db.commit()

    async def read_saved_settings_async(self):
        from database.connection import get_session_factory, init_db
        from services.settings_service import get_or_create_settings

        await init_db()
        async with get_session_factory()() as db:
            return await get_or_create_settings(db)

    def test_tushare(self):
        self.run_background(self.test_tushare_worker)

    def test_tushare_worker(self):
        from data.tushare_client import TushareClient

        token = self.settings_vars["tushare_token"].get().strip()
        if not token:
            settings = asyncio.run(self.read_saved_settings_async())
            token = settings.tushare_token or ""
        if not token:
            raise ValueError("请先输入或保存 Tushare Token。")
        client = TushareClient(token)
        df = client.get_stock_basic()
        if df is None or df.empty:
            raise ValueError("Tushare 测试失败：未返回股票基础数据。")
        self.root.after(0, lambda: messagebox.showinfo("QTsys 启动器", f"Tushare 测试成功，获取 {len(df)} 条股票基础数据。"))
        self.root.after(0, lambda: self.log("Tushare 连接测试成功。"))

    def test_mysql(self):
        self.run_background(self.test_mysql_worker)

    def test_mysql_worker(self):
        import pymysql

        password = self.settings_vars["mysql_password"].get().strip()
        if not password:
            settings = asyncio.run(self.read_saved_settings_async())
            password = settings.mysql_password or ""
        host = self.settings_vars["mysql_host"].get().strip() or "127.0.0.1"
        port = int(self.settings_vars["mysql_port"].get().strip() or 3306)
        user = self.settings_vars["mysql_user"].get().strip()
        database = self.settings_vars["mysql_database"].get().strip() or "qtsys"
        if not user:
            raise ValueError("请填写 MySQL 用户名。")
        conn = pymysql.connect(host=host, port=port, user=user, password=password, database=database, charset="utf8mb4", connect_timeout=8)
        try:
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        finally:
            conn.close()
        self.root.after(0, lambda: messagebox.showinfo("QTsys 启动器", "MySQL 连接测试成功。"))
        self.root.after(0, lambda: self.log("MySQL 连接测试成功。"))

    def test_llm(self):
        self.run_background(self.test_llm_worker)

    def test_llm_worker(self):
        from services.llm_gateway import chat_complete_text, normalize_base_url

        api_key = self.settings_vars["llm_api_key"].get().strip()
        base_url = self.settings_vars["llm_base_url"].get().strip()
        model = self.settings_vars["llm_model"].get().strip()
        if not api_key or not base_url or not model:
            settings = asyncio.run(self.read_saved_settings_async())
            api_key = api_key or settings.llm_api_key or ""
            base_url = base_url or settings.llm_base_url or ""
            model = model or settings.llm_model or ""
        if not api_key or not base_url or not model:
            raise ValueError("请先填写或保存完整的 LLM API Key、接口地址和模型。")
        result = asyncio.run(chat_complete_text(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[{"role": "user", "content": "Reply with OK only."}],
            temperature=0.1,
            max_tokens=16,
        ))
        preview = (result.get("content") or "")[:80]
        self.root.after(0, lambda: self.settings_vars["llm_base_url"].set(normalize_base_url(base_url)))
        self.root.after(0, lambda: messagebox.showinfo("QTsys 启动器", f"大模型测试成功。\n模型：{result.get('model') or model}\n响应：{preview}"))
        self.root.after(0, lambda: self.log("大模型连接测试成功。"))

    def start_system(self):
        self.run_background(self.start_system_worker)

    def start_system_worker(self):
        self.root.after(0, lambda: self.log("正在启动后端服务..."))
        if self.is_port_open():
            self.root.after(0, lambda: self.log("后端已在运行，直接打开 WebUI。"))
            self.root.after(0, self.refresh_backend_state)
            self.root.after(0, lambda: webbrowser.open(self.web_url()))
            return

        python_exe = sys.executable
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.state.backend_process = subprocess.Popen(
            [python_exe, "main.py"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self.state.last_backend_pid = self.state.backend_process.pid
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.is_port_open():
                self.root.after(0, self.refresh_backend_state)
                self.root.after(0, lambda: self.log(f"后端启动成功，PID {self.state.last_backend_pid}。"))
                self.root.after(0, lambda: webbrowser.open(self.web_url()))
                return
            if self.state.backend_process.poll() is not None:
                raise RuntimeError("后端进程已退出，请在命令行运行 python main.py 查看详细错误。")
            time.sleep(0.8)
        raise TimeoutError("后端启动超时，请检查端口 8000 是否被占用或依赖是否完整。")

    def stop_backend(self):
        self.run_background(self.stop_backend_worker)

    def stop_backend_worker(self):
        stopped = False
        proc = self.state.backend_process
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=6)
            except subprocess.TimeoutExpired:
                proc.kill()
            stopped = True

        if os.name == "nt" and self.is_port_open():
            powershell = (
                "$conns = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue; "
                "$owners = $conns | Select-Object -ExpandProperty OwningProcess -Unique; "
                "foreach ($owner in $owners) { Stop-Process -Id $owner -Force -ErrorAction SilentlyContinue }"
            )
            subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", powershell], capture_output=True, timeout=15)
            stopped = True

        self.root.after(0, self.refresh_backend_state)
        self.root.after(0, lambda: self.log("后端已停止。" if stopped else "当前没有检测到后端进程。"))

    def check_update(self):
        self.run_background(self.check_update_worker)

    def remote_version_from_git(self) -> str:
        from updater import _get_branch, _run_git

        branch = _get_branch()
        _run_git("fetch", "origin", branch, timeout=20)
        rc, content, _ = _run_git("show", f"origin/{branch}:config.py", timeout=10)
        if rc != 0:
            return ""
        match = re.search(r'VERSION\s*=\s*["\']([^"\']+)["\']', content)
        return match.group(1) if match else ""

    def check_update_worker(self):
        from updater import check_update

        self.root.after(0, lambda: self.log("正在检查 GitHub 更新..."))
        info = check_update()
        remote_version = self.remote_version_from_git()
        message = f"本地版本 v{VERSION}"
        if remote_version:
            message += f"；远程版本 v{remote_version}"
        if info.get("error"):
            message += f"；检查失败：{info['error']}"
        elif info.get("has_update"):
            message += f"；落后 {info.get('behind', 0)} 个 commit，可更新。"
        else:
            message += "；当前已是最新。"
        self.root.after(0, lambda: self.update_var.set(message))
        self.root.after(0, lambda: self.log(message))

    def do_update(self):
        if self.is_port_open():
            if not messagebox.askyesno("QTsys 启动器", "更新前需要停止后端服务。是否继续？"):
                return
            self.stop_backend_worker()
        self.run_background(self.do_update_worker)

    def do_update_worker(self):
        from updater import do_update

        self.root.after(0, lambda: self.log("正在更新项目，请勿关闭窗口..."))
        result = do_update()
        message = result.get("message", "")
        if result.get("success"):
            self.root.after(0, lambda: self.log(message or "项目更新完成。"))
            self.root.after(0, lambda: messagebox.showinfo("QTsys 启动器", message or "项目更新完成。"))
        else:
            self.root.after(0, lambda: self.show_error(message or "项目更新失败。"))
        self.root.after(0, lambda: self.version_var.set(f"本地版本 v{VERSION}"))

    def on_close(self):
        self.root.destroy()


def main():
    root = Tk()
    QTsysLauncher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
