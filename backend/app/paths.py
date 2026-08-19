# -*- coding: utf-8 -*-
from __future__ import annotations

"""路径解析：登录态目录 / 产物目录的统一入口。
云架构多实例下，这些目录不能再是"每实例本地磁盘"：
- 登录态（browser_profile/）：用户在实例 A 登录，任务可能被实例 B 消费——
  若各实例本地一份，B 读不到 A 存的登录态 → 抓取任务要求登录时必失败。
- 产物（web/）：产物写在 B 的磁盘，用户从 A 下载不到。

解决方案：config 新增 `sandbox_profile_root` / `asset_web_root`，
多实例部署时指向**共享卷**（NFS/EFS 挂载点或对象存储同步目录）。
留空则保持现状（各实例本地目录），单机行为完全不变。
"""

import os

from app.config import get_settings

_APP_DIR = os.path.normpath(os.path.dirname(os.path.abspath(__file__)))  # backend/app
_BACKEND_DIR = os.path.normpath(os.path.join(_APP_DIR, ".."))  # backend/
_REPO_DIR = os.path.normpath(os.path.join(_BACKEND_DIR, ".."))  # 仓库根/


def profile_root() -> str:
    """登录态根目录（browser_profile/ 的上级）。"""
    root = get_settings().sandbox_profile_root.strip()
    if root:
        return os.path.normpath(root)
    return os.path.join(_BACKEND_DIR, "browser_profile")


def user_profile_dir(user_id: str | int | None) -> str:
    """某用户（含匿名会话）的登录态目录；无 user_id 回退全局目录。"""
    if user_id is None or str(user_id) == "":
        return profile_root()
    return os.path.join(profile_root(), str(user_id))


def web_root() -> str:
    """产物根目录（web/）。"""
    root = get_settings().asset_web_root.strip()
    if root:
        return os.path.normpath(root)
    return os.path.join(_REPO_DIR, "web")


def sandbox_tmp_root() -> str:
    """沙箱临时文件根目录（脚本/输出中转）。

    临时文件不需要跨实例共享（产物最终落到 web_root），保持本地即可——
    共享卷 IO 慢且会放大临时垃圾，多实例各自一份反而更安全。
    """
    return os.path.join(_BACKEND_DIR, "tmp")
