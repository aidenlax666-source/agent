from __future__ import annotations
"""Execute Python scripts in isolated Docker containers, with a hardened
subprocess fallback for hosts where Docker is unavailable (e.g. Windows dev)."""

import asyncio
import tempfile
import os
import time
import shutil
import signal
import uuid
from dataclasses import dataclass, field

from app.config import get_settings
from app.sandbox.security import scan_dangerous_code
from app.sandbox.static_check import scan_static_issues
from app.sandbox.auto_fix import apply_auto_fixes

settings = get_settings()

# 全局并发信号量：限制同时执行的脚本数，超出排队（防止开太多浏览器打爆资源）
_sandbox_semaphore = asyncio.Semaphore(settings.sandbox_max_concurrency)

# 沙箱临时文件目录：放到项目盘（backend/tmp），避免写满 C 盘系统 Temp
_SANDBOX_TMP = os.path.join(os.path.dirname(__file__), "..", "..", "tmp")
os.makedirs(_SANDBOX_TMP, exist_ok=True)

# Output extensions we care about when locating the script's result file.
_OUTPUT_EXTS = (".xlsx", ".xls", ".docx", ".pptx", ".csv", ".txt", ".json", ".html", ".png", ".pdf")
_OUTPUT_NAMES = ("output.xlsx", "output.xls", "output.docx", "output.pptx", "output.csv")

# Environment variables that must never be passed to an untrusted subprocess.
_SENSITIVE_ENV_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL", "DATABASE_URL", "REDIS_URL", "JWT")


@dataclass
class ScriptResult:
    success: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    execution_time: float = 0.0
    output_file_path: str | None = None
    auto_fixes_applied: list[str] = field(default_factory=list)


