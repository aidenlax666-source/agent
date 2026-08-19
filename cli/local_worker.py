#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地执行端（Local Worker）——混合架构的用户本地组件。

云端 main 分布式处理普通任务；"创建/操作本地文件"类任务由本端在用户
自己电脑上执行（创建文件、写文本、整理目录等），结果回传云端。

本文件**自包含**：不依赖 backend 包（内置 AST 安全扫描），可用 PyInstaller
打包成双击即用的 local_worker.exe。

**用户使用（只需登录一次）**：
    local_worker.exe
    首次运行：输入云端账号邮箱+密码 → 自动登录并保存
    之后：直接运行即自动接收本地任务，无需再填任何东西

打包 exe（Windows，开发者）：
    powershell -ExecutionPolicy Bypass -File build_local_worker.ps1
    # 可在脚本里设置 DEFAULT_SERVER 为你的云端地址，打包进 exe

参数（一般用户不需要）：
    --server  云端 API 地址（默认内置；可用 --server 覆盖）
    --interval 轮询间隔秒（默认 3）
    --workdir 本地工作目录（默认当前目录）
    --max-tasks 最多执行任务数后退出（0=无限，默认）

安全：
- 云端生成的脚本先经内置 AST 扫描（拦 eval/exec/命令注入/危险文件操作）
- 子进程隔离：独立临时目录、输出字节预算、超时、进程树清理
- 脚本由你自己提交到云端、由你授权本端运行——知情执行
"""

import argparse
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    import httpx
except ImportError:
    print("需要 httpx：pip install httpx")
    sys.exit(1)

API = ""
WORKDIR = ""
# 打包时的默认云端地址（build_local_worker.ps1 可覆盖此值）；开发默认为本地
DEFAULT_SERVER = "https://your-cloud.com"
# 本地配置文件名（保存云端地址 + 登录 token）
CONFIG_FILE = "local_worker_config.json"


# ============================================================
# 内置 AST 安全扫描（自包含，不依赖 backend 包）
# ============================================================

class _DangerVisitor(ast.NodeVisitor):
    """拦截：eval/exec/命令注入/网络外联/危险文件删除。"""

    def __init__(self):
        self.violations = []

    def visit_Call(self, node):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name in ("eval", "exec", "compile", "__import__"):
            self.violations.append("动态执行被禁止: " + str(name))
        if name in ("system", "popen", "spawn", "fork"):
            self.violations.append("命令执行被禁止: " + str(name))
        if name in ("requests", "urlopen", "socket"):
            self.violations.append("网络访问被禁止: " + str(name))
        self.generic_visit(node)

    def visit_Import(self, node):
        for a in node.names:
            if a.name.split(".")[0] in ("subprocess", "socket", "requests", "http"):
                self.violations.append("禁止 import: " + a.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        if node.module and node.module.split(".")[0] in ("subprocess", "socket", "requests", "http"):
            self.violations.append("禁止 from import: " + node.module)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.attr in ("system", "popen", "rmtree"):
            self.violations.append("危险操作: " + node.value.id + "." + node.attr)
        self.generic_visit(node)


def scan_script(code):
    """AST 扫描：返回违规列表（空=通过）。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return ["语法错误: " + str(e)]
    v = _DangerVisitor()
    v.visit(tree)
    return v.violations


def extract_output_files(stdout, workdir):
    """从 stdout 提取 [OUTPUT_FILE] <路径> 标记。"""
    files = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("[OUTPUT_FILE]"):
            p = line[len("[OUTPUT_FILE]"):].strip()
            if p:
                files.append(os.path.normpath(os.path.join(workdir, p)) if not os.path.isabs(p) else p)
    return files


# ============================================================
# 本地执行（子进程隔离）
# ============================================================

