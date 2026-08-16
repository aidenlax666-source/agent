from __future__ import annotations
"""Static security scan for generated scripts.

The sandbox falls back to running scripts directly on the host when Docker is
unavailable (common on Windows dev machines). This module provides a best-effort
defense-in-depth layer: it statically rejects the highest-risk constructs before
execution. It is NOT a substitute for OS-level isolation, but it blocks the most
obvious exfiltration / escalation / destructive primitives while preserving the
legitimate task types the product supports (file/office, API, browser automation).
"""

import ast
import ipaddress
import os
import re
import socket
import urllib.parse

# Names that must not be called as functions at all (dynamic code execution and
# introspection used to smuggle __builtins__ / attribute chains past the scanner).
_BLOCKED_CALL_NAMES = {
    "eval", "exec", "compile", "__import__",
    "globals", "locals", "vars", "getattr",
}

# __import__ 动态导入时视为危险的模块（其余模块的动态导入放行，避免误拦合法脚本）
_DANGEROUS_IMPORT_MODULES = {
    "os", "subprocess", "socket", "smtplib", "ftplib", "sys", "ctypes",
    "shutil", "pty", "winreg", "win32api", "win32process", "pickle", "marshal",
    "importlib",
}

# 内网/云元数据地址（静态扫描用，字符串常量里出现即拦）。
# 覆盖 http/ws/file 协议、IPv6 字面量、0.0.0.0、常见内网网段。
_LAN_URL_RE = re.compile(
    r"(?:https?|ws|wss|file)://(?:\[[0-9a-fA-F:]+\]|localhost|0\.0\.0\.0|"
    r"127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|"
    r"172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+)",
    re.IGNORECASE)

# 云厂商元数据/内网解析名（DNS 可能解析到内网 IP，先按名字拦）
_METADATA_HOST_SUFFIXES = (".internal", ".local", "metadata.google.internal",
                           "metadata.azure.internal", "metadata")
_METADATA_HOST_EXACT = {"metadata.google.internal", "metadata.azure.internal",
                        "metadata", "kubernetes.default.svc", "host.docker.internal",
                        "host.containers.internal"}

# Fully-qualified attribute calls that are always blocked.
_BLOCKED_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
    ("os", "fork"),
    ("os", "posix_spawn"),
    ("os", "startfile"),
    ("pickle", "load"),
    ("pickle", "loads"),
}

# Attribute prefixes that are always blocked (os.spawnl, os.execvp, ...).
_BLOCKED_ATTR_PREFIXES = [
    ("os", "spawn"),
    ("os", "execl"),
    ("os", "execv"),
    ("os", "execvp"),
    ("os", "execve"),
    # 任何 __builtins__.xxx / __builtins__['xxx'] 链都拦（eval/exec 走私通道）
    ("__builtins__", ""),
    # importlib.import_module 是 __import__ 字面量检查的绕过通道，全拦
    ("importlib", ""),
]

# Destructive filesystem ops blocked when their literal argument points outside
# the sandbox workspace (deleting the user's own files).
_DESTRUCTIVE_OPS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("shutil", "rmtree"),
}

# Modules whose import is blocked only in "strict" mode (SANDBOX_ALLOW_SUBPROCESS=false).
# subprocess is legitimately used by TASK C (run commands); socket/smtplib/ftplib are
# exfiltration vectors that no product feature needs.
_STRICT_BLOCKED_MODULES = {"subprocess", "socket", "smtplib", "ftplib", "importlib"}

# File-reading call names: block when their literal argument points at a sensitive file.
_FILE_READ_NAMES = {
    "open",
    "read", "read_text", "read_bytes", "load",
    "read_excel", "read_csv", "read_json", "read_html", "read_sql", "read_sql_query",
    "read_table", "read_pickle", "read_parquet", "read_fwf", "load_workbook",
}

# Sensitive path markers the generated script must never read (API keys, DB, login state).
_SENSITIVE_PATH_MARKERS = (
    ".env", ".db", ".sqlite", "automation.db", "browser_profile",
    "auth_sessions", "storage_state", "config.py", "database.py", ".pem", ".key",
)


