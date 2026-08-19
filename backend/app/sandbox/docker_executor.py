from __future__ import annotations
"""Execute Python scripts in isolated Docker containers, with a hardened
subprocess fallback for hosts where Docker is unavailable (e.g. Windows dev)."""

import asyncio
import re
import tempfile
import os
import time
import shutil
import signal
import threading
import uuid
from dataclasses import dataclass, field

from app.config import get_settings
from app.paths import profile_root, sandbox_tmp_root
from app.sandbox.security import scan_dangerous_code
from app.sandbox.static_check import scan_static_issues
from app.sandbox.auto_fix import apply_auto_fixes

settings = get_settings()

# 全局并发信号量：限制同时执行的脚本数，超出排队（防止开太多浏览器打爆资源）
# 惰性创建：asyncio.Semaphore 在 Python 3.9 会在无事件循环时绑定 get_event_loop()，
# 模块导入时若恰好无 loop（如测试中 asyncio.run 之后）会抛错，故延迟到首次使用
_sandbox_semaphore: asyncio.Semaphore | None = None
_sandbox_sem_lock = threading.Lock()


def _get_sandbox_semaphore() -> asyncio.Semaphore:
    global _sandbox_semaphore
    if _sandbox_semaphore is None:
        with _sandbox_sem_lock:
            if _sandbox_semaphore is None:
                _sandbox_semaphore = asyncio.Semaphore(get_settings().sandbox_max_concurrency)
    return _sandbox_semaphore

# 沙箱临时文件目录：放到项目盘（backend/tmp），避免写满 C 盘系统 Temp
_SANDBOX_TMP = sandbox_tmp_root()
os.makedirs(_SANDBOX_TMP, exist_ok=True)

# Output extensions we care about when locating the script's result file.
_OUTPUT_EXTS = (".xlsx", ".xls", ".docx", ".pptx", ".csv", ".txt", ".json", ".html", ".png", ".pdf",
                ".wav", ".mp3", ".mp4", ".dxf", ".srt", ".mkv", ".mov", ".avi", ".webm", ".m4a")
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
    """把脚本的全部输出文件搬出 workspace 到稳定目录（不再只留一个、删其余），
    返回主输出文件路径；随后清理 workspace 中残留的临时文件。"""
    src = _find_output_file(workspace)
    if not src:
        shutil.rmtree(workspace, ignore_errors=True)
        return None

    stable_dir = os.path.join(_SANDBOX_TMP, f"auto_output_{uuid.uuid4().hex}")
    os.makedirs(stable_dir, exist_ok=True)
    try:
        for name in os.listdir(workspace):
            full = os.path.join(workspace, name)
            if not os.path.isfile(full):
                continue
            lower = name.lower()
            if lower in _OUTPUT_NAMES or lower.endswith(_OUTPUT_EXTS):
                shutil.move(full, os.path.join(stable_dir, name))
        shutil.rmtree(workspace, ignore_errors=True)
        main = _find_output_file(stable_dir)
        return main or src
    except OSError:
        # Move failed — leave the outputs in place so the caller can still read them.
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
    profile_dir: str | None = None,
    task_id: str | None = None,
) -> ScriptResult:
    """Execute a Python script in an isolated environment, with global concurrency cap.

    profile_dir: 该任务所属用户的登录态目录（按账号隔离）；None 时回退全局目录。
    task_id: 任务 ID（云架构多实例用）：容器打标签 → 孤儿清理时定位归属；
             同时用于跨实例全局并发槽位（配置了 sandbox_global_concurrency 时生效）。
    """
    # 全局并发（跨实例）：配置了 sandbox_global_concurrency 且 Redis 可用时，
    # 先抢一个全局槽位再进本地信号量——总并发 = 配置值，而不是实例数×单实例并发。
    global_limit = get_settings().sandbox_global_concurrency
    slot: int | None = None
    if global_limit > 0:
        from app.services import distributed as _dist
        # 排队等待槽位：最多等 sandbox_max_timeout 的 1/3（长任务本就该尽快拿到资源），
        # 拿到后 TTL 与任务执行等长，期间不需要续期（单次执行超时 ≤ sandbox_max_timeout）
        wait_deadline = time.time() + max(60, min(timeout or settings.sandbox_timeout, settings.sandbox_max_timeout) / 3)
        while slot is None:
            slot = _dist.acquire_sandbox_slot(global_limit, ttl_seconds=settings.sandbox_max_timeout)
            if slot is not None:
                break
            if time.time() > wait_deadline:
                return ScriptResult(
                    success=False,
                    stdout="",
                    stderr=f"SandboxBusy: 全局沙箱并发已满（{global_limit} 个任务在执行），排队超时，请稍后重试",
                    exit_code=-1,
                )
            await asyncio.sleep(1)
    try:
        async with _get_sandbox_semaphore():
            return await _execute_in_sandbox_impl(script_code, timeout, preview_mode, profile_dir, task_id)
    finally:
        if slot is not None:
            from app.services import distributed as _dist
            _dist.release_sandbox_slot(slot)


