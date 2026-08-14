from __future__ import annotations
"""Deterministic static auto-fix for common Playwright API misuse.

Runs BEFORE compile/security/static checks in docker_executor.py. Purely
mechanical, regex-based fixes for mistakes the LLM cheatsheet documents but
still occasionally makes. Never touches ambiguous or semantically-loaded code.
"""

import re
from dataclasses import dataclass, field


@dataclass
class AutoFixResult:
    code: str
    fixes_applied: list[str] = field(default_factory=list)


def apply_auto_fixes(script_code: str) -> AutoFixResult:
    """Apply deterministic regex-based fixes for common Playwright API misuse.

    Rules (conservative, only fix unambiguous mistakes from the cheatsheet):
    1. `.first()` -> `.first` (property, not callable)
    2. `.last()` -> `.last` (property, not callable)
    3. `X.locator(...)()`  -> `X.locator(...)` (Locator not callable, empty-arg only)
    4. `wait_until="networkidle"` -> `wait_until="domcontentloaded"`

    Returns the fixed code plus a list of human-readable fix descriptions.
    """
    fixes: list[str] = []
    code = script_code

    # Rule 1: .first() -> .first (property, not method)
    new_code, n = re.subn(r'\.first\(\s*\)', '.first', code)
    if n:
        fixes.append(f".first() -> .first  ({n} 处)")
        code = new_code

    # Rule 2: .last() -> .last (property, not method)
    new_code, n = re.subn(r'\.last\(\s*\)', '.last', code)
    if n:
        fixes.append(f".last() -> .last  ({n} 处)")
        code = new_code

    # Rule 3: X.locator(...)() -> X.locator(...) — calling Locator like a function
    # Only fix empty-arg trailing calls to avoid eating legitimate chained calls
    new_code, n = re.subn(
        r'(\.locator\([^()]*\))\(\s*\)',
        r'\1',
        code,
    )
    if n:
        fixes.append(f"Locator 对象被当函数调用 X(...)() -> X(...)  ({n} 处)")
        code = new_code

    # Rule 4: networkidle -> domcontentloaded (networkidle hangs on SPA sites)
    new_code, n = re.subn(
        r'wait_until\s*=\s*["\']networkidle["\']',
        'wait_until="domcontentloaded"',
        code,
    )
    if n:
        fixes.append(f'networkidle -> domcontentloaded  ({n} 处)')
        code = new_code

    return AutoFixResult(code=code, fixes_applied=fixes)
