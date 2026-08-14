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

# Names that must not be called as functions at all (dynamic code execution).
_BLOCKED_CALL_NAMES = {"eval", "exec", "compile", "__import__"}

# Fully-qualified attribute calls that are always blocked.
_BLOCKED_ATTRS = {
    ("os", "system"),
    ("os", "popen"),
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
]

# Destructive filesystem ops blocked only when their literal argument points
# outside the sandbox workspace (deleting the user's own files).
_DESTRUCTIVE_OPS = {
    ("os", "remove"),
    ("os", "unlink"),
    ("os", "rmdir"),
    ("os", "removedirs"),
    ("shutil", "rmtree"),
}

# Path hints that indicate a destructive op targets something outside the workspace.
_DANGEROUS_PATH_HINTS = (
    "C:", "/etc", "/usr", "/bin", "/var", "/home", "/root", "/tmp", "/",
    "\\Windows", "System32", "~",
)

# Modules whose import is blocked only in "strict" mode (SANDBOX_ALLOW_SUBPROCESS=false).
# subprocess is legitimately used by TASK C (run commands); socket/smtplib/ftplib are
# exfiltration vectors that no product feature needs.
_STRICT_BLOCKED_MODULES = {"subprocess", "socket", "smtplib", "ftplib"}

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


def _is_literal_path_arg(node: ast.AST | None) -> str | None:
    """Return the string literal if `node` is a simple string constant, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_sensitive_path(s: str) -> bool:
    """True if `s` looks like a path to a sensitive file (API keys, DB, login state)."""
    low = s.lower()
    return any(m in low for m in _SENSITIVE_PATH_MARKERS)


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

        if not isinstance(node, ast.Call):
            continue

        func = node.func

        # --- Dynamic code execution: eval/exec/compile/__import__ ---
        if isinstance(func, ast.Name) and func.id in _BLOCKED_CALL_NAMES:
            violations.append(f"动态代码执行被禁止: {func.id}()")

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

        # --- Destructive ops: block only literal paths that escape the workspace ---
        if (root, func.attr) in _DESTRUCTIVE_OPS:
            arg = _is_literal_path_arg(node.args[0] if node.args else None)
            if arg and any(hint in arg for hint in _DANGEROUS_PATH_HINTS):
                violations.append(f"越权删除被禁止: {root}.{func.attr}({arg!r})")

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for v in violations:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique
