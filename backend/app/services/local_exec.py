# -*- coding: utf-8 -*-
from __future__ import annotations

"""本地执行模式（混合架构）：云端生成脚本 → 用户本地 exe 端执行 → 回传收尾。

场景：需要"在用户本地创建/操作文件"的任务（如"在 D:/我的文档 创建 报告.txt"）。
云端沙箱在服务器，写不到用户磁盘——这类任务派发给该用户本地的 local_worker.exe
（用户自己的电脑），由云端生成"本地操作脚本"（Python 标准库），本地执行后回传。

流程：
1. submit(local=True) → 任务落库（status=local_queued）+ 进 Redis 本地队列
2. 用户本地跑 local_worker.exe：轮询 POST /api/local/tasks/poll 领取自己的任务
3. poll 时云端懒生成"本地操作脚本"（标准库，写用户指定路径）→ 返回给 exe
4. exe 本地执行脚本（内置 AST 扫描 + 子进程隔离）→ POST /api/local/tasks/report 回传
5. 云端标记 done/failed，产物信息写回 result

安全：脚本在用户自己机器执行（用户授权本地端运行即知情）；
云端仍做 AST 静态扫描（拦 eval/命令注入/危险文件操作），防恶意/幻觉脚本乱删文件。
"""

import asyncio
import json
import logging
import time

from app.services.llm_client import chat_completion

logger = logging.getLogger("app.services.local_exec")

# 本地操作脚本生成提示词：让 LLM 写一个"执行用户本地需求"的 Python 脚本
# 约束：只用标准库、不碰系统关键目录、输出 [OUTPUT_FILE] 标记
LOCAL_SCRIPT_SYSTEM = """你是一位本地文件操作专家。根据用户需求生成**一个 Python 脚本**，
在**用户自己的电脑**上执行该需求（创建文件、写文本、整理目录、批量重命名等）。

硬性约束：
1. 只用 Python 标准库（os/shutil/pathlib/re/json/csv 等），不得 import 第三方库
2. 不得访问系统关键目录（C:/Windows、/etc、/usr、/bin），不得删除/覆盖非任务目标文件
3. 任务要求创建/写入的文件，路径以用户需求里指定的为准；需求没给路径时写到
   当前工作目录下，并用清晰文件名
4. 脚本必须打印 [OUTPUT_FILE] <绝对路径> 标记（每创建/写入一个文件打一次）
5. 中文路径用 Path 处理，文件编码统一 utf-8
6. 只输出 Python 代码本身，不要解释文字、不要 markdown 代码块

【用户需求】
{requirement}
"""


async def generate_local_script(requirement: str, max_retries: int = 2) -> str | None:
    """云端用 LLM 生成"本地操作脚本"（懒生成，poll 领取时调用）。

    失败返回 None（本地端可降级为简单处理或报错）。
    """
    for attempt in range(max_retries):
        try:
            code = await chat_completion(
                system_prompt=LOCAL_SCRIPT_SYSTEM,
                user_prompt=requirement[:2000],
                temperature=0.2,
                max_tokens=4096,
            )
            code = code.strip()
            # 去掉可能的 markdown 围栏
            if code.startswith("```"):
                code = code.split("\n", 1)[-1]
            if code.endswith("```"):
                code = code.rsplit("```", 1)[0]
            code = code.strip()
            try:
                compile(code, "<local_script>", "exec")
            except SyntaxError as e:
                logger.warning("本地脚本生成第 %d 次语法错误: %s", attempt + 1, str(e)[:100])
                continue
            return code
        except Exception as e:
            logger.warning("本地脚本生成第 %d 次失败: %s", attempt + 1, str(e)[:100])
        await asyncio.sleep(1)
    return None


async def prepare_local_task(task_id: str, requirement: str) -> dict:
    """领取本地任务时调用：生成脚本并返回给本地端执行的任务载荷。

    返回 {"task_id", "requirement", "script"}；脚本生成失败时 script=None
    （本地端用简单本地处理兜底，避免任务永久挂起）。
    """
    script = await generate_local_script(requirement)
    return {
        "task_id": task_id,
        "requirement": requirement,
        "script": script,
        "generated_at": time.time(),
    }