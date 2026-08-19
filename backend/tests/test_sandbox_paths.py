# -*- coding: utf-8 -*-
"""路径解析测试：默认本地目录（单机行为不变）；配置共享目录后指向配置值。"""
import os
from unittest.mock import patch

from app import paths
from app.config import get_settings


def test_default_profile_root():
    """默认：登录态目录 = backend/browser_profile（现状不变）。"""
    root = paths.profile_root()
    assert root.endswith(os.path.join("backend", "browser_profile"))
    assert os.path.isdir(os.path.dirname(root))  # backend/ 存在


def test_default_web_root():
    """默认：产物目录 = 仓库根 web/（现状不变）。"""
    root = paths.web_root()
    assert root.endswith(os.path.join("ai-automation-generator", "web")) or root.endswith(os.path.join("web"))


def test_default_tmp_root():
    """默认：沙箱临时目录 = backend/tmp（本地，不需要共享）。"""
    root = paths.sandbox_tmp_root()
    assert root.endswith(os.path.join("backend", "tmp"))


def test_configured_shared_roots():
    """云架构：配置 SANDBOX_PROFILE_ROOT / ASSET_WEB_ROOT 后指向共享卷。"""
    patcher = patch.object(get_settings(), "sandbox_profile_root", "/mnt/shared/profiles")
    patcher2 = patch.object(get_settings(), "asset_web_root", "/mnt/shared/web")
    patcher.start()
    patcher2.start()
    try:
        assert paths.profile_root() == os.path.normpath("/mnt/shared/profiles")
        assert paths.web_root() == os.path.normpath("/mnt/shared/web")
    finally:
        patcher.stop()
        patcher2.stop()


def test_user_profile_dir():
    """按用户隔离：user_id 存在时返回其子目录；空 user_id 回退全局。"""
    patcher = patch.object(get_settings(), "sandbox_profile_root", "/mnt/shared/profiles")
    patcher.start()
    try:
        assert paths.user_profile_dir("u-42") == os.path.normpath("/mnt/shared/profiles/u-42")
        assert paths.user_profile_dir(None) == os.path.normpath("/mnt/shared/profiles")
        assert paths.user_profile_dir("") == os.path.normpath("/mnt/shared/profiles")
        assert paths.user_profile_dir(42) == os.path.normpath("/mnt/shared/profiles/42")
    finally:
        patcher.stop()
