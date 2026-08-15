from __future__ import annotations
"""mini_generator 后台任务队列：提交 → 后台执行 → 查询状态/结果。

复用 long_task 的进程内 asyncio 注册表，无需 Redis/Celery。
需求含报告意图（报告/可视化/图表/报表）时自动走「报告生成」模式（HTML 可视化报告），
否则走通用生成模式（表格/文件输出）。
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid
import logging

from app.services.long_task import start_background, is_running, cancel

logger = logging.getLogger("app.services.mini_tasks")

# task_id -> 任务记录
_TASKS: dict[str, dict] = {}

# 保留最近 N 个已完成任务，防止内存无限增长
_MAX_KEPT = 200

# 报告意图关键词：命中则走 HTML 可视化报告模式
_REPORT_HINTS = ["报告", "可视化", "图表", "报表", "dashboard", "报告页"]

# 报告输出目录：项目 web/（与隧道/静态服务共享，产出即可公网访问）
_WEB_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "web"))


def _size_mb(path: str) -> str:
    """文件大小人性化显示（MB）。"""
    try:
        return f"{os.path.getsize(path) / 1024 / 1024:.1f}MB"
    except Exception:
        return ""


def _is_report_request(requirement: str) -> bool:
    r = requirement.lower()
    return any(k in r for k in _REPORT_HINTS)


# 联机游戏意图关键词：命中则走多人游戏生成
_GAME_HINTS = ["联机", "多人", "一起玩", "对战", "加入房间", "创建房间", "合作", "同步玩"]


def _is_game_request(requirement: str) -> bool:
    r = requirement.lower()
    return any(k in r for k in _GAME_HINTS)


# 单机内容生成意图：做/生成 + 内容类名词，且非联机/报告/数据抓取任务
_CONTENT_VERBS = ["生成一个", "做一个", "制作一个", "创作", "设计", "写一个", "给我做一个"]
_CONTENT_ITEMS = ["游戏", "漫剧", "网页", "页面", "小工具", "动画", "简历", "海报", "贺卡", "倒计时", "计算器", "主页", "个人网"]


# 视频生成意图（豆包 Seedance）：命中则走文生视频
_VIDEO_HINTS = ["生成视频", "制作视频", "做视频", "短视频", "宣传片", "vlog", "文生视频",
                "视频生成", "动画视频", "动画短片", "视频"]


def _is_video_request(requirement: str) -> bool:
    r = requirement.lower()
    # 排除"看视频/上传视频"这类非生成意图
    if any(k in r for k in ("看视频", "上传视频", "视频教程", "视频会议")):
        return False
    return any(k in r for k in _VIDEO_HINTS)


# 图片生成意图（豆包 Seedream）：命中则走文生图
_IMAGE_HINTS = ["生成图片", "生成一张图", "生成照片", "文生图", "图片生成", "画一张", "画一幅",
                "画一个", "插画", "壁纸", "头像", "表情包", "封面图", "logo", "图标", "设计一张"]


def _is_image_request(requirement: str) -> bool:
    r = requirement.lower()
    return any(k in r for k in _IMAGE_HINTS)


# 音乐生成意图：LLM 作曲 + 标准库合成 WAV（不含裸"音乐"，避免误伤音乐播放页等 HTML 内容）
_MUSIC_HINTS = ["生成音乐", "作曲", "做一首歌", "生成一首歌", "写一首歌", "背景音乐", "配乐", "编曲", "合成音乐"]


def _is_music_request(requirement: str) -> bool:
    r = requirement.lower()
    return any(k in r for k in _MUSIC_HINTS)


def _is_content_request(requirement: str) -> bool:
    r = requirement.lower()
    if (_is_game_request(r) or _is_report_request(r) or _is_video_request(r)
            or _is_image_request(r) or _is_music_request(r)):
        return False
    if any(k in r for k in ("导出excel", "导出csv", "抓取", "爬取", "统计", "汇总", "数据", "接口")):
        return False
    if not any(v in r for v in _CONTENT_VERBS):
        return False
    return any(k in r for k in _CONTENT_ITEMS)


REPORT_SYSTEM_PROMPT = """你是一位 Python 脚本专家 + 数据可视化设计师。生成一个 Python 脚本，完成「按用户需求抓取数据 → 统计分析 → 生成可视化报告」全流程。

