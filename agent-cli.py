#!/usr/bin/env python
"""AI 自动化 Agent 本地 CLI —— 像 Claude Code 一样改你的项目（交互式：先方案，后落地）。

用法：
    python agent-cli.py "给项目加一个导出PDF的功能"          # 在当前目录的项目上改
    python agent-cli.py "加个XX接口" C:\path\to\project      # 指定项目目录
    python agent-cli.py --api http://localhost:8000 "需求"    # 指定后端地址

流程：打包项目(排除 node_modules/.git 等) → 调 /api/dev/plan 生成修改方案
      → 展示方案，Enter 确认 / 输入意见重新规划 / q 退出
      → 确认后调 /api/dev/apply 落地改动 → 展示 diff
      → 输入 y 应用到本地（+git commit 可选）/ n 放弃
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


def main() -> None:
    ap = argparse.ArgumentParser(description="AI 自动化 Agent 本地 CLI：像 Claude Code 一样改你的项目")
    ap.add_argument("requirement", help='需求，如"给项目加一个导出PDF的功能"')
    ap.add_argument("project", nargs="?", default=".", help="项目目录（默认当前目录）")
    ap.add_argument("--api", default="http://localhost:8000", help="后端地址")
    ap.add_argument("--commit", action="store_true", help="应用改动后自动 git commit")
    ap.add_argument("--yes", "-y", action="store_true", help="自动确认方案并应用改动（跳过交互）")
    args = ap.parse_args()

    if not args.requirement.strip():
        print("请提供需求描述", file=sys.stderr)
        sys.exit(1)

    root = os.path.abspath(args.project)
    if not os.path.isdir(root):
        print(f"项目目录不存在: {root}", file=sys.stderr)
        sys.exit(1)

    print(f"[收集] 收集项目: {root}")
    files = collect_files(root)
    if not files:
        print("未收集到任何代码文件（可能目录为空或全被排除）", file=sys.stderr)
        sys.exit(1)
    print(f"   共 {len(files)} 个文件")
    zdata = build_zip(files)

    # ---- 第一步：AI 出修改方案（不改代码） ----
    print("[方案] AI 分析代码并生成修改方案（DeepSeek）...")
    feedback = ""
    plan_text = ""
    while True:
        try:
            data = post_dev(args.api, "/plan", args.requirement, zdata, feedback=feedback)
        except Exception as e:
            print(f"[错误] 方案生成失败: {e}", file=sys.stderr)
            sys.exit(1)

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
            "\n[询问] 按 Enter 确认并开始改码 / 输入你的意见让 AI 调整方案 / 输入 q 退出: "
        ).strip()
        if ans.lower() in ("q", "quit", "exit"):
            print("已退出，未做任何改动。")
            sys.exit(0)
        if ans:
            feedback = ans
            print(f"[方案] 带着你的意见重新规划（{len(feedback)} 字）...")
            continue
        break

    # ---- 第二步：按确认的方案落地改动 ----
    print("\n[改码] 按已确认方案执行改动（DeepSeek）...")
    try:
        data = post_dev(args.api, "/apply", args.requirement, zdata, plan=plan_text, feedback=feedback)
    except Exception as e:
        print(f"[错误] 改码失败: {e}", file=sys.stderr)
        sys.exit(1)

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
        sys.exit(1)

    if args.yes:
        ans = "y"
    else:
        ans = input("\n[询问] 应用这些改动到本地项目？(y/N): ").strip().lower()
    if ans not in ("y", "yes"):
        print("已放弃应用，改动未生效。diff 已展示供参考。")
        sys.exit(0)

    applied = apply_changes(root, modified_zip)
    print(f"[完成] 已应用 {len(applied)} 个文件到 {root}")

    if args.commit:
        try:
            import subprocess
            subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", f"AI: {args.requirement[:60]}"],
                           cwd=root, check=True, capture_output=True)
            print("[完成] git commit 完成")
        except Exception as e:
            print(f"[警告] git commit 失败（手动提交即可）: {str(e)[:120]}")

    print("\n完成！改动已应用到你的项目。")


if __name__ == "__main__":
    main()
