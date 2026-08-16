#!/usr/bin/env python
"""AI 自动化 Agent 本地 CLI —— 像 Claude Code 一样持续对话改你的项目。

用法：
    python agent-cli.py                        # 在当前目录进入交互会话
    python agent-cli.py C:\path\to\project     # 在指定项目进入交互会话
    python agent-cli.py --api http://localhost:8000   # 指定后端地址
    python agent-cli.py --resume               # 恢复该项目上次的会话（不丢上下文）
    python agent-cli.py --yes                  # 自动确认模式（每轮直接改码并应用）
    python agent-cli.py --no-commit            # 不自动 git commit（git 仓库默认自动提交）

进入会话后持续交互（每轮循环，可连续提多个需求）：
    你> 输入需求
    AI 出修改方案（不改代码）→ Enter 确认 / 输入意见重新规划 / q 放弃本轮
    AI 按方案改码 → 展示 diff → 全部(a)/逐文件(f)/放弃(n) 确认应用
    改完后回到 你> 继续下一个需求；输入 exit 或 q 退出会话
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import httpx

# 打包时排除的目录（大/无关）
EXCLUDE_DIRS = {"node_modules", ".next", "__pycache__", ".git", "venv", ".venv",
                "dist", "build", "out", ".idea", ".vscode", "web", "tmp",
                "uploads", "data", "browser_profile", "screens", "node_modules"}
# 排除的文件类型（二进制/锁文件等）
EXCLUDE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf",
                ".pdf", ".zip", ".exe", ".dll", ".so", ".dylib", ".pyc", ".lock"}
# 单个文件大小上限
MAX_FILE_BYTES = 2 * 1024 * 1024
# 总文件数/大小上限
MAX_FILES = 200
MAX_TOTAL_BYTES = 20 * 1024 * 1024

# ===== 彩色输出（无依赖 ANSI；非终端/管道下自动关闭）=====
_USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str, text: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if _USE_COLOR else text


def c_dim(t): return _c("2", t)
def c_green(t): return _c("32", t)
def c_red(t): return _c("31", t)
def c_yellow(t): return _c("33", t)
def c_cyan(t): return _c("36", t)
def c_magenta(t): return _c("35", t)
def c_bold(t): return _c("1", t)


# ===== 会话持久化（--resume 恢复，存用户主目录，不入项目仓库）=====
SESSION_DIR = os.environ.get("AI_AGENT_SESSION_DIR") or os.path.join(
    os.path.expanduser("~"), ".ai_agent", "sessions")


def _session_path(root: str) -> str:
    h = hashlib.sha1(os.path.abspath(root).lower().encode("utf-8")).hexdigest()[:12]
    return os.path.join(SESSION_DIR, f"{h}.json")


def load_session(root: str) -> dict:
    try:
        with open(_session_path(root), encoding="utf-8-sig") as f:
            session = json.load(f)
    except Exception:
        return {"project": root, "rounds": []}
    # 清理历史轮次里可能带 BOM 的需求文本
    for r in session.get("rounds") or []:
        r["requirement"] = str(r.get("requirement") or "").lstrip("\ufeff")
    return session


def save_session(root: str, session: dict) -> None:
    try:
        os.makedirs(SESSION_DIR, exist_ok=True)
        session["updated"] = datetime.now().isoformat(timespec="seconds")
        with open(_session_path(root), "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=1)
    except Exception as e:
        print(f"{c_dim('[警告] 会话保存失败（不影响本次改动）: ')}{str(e)[:100]}")


def collect_files(root: str) -> dict[str, bytes]:
    """遍历项目目录收集文件内容（排除忽略项），返回 {相对路径: bytes}。"""
    root = os.path.abspath(root)
    files: dict[str, bytes] = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXCLUDE_EXTS:
                continue
            fp = os.path.join(dirpath, fn)
            rel = os.path.relpath(fp, root).replace("\\", "/")
            try:
                size = os.path.getsize(fp)
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            if len(files) >= MAX_FILES or total + size > MAX_TOTAL_BYTES:
                continue
            try:
                files[rel] = Path(fp).read_bytes()
                total += size
            except (OSError, PermissionError):
                continue
    return files


def build_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel, data in files.items():
            zf.writestr(rel, data)
    return buf.getvalue()


def post_dev(api: str, path: str, requirement: str, zdata: bytes,
             plan: str = "", feedback: str = "") -> dict:
    """POST 后端开发接口，返回 JSON 或抛错。"""
    data = {"requirement": requirement}
    if plan:
        data["plan"] = plan
    if feedback:
        data["feedback"] = feedback
    with httpx.Client(timeout=600, trust_env=False) as client:
        resp = client.post(
            f"{api}/api/dev{path}",
            data=data,
            files={"file": ("project.zip", zdata, "application/zip")},
        )
    if resp.status_code != 200:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        raise RuntimeError(f"后端返回 {resp.status_code}: {detail}")
    return resp.json()


def apply_changes(root: str, modified_zip_b64: str, only: set[str] | None = None) -> list[str]:
    """把后端返回的修改后文件 zip 应用回本地项目（only 给定时只应用这些文件），返回应用列表。"""
    data = base64.b64decode(modified_zip_b64)
    applied = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            if only is not None and member.filename not in only:
                continue
            target = os.path.normpath(os.path.join(root, member.filename))
            if not target.startswith(os.path.abspath(root) + os.sep):
                continue  # 防穿越
            os.makedirs(os.path.dirname(target), exist_ok=True)
            Path(target).write_bytes(zf.read(member))
            applied.append(member.filename)
    return applied


def is_git_repo(root: str) -> bool:
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                           cwd=root, capture_output=True, timeout=10)
        return r.returncode == 0 and r.stdout.decode().strip() == "true"
    except Exception:
        return False


def git_head(root: str) -> str:
    try:
        import subprocess
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                           capture_output=True, timeout=10)
        return r.stdout.decode().strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def git_commit(root: str, message: str) -> bool:
    try:
        import subprocess
        subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True, timeout=60)
        subprocess.run(["git", "commit", "-m", message], cwd=root, check=True,
                       capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"{c_yellow('[警告] git commit 失败（手动提交即可）: ')}{str(e)[:120]}")
        return False


HELP_TEXT = """支持指令:
  exit / quit / q   退出会话
  help / h          显示本帮助
