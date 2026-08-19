# -*- coding: utf-8 -*-
"""产物存储抽象层测试：本地模式（默认）走 web/ 目录；S3 模式走 boto3。

本地模式：真实文件读写 + 下载/流式响应（Range 支持）。
S3 模式：mock boto3 client 验证统一接口（exists/size/read/stream）。
"""
import io
import os
import tempfile
from unittest.mock import patch, MagicMock

from app.config import get_settings
from app.services import storage


# ---- 本地模式 ----

def test_local_mode_default():
    """默认（STORAGE_BACKEND 空/local）用本地 web/ 目录。"""
    assert storage._backend() == "local"


def test_local_exists_size_read(tmp_path):
    """本地：写入产物后 exists/size/read 正常。"""
    from app.paths import web_root
    p = os.path.join(web_root(), "test_artifact_storage.txt")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("hello storage")
        assert storage.artifact_exists("test_artifact_storage.txt") is True
        assert storage.artifact_size("test_artifact_storage.txt") == 13
        assert storage.artifact_read("test_artifact_storage.txt") == b"hello storage"
        assert storage.artifact_exists("ghost.txt") is False
    finally:
        if os.path.exists(p):
            os.unlink(p)


def test_local_download_response(tmp_path):
    """本地：下载响应强制 attachment + nosniff。"""
    from app.paths import web_root
    p = os.path.join(web_root(), "dl_test.txt")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("download me")
        resp = storage.artifact_download_response("dl_test.txt", "dl_test.txt")
        assert resp.headers.get("x-content-type-options") == "nosniff"
        assert "attachment" in resp.headers.get("content-disposition", "")
    finally:
        if os.path.exists(p):
            os.unlink(p)


def _collect_body(resp) -> bytes:
    """收集 StreamingResponse 的 body（body_iterator 是 async 迭代器）。"""
    import asyncio
    async def _gather():
        chunks = []
        async for chunk in resp.body_iterator:
            chunks.append(chunk)
        return b"".join(chunks)
    try:
        return asyncio.run(_gather())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_gather())
        finally:
            loop.close()


def test_local_stream_range(tmp_path):
    """本地：流式响应支持 Range（206 + 部分内容）。"""
    from app.paths import web_root
    p = os.path.join(web_root(), "stream_test.txt")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("0123456789")
        resp = storage.artifact_stream_response("stream_test.txt", "text/plain",
                                                range_header="bytes=2-5")
        assert resp.status_code == 206
        assert resp.headers.get("content-range") == "bytes 2-5/10"
        assert _collect_body(resp) == b"2345"
    finally:
        if os.path.exists(p):
            os.unlink(p)


def test_local_stream_full(tmp_path):
    """本地：无 Range 返回 200 全量。"""
    from app.paths import web_root
    p = os.path.join(web_root(), "stream_full.txt")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("full content")
        resp = storage.artifact_stream_response("stream_full.txt", "text/plain")
        assert resp.status_code == 200
        assert _collect_body(resp) == b"full content"
    finally:
        if os.path.exists(p):
            os.unlink(p)


def test_local_stream_416(tmp_path):
    """本地：越界 Range 返回 416。"""
    from app.paths import web_root
    p = os.path.join(web_root(), "stream_416.txt")
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write("abc")
        resp = storage.artifact_stream_response("stream_416.txt", "text/plain",
                                                range_header="bytes=99-100")
        assert resp.status_code == 416
    finally:
        if os.path.exists(p):
            os.unlink(p)


# ---- S3 模式 ----

def _enable_s3(fake_client: MagicMock):
    p1 = patch.object(get_settings(), "storage_backend", "s3")
    p1.start()
    p2 = patch.object(get_settings(), "s3_bucket", "test-bucket")
    p2.start()
    p3 = patch.object(storage, "_s3_client", return_value=fake_client)
    p3.start()
    return (p1, p2, p3)


def test_s3_exists_size_read():
    """S3：exists/size/read 走 boto3。"""
    client = MagicMock()
    client.head_object.return_value = {"ContentLength": 42}
    client.get_object.return_value = {"Body": io.BytesIO(b"x" * 42)}
    patchers = _enable_s3(client)
    try:
        assert storage.artifact_exists("a/b.mp4") is True
        client.head_object.assert_called_with(Bucket="test-bucket", Key="a/b.mp4")
        assert storage.artifact_size("a/b.mp4") == 42
        assert storage.artifact_read("a/b.mp4") == b"x" * 42
    finally:
        for p in patchers:
            p.stop()


def test_s3_missing():
    """S3：产物不存在（head_object 抛异常）→ exists False。"""
    client = MagicMock()
    client.head_object.side_effect = Exception("404")
    patchers = _enable_s3(client)
    try:
        assert storage.artifact_exists("nope.mp4") is False
    finally:
        for p in patchers:
            p.stop()


def test_s3_stream_uses_range():
    """S3：流式走 Range 请求（跨实例大文件边下边播）。"""
    client = MagicMock()
    client.head_object.return_value = {"ContentLength": 100}
    client.get_object.return_value = {"Body": io.BytesIO(b"y" * 50)}
    patchers = _enable_s3(client)
    try:
        resp = storage.artifact_stream_response("video.mp4", "video/mp4",
                                                range_header="bytes=0-49")
        assert resp.status_code == 206
        assert _collect_body(resp) == b"y" * 50
        # 验证 Range 传给了 S3
        kwargs = client.get_object.call_args.kwargs
        assert kwargs["Range"] == "bytes=0-49"
    finally:
        for p in patchers:
            p.stop()
