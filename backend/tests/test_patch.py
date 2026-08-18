# -*- coding: utf-8 -*-
"""apply_patch 应用器单元测试（核心：只输出改动行的省 token 机制）。"""
import os

from app.services.mini_tasks import _dev_apply_patch, _dev_files_from_info


def test_patch_array_basic(workspace, write_file):
    """数组形式 patch：改已有文件的一行。"""
    write_file(workspace, "main.py", "print('hello')\nprint('old')\nprint('end')\n")
    patch = {"main.py": ["@@ -1,3 +1,4 @@", " print('hello')", "-print('old')", "+print('new')"]}
    fm, err = _dev_apply_patch(patch, workspace)
    assert not err, err
    assert fm["main.py"] == "print('hello')\nprint('new')\nprint('end')\n"


def test_patch_multiple_files(workspace, write_file):
    """一次 patch 多个文件。"""
    write_file(workspace, "a.py", "x=1\ny=2\n")
    write_file(workspace, "b.py", "1\n2\n")
    patch = {"a.py": ["@@ -1,2 +1,3 @@", " x=1", "+x=2", " y=2"],
             "b.py": ["@@ -1,2 +1,3 @@", " 1", "+1.5", " 2"]}
    fm, err = _dev_apply_patch(patch, workspace)
    assert not err, err
    assert fm["a.py"] == "x=1\nx=2\ny=2\n"
    assert fm["b.py"] == "1\n1.5\n2\n"


def test_patch_line_number_drift(workspace, write_file):
    """行号不准时用内容模糊匹配兜底（模型数错行）。"""
    write_file(workspace, "a.py", "x=1\ny=2\nz=3\nw=4\n")
    # 行号故意错（写 5 而非 2），内容匹配仍应生效
    patch = {"a.py": ["@@ -5,3 +5,3 @@", " x=1", "-y=2", "+y=22", " z=3"]}
    fm, err = _dev_apply_patch(patch, workspace)
    assert not err, err
    assert fm["a.py"] == "x=1\ny=22\nz=3\nw=4\n"


def test_patch_failure_returns_error(workspace, write_file):
    """patch 完全无法定位时报错（供上层回退 files）。"""
    write_file(workspace, "x.py", "aaa\nbbb\nccc\n")
    patch = {"x.py": ["@@ -1,3 +1,3 @@", " xxx", "-yyy", "+zzz"]}
    fm, err = _dev_apply_patch(patch, workspace)
    assert not fm
    assert len(err) == 1
    assert "找不到匹配内容" in err[0]


def test_files_from_info_merges_patch_and_files(workspace, write_file):
    """_dev_files_from_info：patch（改已有）+ files（新增）合并。"""
    write_file(workspace, "old.py", "line1\n")
    info = {"patch": {"old.py": ["@@ -1,1 +1,2 @@", " line1", "+line2"]},
            "files": {"new.py": "print(1)\n"}}
    fm, err = _dev_files_from_info(info, workspace)
    assert not err, err
    assert fm["old.py"] == "line1\nline2\n"
    assert fm["new.py"] == "print(1)\n"


def test_patch_chinese_content(workspace, write_file):
    """中文内容 patch。"""
    write_file(workspace, "zh.py", "你好世界\n这是第二行\n第三行\n")
    patch = {"zh.py": ["@@ -1,3 +1,4 @@", " 你好世界", "-这是第二行", "+这是修改行", " 第三行"]}
    fm, err = _dev_apply_patch(patch, workspace)
    assert not err, err
    assert fm["zh.py"] == "你好世界\n这是修改行\n第三行\n"


def test_patch_illegal_path_rejected(workspace, write_file):
    """非法路径（穿越）被拒绝。"""
    patch = {"../evil.py": ["@@ -1,1 +1,1 @@", "+x"]}
    fm, err = _dev_apply_patch(patch, workspace)
    assert not fm
    assert any("非法路径" in e for e in err)