def run_local(code, timeout=120):
    """本地执行脚本：独立临时目录 + 输出预算 + 超时 + 进程树清理。

    返回 {"success", "stdout", "stderr", "exit_code", "output_file"}
    """
    workspace = tempfile.mkdtemp(prefix="local_run_", dir=WORKDIR)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    proc = None
    try:
        script_path = os.path.join(workspace, "task_script.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(code)

        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True

        proc = subprocess.Popen(
            [sys.executable, script_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=workspace, env=env, **kwargs,
        )
        try:
            stdout_b, stderr_b = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            stdout_b, stderr_b = proc.communicate()
            return {
                "success": False,
                "stdout": stdout_b.decode("utf-8", errors="replace")[:50000],
                "stderr": "TimeoutError: 本地执行超过 %ds\n%s" % (timeout, stderr_b.decode("utf-8", errors="replace")[:10000]),
                "exit_code": -1,
                "output_file": "",
            }

        stdout = stdout_b.decode("utf-8", errors="replace")[:50000]
        stderr = stderr_b.decode("utf-8", errors="replace")[:10000]
        rc = proc.returncode

        output_file = ""
        marked = extract_output_files(stdout, workspace)
        if marked:
            output_file = marked[0]
        elif rc == 0:
            cands = [os.path.join(workspace, n) for n in os.listdir(workspace)
                     if os.path.isfile(os.path.join(workspace, n))
                     and n != "task_script.py"]
            if cands:
                output_file = max(cands, key=os.path.getmtime)

        return {
            "success": rc == 0,
            "stdout": stdout,
            "stderr": stderr,
            "exit_code": rc,
            "output_file": output_file,
        }
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": "SubprocessError: " + str(e),
                "exit_code": -1, "output_file": ""}
    finally:
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(workspace, ignore_errors=True)


def _kill_tree(proc):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


# ============================================================
# 云端交互
# ============================================================

def _headers(token):
    return {"Authorization": "Bearer " + token, "Content-Type": "application/json"}


def poll_task(client, token):
    """轮询领取任务并执行，返回是否执行了一个任务。"""
    try:
        r = client.post(API + "/api/local/tasks/poll", headers=_headers(token), timeout=10)
        r.raise_for_status()
        task = (r.json() or {}).get("task")
    except Exception as e:
        print("[local] poll 失败: " + str(e))
        return False

    if not task:
        return False

    task_id = task.get("task_id")
    requirement = task.get("requirement") or ""
    script = task.get("script")
    print("\n[local] 领取任务 %s: %s" % (task_id, requirement[:60]))

    if not script:
        script = _fallback_script(requirement)

    violations = scan_script(script)
    if violations:
        print("[local] 脚本扫描未通过（%d 项），报告失败" % len(violations))
        report_task(client, token, task_id, success=False, stderr="; ".join(violations),
                    exit_code=-1, detail="本地脚本静态扫描未通过")
        return True

    result = run_local(script)
    print("[local] 任务 %s 执行完成（exit=%s）" % (task_id, result["exit_code"]))
    report_task(client, token, task_id, success=result["success"],
                stdout=result["stdout"], stderr=result["stderr"],
                exit_code=result["exit_code"], output_file=result["output_file"])
    return True


def report_task(client, token, task_id, success, stdout="", stderr="", exit_code=0, output_file="", detail=""):
    try:
        r = client.post(API + "/api/local/tasks/report", headers=_headers(token),
                        json={
                            "task_id": task_id, "success": success,
                            "stdout": stdout, "stderr": stderr,
                            "exit_code": exit_code, "output_file": output_file, "detail": detail,
                        }, timeout=15)
        r.raise_for_status()
        print("[local] 已回传任务 %s 结果" % task_id)
    except Exception as e:
        print("[local] 回传失败: " + str(e))


def _fallback_script(requirement):
    """云端脚本生成失败时的兜底：写一个说明文件，防止任务永久挂起。"""
    import textwrap
    req_repr = repr(requirement[:500])
    return textwrap.dedent(
        "import os, datetime\n"
        "fname = 'local_task_note.txt'\n"
        "with open(fname, 'w', encoding='utf-8') as f:\n"
        "    f.write('任务已由本地设备接收，但云端脚本生成失败。\\n')\n"
        "    f.write('需求：' + " + req_repr + " + '\\n')\n"
        "    f.write('时间：' + str(datetime.datetime.now()) + '\\n')\n"
        "print('[OUTPUT_FILE] ' + os.path.abspath(fname))\n"
        "print('注意：请检查上面需求是否能在本地完成，或重新提交任务。')\n"
    )


# ============================================================
# 配置管理（云端地址 + 登录 token 持久化）
# ============================================================

def _config_path():
    """配置文件放工作目录（exe 同级/当前目录），随软件移动。"""
    return os.path.join(WORKDIR, CONFIG_FILE)


def _load_config():
    try:
        with open(_config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
    except Exception:
        pass
    return {}


def _save_config(cfg):
    try:
        with open(_config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        print("[local] 配置保存失败: " + str(e))
        return False


def _login(client, server, email, password):
    """调云端登录接口，返回 token（失败抛异常）。"""
    r = client.post(server + "/api/auth/login",
                    json={"email": email, "password": password}, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("access_token")


def ensure_token(client, server, cfg, force=False):
    """确保有有效 token：无/失效 → 控制台登录向导（输入邮箱+密码）。

    返回 (token, cfg)。token 验证通过则复用，否则重新登录并保存。
    """
    token = cfg.get("token") or ""
    if token and not force:
        # 轻量验证：调 /me 确认 token 有效
        try:
            r = client.get(server + "/api/auth/me",
                           headers={"Authorization": "Bearer " + token}, timeout=10)
            if r.status_code == 200:
                return token, cfg
        except Exception:
            pass
        print("[local] 登录已过期，请重新登录")
    # 登录向导：控制台输入账号密码
    print("=" * 46)
    print("  本地执行端 · 登录（只需一次，之后自动）")
    print("=" * 46)
    try:
        email = input("  邮箱: ").strip()
        password = input("  密码: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[local] 已取消登录")
        sys.exit(1)
    if not email or not password:
        print("[local] 邮箱或密码不能为空")
        sys.exit(1)
    try:
        token = _login(client, server, email, password)
    except Exception as e:
        print("[local] 登录失败: " + str(e))
        print("       请确认账号密码正确，或先去网页注册账号")
        sys.exit(1)
    cfg["token"] = token
    cfg["server"] = server
    _save_config(cfg)
    print("[local] 登录成功，已保存（下次自动登录）")
    return token, cfg


# ============================================================
# 主循环
# ============================================================

def main():
    global API, WORKDIR
    ap = argparse.ArgumentParser(description="AI 自动化 Agent 本地执行端（混合架构）")
    ap.add_argument("--server", default=None, help="云端 API 地址（默认打包内置）")
    ap.add_argument("--login", action="store_true", help="强制重新登录")
    ap.add_argument("--interval", type=int, default=3, help="轮询间隔秒（默认 3）")
    ap.add_argument("--workdir", default=os.getcwd(), help="本地工作目录（默认当前目录）")
    ap.add_argument("--max-tasks", type=int, default=0, help="最多执行任务数后退出（0=无限）")
    args = ap.parse_args()

    WORKDIR = os.path.abspath(args.workdir)
    os.makedirs(WORKDIR, exist_ok=True)

    # 云端地址：命令行 > 本地配置 > 打包内置默认
    cfg = _load_config()
    API = (args.server or cfg.get("server") or DEFAULT_SERVER).rstrip("/")

    print("[local] 本地执行端启动")
    print("  server : " + API)
    print("  workdir: " + WORKDIR)
    print("  轮询间隔: %ds（Ctrl+C 停止）" % args.interval)

    with httpx.Client() as client:
        # 登录（首次 / token 过期 / 显式 --login）
        token, cfg = ensure_token(client, API, cfg, force=args.login)
        print("[local] 已连接云端，等待本地任务...")
        print("        提示：在网页工作台提交「在 D:/xx 创建 文件.txt」类需求，")
        print("        本端会自动接收并在你电脑上执行。")

        done = 0
        try:
            while True:
                executed = poll_task(client, token)
                if executed:
                    done += 1
                    if args.max_tasks and done >= args.max_tasks:
                        print("[local] 已达上限 %d 个任务，退出" % args.max_tasks)
                        break
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[local] 已停止")


if __name__ == "__main__":
    main()