def _sanitized_env() -> dict[str, str]:
    """Return a copy of os.environ with secrets stripped, for subprocess fallback."""
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(hint in upper for hint in _SENSITIVE_ENV_HINTS):
            continue
        env[key] = value
    # Keep the knobs the generated scripts rely on for browser automation.
    env["PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"] = \
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"
    env["PLAYWRIGHT_BROWSERS_PATH"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _find_output_file(workspace: str) -> str | None:
    """Locate the script's output file inside a workspace directory.

    Prefers well-known output.* names, then the newest file with an output
    extension. Returns the absolute path or None.
    """
    if not os.path.isdir(workspace):
        return None

    candidates: list[str] = []
    for name in os.listdir(workspace):
        full = os.path.join(workspace, name)
        if not os.path.isfile(full):
            continue
        lower = name.lower()
        if lower in _OUTPUT_NAMES:
            candidates.append((3, full))
        elif lower.endswith(_OUTPUT_EXTS):
            candidates.append((1, full))

    if not candidates:
        return None

    # Prefer output.* names (priority 3), break ties by newest mtime.
    best = max(candidates, key=lambda c: (c[0], os.path.getmtime(c[1])))
    return best[1]


def _finalize_output(workspace: str) -> str | None:
    """Move the script's output file out of `workspace` to a stable temp path,
    then remove the (now-empty) workspace. Returns the stable path or None."""
    src = _find_output_file(workspace)
    if not src:
        shutil.rmtree(workspace, ignore_errors=True)
        return None

    ext = os.path.splitext(src)[1] or ".xlsx"
    stable = os.path.join(_SANDBOX_TMP, f"auto_output_{uuid.uuid4().hex}{ext}")
    try:
        shutil.move(src, stable)
        shutil.rmtree(workspace, ignore_errors=True)
        return stable
    except OSError:
        # Move failed — leave the output in place so the caller can still read it.
        return src


def _kill_process_tree(proc) -> None:
    """Kill a subprocess and all its descendants (Playwright browsers, etc.)."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess = __import__("subprocess")
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def execute_in_sandbox(
    script_code: str,
    timeout: int | None = None,
    preview_mode: bool = True,
) -> ScriptResult:
    """Execute a Python script in an isolated environment, with global concurrency cap."""
    async with _sandbox_semaphore:
        return await _execute_in_sandbox_impl(script_code, timeout, preview_mode)


async def _execute_in_sandbox_impl(
    script_code: str,
    timeout: int | None = None,
    preview_mode: bool = True,
) -> ScriptResult:
    """Execute a Python script in an isolated environment.

    Args:
        script_code: The Python code to execute
        timeout: Maximum execution time in seconds
        preview_mode: If True, runs with limited resources

    Returns:
        ScriptResult with execution output
    """
    timeout = timeout or settings.sandbox_timeout
    # 总超时夹紧到上限，防止 LLM 估算出荒谬值导致无限等待
    timeout = min(timeout, settings.sandbox_max_timeout)
    inactivity_timeout = settings.sandbox_inactivity_timeout
    start_time = time.time()

    # Syntax check before execution (catch f-string backslash errors, etc.)
    try:
        compile(script_code, "<script>", "exec")
    except SyntaxError as e:
        return ScriptResult(
            success=False,
            stdout="",
            stderr=f"SyntaxError: {e.msg} (line {e.lineno}, col {e.offset})",
            exit_code=-1,
        )

    # --- Deterministic auto-fix pass for known Playwright API misuse ---
    import logging as _logging
    auto_fix_result = apply_auto_fixes(script_code)
    script_code = auto_fix_result.code
    if auto_fix_result.fixes_applied:
        _logging.getLogger("app.sandbox.docker_executor").info(
            "Auto-fix applied: %s", "; ".join(auto_fix_result.fixes_applied))
    # Re-compile after auto-fix (should never fail since fixes are syntax-preserving,
    # but guard anyway — never trust regex rewrites blindly)
    try:
        compile(script_code, "<script>", "exec")
    except SyntaxError as e:
        return ScriptResult(
            success=False, stdout="",
            stderr=f"SyntaxError after auto-fix: {e.msg} (line {e.lineno})",
            exit_code=-1,
        )
    # --- END auto-fix ---

    # Security scan: reject hard-blocked patterns before running untrusted code.
    violations = scan_dangerous_code(
        script_code,
        block_subprocess=not settings.sandbox_allow_subprocess,
    )
    if violations:
        return ScriptResult(
            success=False,
            stdout="",
            stderr="Sandbox blocked: " + "; ".join(violations),
            exit_code=-1,
        )

    # 执行前预检：检测明显死循环等会卡住的代码（宁可漏报，不可误报）
    static_issues = scan_static_issues(script_code)
    if static_issues:
        return ScriptResult(
            success=False,
            stdout="",
            stderr="脚本预检未通过: " + "; ".join(static_issues),
            exit_code=-1,
        )

    # Inject channel="msedge" only if script doesn't already specify a channel
    if 'channel=' not in script_code:
        # Replace chromium.launch( with chromium.launch(channel="msedge",
        # Handles single-line: p.chromium.launch(headless=True)
        # Handles multi-line: p.chromium.launch(\n    headless=True\n)
        import re as _re
        script_code = _re.sub(
            r'\.chromium\.launch\(',
            '.chromium.launch(channel="msedge", ',
            script_code,
        )

    # 有头模式：headless=True → headless=False（弹真实浏览器窗口，反爬通过率高）
    if settings.sandbox_headful:
        script_code = script_code.replace("headless=True", "headless=False")

    # Inject auth state loading - merge ALL saved per-domain login states
    auth_dir = os.path.join(os.path.dirname(__file__), "..", "..", "browser_profile")
    auth_dir = os.path.normpath(auth_dir)
    if os.path.exists(auth_dir):
        auth_injection = (
            "\n# === AUTH STATE INJECTION (merge all saved domains) ===\n"
            "import json as _json, os as _os, glob as _glob\n"
            f"_AUTH_DIR = r'{auth_dir}'\n"
            "def _load_auth():\n"
            "    try:\n"
            "        merged = {'cookies': [], 'origins': []}\n"
            "        for _f in _glob.glob(_os.path.join(_AUTH_DIR, '*.json')):\n"
            "            try:\n"
            "                with open(_f, encoding='utf-8') as _fp:\n"
            "                    _s = _json.load(_fp)\n"
            "                merged['cookies'].extend(_s.get('cookies', []))\n"
            "                merged['origins'].extend(_s.get('origins', []))\n"
            "            except: pass\n"
            "        return merged if merged['cookies'] else None\n"
            "    except: pass\n"
            "    return None\n"
            "_AUTH = _load_auth()\n"
            "# === END AUTH ===\n"
        )
        # Insert auth loader after the TOP-LEVEL imports (not function-internal imports)
        lines = script_code.split("\n")
        insert_idx = 0
        for i, line in enumerate(lines):
            # 只匹配顶格（无前导空格）的 import/from
            if line.startswith("import ") or line.startswith("from "):
                insert_idx = i + 1
        lines.insert(insert_idx, auth_injection)
        script_code = "\n".join(lines)

        # Inject auth into browser.new_context() calls (add storage_state)
        import re as _re2
        script_code = _re2.sub(
            r'browser\.new_context\(',
            'browser.new_context(storage_state=_AUTH, ',
            script_code,
        )
        # Also handle ctx.new_page() where ctx = browser.new_context()
        script_code = script_code.replace(
            "page = browser.new_page()",
            "page = browser.new_context(storage_state=_AUTH).new_page()"
        )

    # Write script to a temporary file
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".py",
        prefix="script_",
        delete=False,
        dir=_SANDBOX_TMP,
        encoding="utf-8",
    ) as f:
        f.write(script_code)
        script_path = f.name

    try:
        result = await _run_container(script_path, timeout, inactivity_timeout, preview_mode)
        result.execution_time = time.time() - start_time
        result.auto_fixes_applied = auto_fix_result.fixes_applied
        return result
    finally:
        # Cleanup temp file
        try:
            os.unlink(script_path)
        except OSError:
            pass


async def _run_container(
    script_path: str,
    timeout: int,
    inactivity_timeout: int,
    preview_mode: bool,
) -> ScriptResult:
    """Run the script inside a Docker container, or fall back to subprocess."""
    script_name = os.path.basename(script_path)
    container_name = f"sandbox_{script_name.replace('.py', '')}"

    # Build docker run command
    mem_limit = "256m" if preview_mode else "512m"
    cpu_quota = 50000 if preview_mode else 100000  # 50% of one CPU vs full CPU

    try:
        import docker
        from docker.errors import ImageNotFound
        client = docker.from_env()
    except Exception:
        # Docker not available - fallback to subprocess
        return await _run_fallback(script_path, timeout, inactivity_timeout)

    # Create a writable host output dir that we can read back from.
    output_host_dir = tempfile.mkdtemp(prefix="sandbox_output_", dir=_SANDBOX_TMP)

    try:
        # Ensure base image is available
        try:
            client.images.get(settings.sandbox_image)
        except ImageNotFound:
            client.images.pull(settings.sandbox_image)

        # Create and run container.
        # /scripts is read-only (the script itself); /output is the writable
        # working directory where output.xlsx etc. are written.
        container = client.containers.run(
            image=settings.sandbox_image,
            command=["python", f"/scripts/{script_name}"],
            working_dir="/output",
            volumes={
                os.path.dirname(script_path): {
                    "bind": "/scripts",
                    "mode": "ro",
                },
                output_host_dir: {
                    "bind": "/output",
                    "mode": "rw",
                },
            },
            environment={
                "PYTHONUNBUFFERED": "1",
                "PREVIEW_MODE": "true" if preview_mode else "false",
            },
            mem_limit=mem_limit,
            cpu_quota=cpu_quota,
            network_mode="bridge",
            detach=True,
            remove=False,
            name=container_name,
        )

        try:
            # Wait for container with timeout
            exit_result = container.wait(timeout=timeout)

            # Get logs
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")

            exit_code = exit_result.get("StatusCode", -1)

            output_path = _finalize_output(output_host_dir)

            return ScriptResult(
                success=(exit_code == 0),
                stdout=stdout[:50000],
                stderr=stderr[:10000],
                exit_code=exit_code,
                output_file_path=output_path,
            )

        except Exception:
            # Timeout or other issues
            container.kill()
            stdout = container.logs(stdout=True, stderr=False).decode("utf-8", errors="replace")
            stderr = container.logs(stdout=False, stderr=True).decode("utf-8", errors="replace")
            shutil.rmtree(output_host_dir, ignore_errors=True)

            return ScriptResult(
                success=False,
                stdout=stdout[:50000],
                stderr=f"Execution timeout after {timeout}s\n{stderr[:10000]}",
                exit_code=-1,
            )

        finally:
            # Cleanup container (leave output_host_dir for caller to read).
            try:
                container.remove(force=True)
            except Exception:
                pass

    except Exception:
        # Fallback to subprocess
        shutil.rmtree(output_host_dir, ignore_errors=True)
        return await _run_fallback(script_path, timeout, inactivity_timeout)


async def _run_fallback(script_path: str, timeout: int, inactivity_timeout: int) -> ScriptResult:
    """Hardened subprocess execution when Docker is unavailable.

    Streams stdout/stderr and monitors for inactivity: if no new output appears
    for `inactivity_timeout` seconds, the script is considered stuck and killed
    (its whole process tree). A total `timeout` remains as the upper bound.
    """
    import subprocess
    import sys

    # Isolated workspace so the script's output.* files land here, not in the
    # backend working directory.
    workspace = tempfile.mkdtemp(prefix="auto_run_", dir=_SANDBOX_TMP)
    env = _sanitized_env()

    # Process-group flags so we can kill descendants (Playwright browsers).
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workspace,
            env=env,
            **kwargs,
        )

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        last_output = time.monotonic()
        start = time.monotonic()

        async def _drain(stream, buf):
            nonlocal last_output
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    buf.append(line.decode("utf-8", errors="replace"))
                    last_output = time.monotonic()
            except Exception:
                pass

        stdout_task = asyncio.create_task(_drain(proc.stdout, stdout_buf))
        stderr_task = asyncio.create_task(_drain(proc.stderr, stderr_buf))

        # 主循环：等进程结束 / 无输出超时（卡死）/ 总超时
        kill_reason: str | None = None
        while True:
            if stdout_task.done() and stderr_task.done():
                break
            if inactivity_timeout > 0 and time.monotonic() - last_output > inactivity_timeout:
                kill_reason = f"InactivityTimeout: 连续 {inactivity_timeout}s 无日志输出，判定卡死"
                break
            if time.monotonic() - start > timeout:
                kill_reason = f"TimeoutError: 脚本执行超过 {timeout}s"
                break
            await asyncio.sleep(0.5)

        if kill_reason is not None:
            _kill_process_tree(proc)

        # 收集剩余输出并回收进程
        try:
            await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=5)
        except Exception:
            pass
        try:
            await proc.wait()
        except Exception:
            pass

        stdout = "".join(stdout_buf)
        stderr = "".join(stderr_buf)

        if kill_reason is not None:
            shutil.rmtree(workspace, ignore_errors=True)
            return ScriptResult(
                success=False,
                stdout=stdout[:50000],
                stderr=f"{kill_reason}\n{stderr[:10000]}",
                exit_code=-1,
            )

        if proc.returncode == 0:
            output_path = _finalize_output(workspace)
        else:
            shutil.rmtree(workspace, ignore_errors=True)
            output_path = None

        return ScriptResult(
            success=(proc.returncode == 0),
            stdout=stdout[:50000],
            stderr=stderr[:10000],
            exit_code=proc.returncode or 0,
            output_file_path=output_path,
        )

    except Exception as e:
        return ScriptResult(
            success=False,
            stdout="",
            stderr=f"SubprocessError: {type(e).__name__}: {str(e)}",
            exit_code=-1,
        )
