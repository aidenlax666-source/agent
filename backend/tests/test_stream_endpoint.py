# -*- coding: utf-8 -*-
"""流式端点逻辑测试：直接测 stream_output 处理函数（mock 任务状态）。"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from unittest.mock import patch

from fastapi import Request
from starlette.datastructures import Headers


def _make_request(range_header: str | None):
    """构造带可选 Range 头的 Request。"""
    headers = {}
    if range_header:
        headers["range"] = range_header
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/mini/tasks/t/stream",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
        "scheme": "http",
    }
    return Request(scope)


def test_stream_full():
    """无 Range：返回完整文件（200）。"""
    import app.api.mini as mini
    from fastapi import Response

    tmp = tempfile.mkdtemp()
    fp = os.path.join(tmp, "audio.mp3")
    with open(fp, "wb") as f:
        f.write(b"MP3DATA" * 1000)  # 7000 字节（MP3DATA 是 7 字符）

    fake_status = {"result": {"output_file": fp}}

    async def run():
        with patch.object(mini.mini_tasks, "get_status", return_value=fake_status):
            with patch.object(mini, "Depends", lambda x: lambda: None):
                resp = await mini.stream_output("t", _make_request(None), {"id": "u"})
                assert resp.status_code == 200
                body = b"".join([chunk async for chunk in resp.body_iterator])
                assert len(body) == 7000
                assert resp.headers["accept-ranges"] == "bytes"
                return resp

    resp = asyncio.run(run())
    assert resp.status_code == 200


def test_stream_range():
    """Range bytes=0-99：返回部分内容（206 + Content-Range）。"""
    import app.api.mini as mini

    tmp = tempfile.mkdtemp()
    fp = os.path.join(tmp, "video.mp4")
    with open(fp, "wb") as f:
        f.write(b"V" * 5000)

    fake_status = {"result": {"output_file": fp}}

    async def run():
        with patch.object(mini.mini_tasks, "get_status", return_value=fake_status):
            with patch.object(mini, "Depends", lambda x: lambda: None):
                resp = await mini.stream_output("t", _make_request("bytes=0-99"), {"id": "u"})
                assert resp.status_code == 206
                body = b"".join([chunk async for chunk in resp.body_iterator])
                assert len(body) == 100
                assert resp.headers["content-range"] == "bytes 0-99/5000"
                return resp

    resp = asyncio.run(run())
    assert resp.status_code == 206
    assert resp.headers["accept-ranges"] == "bytes"


def test_stream_range_invalid():
    """非法 Range（start > end）：返回 416。"""
    import app.api.mini as mini

    tmp = tempfile.mkdtemp()
    fp = os.path.join(tmp, "a.mp3")
    with open(fp, "wb") as f:
        f.write(b"A" * 100)

    fake_status = {"result": {"output_file": fp}}

    async def run():
        with patch.object(mini.mini_tasks, "get_status", return_value=fake_status):
            with patch.object(mini, "Depends", lambda x: lambda: None):
                resp = await mini.stream_output("t", _make_request("bytes=500-10"), {"id": "u"})
                assert resp.status_code == 416
                assert resp.headers["content-range"] == "bytes */100"
                return resp

    resp = asyncio.run(run())
    assert resp.status_code == 416
