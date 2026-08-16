#!/usr/bin/env python
"""AI 自动化 Agent 本地 CLI —— 像 Claude Code 一样持续对话改你的项目。

用法：
    python agent-cli.py                        # 在当前目录进入交互会话
    python agent-cli.py C:\path\to\project     # 在指定项目进入交互会话
    python agent-cli.py --api http://localhost:8000   # 指定后端地址
    python agent-cli.py --yes                  # 自动确认模式（每轮直接改码并应用）

进入会话后持续交互（每轮循环，可连续提多个需求）：
    你> 输入需求
    AI 出修改方案（不改代码）→ Enter 确认 / 输入意见重新规划 / q 放弃本轮
    AI 按方案改码 → 展示 diff → y 应用 / n 跳过
    改完后回到 你> 继续下一个需求；输入 exit 或 q 退出会话
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import zipfile
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


def apply_changes(root: str, modified_zip_b64: str) -> list[str]:
    """把后端返回的修改后文件 zip 应用回本地项目，返回应用的文件列表。"""
    data = base64.b64decode(modified_zip_b64)
    applied = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            target = os.path.normpath(os.path.join(root, member.filename))
            if not target.startswith(os.path.abspath(root) + os.sep):
                continue  # 防穿越
            os.makedirs(os.path.dirname(target), exist_ok=True)
            Path(target).write_bytes(zf.read(member))
            applied.append(member.filename)
    return applied


HELP_TEXT = """支持指令:
  exit / quit / q   退出会话
  help / h          显示本帮助
其余输入都会被当作改码需求提交给 AI（先出方案，确认后改码）。
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 自动化 Agent 本地 CLI：像 Claude Code 一样持续对话改你的项目")
    ap.add_argument("project", nargs="?", default=".", help="项目目录（默认当前目录）")
    ap.add_argument("--api", default="http://localhost:8000", help="后端地址")
    ap.add_argument("--commit", action="store_true", help="每轮应用改动后自动 git commit")
    ap.add_argument("--yes", "-y", action="store_true", help="自动确认方案并应用改动（跳过交互）")
    args = ap.parse_args()

    root = os.path.abspath(args.project)
    if not os.path.isdir(root):
        print(f"项目目录不存在: {root}", file=sys.stderr)
        sys.exit(1)

    print("=" * 60)
    print(" AI 通用 Agent · 交互改码会话（像 Claude Code）")
    print(f" 项目目录: {root}")
    print(" 输入需求开始改码；输入 exit 或 q 退出；help 查看指令")
    print("=" * 60)

    def collect() -> bytes:
        """重新收集项目文件并打包 zip（包含之前已应用的改动）。"""
        files = collect_files(root)
        if not files:
            raise RuntimeError("未收集到任何代码文件（可能目录为空或全被排除）")
        print(f"[收集] 共 {len(files)} 个文件")
        return build_zip(files)

    try:
        zdata = collect()
    except RuntimeError as e:
        print(f"[错误] {e}", file=sys.stderr)
        sys.exit(1)

    while True:
        print()
        try:
            requirement = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break
        if not requirement:
            continue
        low = requirement.lower()
        if low in ("exit", "quit", "q"):
            print("再见！")
            break
        if low in ("help", "h"):
            print(HELP_TEXT)
            continue

        # ---- 本轮第 1 步：AI 出修改方案（不改代码） ----
        print("[方案] AI 分析代码并生成修改方案（DeepSeek）...")
        feedback = ""
        plan_text = ""
        while True:
            try:
                data = post_dev(args.api, "/plan", requirement, zdata, feedback=feedback)
            except Exception as e:
                print(f"[错误] 方案生成失败: {e}", file=sys.stderr)
                break

            plan_text = (data.get("plan") or "").strip()
            plan_files = data.get("files") or []
            questions = data.get("questions") or []

            print("\n" + "=" * 60)
            print("[方案] AI 修改方案:")
            print("=" * 60)
            print(plan_text or "(AI 未给出方案文本)")

            if plan_files:
                print("\n[文件] 预计改动文件:")
                for f in plan_files:
                    if isinstance(f, dict):
                        print(f"  - {f.get('path', f)}")
                    else:
                        print(f"  - {f}")
            if questions:
                print("\n[问题] AI 需要你确认:")
                for q in questions:
                    print(f"  ? {q}")

            if args.yes:
                break

            ans = input(
                "\n[询问] Enter 确认 / 输入你的意见让 AI 调整方案 / 输入 q 放弃本轮: "
            ).strip()
            if ans.lower() in ("q", "quit", "exit"):
                print("已放弃本轮，未做任何改动。")
                plan_text = ""
                break
            if ans:
                feedback = ans
                print(f"[方案] 带着你的意见重新规划（{len(feedback)} 字）...")
                continue
            break

        if not plan_text:
            continue  # 放弃本轮，回到主循环

        # ---- 本轮第 2 步：按确认的方案落地改动 ----
        print("\n[改码] 按已确认方案执行改动（DeepSeek）...")
        try:
            data = post_dev(args.api, "/apply", requirement, zdata, plan=plan_text, feedback=feedback)
        except Exception as e:
            print(f"[错误] 改码失败: {e}", file=sys.stderr)
            continue

        print("\n" + "=" * 60)
        print("[AI] AI 改动说明:", data.get("dev_summary") or "(无)")
        print("=" * 60)
        for f in data.get("dev_files") or []:
            icon = "[新]" if f["status"] == "新增" else "[改]"
            print(f"  {icon} {f['path']}  ({f['status']}, {f['size']} 字符)")
        diff = data.get("dev_diff") or ""
        if diff:
            print("\n[Diff] Diff 预览（前 3000 字符）:")
            print(diff[:3000])
            if len(diff) > 3000:
                print(f"  ...（共 {len(diff)} 字符）")

        modified_zip = data.get("dev_modified_zip")
        if not modified_zip:
            print("[错误] 后端未返回修改后的文件", file=sys.stderr)
            continue

        if args.yes:
            ans = "y"
        else:
            ans = input("\n[询问] 应用这些改动到本地项目？(y/N): ").strip().lower()
        if ans not in ("y", "yes"):
            print("已放弃应用，改动未生效。diff 已展示供参考。")
            continue

        applied = apply_changes(root, modified_zip)
        print(f"[完成] 已应用 {len(applied)} 个文件到 {root}")

        if args.commit:
            try:
                import subprocess
                subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
                subprocess.run(["git", "commit", "-m", f"AI: {requirement[:60]}"],
                               cwd=root, check=True, capture_output=True)
                print("[完成] git commit 完成")
            except Exception as e:
                print(f"[警告] git commit 失败（手动提交即可）: {str(e)[:120]}")

        # 重新收集项目（把刚才的改动纳入下一轮）
        try:
            zdata = collect()
        except RuntimeError as e:
            print(f"[错误] {e}", file=sys.stderr)
            break


if __name__ == "__main__":
    main()