def _walk_attr(node: ast.Attribute) -> str | None:
    """Resolve a dotted attribute access to its root module name.

    Returns the root name (e.g. 'os' for os.path.join, 'shutil' for shutil.rmtree)
    or None if the base is not a simple Name.
    """
    if isinstance(node.value, ast.Name):
        return node.value.id
    if isinstance(node.value, ast.Attribute):
        return _walk_attr(node.value)
    return None


def _root_name(node: ast.AST) -> str | None:
    """Root identifier of a Name/Attribute/Subscript expression (e.g. '__builtins__')."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _root_name(node.value)
    if isinstance(node, ast.Subscript):
        return _root_name(node.value)
    return None


def _is_literal_path_arg(node: ast.AST | None) -> str | None:
    """Return the string literal if `node` is a simple string constant, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_sensitive_path(s: str) -> bool:
    """True if `s` looks like a path to a sensitive file (API keys, DB, login state)."""
    low = s.lower()
    return any(m in low for m in _SENSITIVE_PATH_MARKERS)


def _is_escaping_path(s: str) -> bool:
    """True 如果路径是绝对路径、带盘符、或含 '..' 穿越段（相对 workspace 逃逸）。"""
    norm = os.path.normpath(s)
    if os.path.isabs(norm) or re.match(r"^[A-Za-z]:", s):
        return True
    parts = norm.replace("\\", "/").split("/")
    return ".." in parts


def _host_is_blocked(host: str) -> bool:
    """按主机名/IP 判定是否内网/回环/链路本地/元数据（DNS 解析后按 IP 判定）。"""
    host = (host or "").strip().strip("[]").lower()
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host in _METADATA_HOST_EXACT or any(host.endswith(s) for s in _METADATA_HOST_SUFFIXES):
        return True
    try:
        # 字面 IP（含十进制/十六进制/IPv6 变体）直接判定
        ip = ipaddress.ip_address(host)
        return _ip_is_blocked(ip)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return True  # 解析失败按不安全处理
    return any(_ip_is_blocked(ipaddress.ip_address(info[4][0])) for info in infos)


def _ip_is_blocked(ip: "ipaddress._BaseAddress") -> bool:
    return (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
            or ip.is_multicast or ip.is_reserved or ip.is_global is False)


def is_lan_url(url: str) -> bool:
    """True 如果 URL 指向内网/回环/链路本地/云元数据地址，或不是 http(s)（SSRF 防护）。

    域名会做 DNS 解析后按解析出的 IP 判定；IPv6/进制编码/简写 IP 均能识别；
    file:/data:/ws: 等非 http(s) scheme 一律视为不安全。
    """
    if not url:
        return False
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        return True
    if p.scheme not in ("http", "https"):
        return True
    return _host_is_blocked(p.hostname or "")


def validate_public_http_url(url: str) -> None:
    """强制校验服务端要真实访问的 URL：仅 http/https 且目标非内网/回环/元数据。

    Raises ValueError（含原因），供 page_capture/site_analyzer 等在 goto 前调用。
    """
    if not url or not isinstance(url, str):
        raise ValueError("URL 为空")
    try:
        p = urllib.parse.urlparse(url)
    except Exception:
        raise ValueError(f"URL 无法解析: {url[:120]}")
    if p.scheme not in ("http", "https"):
        raise ValueError(f"仅支持 http/https 协议: {url[:120]}")
    if _host_is_blocked(p.hostname or ""):
        raise ValueError(f"禁止访问内网/回环/元数据地址: {url[:120]}")


