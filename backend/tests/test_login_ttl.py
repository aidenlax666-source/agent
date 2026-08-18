# -*- coding: utf-8 -*-
"""登录态短时存储测试：保存带时间戳 / 过期判定 / 过期清理。"""
import json
import os
import tempfile
import time

from app.api.auth_sessions import (_save_state, _state_valid_at, _cleanup_expired,
                                   LOGIN_TTL_SECONDS)


class FakeCtx:
    """模拟 Playwright context 的 storage_state()。"""
    def __init__(self, cookies):
        self._cookies = cookies

    def storage_state(self):
        return {"cookies": self._cookies, "origins": []}


def test_save_state_has_timestamp():
    """保存的登录态文件必须带 _saved_at 时间戳。"""
    tmp = tempfile.mkdtemp()
    ctx = FakeCtx([{"name": "sid", "value": "abc", "domain": "example.com"}])
    _save_state(ctx, "example.com", tmp)
    rec = json.loads(open(os.path.join(tmp, "example.com.json"), encoding="utf-8").read())
    assert "_saved_at" in rec
    assert abs(time.time() - rec["_saved_at"]) < 5  # 时间戳接近现在
    assert rec["state"]["cookies"][0]["value"] == "abc"


def test_state_valid_within_ttl():
    """有效期内判定为可用。"""
    tmp = tempfile.mkdtemp()
    ctx = FakeCtx([{"name": "sid", "value": "x", "domain": "a.com"}])
    _save_state(ctx, "a.com", tmp)
    assert _state_valid_at(tmp, "a.com", now=time.time()) is True


def test_state_invalid_after_ttl():
    """超过 TTL 判定为过期。"""
    tmp = tempfile.mkdtemp()
    ctx = FakeCtx([{"name": "sid", "value": "x", "domain": "a.com"}])
    _save_state(ctx, "a.com", tmp)
    later = time.time() + LOGIN_TTL_SECONDS + 10
    assert _state_valid_at(tmp, "a.com", now=later) is False


def test_cleanup_removes_expired_only():
    """清理只删过期文件，保留有效文件。"""
    import app.api.auth_sessions as mod
    from pathlib import Path as _Path

    parent = tempfile.mkdtemp()
    user_dir = _Path(parent) / "someuser"
    user_dir.mkdir()
    # 有效文件（刚保存）
    ctx = FakeCtx([{"name": "sid", "value": "fresh", "domain": "fresh.com"}])
    _save_state(ctx, "fresh.com", str(user_dir))
    # 过期文件（伪造旧时间戳）
    old = {"_saved_at": time.time() - LOGIN_TTL_SECONDS - 100, "state": {"cookies": [{"name": "s", "value": "old", "domain": "old.com"}], "origins": []}}
    json.dump(old, open(os.path.join(user_dir, "old.com.json"), "w", encoding="utf-8"))

    # 临时替换 PROFILE_DIR 指向测试父目录
    original = mod.PROFILE_DIR
    mod.PROFILE_DIR = _Path(parent)
    try:
        _cleanup_expired("someuser")
    finally:
        mod.PROFILE_DIR = original

    files = os.listdir(user_dir)
    assert "fresh.com.json" in files      # 有效保留
    assert "old.com.json" not in files    # 过期删除
