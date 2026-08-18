# -*- coding: utf-8 -*-
"""共享 fixtures：隔离的临时工作区 + backend 模块路径。"""
import os
import sys
import tempfile

import pytest

# 确保能 import app.*
_BACKEND = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def workspace():
    """创建一个带测试文件的临时工作区，测试后自动清理。"""
    tmp = tempfile.mkdtemp(prefix="test_ws_")
    yield tmp
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture
def write_file():
    """在指定目录写文件的辅助工厂。"""
    def _write(directory: str, rel: str, content: str) -> str:
        p = os.path.join(directory, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p
    return _write
