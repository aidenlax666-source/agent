# -*- coding: utf-8 -*-
"""流式输出与浏览器可靠性测试：TTS 分段 / 流式端点 / 懒加载骨架。"""
import os
import tempfile

from app.services.tts_client import _split_tts_text


def test_tts_split_short_text():
    """短文本不分段。"""
    assert _split_tts_text("你好") == ["你好"]


def test_tts_split_long_text():
    """长文本按标点分段，每段 ≤ limit。"""
    text = "第一句。第二句。第三句。" * 200  # 2400 字
    segs = _split_tts_text(text, limit=1000)
    assert len(segs) > 1
    assert all(len(s) <= 1000 for s in segs)
    # 拼接后内容长度 ≈ 原文（分段不丢字）
    assert abs(sum(len(s) for s in segs) - len(text)) <= 50  # 标点可能被吃掉几个


def test_tts_split_hard_cut():
    """无标点的超长文本硬切。"""
    text = "x" * 2500
    segs = _split_tts_text(text, limit=1000)
    assert all(len(s) <= 1000 for s in segs)
    assert sum(len(s) for s in segs) == 2500  # 不丢字


def test_tts_split_empty():
    assert _split_tts_text("") == []
    assert _split_tts_text("   ") == []


def test_gen_prompt_has_lazy_load_rule():
    """生成提示词包含懒加载/无限滚动规则（浏览器可靠性增强）。"""
    from app.services.mini_generator import GEN_SYSTEM_PROMPT
    assert "懒加载" in GEN_SYSTEM_PROMPT
    assert "无限滚动" in GEN_SYSTEM_PROMPT
    assert "scroll_into_view_if_needed" in GEN_SYSTEM_PROMPT


def test_gen_prompt_has_captcha_handling():
    """生成提示词包含验证码处理（等待消失 / LOGIN_REQUIRED 不硬闯）。"""
    from app.services.mini_generator import GEN_SYSTEM_PROMPT
    assert "LOGIN_REQUIRED" in GEN_SYSTEM_PROMPT
    assert "验证码" in GEN_SYSTEM_PROMPT


def test_skeleton_has_lazy_scroll_helper():
    """脚本骨架包含 lazy_scroll_load 辅助函数（生成脚本可直接调用）。"""
    from app.services.mini_generator import SKELETON
    assert "def lazy_scroll_load" in SKELETON
    assert "mouse.wheel" in SKELETON
    assert "加载更多" in SKELETON


def test_stream_endpoint_registered():
    """流式输出端点已注册。"""
    from app.api.mini import router
    paths = {getattr(r, "path", "") for r in router.routes}
    assert "/mini/tasks/{task_id}/stream" in paths
    methods = {getattr(r, "path", "") + ":" + ",".join(sorted(getattr(r, "methods", []) or []))
               for r in router.routes}
    assert "/mini/tasks/{task_id}/stream:GET" in methods