def scan_dangerous_code(script_code: str, block_subprocess: bool = False) -> list[str]:
    """Scan generated code for hard-blocked patterns.

    Args:
        script_code: The generated Python source.
        block_subprocess: When True, also reject imports of subprocess/socket/
            smtplib/ftplib. Mirrors the SANDBOX_ALLOW_SUBPROCESS setting.

    Returns a list of human-readable violation descriptions. Empty list == safe.
    """
    violations: list[str] = []

    try:
        tree = ast.parse(script_code)
    except SyntaxError:
        # Syntax errors are handled separately by the caller (compile check).
        return []

    for node in ast.walk(tree):
        # --- 内网/元数据地址访问拦截（SSRF 缓解） ---
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _LAN_URL_RE.search(node.value):
                violations.append("禁止访问内网地址（SSRF 防护）")

        # --- Strict-mode module import blocks ---
        if block_subprocess and isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif node.module:
                roots = [node.module.split(".")[0]]
            else:
                roots = []
            for root in roots:
                if root in _STRICT_BLOCKED_MODULES:
                    violations.append(f"严格模式下禁止导入: {root}")

        # --- __builtins__ 访问（下标/属性链）一律拦 ---
        if (isinstance(node, (ast.Subscript, ast.Attribute))
                and _root_name(node) == "__builtins__"):
            violations.append("禁止访问 __builtins__")
            continue

        if not isinstance(node, ast.Call):
            continue

        func = node.func

        # --- Dynamic code execution: eval/exec/compile/globals/locals/vars/getattr 全拦 ---
        if isinstance(func, ast.Name) and func.id in _BLOCKED_CALL_NAMES:
            if func.id == "__import__":
                # 合法动态导入（如 __import__("json")）放行；但：
                # 1) 模块名非字面量（变量/拼接，如 __import__("o"+"s")）→ 无法静态确认安全，一律拦
                # 2) 字面量指向危险模块（os/subprocess/socket 等）→ 拦
                arg = _is_literal_path_arg(node.args[0] if node.args else None)
                if arg is None:
                    violations.append("禁止动态导入（模块名非字面量）: __import__()")
                elif arg.split(".")[0] in _DANGEROUS_IMPORT_MODULES:
                    violations.append(f"禁止动态导入危险模块: __import__({arg!r})")
            else:
                violations.append(f"动态代码执行被禁止: {func.id}()")
        # 下标调用：__builtins__['ev'+'al'](...) 形态
        if isinstance(func, ast.Subscript) and _root_name(func) == "__builtins__":
            violations.append("禁止动态调用: __builtins__[...](...)")

        # --- 敏感文件读取拦截：open/read_* 读 .env/.db 等 ---
        call_name = func.id if isinstance(func, ast.Name) else (
            func.attr if isinstance(func, ast.Attribute) else None
        )
        if call_name in _FILE_READ_NAMES and node.args:
            arg = _is_literal_path_arg(node.args[0])
            if arg and _is_sensitive_path(arg):
                violations.append(f"禁止读取敏感文件: {call_name}({arg!r})")
        # Path("path").read_text() / .read_bytes() 形式
        if isinstance(func, ast.Attribute) and func.attr in {"read_text", "read_bytes", "write_text", "write_bytes"}:
            if isinstance(func.value, ast.Call) and func.value.args:
                p = _is_literal_path_arg(func.value.args[0])
                if p and _is_sensitive_path(p):
                    violations.append(f"禁止读取敏感文件: Path({p!r}).{func.attr}()")

        if not isinstance(func, ast.Attribute):
            continue

        root = _walk_attr(func)
        if root is None:
            continue

        # --- Always-blocked attribute calls ---
        if (root, func.attr) in _BLOCKED_ATTRS:
            violations.append(f"危险调用被禁止: {root}.{func.attr}()")
            continue
        for r, prefix in _BLOCKED_ATTR_PREFIXES:
            if root == r and func.attr.startswith(prefix):
                violations.append(f"危险调用被禁止: {root}.{func.attr}()")
                break

        # --- Destructive ops: block when the literal path escapes the workspace ---
        if (root, func.attr) in _DESTRUCTIVE_OPS:
            arg = _is_literal_path_arg(node.args[0] if node.args else None)
            if arg and _is_escaping_path(arg):
                violations.append(f"越权删除被禁止: {root}.{func.attr}({arg!r})")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique
