"""QTsys 自动更新模块 - 启动时检查GitHub远程仓库是否有新版本"""
import subprocess
import sys
import os
import signal

from config import VERSION, REPO_URL

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _run_git(*args, timeout=10):
    """执行git命令，超时后强制杀死进程"""
    try:
        proc = subprocess.Popen(
            ["git"] + list(args),
            cwd=BASE_DIR,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
            if sys.platform == "win32" else 0,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            return proc.returncode, stdout.decode("utf-8", errors="replace").strip(), stderr.decode("utf-8", errors="replace").strip()
        except subprocess.TimeoutExpired:
            # Windows: 强制终止进程树
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True)
            else:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()
            return -2, "", "git命令超时"
    except FileNotFoundError:
        return -1, "", "git未安装"
    except Exception as e:
        return -3, "", str(e)


def _get_branch():
    """获取当前分支名"""
    rc, out, _ = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    return out if rc == 0 else "main"


def get_local_info():
    """获取本地版本信息（不联网）"""
    rc, commit, _ = _run_git("rev-parse", "--short", "HEAD")
    local_commit = commit if rc == 0 else "unknown"
    rc2, msg, _ = _run_git("log", "-1", "--format=%s")
    last_msg = msg if rc2 == 0 else ""
    return {
        "version": VERSION,
        "commit": local_commit,
        "last_message": last_msg,
        "repo_url": REPO_URL,
    }


def check_update():
    """检查远程是否有更新（需联网 fetch）"""
    branch = _get_branch()
    # fetch 超时设短一些，网络不通就快速跳过
    rc, _, err = _run_git("fetch", "origin", branch, timeout=15)
    if rc != 0:
        return {"has_update": False, "error": f"fetch失败: {err}", "local": "", "remote": "", "behind": 0}

    rc1, local, _ = _run_git("rev-parse", "HEAD")
    rc2, remote, _ = _run_git("rev-parse", f"origin/{branch}")
    if rc1 != 0 or rc2 != 0:
        return {"has_update": False, "error": "无法获取commit信息", "local": "", "remote": "", "behind": 0}

    if local == remote:
        return {"has_update": False, "error": "", "local": local[:8], "remote": remote[:8], "behind": 0}

    # 计算落后多少个commit
    rc3, count_str, _ = _run_git("rev-list", "--count", f"HEAD..origin/{branch}")
    behind = int(count_str) if rc3 == 0 and count_str.isdigit() else 0

    return {"has_update": behind > 0, "error": "", "local": local[:8], "remote": remote[:8], "behind": behind}


def do_update():
    """执行更新：stash → pull --rebase → stash pop → 检查依赖"""
    branch = _get_branch()

    # 检查是否有未提交的修改
    rc, status, _ = _run_git("status", "--porcelain")
    has_changes = rc == 0 and bool(status)

    # 有修改先 stash
    if has_changes:
        rc, _, err = _run_git("stash", "push", "-m", "auto-update-stash")
        if rc != 0:
            return {"success": False, "message": f"stash失败: {err}"}

    # 记录更新前的 requirements.txt hash
    req_path = os.path.join(BASE_DIR, "requirements.txt")
    old_req = ""
    if os.path.exists(req_path):
        with open(req_path, "r") as f:
            old_req = f.read()

    # pull --rebase
    rc, out, err = _run_git("pull", "--rebase", "origin", branch, timeout=60)
    if rc != 0:
        # 回滚
        if has_changes:
            _run_git("stash", "pop")
        return {"success": False, "message": f"pull失败: {err}"}

    # 恢复 stash
    if has_changes:
        rc2, _, err2 = _run_git("stash", "pop")
        if rc2 != 0:
            return {"success": True, "message": f"更新成功，但stash恢复失败: {err2}"}

    # 检查 requirements.txt 是否变化
    new_req = ""
    if os.path.exists(req_path):
        with open(req_path, "r") as f:
            new_req = f.read()

    pip_msg = ""
    if new_req and new_req != old_req:
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", req_path, "-q"],
                cwd=BASE_DIR, timeout=120,
            )
            pip_msg = "，依赖已更新"
        except Exception as e:
            pip_msg = f"，依赖更新失败: {e}"

    return {"success": True, "message": f"更新成功{pip_msg}"}


def auto_update_on_startup():
    """启动时自动检查更新，网络不通或出错时静默跳过"""
    try:
        print(f"[QTsys v{VERSION}] 正在检查更新...")
        info = check_update()

        if info["error"]:
            print(f"[QTsys] 更新检查跳过: {info['error']}")
            return

        if not info["has_update"]:
            print(f"[QTsys] 已是最新版本 ({info['local']})")
            return

        print(f"[QTsys] 发现新版本: 本地 {info['local']} → 远程 {info['remote']} (落后{info['behind']}个提交)")
        try:
            choice = input("[QTsys] 是否立即更新？(y/n, 默认n): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            choice = "n"

        if choice != "y":
            print("[QTsys] 已跳过更新，使用当前版本启动")
            return

        result = do_update()

        if result["success"]:
            print(f"[QTsys] {result['message']}")
        else:
            print(f"[QTsys] 更新失败: {result['message']}，继续使用当前版本")
    except Exception as e:
        print(f"[QTsys] 更新检查异常: {e}，继续启动")


if __name__ == "__main__":
    auto_update_on_startup()