【数据抓取】按用户需求抓取指定网站/搜索关键词的数据（用 urllib.request 或 requests 加 proxies={"http": None, "https": None}，注意代理 SSL 降级；网页任务可用 Playwright）
【统计分析】用 pandas 做需求要求的统计（分组/排序/TopN 等）
【可视化报告 report.html】（重点，必须精美）
- 单文件 HTML，纯 HTML/CSS/SVG 图表，禁止外链 CDN/库/字体
- 现代浅色或优雅深色主题，圆角卡片、阴影、留白
- 结构：标题（大号渐变）+ 统计摘要卡片 + SVG 图表（条形/折线/饼图，带数值标签）+ 数据表格 + 页脚（生成时间、数据来源）
- 中文界面
【输出】
- 报告保存为 report.html 到当前目录
- 打印 SUCCESS:DATA_ROWS:N（N=统计行数）和 PREVIEW_DATA:JSON（前5行）

只输出完整 Python 代码，不要解释。"""


MUSIC_SYSTEM_PROMPT = """你是一位作曲家 + Python 音频合成专家。用 Python **标准库**（wave / math / struct，禁止 numpy/pygame/mido/任何第三方库）编写一个脚本，合成一首契合主题的音乐并保存为 WAV。

【作曲要求（按给定主题创作）】
1. 主题情绪要贴合（如"星空"→空灵慢速、"夏日海边"→轻快明亮、"森林"→悠扬自然）
2. 用 C 大调/A 小调等音阶设计旋律：主旋律 + 低音和声（可叠加两个音轨让声音更饱满）
3. 节奏：主旋律音符序列（音符名+八度+时值+休止），速度/时值体现情绪
4. 时长 30-60 秒

【合成技术（必须）】
1. 采样率 44100，16-bit 单声道（或双声道）
2. 正弦波合成，音符频率用公式 f = 440 * 2^((midi-69)/12)
3. 每个音符加**衰减包络**（attack/decay），音符间无爆音
4. 整体淡入淡出（开头 0.5s 渐入、结尾渐出）
5. 音量适中（峰值 ~0.5 避免削波）

【输出】
- 保存 melody.wav 到当前目录
- 打印 DURATION:秒数、NOTES:音符数

只输出完整 Python 代码，不要解释。"""


QA_ROUTER_PROMPT = """你是任务路由专家。判断用户需求属于哪一类，只返回 JSON {"type":"qa"} 或 {"type":"task"}：

- "qa"：用户已上传数据文件（Excel/CSV）并要对这些数据提问/分析/统计/对比/汇总（如"哪个产品销量最高"、"分析一下这份数据"、"按月份汇总金额"、"帮我看看有什么规律"）
- "task"：一切自动化执行任务——生成/抓取/爬取/导出/制作/网页/视频/图片/音乐/游戏/文件操作等（这类需求需要写脚本执行）