async def _execute_in_sandbox_impl(
    script_code: str,
    timeout: int | None = None,
    preview_mode: bool = True,
    profile_dir: str | None = None,
    task_id: str | None = None,
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

    # Inject auth state loading - load THIS USER's saved per-domain login states
    # (按账号隔离：任务只加载所属用户的登录态；未指定时回退全局目录)
    auth_dir = profile_dir or profile_root()
    auth_dir = os.path.normpath(auth_dir)
    if os.path.exists(auth_dir):
        _ttl = getattr(settings, "sandbox_login_ttl", 7200)
        auth_injection = (
            "\n# === AUTH STATE INJECTION (merge valid, non-expired domains) ===\n"
            "import json as _json, os as _os, glob as _glob, time as _time\n"
            f"_AUTH_DIR = r'{auth_dir}'\n"
            f"_AUTH_TTL = {_ttl}\n"
            "def _load_auth():\n"
            "    try:\n"
            "        merged = {'cookies': [], 'origins': []}\n"
            "        _now = _time.time()\n"
            "        for _f in _glob.glob(_os.path.join(_AUTH_DIR, '*.json')):\n"
            "            try:\n"
            "                with open(_f, encoding='utf-8') as _fp:\n"
            "                    _rec = _json.load(_fp)\n"
            "                # 短时登录态：无 _saved_at（旧格式）或超 TTL 均视为过期跳过\n"
            "                if not isinstance(_rec, dict) or not _rec.get('_saved_at'):\n"
            "                    continue\n"
            "                if _now - _rec['_saved_at'] > _AUTH_TTL:\n"
            "                    continue\n"
            "                _s = _rec.get('state', {})\n"
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
        result = await _run_container(script_path, timeout, inactivity_timeout, preview_mode, auth_dir, task_id)
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
    auth_dir: str = "",
    task_id: str | None = None,
) -> ScriptResult:
    """Run the script inside a Docker container, or fall back to subprocess.

    auth_dir 通过显式参数传入（不能用模块级全局：并发任务会互相覆盖导致跨用户登录态串号）。
    task_id 用于容器打标签（孤儿清理定位归属）。
    """
    import logging as _logging
    _log = _logging.getLogger("app.sandbox.docker_executor")
    script_name = os.path.basename(script_path)
    # 容器名：带 task_id 前缀，崩溃残留时按标签即可定位到归属任务
    _tag = re.sub(r"[^A-Za-z0-9_.-]", "", (task_id or script_name.replace(".py", "")))[:60] or "anon"
    container_name = f"sandbox_{_tag}"

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

    container = None
    try:
        # Ensure base image is available（同步 docker SDK 调用放线程池，避免阻塞事件循环）
        try:
            await asyncio.to_thread(client.images.get, settings.sandbox_image)
        except ImageNotFound:
            await asyncio.to_thread(client.images.pull, settings.sandbox_image)

        # 登录态目录挂载进容器 /auth（fallback 路径直接用宿主路径，这里替换成容器内路径）
        volumes = {
            os.path.dirname(script_path): {"bind": "/scripts", "mode": "ro"},
            output_host_dir: {"bind": "/output", "mode": "rw"},
        }
        if auth_dir and os.path.isdir(auth_dir):
            volumes[auth_dir] = {"bind": "/auth", "mode": "ro"}
            _rewrite_script_auth_dir(script_path, "/auth")

        # Create and run container.
        # /scripts is read-only (the script itself); /output is the writable
        # working directory where output.xlsx etc. are written.
        from app.services import distributed as _dist
        labels = {
            "aiagent.sandbox": "1",
            "aiagent.worker": _dist.get_worker_id(),  # 谁启动的谁负责回收（孤儿清理）
        }
        if task_id:
            labels["aiagent.task"] = task_id  # 定位归属任务（租约过期即孤儿）
        container = await asyncio.to_thread(
            client.containers.run,
            image=settings.sandbox_image,
            command=["python", f"/scripts/{script_name}"],
            working_dir="/output",
            volumes=volumes,
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
            labels=labels,
        )

        try:
            # Wait for container with timeout
            exit_result = await asyncio.to_thread(container.wait, timeout=timeout)

            # Get logs
            stdout = (await asyncio.to_thread(container.logs, stdout=True, stderr=False)).decode("utf-8", errors="replace")
            stderr = (await asyncio.to_thread(container.logs, stdout=False, stderr=True)).decode("utf-8", errors="replace")

            exit_code = exit_result.get("StatusCode", -1)

            output_path = _finalize_output(output_host_dir)

            return ScriptResult(
                success=(exit_code == 0),
                stdout=stdout[:50000],
                stderr=stderr[:10000],
                exit_code=exit_code,
                output_file_path=output_path,
            )

        except Exception as e:
            # 容器已启动：超时/连接问题不再回退重跑（避免副作用重复执行）
            try:
                await asyncio.to_thread(container.kill)
            except Exception as ke:
                _log.warning("container kill failed: %s", ke)
            try:
                stdout = (await asyncio.to_thread(container.logs, stdout=True, stderr=False)).decode("utf-8", errors="replace")
                stderr = (await asyncio.to_thread(container.logs, stdout=False, stderr=True)).decode("utf-8", errors="replace")
            except Exception:
                stdout, stderr = "", ""
            shutil.rmtree(output_host_dir, ignore_errors=True)
            return ScriptResult(
                success=False,
                stdout=stdout[:50000],
                stderr=f"Execution failed/timeout after {timeout}s: {str(e)[:120]}\n{stderr[:10000]}",
                exit_code=-1,
            )

        finally:
            # Cleanup container (leave output_host_dir for caller to read).
            try:
                await asyncio.to_thread(container.remove, force=True)
            except Exception:
                pass

    except Exception as e:
        # 容器创建阶段失败（镜像/启动异常）才回退 subprocess；已启动的不再回退
        shutil.rmtree(output_host_dir, ignore_errors=True)
        _log.warning("docker create failed (%s), falling back to subprocess", str(e)[:120])
        return await _run_fallback(script_path, timeout, inactivity_timeout)


def _rewrite_script_auth_dir(script_path: str, container_path: str) -> None:
    """把脚本里注入的宿主登录目录路径改写为容器内路径（仅 Docker 模式用）。"""
    try:
        with open(script_path, encoding="utf-8") as f:
            content = f.read()
        import re as _re
        content = _re.sub(r"_AUTH_DIR = r'[^']*'", f"_AUTH_DIR = r'{container_path}'", content, count=1)
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception:
        pass


async def _run_fallback(script_path: str, timeout: int, inactivity_timeout: int) -> ScriptResult:
    """Hardened subprocess execution when Docker is unavailable.

    Streams stdout/stderr and monitors for inactivity: if no new output appears
    for `inactivity_timeout` seconds, the script is considered stuck and killed
    (its whole process tree). A total `timeout` remains as the upper bound.
    输出缓冲有字节预算（防海量输出撑爆内存）；任务取消时确保子进程被杀。
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

    proc = None
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
        _OUT_BUDGET = 100 * 1024  # 每流最多缓存 100KB，超出继续读但丢弃（防 OOM）
        last_output = time.monotonic()
        start = time.monotonic()

        async def _drain(stream, buf):
            nonlocal last_output
            size = 0
            try:
                while True:
                    line = await stream.readline()
                    if not line:
                        break
                    last_output = time.monotonic()
                    size += len(line)
                    if size <= _OUT_BUDGET:
                        buf.append(line.decode("utf-8", errors="replace"))
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
            await asyncio.to_thread(_kill_process_tree, proc)

        # 收集剩余输出并回收进程（wait 带超时，防永久挂起占住沙箱信号量）
        try:
            await asyncio.wait_for(asyncio.gather(stdout_task, stderr_task), timeout=5)
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except Exception:
            await asyncio.to_thread(_kill_process_tree, proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:
                pass

        stdout = "".join(stdout_buf)
        stderr = "".join(stderr_buf)
        rc = proc.returncode

        if kill_reason is not None:
            shutil.rmtree(workspace, ignore_errors=True)
            return ScriptResult(
                success=False,
                stdout=stdout[:50000],
                stderr=f"{kill_reason}\n{stderr[:10000]}",
                exit_code=-1,
            )

        if rc == 0:
            output_path = _finalize_output(workspace)
        else:
            shutil.rmtree(workspace, ignore_errors=True)
            output_path = None

        return ScriptResult(
            success=(rc == 0),
            stdout=stdout[:50000],
            stderr=stderr[:10000],
            exit_code=rc if rc is not None else -1,
            output_file_path=output_path,
        )

    except asyncio.CancelledError:
        # 任务被取消：必须杀掉子进程并清理，避免残留进程/文件
        if proc is not None:
            await asyncio.to_thread(_kill_process_tree, proc)
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    except Exception as e:
        if proc is not None:
            await asyncio.to_thread(_kill_process_tree, proc)
        shutil.rmtree(workspace, ignore_errors=True)
        return ScriptResult(
            success=False,
            stdout="",
            stderr=f"SubprocessError: {type(e).__name__}: {str(e)}",
            exit_code=-1,
        )


# ============================================================
# 孤儿沙箱清理（云架构多实例）
# ============================================================

def cleanup_orphan_containers(force_all: bool = False) -> int:
    """清理孤儿沙箱容器（worker 崩溃/重启后残留，占资源且可能继续跑副作用）。

    回收规则（宁可保守不可误杀）：
    - 本 worker 之前启动的容器（label aiagent.worker == 本实例）——重启后必是孤儿，直接回收；
    - 其他 worker 的容器：仅当带 aiagent.task 标签且该任务**执行租约已不存在**
      （= 任务已结束或 worker 已崩溃）才回收；
    - force_all=True（如手动运维）时无差别回收全部沙箱容器。

    返回清理数量。Docker 不可用/非容器执行时返回 0（无操作）。
    """
    import logging as _logging
    _log = _logging.getLogger("app.sandbox.docker_executor")
    try:
        import docker
        client = docker.from_env()
    except Exception:
        return 0  # 无 Docker（宿主回退模式），无容器可清理

    from app.services import distributed as _dist
    removed = 0
    try:
        containers = client.containers.list(all=True, filters={"label": "aiagent.sandbox=1"})
    except Exception as e:
        _log.warning("孤儿容器列表获取失败: %s", str(e)[:120])
        return 0

    for c in containers:
        try:
            labels = c.labels or {}
            mine = labels.get("aiagent.worker") == _dist.get_worker_id()
            task_id = labels.get("aiagent.task", "")
            lease_gone = bool(task_id) and not _dist.task_lease_alive(task_id)
            if force_all or mine or lease_gone:
                try:
                    c.remove(force=True)
                    removed += 1
                    _log.info("已清理孤儿沙箱容器 %s (worker=%s task=%s)",
                              c.name, labels.get("aiagent.worker", "?"), task_id or "-")
                except Exception as e2:
                    _log.warning("孤儿容器 %s 清理失败: %s", c.name, str(e2)[:100])
        except Exception:
            continue
    return removed