其余输入都会被当作改码需求提交给 AI（先出方案，确认后改码）。
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 自动化 Agent 本地 CLI：像 Claude Code 一样持续对话改你的项目")
    ap.add_argument("project", nargs="?", default=".", help="项目目录（默认当前目录）")
    ap.add_argument("--api", default="http://localhost:8000", help="后端地址")
    ap.add_argument("--resume", action="store_true", help="恢复该项目上次的会话（保留之前轮次记录）")
    ap.add_argument("--no-commit", action="store_true", help="git 仓库下不自动 commit（默认自动）")
    ap.add_argument("--yes", "-y", action="store_true", help="自动确认方案并应用改动（跳过交互）")
    args = ap.parse_args()

    root = os.path.abspath(args.project)
    if not os.path.isdir(root):
        print(f"{c_red('[错误] 项目目录不存在:')} {root}", file=sys.stderr)
        sys.exit(1)

    # Windows 控制台启用 ANSI 颜色
    if _USE_COLOR and os.name == "nt":
        try:
            os.system("")
        except Exception:
            pass

    git_repo = is_git_repo(root)
    session = load_session(root) if args.resume else {"project": root, "rounds": []}
    if session.get("rounds"):
        print(c_cyan(f"[恢复] 上次会话有 {len(session['rounds'])} 轮改动，最近一轮:"))
        last = session["rounds"][-1]
        print(f"  {c_dim('·')} {last.get('requirement', '')[:80]}")
        print(f"  {c_dim('·')} 改动: {', '.join((last.get('files') or [])[:5])}")

    print("=" * 60)
    print(c_bold(" AI 通用 Agent · 交互改码会话（像 Claude Code）"))
    print(f" 项目目录: {root}")
    print(f" 后端: {args.api}" + (c_dim("   |  git 自动提交已开启（--no-commit 关闭）") if git_repo else "   |  非 git 仓库"))
    print(" 输入需求开始改码；输入 exit 或 q 退出；help 查看指令")
    print("=" * 60)

    def collect() -> bytes:
        """重新收集项目文件并打包 zip（包含之前已应用的改动）。空目录也允许（从零建项目）。"""
        files = collect_files(root)
        print(f"{c_dim('[收集]')} 共 {len(files)} 个文件" + (c_dim("（空项目，将从零创建）") if not files else ""))
        return build_zip(files)

    try:
        zdata = collect()
    except RuntimeError as e:
        print(f"{c_red('[错误]')} {e}", file=sys.stderr)
        sys.exit(1)

    while True:
        print()
        try:
            requirement = input(c_cyan("你> ")).strip().lstrip("\ufeff")
        except (EOFError, KeyboardInterrupt):
            print(f"\n{c_green('再见！')}")
            break
        if not requirement:
            continue
        low = requirement.lower()
        if low in ("exit", "quit", "q"):
            print(c_green("再见！"))
            break
        if low in ("help", "h"):
            print(HELP_TEXT)
            continue

        # ---- 本轮第 1 步：AI 出修改方案（不改代码）----
        print(f"{c_yellow('[方案]')} AI 分析代码并生成修改方案（DeepSeek）...")
        feedback = ""
        plan_text = ""
        while True:
            try:
                data = post_dev(args.api, "/plan", requirement, zdata, feedback=feedback)
            except Exception as e:
                print(f"{c_red('[错误] 方案生成失败:')} {e}", file=sys.stderr)
                break

            plan_text = (data.get("plan") or "").strip()
            plan_files = data.get("files") or []
            questions = data.get("questions") or []

            print("\n" + "=" * 60)
            print(f"{c_magenta('[方案] AI 修改方案:')}")
            print("=" * 60)
            print(plan_text or "(AI 未给出方案文本)")

            if plan_files:
                print(f"\n{c_dim('[文件] 预计改动文件:')}")
                for f in plan_files:
                    if isinstance(f, dict):
                        print(f"  {c_dim('-')} {f.get('path', f)}")
                    else:
                        print(f"  {c_dim('-')} {f}")
            if questions:
                print(f"\n{c_yellow('[问题] AI 需要你确认:')}")
                for q in questions:
                    print(f"  {c_yellow('?')} {q}")

            if args.yes:
                break

            try:
                ans = input(
                    f"\n{c_yellow('[询问]')} Enter 确认 / 输入你的意见让 AI 调整方案 / 输入 q 放弃本轮: "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print("\n已放弃本轮，未做任何改动。")
                plan_text = ""
                break
            if ans.lower() in ("q", "quit", "exit"):
                print("已放弃本轮，未做任何改动。")
                plan_text = ""
                break
            if ans:
                feedback = ans
                print(f"{c_yellow('[方案]')} 带着你的意见重新规划（{len(feedback)} 字）...")
                continue
            break

        if not plan_text:
            continue  # 放弃本轮，回到主循环

        # ---- 本轮第 2 步：按确认的方案落地改动 ----
        print(f"\n{c_yellow('[改码]')} 按已确认方案执行改动（DeepSeek）...")
        try:
            data = post_dev(args.api, "/apply", requirement, zdata, plan=plan_text, feedback=feedback)
        except Exception as e:
            print(f"{c_red('[错误] 改码失败:')} {e}", file=sys.stderr)
            continue

        print("\n" + "=" * 60)
        print(f"{c_magenta('[AI]')} AI 改动说明:", data.get("dev_summary") or "(无)")
        print("=" * 60)
        for f in data.get("dev_files") or []:
            icon = c_green("[新]") if f["status"] == "新增" else c_cyan("[改]")
            print(f"  {icon} {f['path']}  ({f['status']}, {f['size']} 字符)")
        diff = data.get("dev_diff") or ""
        if diff:
            print(f"\n{c_dim('[Diff] Diff 预览（前 3000 字符）:')}")
            print(diff[:3000])
            if len(diff) > 3000:
                print(f"  {c_dim('...（共 ')}{len(diff)}{c_dim(' 字符）')}")

        # 操作型需求：显示执行过的命令与输出
        dev_cmd = data.get("dev_command") or ""
        if dev_cmd:
            out_ok = data.get("dev_output_ok") is not False
            icon = c_green("▶ 已执行:") if out_ok else c_red("⚠ 执行失败:")
            print(f"\n{icon} {dev_cmd}")
            dev_out = data.get("dev_output") or ""
            if dev_out:
                print(c_dim(dev_out[:2000]))
        # 分析代码：显示分析结果
        dev_analysis = data.get("dev_analysis") or ""
        if dev_analysis:
            print(f"\n{c_cyan('[分析]')}")
            print(dev_analysis[:4000])

        modified_zip = data.get("dev_modified_zip")
        if not modified_zip and not dev_cmd and not dev_analysis:
            print(f"{c_red('[错误] 后端未返回文件改动/命令执行/分析结果')}", file=sys.stderr)
            continue

        # ---- 第 3 步：分级审批（全部 / 逐文件 / 放弃）；纯命令/分析场景（无文件改动）跳过审批 ----
        dev_files = data.get("dev_files") or []
        only: set[str] | None = None
        if not dev_files and (dev_cmd or dev_analysis):
            pass  # 操作型/分析型需求：无文件可应用
        elif args.yes:
            ans = "a"
        else:
            try:
                ans = input(f"\n{c_yellow('[询问]')} 应用这些改动？全部 (a) / 逐文件 (f) / 放弃 (n): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n已放弃应用，改动未生效。")
                continue
        if ans in ("f", "file", "逐文件"):
            only = set()
            for f in dev_files:
                try:
                    pick = input(f"  应用 {f['path']}？(y/N): ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    pick = ""
                if pick in ("y", "yes"):
                    only.add(f["path"])
            if not only:
                print("未选择任何文件，放弃应用。")
                continue
        elif ans not in ("a", "all", "y", "yes", ""):
            print("已放弃应用，改动未生效。diff 已展示供参考。")
            continue

        rollback_sha = git_head(root) if git_repo else ""  # 应用前的提交点
        applied = []
        if dev_files:
            applied = apply_changes(root, modified_zip, only)
            print(f"{c_green('[完成]')} 已应用 {len(applied)} 个文件到 {root}")
        else:
            print(f"{c_green('[完成]')} 操作/分析型需求：无文件改动" + ("，命令已执行" if dev_cmd else ""))

        if rollback_sha:
            print(f"  {c_dim('[回滚点] git reset --hard ')}{rollback_sha}")

        if git_repo and not args.no_commit:
            if git_commit(root, f"AI: {requirement[:60]}"):
                print(f"  {c_green('[git]')} 已自动 commit")

        # 记录本轮到会话
        session.setdefault("rounds", []).append({
            "requirement": requirement,
            "plan": plan_text,
            "summary": data.get("dev_summary") or "",
            "files": [f.get("path") for f in dev_files],
            "applied": applied,
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        save_session(root, session)

        # 重新收集项目（把刚才的改动纳入下一轮）
        try:
            zdata = collect()
        except RuntimeError as e:
            print(f"{c_red('[错误]')} {e}", file=sys.stderr)
            break


if __name__ == "__main__":
    main()