注意：只有明确针对"已上传的数据文件"的提问/分析才算 qa；"生成XX数据"、"爬取XX数据"属于 task。"""


async def _classify_intent(requirement: str, has_data_files: bool) -> str:
    """LLM 判断：数据问答(qa) 还是 自动化任务(task)。没传数据文件时直接判 task。"""
    if not has_data_files:
        return "task"
    try:
        from app.services.llm_client import chat_completion_json
        info = await chat_completion_json(QA_ROUTER_PROMPT, requirement[:300], max_tokens=20)
        return "qa" if info.get("type") == "qa" else "task"
    except Exception:
        return "task"


def submit(requirement: str, url: str | None = None, user_id: str = "", image_paths: list[str] | None = None,
           data_paths: list[str] | None = None) -> dict:
    """提交一个后台任务，立即返回任务信息（持久化到 SQLite，重启不丢）。

    统一入口：LLM 自动判断"数据问答"还是"任务执行"，无需用户选择。
    """
    task_id = uuid.uuid4().hex[:12]
    record = {
        "id": task_id,
        "user_id": user_id,
        "requirement": requirement,
        "url": url or "",
        "status": "queued",
        "progress": 0,
        "message": "排队中",
        "created_at": time.time(),
        "result": None,
        "error": None,
        "image_paths": image_paths or [],
        "data_paths": data_paths or [],
    }
    _TASKS[task_id] = record

    # 清理超出上限的旧任务
    if len(_TASKS) > _MAX_KEPT:
        for tid in list(_TASKS.keys())[: len(_TASKS) - _MAX_KEPT]:
            if not is_running(tid):
                _TASKS.pop(tid, None)

    started = start_background(task_id, _run_task(task_id, requirement, url, record))
    if not started:
        record["status"] = "error"
        record["error"] = "同名任务已在运行"
        record["message"] = "任务已在运行"
    return record

def schedule_task(task_id: str, schedule_type: str, schedule_value: str, enabled: bool, user_id: str = "") -> dict | None:
    """设置任务定时执行（interval 分钟 / daily HH:MM），由调度器到点重新提交任务。"""
    rec = _TASKS.get(task_id)
    if rec is None:
        try:
            import sqlite3
            from app.database import DB_PATH
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM mini_tasks WHERE id=?", (task_id,)).fetchone()
            conn.close()
            if row:
                rec = dict(row)
        except Exception:
            rec = None
    if rec is None:
        return None
    if user_id and rec.get("user_id") and rec["user_id"] != user_id:
        return None

    schedule_type = (schedule_type or "interval").strip()
    schedule_value = (schedule_value or "").strip()
    if enabled:
        if schedule_type not in ("interval", "daily"):
            return {"error": "schedule_type 只能是 interval 或 daily"}
        if schedule_type == "interval":
            try:
                if int(schedule_value) <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                return {"error": "interval 需要正整数分钟"}
        else:
            # daily：严格校验 HH:MM（00-23:00-59），防非法值导致 500
            import re as _re
            if not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", schedule_value):
                return {"error": "daily 需要 HH:MM 格式（如 08:30）"}

    import time as _t
    now = _t.time()
    next_run = None
    if enabled:
        if schedule_type == "interval":
            next_run = now + int(schedule_value) * 60
        else:
            # daily：今天 HH:MM 未过则今天，否则明天
            from datetime import datetime, timedelta
            hh, mm = (int(x) for x in schedule_value.split(":"))
            local = datetime.now()
            target = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if target <= local:
                target += timedelta(days=1)
            next_run = target.timestamp()

    try:
        import asyncio as _a
        from app.database import set_mini_schedule
        loop = _a.get_running_loop()
        loop.create_task(set_mini_schedule(task_id, schedule_type, schedule_value, enabled, next_run))
    except Exception:
        pass
    if task_id in _TASKS:
        _TASKS[task_id].update({
            "schedule_type": schedule_type, "schedule_value": schedule_value,
            "enabled": 1 if enabled else 0, "next_run_at": next_run,
        })
    return {"id": task_id, "schedule_type": schedule_type, "schedule_value": schedule_value,
            "enabled": enabled, "next_run_at": next_run}


# ============================================================
# mini 定时调度循环（原 scheduler.py 的 mini 部分迁移至此）
# ============================================================

def _next_mini_run(task: dict, now_ts: float) -> float | None:
    """计算 mini 定时任务的下次执行时间（epoch 秒）。"""
    from datetime import datetime, timedelta
    stype = task.get("schedule_type", "interval")
    sval = task.get("schedule_value", "60")
    if stype == "interval":
        try:
            return now_ts + int(sval) * 60
        except (ValueError, TypeError):
            return now_ts + 3600
    if stype == "daily":
        try:
            hh, mm = (int(x) for x in sval.split(":"))
            local = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
            if local.timestamp() <= now_ts:
                local += timedelta(days=1)
            return local.timestamp()
        except Exception:
            return now_ts + 86400
    return None


async def mini_scheduler_loop() -> None:
    """调度循环：每分钟检查一次到期的 mini 定时任务，到点重新提交。"""
    logger.info("[mini调度器] 启动，每 60 秒检查一次")
    while True:
        try:
            import time as _t
            from app.database import get_due_mini_tasks, update_mini_run
            due = await get_due_mini_tasks(_t.time())
            for mtask in due:
                tid = mtask["id"]
                key = f"mini_sched:{tid}"
                if is_running(key):
                    continue
                submit(mtask.get("requirement") or "", mtask.get("url") or None, mtask.get("user_id") or "")
                now_ts = _t.time()
                nxt = _next_mini_run(mtask, now_ts)
                start_background(key, update_mini_run(tid, now_ts, nxt))
                logger.info(f"[mini调度器] 定时触发: {tid[:8]} - {(mtask.get('requirement') or '')[:40]}")
        except Exception as e:
            logger.error(f"[mini调度器] 检查异常: {e}")
        await asyncio.sleep(60)


def confirm_task(task_id: str, user_id: str = "") -> dict | None:
    """确认任务结果（满意）：标记 confirmed，结果即最终版。"""
    rec = _TASKS.get(task_id)
    if rec is None:
        try:
            import sqlite3
            from app.database import DB_PATH
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM mini_tasks WHERE id=?", (task_id,)).fetchone()
            conn.close()
            if row:
                rec = dict(row)
        except Exception:
            rec = None
    if rec is None:
        return None
    if user_id and rec.get("user_id") and rec["user_id"] != user_id:
        return None
    try:
        from app.database import update_mini_task
        asyncio.create_task(update_mini_task(task_id, status="confirmed", message="已确认"))
    except Exception:
        pass
    if task_id in _TASKS:
        _TASKS[task_id]["status"] = "confirmed"
        _TASKS[task_id]["message"] = "已确认"
    return {"id": task_id, "status": "confirmed"}


def iterate(task_id: str, feedback: str, user_id: str = "") -> dict | None:
    """迭代修改：对已完成任务提反馈，同一任务原地重跑（需求=原需求+反馈）。"""
    rec = _TASKS.get(task_id)
    if rec is None:
        try:
            import sqlite3
            from app.database import DB_PATH
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM mini_tasks WHERE id=?", (task_id,)).fetchone()
            conn.close()
            if row:
                rec = dict(row)
        except Exception:
            rec = None
    if rec is None:
        return None
    if user_id and rec.get("user_id") and rec["user_id"] != user_id:
        return None
    if is_running(task_id):
        return {"id": task_id, "status": "running", "error": "任务正在执行中，请等待完成"}

    feedback = (feedback or "").strip()
    if not feedback:
        return {"id": task_id, "status": "error", "error": "反馈不能为空"}

    base_req = rec.get("requirement") or ""
    new_req = f"{base_req}（用户修改意见：{feedback}）"
    url = rec.get("url") or ""

    # 更新内存记录 + 重置状态（保留原图片）
    _TASKS[task_id] = {
        "id": task_id,
        "user_id": rec.get("user_id", ""),
        "requirement": new_req,
        "url": url,
        "status": "queued",
        "progress": 0,
        "message": "迭代中",
        "created_at": rec.get("created_at", time.time()),
        "result": None,
        "error": None,
        "image_paths": rec.get("image_paths") or [],
    }
    started = start_background(task_id, _run_task(task_id, new_req, url, _TASKS[task_id]))
    return {"id": task_id, "status": "queued" if started else "error", "message": "迭代修改已提交"}


async def _run_task(task_id: str, requirement: str, url: str | None, record: dict) -> None:
    """后台执行一个任务（联机游戏/报告/内容/视频/图片/音乐/代码任务，可组合多模式），同步 SQLite。"""
    from app.database import save_mini_task, update_mini_task

    record["status"] = "running"
    record["message"] = "正在执行..."
    image_paths = record.get("image_paths") or []
    data_paths = record.get("data_paths") or []

    # ---- 统一入口：LLM 判断"数据问答"还是"任务执行" ----
    intent = await _classify_intent(requirement, bool(data_paths))
    if intent == "qa":
        record["message"] = "识别为数据问答，AI 正在分析..."
        await update_mini_task(task_id, status="running", message=record["message"])
        await _run_qa_task(task_id, requirement, data_paths, record)
        return

    # ---- 多模式检测（需求可同时含多种意图，依次执行并合并产物） ----
    modes: list[str] = []
    r_low = requirement.lower()
    if _is_game_request(requirement):
        modes.append("game")
    if _is_report_request(requirement):
        modes.append("report")
    if _is_content_request(requirement):
        modes.append("content")
    if _is_video_request(requirement):
        modes.append("video")
    if _is_image_request(requirement):
        modes.append("image")
    if _is_music_request(requirement):
        modes.append("music")
    if not modes:
        modes.append("code")
    # 生成类与"明确要数据文件"意图并存时补代码任务（如"抓XX数据导出Excel，并做成XX网页"）
    if "code" not in modes and any(k in r_low for k in ("导出excel", "导出csv", "导出xlsx", "下载数据", "输出文件")):
        modes.append("code")

    try:
        await save_mini_task(record)
        record["message"] = f"检测到 {len(modes)} 个执行模式: {'+'.join(modes)}，开始执行..."
        await update_mini_task(task_id, status="running", message=record["message"])

        merged: dict = {"status": "ok", "rows": 0, "preview": [], "elapsed": 0, "error": None}
        failed: list[str] = []
        for mode in modes:
            if mode == "game":
                from app.services.game_generator import generate_multiplayer_game
                g = await generate_multiplayer_game(requirement)
                if g.get("success"):
                    merged["game_url"] = g.get("url")
                    merged["output_file"] = g.get("file_path")
                    merged["message_game"] = f"联机游戏已生成：{g.get('url')}"
                else:
                    failed.append(f"联机游戏: {g.get('error', '失败')}")
            elif mode == "report":
                rp = await _run_report_task(task_id, requirement, url)
                if rp.get("report_url"):
                    merged["report_url"] = rp.get("report_url")
                    merged["output_file"] = rp.get("output_file")
                else:
                    failed.append(f"报告: {rp.get('error', '失败')}")
            elif mode == "content":
                from app.services.content_generator import generate_content
                g = await generate_content(requirement)
                if g.get("success"):
                    merged["content_url"] = g.get("url")
                    merged["output_file"] = g.get("file_path")
                else:
                    failed.append(f"内容: {g.get('error', '失败')}")
            elif mode == "video":
                # 豆包 Seedance 文生视频/图生视频：提交异步任务 → 轮询 → 下载到 web/ 目录
                from app.services.video_client import generate_video, download_video
                # 用户上传了参考图 → 图生视频（第一张作为首帧/参考）
                ref_img = image_paths[0] if image_paths else None
                v = await generate_video(requirement, max_wait=600, image_path=ref_img)
                if v.get("success") and v.get("video_url"):
                    merged["message_video"] = "视频已生成，正在下载..."
                    fname = f"video_{task_id}.mp4"
                    save_path = os.path.join(_WEB_DIR, fname)
                    os.makedirs(_WEB_DIR, exist_ok=True)
                    if await download_video(v["video_url"], save_path):
                        merged["video_url"] = f"/{fname}"
                        merged["output_file"] = save_path
                        merged["message_video"] = f"视频已生成：/{fname}（{_size_mb(save_path)}）"
                    else:
                        failed.append("视频已生成但下载失败")
                else:
                    failed.append(f"视频: {v.get('detail', '失败')}")
            elif mode == "image":
                # 豆包 Seedream 文生图：同步生成 → 下载全部图片到 web/ 目录
                from app.services.video_client import generate_image, download_video
                img = await generate_image(requirement)
                if img.get("success") and img.get("image_urls"):
                    os.makedirs(_WEB_DIR, exist_ok=True)
                    urls: list[str] = []
                    for i, u in enumerate(img["image_urls"][:4]):
                        fname = f"image_{task_id}_{i}.png"
                        save_path = os.path.join(_WEB_DIR, fname)
                        if await download_video(u, save_path):
                            urls.append(f"/{fname}")
                    if urls:
                        merged["image_urls"] = urls
                        merged["image_url"] = urls[0]
                        merged["output_file"] = os.path.join(_WEB_DIR, f"image_{task_id}_0.png")
                        merged["message_image"] = f"生成 {len(urls)} 张图片"
                    else:
                        failed.append("图片已生成但下载失败")
                else:
                    failed.append(f"图片: {img.get('detail', '失败')}")
            elif mode == "music":
                # LLM 作曲 + 标准库合成 WAV → web/ 目录（公网可访问）
                ms = await _run_music_task(task_id, requirement)
                if ms.get("music_url"):
                    merged["music_url"] = ms.get("music_url")
                    merged["output_file"] = ms.get("output_file")
                    merged["message_music"] = f"音乐已生成：{ms.get('music_url')}"
                else:
                    failed.append(f"音乐: {ms.get('error', '失败')}")
            else:  # code：数据/抓取任务
                from app.services.mini_generator import generate_and_verify
                result = await generate_and_verify(requirement, url or None, image_paths=image_paths or None,
                                                   user_id=record.get("user_id") or "")
                keep = (
                    "status", "rows", "preview", "error", "elapsed", "script",
                    "expected_count", "expected_fields", "missing_fields",
                    "coverage_missing", "value_issues", "count_heals", "field_heals",
                    "coverage_heals", "value_heals", "output_file", "report_url",
                    "image_context",
                )
                merged.update({k: result.get(k) for k in keep})
                if result.get("status") != "ok":
                    failed.append(f"数据处理: {result.get('error') or result.get('status')}")

        if failed and not any(k in merged for k in ("game_url", "report_url", "content_url", "video_url", "image_url", "image_urls", "music_url")):
            merged["status"] = "failed"
            merged["error"] = "；".join(failed)[:300]
        else:
            merged["status"] = "ok"
            if failed:
                merged["partial_errors"] = failed

        record["result"] = merged
        record["status"] = "done"
        record["message"] = "完成"
        record["progress"] = 100
        await update_mini_task(task_id, status="done", message="完成",
                               result=json.dumps(merged, ensure_ascii=False), error=None)
    except asyncio.CancelledError:
        record["status"] = "cancelled"
        record["message"] = "已取消"
        await update_mini_task(task_id, status="cancelled", message="已取消")
    except Exception as e:
        logger.exception("[mini_task:%s] failed", task_id)
        record["status"] = "error"
        record["error"] = str(e)[:300]
        record["message"] = f"执行出错: {str(e)[:120]}"
        await update_mini_task(task_id, status="error", error=record["error"], message=record["message"])


async def _run_report_task(task_id: str, requirement: str, url: str | None) -> dict:
    """报告模式：LLM 生成脚本 → 本地运行 → report.html 落 web 目录（公网可访问）。"""
    import subprocess
    from app.services.llm_client import chat_completion

    started = time.time()
    report_name = f"report_{task_id}.html"
    out_dir = _WEB_DIR
    os.makedirs(out_dir, exist_ok=True)

    # 1. 生成脚本（带需求上下文）
    code = ""
    for attempt in range(3):
        try:
            code = await chat_completion(
                REPORT_SYSTEM_PROMPT,
                f"【用户需求】{requirement}\n\n【目标URL】{url or '（按需求推断）'}\n请生成报告脚本，报告保存为 report.html",
                temperature=0.3, max_tokens=8000,
            )
            code = code.strip()
            if code.startswith("```python"):
                code = code[9:]
            elif code.startswith("```"):
                code = code[3:]
            if code.endswith("```"):
                code = code[:-3]
            code = code.strip()
            compile(code, "<script>", "exec")
            if "report.html" in code:
                break
        except Exception as e:
            logger.warning("报告脚本生成第 %d 次失败: %s", attempt + 1, str(e)[:120])
            code = ""

    if not code:
        return {"status": "generate_failed", "error": "报告脚本生成失败", "elapsed": round(time.time() - started, 1)}

    # 2. 沙箱执行（隔离环境 + 静态扫描 + 超时/资源限制），产物从沙箱工作区复制到 web/
    from app.sandbox.docker_executor import execute_in_sandbox
    result = await execute_in_sandbox(code, timeout=180, preview_mode=False)
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if not result.success:
        return {
            "status": "failed",
            "error": f"脚本执行失败: {(stderr or stdout)[-300:]}",
            "elapsed": round(time.time() - started, 1),
            "script": code,
        }

    report_path = os.path.join(out_dir, report_name)
    if result.output_file_path and os.path.exists(result.output_file_path):
        os.makedirs(out_dir, exist_ok=True)
        import shutil as _shutil
        _shutil.copyfile(result.output_file_path, report_path)
    elif not os.path.exists(report_path):
        return {
            "status": "failed",
            "error": f"未生成 report.html: {(stderr or stdout)[-300:]}",
            "elapsed": round(time.time() - started, 1),
            "script": code,
        }

    rows = 0
    m = re.search(r"DATA_ROWS:(\d+)", stdout)
    if m:
        rows = int(m.group(1))
    preview = []
    m = re.search(r"PREVIEW_DATA:(\[.*\]|\{.*\})", stdout)
    if m:
        try:
            import json as _json
            preview = _json.loads(m.group(1))
        except Exception:
            preview = []

    return {
        "status": "ok" if rows >= 0 else "ok",
        "rows": rows,
        "preview": preview,
        "output_file": report_path,
        "report_url": f"/{report_name}",
        "elapsed": round(time.time() - started, 1),
        "script": code,
        "error": None,
    }


QA_SYSTEM_PROMPT = """你是一位数据分析师。根据用户上传的数据文件的摘要信息，回答用户的自然语言问题。

