from __future__ import annotations
"""执行前静态预检：检测明显会卡死/异常的代码，避免跑满长时间超时才失败。

与 security.py 不同，这里关注的是「合法但会卡住」的代码（死循环等），
而不是「危险」的代码。原则是宁可漏报、不可误报——只要循环体里有任何退出
语句就认为安全，不拦好脚本。
"""

import ast


def _has_exit(node: ast.AST) -> bool:
    """检查子树里是否存在任何退出语句（break / return / raise / exit 调用）。"""
    for child in ast.walk(node):
        if isinstance(child, (ast.Break, ast.Return, ast.Raise)):
            return True
        if isinstance(child, ast.Call):
            f = child.func
            if isinstance(f, ast.Attribute) and f.attr in ("exit", "_exit"):
                return True
            if isinstance(f, ast.Name) and f.id in ("exit", "quit"):
                return True
    return False


def scan_static_issues(script_code: str) -> list[str]:
    """检测明显会卡死的代码。返回问题描述列表（空 = 通过）。"""
    issues: list[str] = []
    try:
        tree = ast.parse(script_code)
    except SyntaxError:
        # 语法错误由 execute_in_sandbox 的 compile 检查单独处理
        return []

    for node in ast.walk(tree):
        if isinstance(node, ast.While):
            cond = node.test
            is_always_true = isinstance(cond, ast.Constant) and bool(cond.value) is True
            # while True / while 1 且循环体里没有任何退出语句 → 死循环
            if is_always_true and not _has_exit(node):
                issues.append("检测到 while True 死循环（循环体内没有 break/return/raise）")

    # 去重、保序
    seen: set[str] = set()
    unique: list[str] = []
    for i in issues:
        if i not in seen:
            seen.add(i)
            unique.append(i)
    return unique