【回答要求】
1. 基于给出的数据摘要（列名、行数、示例数据、统计信息）作答
2. 如果问题需要具体计算（求和/平均/最大等），基于摘要中能获取的数据估算，并说明"基于示例数据"
3. 中文回答，简洁清晰，必要时给出关键数字
4. 摘要信息不足时，明确说明还缺什么数据

只输出回答内容，不要解释过程。"""


async def _run_qa_task(task_id: str, requirement: str, data_paths: list[str], record: dict) -> None:
    """数据问答：读取上传的 Excel/CSV 摘要 → LLM 回答自然语言问题。"""
    import json as _json
    from app.database import update_mini_task
    from app.services.llm_client import chat_completion

    started = time.time()
    summary: dict = {}
    last_err = ""
    for fp in data_paths or []:
        if not os.path.exists(fp):
            continue
        try:
            if fp.lower().endswith((".xlsx", ".xls")):
                import pandas as pd
                df = pd.read_excel(fp)
            elif fp.lower().endswith(".csv"):
                import pandas as pd
                df = pd.read_csv(fp)
            else:
                continue
            summary = {
                "rows": int(len(df)),
                "columns": list(df.columns),
                "head": df.head(8).fillna("").to_dict(orient="records"),
                "describe": df.describe(include="all").fillna("").to_dict() if len(df) > 0 else {},
            }
            break
        except Exception as e:
            last_err = str(e)[:200]

    if not summary:
        merged = {"status": "failed", "answer": "",
                  "error": f"无法读取上传的数据文件（仅支持 Excel/CSV）: {last_err}",
                  "elapsed": round(time.time() - started, 1)}
        record["result"] = merged
        record["status"] = "done"
        record["message"] = "完成（读取失败）"
        record["progress"] = 100
        await update_mini_task(task_id, status="done", message=record["message"],
                               result=_json.dumps(merged, ensure_ascii=False), error=merged["error"])
        return

    user_prompt = (
        f"【数据摘要】\n{_json.dumps(summary, ensure_ascii=False, default=str)[:4000]}\n\n"
        f"【用户问题】{requirement}"
    )
    try:
        answer = await chat_completion(QA_SYSTEM_PROMPT, user_prompt, temperature=0.3, max_tokens=800)
    except Exception as e:
        answer = f"分析失败: {str(e)[:200]}"

    merged = {
        "status": "ok",
        "answer": answer,
        "rows": summary["rows"],
        "columns": summary["columns"],
        "elapsed": round(time.time() - started, 1),
        "error": None,
    }
    record["result"] = merged
    record["status"] = "done"
    record["message"] = "完成"
    record["progress"] = 100
    await update_mini_task(task_id, status="done", message="完成",
                           result=_json.dumps(merged, ensure_ascii=False), error=None)


async def _run_music_task(task_id: str, requirement: str) -> dict:
    """音乐模式：LLM 作曲脚本 → 本地运行 → WAV 落 web 目录（公网可访问）。"""
    from app.services.llm_client import chat_completion

    started = time.time()
    out_dir = _WEB_DIR
    os.makedirs(out_dir, exist_ok=True)

    # 1. 生成合成脚本（带需求主题）
    code = ""
    for attempt in range(3):
        try:
            code = await chat_completion(
                MUSIC_SYSTEM_PROMPT,
                f"【音乐主题】{requirement}\n请生成合成脚本，保存为 melody.wav",
                temperature=0.4, max_tokens=8000,
            )
            code = code.strip()
            if code.startswith("```python"):
                code = code[9:]
            elif code.startswith("```"):
                code = code[3:]
            if code.endswith("```"):
                code = code[:-3]
            code = code.strip()
            compile(code, "<script>", "exec")
            if "wav" in code:
                break
        except Exception as e:
            logger.warning("音乐脚本生成第 %d 次失败: %s", attempt + 1, str(e)[:120])
            code = ""

    if not code:
        return {"status": "generate_failed", "error": "音乐合成脚本生成失败", "elapsed": round(time.time() - started, 1)}

    # 2. 沙箱执行（隔离环境 + 静态扫描 + 超时/资源限制），产物从沙箱工作区复制到 web/
    from app.sandbox.docker_executor import execute_in_sandbox
    result = await execute_in_sandbox(code, timeout=180, preview_mode=False)
    stdout = result.stdout or ""
    stderr = result.stderr or ""

    if not result.success:
        return {
            "status": "failed",
            "error": f"脚本执行失败: {(stderr or stdout)[-300:]}",
            "elapsed": round(time.time() - started, 1),
            "script": code,
        }

    music_name = f"music_{task_id}.wav"
    music_path = os.path.join(out_dir, music_name)
    if result.output_file_path and os.path.exists(result.output_file_path):
        os.makedirs(out_dir, exist_ok=True)
        import shutil as _shutil
        _shutil.copyfile(result.output_file_path, music_path)
    elif not os.path.exists(music_path):
        return {
            "status": "failed",
            "error": f"未生成 WAV: {(stderr or stdout)[-300:]}",
            "elapsed": round(time.time() - started, 1),
            "script": code,
        }

    return {
        "status": "ok",
        "output_file": music_path,
        "music_url": f"/{music_name}",
        "elapsed": round(time.time() - started, 1),
        "script": code,
        "error": None,
    }


def get_status(task_id: str, user_id: str = "") -> dict | None:
    """查询任务状态；user_id 非空时校验归属（账号数据独立）。"""
    rec = _TASKS.get(task_id)
    if rec is None:
        # 内存没有（如重启后）→ 从 SQLite 恢复（同步查询）
        try:
            import sqlite3
            from app.database import DB_PATH
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM mini_tasks WHERE id=?", (task_id,)).fetchone()
            conn.close()
            if not row:
                return None
            d = dict(row)
            if user_id and d.get("user_id") and d["user_id"] != user_id:
                return None
            if d.get("result"):
                try:
                    d["result"] = json.loads(d["result"])
                except Exception:
                    d["result"] = None
            return {
                "id": d["id"],
                "requirement": d["requirement"],
                "url": d["url"],
                "status": d["status"],
                "progress": 100 if d["status"] == "done" else 0,
                "message": d["message"] or "",
                "created_at": d["created_at"],
                "error": d["error"],
                "result": d["result"],
                "running": False,
            }
        except Exception as e:
            logger.warning("从 DB 恢复任务 %s 失败: %s", task_id, str(e)[:100])
            return None
    if user_id and rec.get("user_id") and rec["user_id"] != user_id:
        return None
    out = {
        "id": rec["id"],
        "requirement": rec["requirement"],
        "url": rec["url"],
        "status": rec["status"],
        "progress": rec["progress"],
        "message": rec["message"],
        "created_at": rec["created_at"],
        "error": rec["error"],
        "result": rec["result"],
        "running": is_running(task_id),
    }
    return out


def list_tasks(limit: int = 20, user_id: str = "") -> list[dict]:
    """任务列表：优先 SQLite（持久化历史），内存 running 任务补充，按 user_id 过滤。"""
    items: list[dict] = []
    try:
        import sqlite3
        from app.database import DB_PATH
        conn = sqlite3.connect(str(DB_PATH))
        conn.row_factory = sqlite3.Row
        if user_id:
            rows = conn.execute(
                "SELECT * FROM mini_tasks WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit * 2),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM mini_tasks ORDER BY created_at DESC LIMIT ?", (limit * 2,)
            ).fetchall()
        conn.close()
        for row in rows:
            d = dict(row)
            items.append({
                "id": d["id"],
                "requirement": (d.get("requirement") or "")[:60],
                "status": d.get("status", ""),
                "message": d.get("message") or "",
                "created_at": d.get("created_at", 0),
            })
    except Exception as e:
        logger.warning("list_mini_tasks DB 失败: %s", str(e)[:100])
    # 补充内存中还在 running 的任务（可能尚未落库完成）
    for tid, rec in _TASKS.items():
        if is_running(tid) and not any(i["id"] == tid for i in items):
            if user_id and rec.get("user_id") != user_id:
                continue
            items.append({
                "id": tid,
                "requirement": (rec.get("requirement") or "")[:60],
                "status": rec.get("status", "running"),
                "message": rec.get("message") or "",
                "created_at": rec.get("created_at", 0),
            })
    return items[:limit]


def cancel_task(task_id: str, user_id: str = "") -> bool:
    """取消任务；user_id 非空时校验归属（账号数据独立）。"""
    if user_id:
        # 先确认任务属于该用户
        rec = _TASKS.get(task_id)
        if rec is None:
            try:
                import sqlite3
                from app.database import DB_PATH
                conn = sqlite3.connect(str(DB_PATH))
                conn.row_factory = sqlite3.Row
                row = conn.execute("SELECT user_id FROM mini_tasks WHERE id=?", (task_id,)).fetchone()
                conn.close()
                if not row:
                    return False
                rec = dict(row)
            except Exception:
                return False
        if rec.get("user_id") and rec["user_id"] != user_id:
            return False
    return cancel(task_id)
