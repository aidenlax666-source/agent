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
import shutil
import sys
import time
import uuid
import logging

from app.config import get_settings
from app.paths import web_root, user_profile_dir
from app.services.long_task import start_background, is_running, cancel
# 模块级引用：测试可替换 chat_completion_json 做 mock（agent 循环/分类等）
from app.services.llm_client import chat_completion_json as chat_completion_json

logger = logging.getLogger("app.services.mini_tasks")

# task_id -> 任务记录
_TASKS: dict[str, dict] = {}

# 保留最近 N 个已完成任务，防止内存无限增长
_MAX_KEPT = 200

# 报告意图关键词：命中则走 HTML 可视化报告模式
_REPORT_HINTS = ["报告", "可视化", "图表", "报表", "dashboard", "报告页"]

# 报告输出目录：默认项目 web/（与静态服务共享，产出即可访问）；
# 云架构多实例可配置 ASSET_WEB_ROOT 指向共享卷，保证产物跨实例可见
_WEB_DIR = web_root()


def _size_mb(path: str) -> str:
    """文件大小人性化显示（MB）。"""
    try:
        return f"{os.path.getsize(path) / 1024 / 1024:.1f}MB"
    except Exception:
        return ""


async def _explain_failure(requirement: str, error: str, output: str = "") -> str:
    """把任务失败的技术错误转成用户能看懂的大白话（可能原因 + 建议操作）。

    用便宜的 chat 模型做"翻译"，失败时降级返回原始错误（不阻塞任务收尾）。
    """
    from app.services.llm_client import chat_completion
    try:
        output_part = ("执行输出片段：\n" + output[:800]) if output else ""
        explain_prompt = (
            f"用户让 AI 助手做这个任务：{requirement[:500]}\n\n"
            f"任务执行失败了，技术错误信息如下：\n{error[:800]}\n"
            f"{output_part}\n\n"
            "请用大白话（中文，非技术用户能懂）解释：1) 大概是什么原因导致失败；"
            "2) 用户可以怎么调整（比如换个说法/检查什么/换种方式）。"
            "控制在 80 字以内，两句话，不要贴代码。如果确实看不出来，就说\"可能是临时问题，请重试\"。"
        )
        text = await chat_completion(
            "你是面向非技术用户的 AI 助手客服，把技术错误翻译成人话。",
            explain_prompt, temperature=0.3, max_tokens=300,
        )
        text = (text or "").strip()
        return text[:300] if text else error
    except Exception:
        return error


def _safe_output_src(path: str) -> bool:
    """校验沙箱产物路径：只允许位于沙箱输出根（backend/tmp）或 web/ 目录内。

    防 H2 任意文件读取：LLM 生成的脚本可打印 [OUTPUT_FILE] 任意存在的绝对路径
    （如 .env、.ssh 密钥），若不加校验会被复制到 web/ 公开目录外泄。
    """
    try:
        resolved = os.path.realpath(str(path))
    except Exception:
        return False
    roots = (
        os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "tmp")),  # 沙箱输出根 backend/tmp
        _WEB_DIR,
    )
    for root in roots:
        r = os.path.normpath(root)
        if resolved == r or resolved.startswith(r + os.sep):
            return True
    return False


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


# 语音合成意图（豆包 TTS）：命中则走文本配音
_TTS_HINTS = ["配音", "语音合成", "语音朗读", "朗读", "念出来", "读出来", "语音播报", "生成语音", "文本转语音", "tts"]


def _is_tts_request(requirement: str) -> bool:
    r = requirement.lower()
    # 排除"看朗读视频"等非合成意图
    if any(k in r for k in ("看朗读", "朗读视频")):
        return False
    # "朗读网页/朗读练习/朗读器" 等是内容制作（HTML），不是配音
    if "朗读" in r and any(k in r for k in ("网页", "页面", "网站", "练习", "工具", "应用", "程序")):
        return False
    return any(k in r for k in _TTS_HINTS)


_TTS_EXTRACT_RE = [
    r"配音\s*[:：]\s*[「\"'“”‘’]?([^」\"'“”‘’]{1,2000})",
    r"朗读\s*[:：]\s*[「\"'“”‘’]?([^」\"'“”‘’]{1,2000})",
    r"朗读[「\"'“”‘’]([^」\"'“”‘’]{1,2000})[」\"'“”‘’]",
    r"[「\"'“”‘’]([^」\"'“”‘’]{1,2000})[」\"'“”‘’]\s*(?:配音|朗读|读出来|念出来)",
]


def _extract_tts_text(requirement: str) -> str:
    """从需求里提取要配音的文本（规则优先，兜底用 LLM）。"""
    import re as _re
    for pat in _TTS_EXTRACT_RE:
        m = _re.search(pat, requirement)
        if m:
            txt = m.group(1).strip()
            if txt:
                return txt
    return ""


# 音色识别：需求关键词 → 音色别名（tts_client.VOICES）
_TTS_VOICE_MAP = [
    (("男声", "男生", "男音", "云舟"), "m191"),
    (("知性灿灿", "灿灿"), "cancan"),
    (("甜美", "小源", "甜美女声"), "xiaoyuan"),
    (("晓荷", "温柔女声", "温柔"), "xiaohe"),
    (("晓田",), "taocheng"),
    (("客服", "暖阳"), "kefunv"),
    (("英文", "英语", "english", "dacey"), "dacey"),
]

# 情绪识别：需求关键词 → 豆包情绪（旁白/讲故事等更精确的词放前面）
_TTS_EMOTION_MAP = [
    (("旁白", "解说"), "narrator"),
    (("讲故事", "故事"), "storytelling"),
    (("开心", "高兴", "欢快", "愉快"), "happy"),
    (("悲伤", "难过", "忧伤"), "sad"),
    (("生气", "愤怒"), "angry"),
]


def _extract_tts_voice(requirement: str) -> str:
    """从需求里识别音色（如"用男声配音"→ m191）。"""
    r = requirement.lower()
    for keys, voice in _TTS_VOICE_MAP:
        if any(k in r for k in keys):
            return voice
    return ""


def _extract_tts_emotion(requirement: str) -> str:
    """从需求里识别情绪（如"开心地朗读"→ happy）。"""
    for keys, e in _TTS_EMOTION_MAP:
        if any(k in requirement for k in keys):
            return e
    return ""


async def _extract_tts_text_llm(requirement: str) -> str:
    """LLM 提取配音文本（规则未命中时兜底）。"""
    try:
        from app.services.llm_client import chat_completion_json
        info = await chat_completion_json(
            "你是文本提取助手。从用户需求中提取【需要语音朗读的原文】，只返回 JSON {\"text\":\"原文\"}；"
            "如果需求本身就是要朗读的内容（如\"朗读今天天气很好\"），返回该内容去掉指令词后的文本。",
            requirement[:2000], max_tokens=300)
        return str(info.get("text") or "").strip()[:3000]
    except Exception:
        return ""


def _is_content_request(requirement: str) -> bool:
    r = requirement.lower()
    if (_is_game_request(r) or _is_report_request(r) or _is_video_request(r)
            or _is_image_request(r) or _is_music_request(r) or _is_tts_request(r)):
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

【代码完整性（必须遵守）】
- 脚本必须**自包含**：所有辅助函数（如 create_pie_chart/generate_html 等）必须在同一脚本内先完整定义再调用，禁止引用未定义函数
- 生成 HTML 时所有用到的变量必须先赋值再使用
- 数值变量（width/height/坐标/尺寸等）必须用数字类型（int/float），禁止用字符串参与运算
- 大段 HTML 模板**避免用 f-string**（极易因变量未定义报 KeyError），用三引号字符串 + .replace()/.format() 或普通拼接
- 执行前在脑中走查一遍，确保无 NameError/变量未定义/缩进错误

只输出完整 Python 代码，不要解释。"""


REPORT_HEAL_PROMPT = """你是一位 Python 修复专家。用户需求是生成可视化报告脚本，脚本运行报错。请根据报错修复：

1. 先看报错定位问题（NameError/KeyError/TypeError/未定义函数等）
2. 常见原因：f-string 引用了未定义变量（改普通拼接）、数值变量是字符串（转 int/float）、辅助函数未定义或顺序错误
3. 保持 def run_task()/main() 或主流程结构，保持 report.html 输出
4. 直接输出修复后的完整 Python 代码，不要解释"""


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
        info = await chat_completion_json(QA_ROUTER_PROMPT, requirement[:1000], max_tokens=20)
        return "qa" if info.get("type") == "qa" else "task"
    except Exception:
        return "task"


def _json_list(v) -> list:
    """把 DB 里的 JSON 字符串列安全解码为 list（容错：None/坏 JSON/非 list）。"""
    if not v:
        return []
    try:
        val = json.loads(v) if isinstance(v, str) else v
        return val if isinstance(val, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def submit(requirement: str, url: str | None = None, user_id: str = "", image_paths: list[str] | None = None,
           data_paths: list[str] | None = None, skip_run: bool = False,
           schedule: dict | None = None, priority: str = "normal") -> dict:
    """提交一个后台任务，立即返回任务信息（持久化到 SQLite，重启不丢）。

    统一入口：LLM 自动判断"数据问答"还是"任务执行"，无需用户选择。
    skip_run=True：只落库不执行（用于提醒/监控等"设置型"任务，由调度器驱动）。
    schedule={type,value}：提交时直接设置定时重跑（随 INSERT 一次写入，无竞态）。
    priority=normal|high：高优任务（用户主动提交/紧急迭代）先进高优队列。
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
    if isinstance(schedule, dict) and schedule.get("type") in ("interval", "daily"):
        stype, sval = schedule["type"], str(schedule.get("value") or "")
        if stype == "interval":
            try:
                ival = max(1, int(sval))
            except (ValueError, TypeError):
                ival = 60
            record["schedule_type"] = "interval"
            record["schedule_value"] = str(ival)
            record["enabled"] = 1
            record["next_run_at"] = time.time() + ival * 60
        else:
            from datetime import datetime as _dt, timedelta as _td
            try:
                hh, mm = (int(x) for x in sval.split(":"))
                target = _dt.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
                if target <= _dt.now():
                    target += _td(days=1)
                record["schedule_type"] = "daily"
                record["schedule_value"] = sval
                record["enabled"] = 1
                record["next_run_at"] = target.timestamp()
            except Exception:
                pass
    _TASKS[task_id] = record

    # 清理超出上限的旧任务
    if len(_TASKS) > _MAX_KEPT:
        for tid in list(_TASKS.keys())[: len(_TASKS) - _MAX_KEPT]:
            if not is_running(tid):
                _TASKS.pop(tid, None)

    if skip_run:
        record["status"] = "done"
        record["message"] = "已设置"
        record["progress"] = 100
        # 落库（同步写）：提醒/监控的"设置型"记录持久化，重启不丢
        try:
            from app.database import _save_mini_task
            _save_mini_task(record)
        except Exception as e:
            logger.warning("[mini:%s] skip_run 落库失败: %s", task_id, str(e)[:100])
        return record
    # ---- 分布式队列模式（云架构）：任务进 Redis 队列，worker 消费执行 ----
    # 有 Redis：先落库（worker 从 DB 重建执行上下文），再入队；任一失败回退单机进程内
    from app.services import distributed as _dist
    if _dist.redis_enabled():
        try:
            from app.database import _save_mini_task
            _save_mini_task(record)  # 持久化：worker 或本实例崩溃后任务不丢
            if _dist.enqueue_task(task_id, priority=priority):
                logger.info("[mini:%s] 已入分布式队列（%s）", task_id, priority)
                return record
            logger.warning("[mini:%s] 入队失败，回退单机执行", task_id)
        except Exception as e:
            logger.warning("[mini:%s] 队列模式异常，回退单机执行: %s", task_id, str(e)[:100])
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
    if user_id and rec.get("user_id") != user_id:  # 空 user_id 也不放行（防越权）
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
        _hold_task(loop.create_task(set_mini_schedule(task_id, schedule_type, schedule_value, enabled, next_run)))
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
# 自动化意图解析（定时提醒 / 循环执行 / 监控触发）——纯正则，零 LLM 成本
# ============================================================

def parse_automation(requirement: str) -> dict:
    """从自然语言需求解析自动化意图，返回:
    {"kind": "task"|"reminder"|"monitor",
     "reminders": [{"time": "HH:MM", "text": "..."}],
     "monitor": {"type": "window"|"screen", "keywords": "...", "condition": "...",
                 "action_requirement": "...", "check_interval": int},
     "schedule": {"type": "interval"|"daily", "value": ...} | None}
    """
    req = (requirement or "").strip()
    out: dict = {"kind": "task", "reminders": [], "monitor": None, "schedule": None}
    if not req:
        return out

    # ---- 1) 定时提醒：每天X点(分)提醒我Y ----
    reminders = []
    pat = re.compile(r"(?:每天|每日|天天)?\s*(\d{1,2})\s*[点时:：]\s*(?:(\d{1,2})\s*分?)?\s*提醒(?:我)?\s*([^，,。；;]+)")
    for m in pat.finditer(req):
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            text = (m.group(3) or "").strip()
            if text and "每隔" not in text[:6]:
                reminders.append({"time": f"{hh:02d}:{mm:02d}", "text": text})
    if reminders and "提醒" in req:
        out["kind"] = "reminder"
        out["reminders"] = reminders

    # ---- 2) 监控任务：监控屏幕 / 当打开XX时 ----
    monitor = None
    # 2a) 屏幕监控：监控屏幕[，]当[画面/出现]X时Y
    m_screen = re.search(r"监控(?:屏幕|电脑屏幕|显示器)[，,]?(?:当|如果)?(?:画面|屏幕|出现)?(.{0,30}?)(?:时|就)(?:[，,]?)(.+)", req)
    if m_screen:
        monitor = {
            "type": "screen",
            "keywords": "",
            "condition": (m_screen.group(1) or "").strip()[:60],
            "action_requirement": (m_screen.group(2) or "").strip(),
            "check_interval": 30,
        }
    # 2b) 软件/窗口监控：当打开/启动XX时Y
    m_win = re.search(r"(?:当|如果|一旦)?(?:我)?(?:打开|开启|启动|运行|切到)([^，,。；;]{1,30}?)(?:时|后)(?:[，,]?)(.+)", req)
    if not monitor and m_win:
        monitor = {
            "type": "window",
            "keywords": (m_win.group(1) or "").strip(),
            "condition": "",
            "action_requirement": (m_win.group(2) or "").strip(),
            "check_interval": 20,
        }
    if monitor and ("监控" in req or "时" in req or "打开" in req or "启动" in req):
        # 混合意图：已有提醒时不覆盖（提醒优先，create_task 会同时处理）
        if not reminders:
            out["kind"] = "monitor"
        out["monitor"] = monitor

    # ---- 3) 循环执行：每隔N分钟 / 每天X点执行 ----
    schedule = None
    m_int = re.search(r"每隔\s*(\d+)\s*分钟", req)
    if m_int:
        val = int(m_int.group(1))
        if val > 0:
            schedule = {"type": "interval", "value": min(val, 24 * 60)}
    else:
        m_daily = re.search(r"每天\s*(\d{1,2})\s*[点时:：]\s*(?:(\d{1,2})\s*分?)?\s*(?:执行|跑|运行|查看|更新)", req)
        if m_daily:
            hh, mm = int(m_daily.group(1)), int(m_daily.group(2) or 0)
            if 0 <= hh <= 23 and 0 <= mm <= 59:
                schedule = {"type": "daily", "value": f"{hh:02d}:{mm:02d}"}
    if schedule and out["kind"] == "task":
        out["kind"] = "task"  # 普通任务 + 定时重跑
        out["schedule"] = schedule
    return out


# 提醒/监控触发去重（进程内：同一提醒项同一分钟只发一次；同一监控关键词 5 分钟冷却）
_fire_memory: dict[str, float] = {}
# 后台 Task 引用池：防止 fire-and-forget 的 create_task 被 GC 中断
_held_tasks: set = set()


def _hold_task(t: "asyncio.Task") -> None:
    _held_tasks.add(t)
    t.add_done_callback(_held_tasks.discard)


async def _fire_reminder(reminder: dict, user_id: str) -> None:
    """定时提醒到点 → 写站内通知（去重：同 id 同分钟只发一次；先写库成功再记去重 key，失败可重试）。"""
    from app.database import add_notification
    from app.services import distributed
    key = f"rem:{reminder['id']}:{time.strftime('%Y%m%d%H%M')}"
    # 分布式去重（有 Redis 跨实例生效；无 Redis 回退进程内）
    if not distributed.dedup_mark(key, ttl_seconds=120):
        return
    try:
        await add_notification(user_id, "⏰ 定时提醒",
                               f"{reminder.get('time', '')} - {reminder.get('text', '')}")
    except Exception as e:
        distributed.dedup_clear(key)  # 写库失败：清除去重标记，下轮重试
        logger.warning("[提醒] %s 写库失败（本次不记去重，下轮重试）: %s", user_id[:8], str(e)[:120])
        return
    logger.info("[提醒] %s 触发: %s", user_id[:8], reminder.get("text", ""))


def _window_titles() -> list[str]:
    """枚举当前可见窗口标题（Windows，零依赖 ctypes）。"""
    import ctypes
    titles: list[str] = []
    try:
        user32 = ctypes.windll.user32
        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lp):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    t = (buf.value or "").strip()
                    if t:
                        titles.append(t)
            return True

        user32.EnumWindows(enum_proc(_cb), 0)
    except Exception:
        pass
    return titles


def _screen_hash() -> str:
    """截屏并计算感知哈希（16x16 灰度 → md5），用于画面变化检测。"""
    from PIL import ImageGrab
    img = ImageGrab.grab()
    small = img.convert("L").resize((16, 16))
    import hashlib
    return hashlib.md5(small.tobytes()).hexdigest()


async def _run_monitor_check(monitor: dict) -> None:
    """执行一次监控检查：条件满足 → 通知 +（可选）执行动作任务。

    无论成败都会推进 last_checked_at（防失败后每 30 秒重试风暴）。
    """
    from app.database import add_notification as _add_note, update_monitor_state
    mid = monitor["id"]
    user_id = monitor["user_id"]
    mtype = monitor.get("monitor_type", "window")
    keywords = (monitor.get("keywords") or "").strip()
    condition = (monitor.get("condition") or "").strip()
    action_req = (monitor.get("action_requirement") or "").strip()

    now = time.time()
    fired: list[str] = []
    state_updated = False
    from app.services import distributed as _dist
    try:
        if mtype == "window":
            titles = await asyncio.to_thread(_window_titles)  # ctypes 同步调用不阻塞事件循环
            joined = " ".join(titles).lower()
            for kw in [k for k in keywords.replace("，", ",").split(",") if k.strip()]:
                kw = kw.strip().lower()
                if kw and kw in joined:
                    key = f"mon:{mid}:{kw}"
                    # 5 分钟冷却（分布式：跨实例只触发一次）
                    if _dist.dedup_mark(key, ttl_seconds=300):
                        fired.append(kw)
            await update_monitor_state(mid, now, monitor.get("last_state") or "")
            state_updated = True
        else:  # screen
            cur = await asyncio.to_thread(_screen_hash)  # Pillow 全屏截图不阻塞事件循环
            prev = monitor.get("last_state") or ""
            changed = bool(prev) and cur != prev
            if "静止" in condition or "没变化" in condition or "不动" in condition:
                m_min = re.search(r"(\d+)\s*分钟", condition)
                mins = int(m_min.group(1)) if m_min else 5
                key = f"screen:{mid}"
                if changed:
                    _dist.dedup_clear(key)  # 画面动了，重置计时
                else:
                    # 开始计时 / 到点触发：用带 TTL 的去重标记（跨实例一致）
                    if _dist.dedup_mark(key, ttl_seconds=int(mins * 60)):
                        # 首次标记 = 开始计时，不触发；需等待 TTL 过期后再次 mark 才到点
                        pass
                    else:
                        # TTL 过期后再次进入 = 已静止 mins 分钟 → 触发一次并重新计时
                        fired.append(f"屏幕静止超过 {mins} 分钟")
                        _dist.dedup_clear(key)
            else:  # 默认：画面变化触发（冷却 60s）
                if changed:
                    key = f"mon:{mid}:change"
                    if _dist.dedup_mark(key, ttl_seconds=60):
                        fired.append("屏幕画面发生变化")
            await update_monitor_state(mid, now, cur)
            state_updated = True
    except Exception as e:
        logger.warning("[监控] %s 检查异常: %s", mid[:8], str(e)[:150])
    finally:
        # 无论成败都推进 last_checked_at（异常路径保持 last_state 不变，screen 不覆盖已更新的哈希）
        if not state_updated:
            try:
                await update_monitor_state(mid, now, monitor.get("last_state") or "")
            except Exception:
                pass

    for what in fired:
        try:
            await _add_note(user_id, "👁️ 监控触发",
                            f"{what} → {action_req or '（仅提醒）'}")
        except Exception as e:
            logger.warning("[监控] %s 通知失败: %s", mid[:8], str(e)[:120])
        logger.info("[监控] %s 触发: %s", user_id[:8], what)
        # "提醒/通知我xxx"语义 = 仅通知；其余作为动作任务执行（本地使用不扣积分）
        notify_only = (not action_req or action_req in ("仅提醒", "提醒", "通知")
                       or action_req.startswith(("提醒", "通知")))
        if action_req and not notify_only:
            submit(action_req, None, user_id)



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


async def _run_scheduled(mtask: dict) -> None:
    """执行一次定时重跑（注册在稳定 key mini_sched:{tid} 下，运行期间调度器跳过，防重叠并发）。"""
    user_id = mtask.get("user_id") or ""
    submit(mtask.get("requirement") or "", mtask.get("url") or None, user_id,
           image_paths=_json_list(mtask.get("image_paths")),
           data_paths=_json_list(mtask.get("data_paths")))


async def mini_scheduler_loop() -> None:
    """调度循环：每 30 秒检查一次到期的 mini 定时任务、定时提醒、监控任务；每天清理超期产物。

    30 秒间隔保证提醒（1 分钟窗口）不会因整分钟边界错过。
    """
    logger.info("[mini调度器] 启动，每 30 秒检查一次")
    _last_cleanup_day = -1
    while True:
        try:
            import time as _t
            from app.database import (claim_mini_run, get_due_mini_tasks, list_reminders,
                                      list_monitors, get_mini_task)
            # 1) 定时任务重跑（注册在稳定 key 下，运行期间不重复触发）
            due = await get_due_mini_tasks(_t.time())
            for mtask in due:
                tid = mtask["id"]
                key = f"mini_sched:{tid}"
                if is_running(key):
                    continue
                now_ts = _t.time()
                nxt = _next_mini_run(mtask, now_ts)
                # 原子抢占：只在 next_run_at 仍是到期值时更新（多 worker 也不会重复执行）
                if not await claim_mini_run(tid, now_ts, nxt):
                    continue
                start_background(key, _run_scheduled(mtask))
                logger.info(f"[mini调度器] 定时触发: {tid[:8]} - {(mtask.get('requirement') or '')[:40]}")

            # 2) 定时提醒：当前 HH:MM 匹配 → 发通知（去重，同一分钟只发一次）
            now_local = time.localtime()
            cur_hhmm = f"{now_local.tm_hour:02d}:{now_local.tm_min:02d}"
            try:
                rems = await list_reminders("*", enabled_only=True)
                for r in rems:
                    if str(r.get("time", "")).strip() == cur_hhmm:
                        await _fire_reminder(r, r.get("user_id", ""))
            except Exception as e:
                logger.warning("[调度器] 提醒检查异常: %s", str(e)[:120])

            # 3) 监控任务：到期检查（截屏/窗口），检查逻辑放后台协程避免阻塞
            try:
                mons = await list_monitors("*", enabled_only=True)
                for m in mons:
                    interval = max(5, int(m.get("check_interval") or 60))
                    last = float(m.get("last_checked_at") or 0)
                    if _t.time() - last >= interval:
                        key = f"monitor:{m['id']}"
                        if is_running(key):
                            continue
                        start_background(key, _run_monitor_check(m))
            except Exception as e:
                logger.warning("[调度器] 监控检查调度异常: %s", str(e)[:120])

            # 每天清理一次超期产物（配置 asset_cleanup_days>0 时启用）
            day = int(_t.time() // 86400)
            if day != _last_cleanup_day:
                _last_cleanup_day = day
                _cleanup_assets()
        except Exception as e:
            logger.error(f"[mini调度器] 检查异常: {e}")
        await asyncio.sleep(30)


def _cleanup_assets() -> None:
    """清理超过 asset_cleanup_days 天的 web/ 产物与 uploads/ 上传文件（防磁盘无限增长）。

    只删除已知产物前缀的文件（report_/game_/video_/image_/music_/tts_/dev_/content_/auto_output_），
    避免误删 index.html/manga.html 等演示资产。
    """
    try:
        days = get_settings().asset_cleanup_days
        if not days or days <= 0:
            return
        cutoff = time.time() - days * 86400
        removed = 0
        _PRODUCT_PREFIXES = ("report_", "game_", "video_", "image_", "music_", "tts_",
                             "dev_", "content_", "auto_output_")
        for d in (_WEB_DIR, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend", "uploads")):
            d = os.path.normpath(d)
            if not os.path.isdir(d):
                continue
            for name in os.listdir(d):
                p = os.path.join(d, name)
                if not os.path.isfile(p):
                    continue
                if d == _WEB_DIR and not name.lower().startswith(_PRODUCT_PREFIXES):
                    continue  # web/ 只清产物前缀文件，保留演示资产
                try:
                    if os.path.getmtime(p) < cutoff:
                        os.unlink(p)
                        removed += 1
                except Exception:
                    pass
        # 沙箱稳定产物目录（auto_output_*/sandbox_output_*）+ 后台运行保留目录（dev_api_*）：
        # 产物已复制到 web/，运行进程结束后的旧目录按 mtime 清超期
        _tmp_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "tmp"))
        if os.path.isdir(_tmp_dir):
            for name in os.listdir(_tmp_dir):
                p = os.path.join(_tmp_dir, name)
                if not os.path.isdir(p):
                    continue
                if not (name.startswith("auto_output_") or name.startswith("sandbox_output_")
                        or name.startswith("dev_api_")):
                    continue
                try:
                    if os.path.getmtime(p) < cutoff:
                        shutil.rmtree(p, ignore_errors=True)
                        removed += 1
                except Exception:
                    pass
        if removed:
            logger.info("[清理] 已删除 %d 个超期产物/目录（保留 %d 天）", removed, days)
    except Exception as e:
        logger.warning("[清理] 失败: %s", str(e)[:120])


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
    if user_id and rec.get("user_id") != user_id:  # 空 user_id 也不放行（防越权）
        return None
    try:
        from app.database import update_mini_task
        _hold_task(asyncio.create_task(update_mini_task(task_id, status="confirmed", message="已确认")))
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
    if user_id and rec.get("user_id") != user_id:  # 空 user_id 也不放行（防越权）
        return None
    if is_running(task_id):
        return {"id": task_id, "status": "running", "error": "任务正在执行中，请等待完成"}

    feedback = (feedback or "").strip()
    if not feedback:
        return {"id": task_id, "status": "error", "error": "反馈不能为空"}

    base_req = rec.get("requirement") or ""
    new_req = f"{base_req}（用户修改意见：{feedback}）"
    url = rec.get("url") or ""

    # 更新内存记录 + 重置状态（保留原图片/数据路径）
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
        "image_paths": _json_list(rec.get("image_paths")),
        "data_paths": _json_list(rec.get("data_paths")),
    }
    started = start_background(task_id, _run_task(task_id, new_req, url, _TASKS[task_id]))
    return {"id": task_id, "status": "queued" if started else "error", "message": "迭代修改已提交"}


async def _extract_memory(requirement: str, user_id: str, result_summary: str = "") -> None:
    """任务成功后，从需求+结果中提取用户偏好/习惯存入长期记忆（零成本正则优先，LLM 兜底）。

    提取规则（简单可靠，不打断任务）：
    - 需求里带"我喜欢/我喜欢用/习惯/偏好/每次都/记得"等 → 直接记下
    - 明确格式偏好（如"要 Excel""用中文""带图表"）→ 记下
    """
    if not user_id:
        return
    from app.database import remember
    req = requirement or ""
    candidates: list[str] = []
    # 1. 显式偏好表达（只取偏好短语本身，截断到第一个标点）
    for kw in ("我喜欢", "我爱用", "习惯", "偏好", "每次都", "记得我", "我一般"):
        idx = req.find(kw)
        if idx >= 0:
            tail = req[idx + len(kw):].strip("，。,.！？ ")
            # 截断到第一个标点/动词边界，只保留偏好短语
            import re as _re2
            m = _re2.match(r"([^，。,.！？；;]{1,20})", tail)
            tail = m.group(1) if m else tail
            if tail and len(tail) >= 2:
                candidates.append(tail)
    # 2. 明确的格式/语言偏好
    for pat, memo in (
        (r"要?excel|导出excel|xlsx", "输出用 Excel 格式"),
        (r"要?csv|导出csv", "输出用 CSV 格式"),
        (r"用中文", "用中文输出"),
        (r"带?图表|加图表", "输出带图表"),
        (r"要?word|docx", "输出用 Word 格式"),
        (r"英文输出|用英文", "用英文输出"),
    ):
        if re.search(pat, req, re.IGNORECASE):
            candidates.append(memo)
    # 3. 去重后入库（最多记 3 条，防噪音）
    seen: set[str] = set()
    for c in candidates:
        c = c[:80]
        if c and c not in seen:
            seen.add(c)
            try:
                await remember(user_id, "preference", c)
            except Exception:
                pass
        if len(seen) >= 3:
            break


# ============================================================
# Agent 循环模式（复杂任务）：模型多轮自主决策 run/write/finish
# 普通模式 = 单轮"编译"（便宜，适合大多数任务）；Agent 模式 = 多轮动态决策
# 成本控制：默认关闭，仅需求明显复杂或用户显式要求时启用；每轮用 chat 模型
# ============================================================

_AGENT_HINT_WORDS = [
    "调研", "对比", "比较", "多步", "多个", "分别", "依次", "然后", "接着", "再",
    "搜集", "整理成", "汇总", "综合", "分步", "循环", "逐", "每家", "每个", "全部",
    "agent", "多轮", "自主", "复杂",
]


def _needs_agent(requirement: str) -> bool:
    """判断需求是否需要 agent 循环：命中多个复杂意图词，或用户显式要求 agent/多轮。"""
    req = (requirement or "").lower()
    if "agent" in req or "多轮" in req or "自主" in req or "复杂任务" in req:
        return True
    hits = sum(1 for w in _AGENT_HINT_WORDS if w in req)
    return hits >= 3  # 命中 ≥3 个复杂意图词才启用（防误伤普通任务）


async def _run_agent_task(task_id: str, requirement: str, record: dict,
                          max_rounds: int = 8) -> dict:
    """Agent 循环：模型自主决定每步动作，后端执行并回填结果，直到 finish 或超轮次。

    每轮模型输出 JSON（action 必填）：
      {"action": "run",   "cmd": "shell 命令"}                     → 在独立工作区执行
      {"action": "write", "file": "相对路径", "content": "内容"}   → 写入工作区文件
      {"action": "finish", "summary": "...", "output_file": "相对路径"}
    run/write 的结果会拼回上下文，让模型看到实际输出再决定下一步。
    """
    import tempfile as _tf

    workspace = _tf.mkdtemp(prefix="agent_ws_", dir=os.path.join(os.path.dirname(__file__), "..", "..", "tmp"))
    history: list[str] = []
    final: dict = {"status": "ok", "summary": "", "output_file": "", "steps": []}
    last_cmd_output = ""
    for round_no in range(1, max_rounds + 1):
        record["message"] = f"Agent 第 {round_no}/{max_rounds} 轮..."
        record["progress"] = min(95, int(90 * round_no / max_rounds))
        await update_mini_task(task_id, status="running", message=record["message"])

        _history_text = "\n".join(history[-12:]) if history else "(无)"
        _last_out = (last_cmd_output or "(无)")[:2000]
        prompt = (
            "你是一个自主执行的 AI Agent，目标是**尽快完成任务**（最多 8 轮，轮次宝贵）。用户需求：\n"
            f"{requirement}\n\n"
            "每轮只输出一个 JSON 动作：\n"
            "1. {\"action\": \"write\", \"file\": \"相对路径\", \"content\": \"文件完整内容\"} —— 创建脚本/文件\n"
            "2. {\"action\": \"run\", \"cmd\": \"命令\"} —— 执行脚本/命令（如 python x.py）\n"
            "3. {\"action\": \"finish\", \"summary\": \"完成说明\", \"output_file\": \"最终产物相对路径(可空)\"} —— 任务完成，**一旦得到结果立即 finish**\n\n"
            "执行策略：**最多 2-3 轮**完成——write 一个脚本 → run 一次 → 看结果立即 finish。\n"
            "不要做环境探测（python --version、pwd 之类）——环境已就绪，直接写业务脚本。\n"
            "**脚本保持精简**：单文件 ≤150 行，内容完整但不要写多余注释/空行；超长逻辑拆多个文件分步写。\n"
            "如果 run 报错，修脚本再 run 一次，然后 finish。\n"
            f"已完成的动作：\n{_history_text}\n"
            f"最近一次命令输出：\n{_last_out}"
        )
        try:
            info = await chat_completion_json(
                prompt, requirement, temperature=0.2, max_tokens=20000,
                model=get_settings().ai_model,
            )
        except Exception as e:
            history.append(f"[第{round_no}轮] 模型调用失败: {str(e)[:100]}")
            continue
        action = str(info.get("action") or "").strip().lower()

        if action == "run":
            cmd = str(info.get("cmd") or "").strip()
            if cmd:
                err = _check_dev_command_safety(cmd)
                if err:
                    last_cmd_output = f"命令被安全拦截: {err}"
                else:
                    r = await asyncio.to_thread(_run_dev_command, workspace, cmd, timeout=120)
                    last_cmd_output = r.get("output") or r.get("error") or "(无输出)"
                    if not r.get("ok"):
                        last_cmd_output = f"执行失败：{last_cmd_output}"
                    elif r.get("ok"):
                        # run 成功且工作区出现产物文件 → 自动收尾（不用等模型 finish）
                        out_files = [f for f in os.listdir(workspace)
                                     if os.path.isfile(os.path.join(workspace, f))
                                     and os.path.splitext(f)[1].lower() in
                                     (".txt", ".csv", ".xlsx", ".json", ".html", ".png", ".pdf", ".md", ".log")]
                        if out_files:
                            _pick = sorted(out_files, key=lambda f: os.path.getmtime(os.path.join(workspace, f)))[-1]
                            final["summary"] = f"命令执行成功，产物: {_pick}"
                            _fname = f"agent_{task_id}_{_pick}"
                            _dst = os.path.join(_WEB_DIR, _fname)
                            os.makedirs(_WEB_DIR, exist_ok=True)
                            shutil.copyfile(os.path.join(workspace, _pick), _dst)
                            final["output_file"] = _dst
                            final["content_url"] = f"/{_fname}"
                            history.append(f"[第{round_no}轮] run: {cmd}")
                            history.append(f"[第{round_no}轮] 检测到产物 {_pick}，自动完成")
                            final["steps"] = history
                            final["last_cmd"] = cmd
                            break
                history.append(f"[第{round_no}轮] run: {cmd}")
        elif action == "write":
            rel = str(info.get("file") or "").strip()
            content = str(info.get("content") or "")
            safe = _safe_dev_rel(rel, workspace)
            if safe is None:
                last_cmd_output = f"非法路径: {rel}"
            else:
                p = os.path.join(workspace, safe)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(content)
                last_cmd_output = f"已写入 {safe} ({len(content)} 字符)"
                history.append(f"[第{round_no}轮] write: {safe}")
        elif action == "finish":
            final["summary"] = str(info.get("summary") or "")[:300]
            out_rel = str(info.get("output_file") or "").strip()
            if out_rel:
                src = os.path.join(workspace, out_rel)
                if os.path.isfile(src) and _safe_output_src(src):
                    # 复制到 web/ 发布（安全校验：必须来自 agent 工作区）
                    _fname = f"agent_{task_id}_{os.path.basename(out_rel)}"
                    _dst = os.path.join(_WEB_DIR, _fname)
                    os.makedirs(_WEB_DIR, exist_ok=True)
                    shutil.copyfile(src, _dst)
                    final["output_file"] = _dst
                    final["content_url"] = f"/{_fname}"
                else:
                    final["summary"] += f"（提示：产物 {out_rel} 不存在或路径非法，未发布）"
            final["steps"] = history
            break
        else:
            last_cmd_output = f"未知动作: {action}（应为 run/write/finish）"
            history.append(f"[第{round_no}轮] 无效动作: {action}")
    else:
        final["summary"] = (final.get("summary") or "") + f"（已达 {max_rounds} 轮上限，自动结束）"
        final["steps"] = history

    # 清理工作区（产物已复制到 web/）
    try:
        shutil.rmtree(workspace, ignore_errors=True)
    except Exception:
        pass
    if not final.get("output_file") and not final.get("summary"):
        final["status"] = "failed"
        final["error"] = "Agent 未产出任何结果"
        final["error_human"] = "AI 在复杂模式下没有完成目标，可以换个更具体的说法重试，或改用普通模式。"
    return final



def _task_record_from_db(task_id: str) -> dict | None:
    """从 SQLite 重建任务执行上下文（worker 消费队列时用，进程重启不丢）。"""
    try:
        from app.database import _get_mini_task
        rec = _get_mini_task(task_id)
        if not rec:
            return None
        # 转成 _run_task 需要的 record 形态
        return {
            "id": rec["id"],
            "user_id": rec.get("user_id") or "",
            "requirement": rec.get("requirement") or "",
            "url": rec.get("url") or "",
            "status": "queued",
            "progress": 0,
            "message": "排队中",
            "result": rec.get("result"),
            "error": None,
            "image_paths": _json_list(rec.get("image_paths")),
            "data_paths": _json_list(rec.get("data_paths")),
        }
    except Exception as e:
        logger.warning("[worker] 重建任务 %s 上下文失败: %s", task_id[:8], str(e)[:100])
        return None


async def distributed_worker_loop(poll_seconds: float = 1.0) -> None:
    """分布式任务 worker：从 Redis 队列 BRPOP 取任务并执行（云架构多 worker 自动分发）。

    每个后端实例可启动一个 worker；多个 worker 从同一队列竞争消费（BRPOP 天然互斥）。
    任务执行期间续租约（心跳），防租约过期被其他 worker 抢走；
    worker 崩溃时：任务已在 SQLite 持久化，可重新入队恢复（任务不丢）。
    无 Redis 时不启动（单机模式走进程内 asyncio）。
    """
    from app.services import distributed as _dist
    logger.info("[worker] 分布式任务 worker 启动（监听队列）")

    async def _heartbeat(task_id: str, ttl: int = 1800):
        """长任务心跳：每 30s 续一次租约，防过期被抢。"""
        while True:
            await asyncio.sleep(30)
            _dist.renew_lock(f"task-run:{task_id}", ttl_seconds=ttl)

    # worker 自身心跳（可观测性）：有任务时随循环刷新，空闲时也保活
    while True:
        try:
            _dist.worker_heartbeat()
            task_id = _dist.dequeue_task(timeout=poll_seconds)
            if not task_id:
                continue
            # 领取执行租约：防多 worker 同时执行同一任务（BRPOP 已互斥，双保险）
            if not _dist.claim_task_lease(task_id, ttl_seconds=1800):
                continue
            record = _task_record_from_db(task_id)
            if record is None:
                _dist.release_task_lease(task_id)
                continue
            requirement = record["requirement"]
            url = record.get("url") or ""
            hb = asyncio.create_task(_heartbeat(task_id))
            try:
                await _run_task(task_id, requirement, url, record)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("[worker] 任务 %s 执行异常: %s", task_id[:8], str(e)[:120])
            finally:
                hb.cancel()
                try:
                    await hb
                except (asyncio.CancelledError, Exception):
                    pass
                _dist.release_task_lease(task_id)
        except asyncio.CancelledError:
            logger.info("[worker] 分布式 worker 已停止")
            raise
        except Exception as e:
            logger.warning("[worker] 循环异常（继续）: %s", str(e)[:120])
            await asyncio.sleep(2)


# 任务重试上限：超过则标记死信（失败），不再自动重试
MAX_TASK_RETRIES = 3
# reaper 判定"任务失联"的阈值：updated_at 超过该秒数未变（租约 TTL 1800s，取稍大值）
STALE_TASK_AGE = 2400


async def distributed_reaper_loop(poll_seconds: float = 60.0) -> None:
    """崩溃恢复循环（云架构）：自动找回失联任务并重新入队。

    场景：worker BRPOP 取出任务 → 领取租约 → 执行中崩溃。此时任务已不在队列
    （BRPOP 取出即移出），租约 TTL 过期后无人接管 → 任务卡死。
    reaper 周期扫描：status 仍为 queued/running 且 updated_at 很久未变的任务，
    若其租约已过期且不在队列 → 判定失联 → retry_count+1 重新入队；
    重试超限 → 标记死信（用户可见失败原因），不再无限重试。
    """
    from app.services import distributed as _dist
    from app.database import get_stale_mini_tasks, bump_retry_count, mark_task_dead
    logger.info("[reaper] 崩溃恢复循环启动（每 %ss 扫描一次）", poll_seconds)
    while True:
        try:
            stale = await get_stale_mini_tasks(older_than=time.time() - STALE_TASK_AGE)
            for rec in stale:
                tid = rec["id"]
                # 租约仍存活 → 有 worker 正在执行，正常
                if _dist.task_lease_alive(tid):
                    continue
                # 仍在队列中 → 只是排队久，正常
                if _dist.task_in_queue(tid):
                    continue
                retries = int(rec.get("retry_count") or 0)
                if retries >= MAX_TASK_RETRIES:
                    await mark_task_dead(tid, f"任务多次执行失败（worker 崩溃 {retries} 次），已停止自动重试")
                    logger.warning("[reaper] 任务 %s 重试超限，标记死信", tid[:8])
                    continue
                new_count = await bump_retry_count(tid)
                if _dist.enqueue_task(tid):
                    logger.info("[reaper] 任务 %s 失联已重新入队（第 %d/%d 次重试）", tid[:8], new_count, MAX_TASK_RETRIES)
        except asyncio.CancelledError:
            logger.info("[reaper] 崩溃恢复循环已停止")
            raise
        except Exception as e:
            logger.warning("[reaper] 扫描异常（继续）: %s", str(e)[:120])
        await asyncio.sleep(poll_seconds)


async def _run_task(task_id: str, requirement: str, url: str | None, record: dict) -> None:
    """后台执行一个任务（联机游戏/报告/内容/视频/图片/音乐/代码任务，可组合多模式），同步 SQLite。"""
    from app.database import save_mini_task, update_mini_task, get_memory

    record["status"] = "running"
    record["message"] = "正在执行..."
    image_paths = record.get("image_paths") or []
    data_paths = record.get("data_paths") or []

    # ---- 记忆注入：把该用户长期记忆（偏好/习惯/事实）拼进需求，让 LLM 按用户习惯执行 ----
    user_id = record.get("user_id") or ""
    if user_id:
        try:
            memories = await get_memory(user_id, limit=15)
            if memories:
                mem_text = "；".join(f"{m.get('content', '')}" for m in memories)
                requirement = f"{requirement}\n\n【用户偏好记忆】{mem_text}\n（如果需求与此冲突，以用户本次明确的说法为准；无冲突则默认按记忆执行）"
        except Exception:
            pass

    # ---- 统一入口：LLM 判断"数据问答"还是"任务执行" ----
    intent = await _classify_intent(requirement, bool(data_paths))
    if intent == "qa":
        record["message"] = "识别为数据问答，AI 正在分析..."
        await save_mini_task(record)  # QA 任务也落库（否则重启即丢）
        await update_mini_task(task_id, status="running", message=record["message"])
        await _run_qa_task(task_id, requirement, data_paths, record)
        return

    # ---- 技能任务（视频剪辑/CAD 制图等）：一律走 code 模式（生成脚本执行），不被"画/视频"等词误判为 image/video 模式 ----
    try:
        from app.skills import select_skill
        skill = select_skill(requirement)
    except Exception:
        skill = None

    # ---- Agent 循环模式（复杂任务）：模型多轮自主决策；普通模式（默认）走单轮编译 ----
    # 需求命中复杂意图（或含 agent/多轮/自主）→ 走 agent 循环；技能任务不走 agent
    use_agent = _needs_agent(requirement) and not skill
    if use_agent:
        try:
            await save_mini_task(record)
            record["message"] = "检测到复杂需求，使用 Agent 多轮模式..."
            await update_mini_task(task_id, status="running", message=record["message"])
            agent_result = await _run_agent_task(task_id, requirement, record)
            record["result"] = agent_result
            record["status"] = "done" if agent_result.get("status") == "ok" else "error"
            record["message"] = "完成" if agent_result.get("status") == "ok" else "Agent 未完成"
            record["progress"] = 100
            if user_id and agent_result.get("status") == "ok":
                try:
                    await _extract_memory(requirement, user_id)
                except Exception:
                    pass
            await update_mini_task(task_id, status=record["status"], message=record["message"],
                                   result=json.dumps(agent_result, ensure_ascii=False),
                                   error=None if agent_result.get("status") == "ok" else agent_result.get("error"))
            return
        except Exception as e:
            logger.exception("[mini:%s] agent 模式失败，回退普通模式", task_id)

    if skill:
        modes = ["code"]
        record["message"] = f"技能: {skill['name']}，开始执行..."
    else:
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
        if _is_tts_request(requirement):
            modes.append("tts")
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
        steps: list[str] = []  # 过程可见性：记录每个执行步骤（前端实时展示）
        _MODE_LABEL = {"game": "生成联机游戏", "report": "生成可视化报告", "content": "生成内容作品",
                       "video": "生成视频", "image": "生成图片", "music": "生成音乐", "tts": "生成语音", "code": "执行代码任务"}
        for mode in modes:
            steps.append(_MODE_LABEL.get(mode, mode))
            record["steps"] = steps
            record["progress"] = min(95, 10 + int(85 * len(steps) / max(1, len(modes))))
            record["message"] = f"正在{_MODE_LABEL.get(mode, mode)}..."
            await update_mini_task(task_id, status="running", message=record["message"])
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
            elif mode == "tts":
                # 豆包 TTS 语音合成：提取配音文本 → 合成 MP3 → web/ 目录
                from app.services.tts_client import tts_speak
                text = _extract_tts_text(requirement)
                if not text:
                    text = await _extract_tts_text_llm(requirement)
                if not text:
                    failed.append("配音: 无法从需求中提取要朗读的文本")
                else:
                    os.makedirs(_WEB_DIR, exist_ok=True)
                    fname = f"tts_{task_id}.mp3"
                    save_path = os.path.join(_WEB_DIR, fname)
                    # 从需求识别音色与情绪（如"用男声开心地朗读…"）
                    voice = _extract_tts_voice(requirement)
                    emotion = _extract_tts_emotion(requirement)
                    tr = await tts_speak(text, save_path, voice=voice or None, emotion=emotion or None)
                    if tr.get("success"):
                        merged["tts_url"] = f"/{fname}"
                        merged["output_file"] = save_path
                        note = f"（{voice or '默认音色'}{'/' + emotion if emotion else ''}）"
                        merged["message_tts"] = f"配音已生成：/{fname}{note}"
                    else:
                        failed.append(f"配音: {tr.get('error', '失败')}")
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
                rs = result.get("status")
                if rs == "ok":
                    pass
                elif rs in ("no_data", "login_required", "robots_blocked"):
                    # 业务结果（无符合条件的数据 / 需登录 / 禁止抓取）：如实告知用户，不算执行失败
                    merged["status"] = rs
                    hint = {
                        "no_data": "没有符合条件的数据（筛选/抓取结果为空，可能条件过严或站点无此类数据）",
                        "login_required": "目标网站需要登录，请先在「登录态」功能保存登录状态后重试",
                        "robots_blocked": "目标网站禁止抓取",
                    }.get(rs, rs)
                    merged["error"] = result.get("error") or hint
                else:
                    failed.append(f"数据处理: {result.get('error') or rs}")
                # 技能任务产物发布（视频/音频/CAD 等）：从沙箱临时目录复制到 web/ 供预览/下载
                if rs == "ok" and result.get("output_file") and os.path.exists(str(result["output_file"])):
                    _src = str(result["output_file"])
                    # 安全校验：产物必须来自沙箱输出目录（防 [OUTPUT_FILE] 指向任意文件被外泄）
                    if _safe_output_src(_src) and not os.path.normpath(_src).startswith(os.path.normpath(_WEB_DIR)):
                        try:
                            _ext = os.path.splitext(_src)[1].lower() or ".bin"
                            _fname = f"content_{task_id}{_ext}"
                            _dst = os.path.join(_WEB_DIR, _fname)
                            os.makedirs(_WEB_DIR, exist_ok=True)
                            shutil.copyfile(_src, _dst)
                            merged["output_file"] = _dst
                            merged["content_url"] = f"/{_fname}"
                            merged["message_file"] = f"文件已生成：/{_fname}（{_size_mb(_dst)}）"
                        except Exception as e:
                            logger.warning("[mini:%s] 产物发布失败: %s", task_id, str(e)[:100])

        if failed and not any(merged.get(k) for k in ("game_url", "report_url", "content_url", "video_url", "image_url", "image_urls", "music_url", "tts_url", "dev_diff")):
            merged["status"] = "failed"
            merged["error"] = "；".join(failed)[:300]
            # 把技术错误翻译成用户能看懂的大白话（降级：失败也不影响收尾）
            try:
                merged["error_human"] = await _explain_failure(
                    requirement, merged["error"], (record.get("result") or {}).get("stdout", "") if isinstance(record.get("result"), dict) else "")
            except Exception:
                merged["error_human"] = merged["error"]
        elif merged.get("status") in ("no_data", "login_required", "robots_blocked"):
            pass  # 保留业务状态（无数据/需登录/禁止抓取），前端有对应友好展示
        else:
            merged["status"] = "ok"
            if failed:
                merged["partial_errors"] = failed

        record["result"] = merged
        if steps:
            merged["steps"] = steps  # 过程日志随结果返回（前端展示）
            merged["message_final"] = "完成" + ("（部分失败，见详情）" if failed else "")
        record["status"] = "done"
        record["message"] = "完成"
        record["progress"] = 100
        # 任务成功 → 提取用户偏好进长期记忆（供后续任务使用）
        if user_id and merged.get("status") == "ok":
            try:
                await _extract_memory(requirement, user_id)
            except Exception:
                pass
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
        # 大白话解释（降级：失败不影响收尾）
        try:
            record["error_human"] = await _explain_failure(requirement, str(e)[:300])
        except Exception:
            record["error_human"] = record["message"]
        _res = dict(record.get("result") or {})
        _res["error_human"] = record.get("error_human", "")
        await update_mini_task(task_id, status="error", error=record["error"], message=record["message"],
                               result=json.dumps(_res, ensure_ascii=False))


# ============================================================
# 开发类任务：外部代码目录（API/CLI 上传解压的隔离副本）→ AI 改码 → 校验 → diff
# 用 DeepSeek 驱动，隔离副本保证安全，产出 diff 供用户确认/应用
# ============================================================

# 遍历时排除的目录（大/无关）
DEV_EXCLUDE_DIRS = {"node_modules", ".next", "__pycache__", ".git", "web", "data", "tmp",
                    "uploads", "browser_profile", "screens", "benchmark"}

DEV_MODIFY_PROMPT = """你是一位资深软件工程师。用户需求：{requirement}

【表达原则（贯穿全文）】信息完整前提下用最精炼的表达：能用一句话说清就不用两句话，能短句就不用长句——但绝不能为了简短而丢失必要信息。
以下是项目源码（文件路径 + 内容）：
{context}
{plan_part}

根据需求类型选择执行方式（可以组合）：
1. **写代码/创建文件**：修改或新增文件 → 填 `files`
2. **运行/测试/启动**：执行命令（如 "python main.py"、"pytest"、"pip install -r requirements.txt"）→ 填 `command`
   - 普通命令（测试/一次性脚本）：直接填 command
   - **启动服务/长驻进程**（如 "python app.py"、"npm run dev" 这类不会自己结束的命令）：
     填 `command` 且 `background=true`，系统会后台启动并持续运行
   - **注意**：需求只要涉及"启动/运行/安装依赖/测试"就必须填 command（不能只写 summary 描述而不执行）
3. **分析代码**：用户要你解释/审查/找问题（不写文件、不执行）→ 填 `analysis`（详细的分析结论文本）
4. 如果先改代码再运行测试（如"修复测试"），files 和 command 一起填

【输出格式（最重要，违反即失败）】
只输出一个 JSON 对象，除此之外**不允许输出任何内容**：
{{"patch": {{"相对路径": ["diff 行", "..."]}} 或 "patch": "unified diff 字符串",
  "files": {{"相对路径": "该文件的完整新内容（仅新增文件用）"}},
  "summary": "一句话说明你做了什么",
  "command": "要执行的命令（可为空）", "background": false,
  "analysis": "分析结论（纯文本，可为空）"}}
禁止输出：markdown 围栏（```）、目录树（├──/│）、任何解释/说明文字。JSON 必须完整闭合。
patch / files / command / analysis 至少填一个。

【patch 格式（修改已有文件时优先用 patch，省 token；新增文件用 files 给完整内容）】
**推荐数组形式**（每个元素是一行 diff，无需转义），例如改 main.py 第 1-3 行：
"patch": {{"main.py": ["@@ -1,3 +1,4 @@", " print('hello')", "-print('old')", "+print('new')"]}}
- hunk 头：@@ -旧起始行,旧行数 +新起始行,新行数 @@；行号从 1 开始，必须与源码真实行号一致
- 上下文行（前面一个空格）必须与源码逐字一致；删除行前加 -；新增行前加 +
- 只输出改动位置附近的几行（上下文 1-3 行 + 改动行），**绝不输出整个文件**
- 一次可给多个文件（每个文件一个 key）；一个文件可多个 hunk（数组里连续排列）

【严格要求】
1. files 里只列出**新增**文件（完整内容）；已有文件的修改一律用 patch（绝不用 files 重写整个文件）
2. 每个文件内容必须**完整且精炼**：只保留真实需要的代码，禁止输出未修改的大段原样内容
3. command 必须是可在项目目录执行的简单命令（python/npm/pip/pytest 等），不要用删除/格式化等危险命令
4. background=true 只用于不会自己结束的长驻服务（web 服务器等）
5. 改动必须真实可用：import、函数定义、调用关系完整
6. 中文 summary 一句话说明你做了什么（尽量短）
"""

# 第一步：只读代码、出修改方案（不写文件）——让用户确认后再动手
DEV_PLAN_PROMPT = """你是一位资深软件工程师。用户需求：{requirement}

【表达原则（贯穿全文）】信息完整前提下用最精炼的表达：能用一句话说清就不用两句话，能短句就不用长句——但绝不能为了简短而丢失必要信息。
以下是项目源码（文件路径 + 内容）：
{context}

请基于上述真实源码制定**修改方案**（只分析和规划，不要动手改代码），输出 JSON：
{{"plan": "详细方案：要改哪些文件、每个文件怎么改、实现思路、涉及的风险或影响",
  "files": ["将要修改/新增的文件相对路径", ...],
  "questions": ["需要用户确认或决定的事项（有则列出，没有则空数组）"]}}

【要求】
1. 方案具体到文件级别：每个文件改什么、加什么
2. 指出潜在风险和影响（比如会动公共函数、影响其他调用方）
3. 如果需求有多种实现方式，在 questions 里让用户选择
4. 中文；**方案精炼**：只说必要的改动和关键风险，不要长篇分析（控制输出量）
"""

DEV_VALIDATE_PROMPT = """你是软件工程师。下面是上一个 AI 按需求 {requirement} 改的代码，但校验报错：
{errors}

涉及文件当前内容（可据此精确定位行号）：
{files_context}

请修复：只输出 JSON。已有文件的修改用 {{"patch": {{"相对路径": ["@@ -旧起始行,旧行数 +新起始行,新行数 @@", " 上下文行（空格开头，与上面内容一致）", "-删除行", "+新增行"]}}}}，
新增文件用 {{"files": {{"相对路径": "完整内容"}}}}。
只列出需要修改的文件（其他文件不用重复输出）。行号必须与上面的内容一致，只输出改动附近的几行，不要重写整个文件。"""


# 两阶段上下文第 1 阶段：只给文件清单，让模型挑要读的文件（省去无关文件全文的输入 token）
DEV_SELECT_PROMPT = """你是一位资深软件工程师。用户需求：{requirement}
{plan_part}
以下是项目**全部文件的清单**（路径 + 大小 + 首行预览，不是完整内容）：
{index}

要正确完成修改，你需要先决定读哪些文件的完整内容。只输出一个 JSON 对象：
{{"files_to_read": ["需要读取完整内容的文件相对路径", ...],
  "grep": ["要全文搜索的符号/关键词（如函数名、变量名，用于定位调用点/定义点；不需要可省略）", ...]}}

选择原则：
1. **必选**：本次要修改/新增的文件，以及修改会直接影响的文件（被 import 的模块、被模板引用的文件、依赖的配置）
2. 拿不准哪些文件涉及某个符号时，**用 grep 搜索它**（如搜函数名找到所有调用点），grep 命中的文件会自动读入
3. 与需求明显无关的文件（如其它模块的测试、文档）不要选
4. 如果这是空项目（清单为空）或项目极小，files_to_read 可为空数组——你会基于需求从零创建
禁止输出任何其他内容（不要 markdown 围栏、不要解释）。"""


# 关键词打分时忽略的常见词（英文 + 中文 2-gram）
_DEV_STOPWORDS = {
    "the", "and", "for", "with", "from", "this", "that", "project", "code",
    "file", "files", "add", "new", "make", "function", "in", "to", "of", "a",
    "an", "is", "are", "be", "on", "it", "as", "by", "at", "or", "not", "my",
    "we", "our", "需要", "项目", "代码", "文件", "功能", "函数", "增加",
    "添加", "一个", "这个", "那个", "修改", "改成", "实现", "并且", "然后",
}


def _requirement_keywords(requirement: str, limit: int = 24) -> set[str]:
    """从需求提取关键词：ASCII 单词 + 中文 2-gram（用于相关文件打分，零成本）。"""
    import re
    words: set[str] = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{1,}", (requirement or "").lower()):
        if tok not in _DEV_STOPWORDS:
            words.add(tok)
    cn = [c for c in (requirement or "") if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(cn) - 1):
        bigram = cn[i] + cn[i + 1]
        if bigram not in _DEV_STOPWORDS:
            words.add(bigram)
    return set(list(words)[:limit])


def _dev_file_index(workspace: str, files: list[str], max_items: int = 200,
                    preview: int = 100) -> str:
    """极简文件清单：路径 + 字符数 + 首行预览（每文件 ≤ preview+20 字符）。

    供两阶段上下文的第 1 阶段：模型先看清单决定要读哪些文件（省去无关文件全文的输入 token）。
    """
    parts: list[str] = []
    for rel in sorted(files)[:max_items]:
        try:
            with open(os.path.join(workspace, rel), encoding="utf-8-sig") as f:
                content = f.read()
        except Exception:
            continue
        first = content.splitlines()[0].strip() if content.splitlines() else ""
        if len(first) > preview:
            first = first[:preview] + "..."
        parts.append(f"=== {rel} ({len(content)} 字符) === {first}")
    if not parts:
        return "(空项目目录：没有任何文件。请根据需求从零创建所需的项目文件结构。)"
    return "\n".join(parts)


def _dev_read_files(workspace: str, rels: list[str], per_file: int = 4000,
                    total_cap: int = 40000) -> str:
    """读取指定文件的完整内容（带截断），供两阶段上下文的第 2 阶段。

    只读模型选中的文件；读取失败（二进制/编码问题）跳过。
    """
    parts: list[str] = []
    total = 0
    for rel in rels or []:
        safe = _safe_dev_rel(rel, workspace)
        if safe is None:
            continue
        try:
            with open(os.path.join(workspace, safe), encoding="utf-8-sig") as f:
                content = f.read()
        except Exception:
            continue
        if len(content) > per_file:
            body = content[:per_file] + "\n...(内容过长已截断)"
        else:
            body = content
        block = f"=== {safe} ===\n{body}"
        if total + len(block) > total_cap:
            parts.append("...(已读文件上下文达上限，其余省略)")
            break
        parts.append(block)
        total += len(block)
    if not parts:
        return "(未能读取任何文件)"
    return "\n\n".join(parts)


def _dev_grep(workspace: str, files: list[str], queries: list[str],
              max_hits_per_query: int = 30, preview: int = 120) -> tuple[str, list[str]]:
    """在工作区文件里搜索（不区分大小写的子串匹配，类 grep）。

    返回 (结果文本, 命中文件列表)。结果文本含 文件:行号: 行内容 前缀；
    命中文件列表供调用方并入读取清单（找调用点/定义点）。
    """
    if not queries:
        return "", []
    results: list[str] = []
    hit_files: set[str] = set()
    qs = [str(q).strip().lower() for q in queries if str(q).strip()]
    if not qs:
        return "", []
    for rel in sorted(files):
        try:
            with open(os.path.join(workspace, rel), encoding="utf-8-sig") as f:
                lines = f.read().splitlines()
        except Exception:
            continue
        for lineno, line in enumerate(lines, 1):
            low = line.lower()
            if any(q in low for q in qs):
                shown = line.strip()
                if len(shown) > preview:
                    shown = shown[:preview] + "..."
                results.append(f"{rel}:{lineno}: {shown}")
                hit_files.add(rel)
                if len(results) >= max_hits_per_query * max(1, len(qs)):
                    break
    if not results:
        return "(grep 无结果)", []
    text = "【grep 搜索结果】\n" + "\n".join(results[:200])
    return text, sorted(hit_files)


def _dev_context(workspace: str, files: list[str], requirement: str | None = None,
                 max_items: int = 80, per_file: int = 4000, total_cap: int = 40000) -> str:
    """文件树 + 每个文件的内容（让模型看到真实代码而非只看到文件名）。

    上下文精简：requirement 给出时，按关键词给文件打分——
    相关文件（文件名/开头命中需求关键词）给完整内容，其余文件只给前 300 字符摘要，
    既大幅省 token（低成本），又不会漏掉任何文件名。
    每个文件内容截断到 per_file 字符；总上下文上限 total_cap 字符。
    读取失败（二进制/编码问题）的文件跳过。
    """
    kws = _requirement_keywords(requirement[:1500]) if requirement else set()
    entries: list[tuple[str, str, int]] = []
    for rel in sorted(files)[:max_items]:
        try:
            with open(os.path.join(workspace, rel), encoding="utf-8-sig") as f:
                content = f.read()
        except Exception:
            continue
        score = 0
        if kws:
            hay = (rel + " " + content[:600]).lower()
            for k in kws:
                if k in hay:
                    score += 1
                if k in rel.lower():
                    score += 3
        entries.append((rel, content, score))
    entries.sort(key=lambda e: -e[2])  # 相关文件在前
    parts: list[str] = []
    total = 0
    for rel, content, score in entries:
        if kws and score == 0:
            body = content[:300] + ("\n...(仅摘要)" if len(content) > 300 else "")
        elif len(content) > per_file:
            body = content[:per_file] + "\n...(内容过长已截断)"
        else:
            body = content
        block = f"=== {rel} ===\n{body}"
        if total + len(block) > total_cap:
            parts.append("...(上下文已达上限，其余文件省略)")
            break
        parts.append(block)
        total += len(block)
    if not parts:
        return "(空项目目录：没有任何文件。请根据需求从零创建所需的项目文件结构。)"
    return "\n\n".join(parts)


def _safe_dev_rel(rel: str, workspace: str) -> str | None:
    """校验 LLM 返回的文件相对路径：拒绝绝对路径/盘符/穿越段，且必须落在 workspace 内。

    返回规范化后的相对路径；非法返回 None（调用方应跳过该文件并记错误）。
    """
    if not isinstance(rel, str) or not rel.strip():
        return None
    rel = rel.strip().replace("\\", "/").lstrip("/")
    if re.match(r"^[A-Za-z]:", rel):
        return None
    if ".." in rel.split("/"):
        return None
    ws = os.path.normpath(workspace)
    norm = os.path.normpath(os.path.join(ws, rel))
    if norm != ws and not norm.startswith(ws + os.sep):
        return None
    return rel


def _dev_validate(files_map: dict, workspace: str) -> list[str]:
    """写盘所有改动文件（含非 .py：模板/样式/数据等，patch 应用需要读到磁盘原文件），
    并对 .py 文件做语法校验，返回错误列表（空=通过）。"""
    errors = []
    for rel, content in (files_map or {}).items():
        safe = _safe_dev_rel(rel, workspace)
        if safe is None:
            errors.append(f"{rel}: 非法路径（不允许越出项目目录）")
            continue
        p = os.path.join(workspace, safe)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            if rel.endswith(".py"):
                compile(content, safe, "exec")
        except SyntaxError as e:
            errors.append(f"{safe}: 语法错误 line {e.lineno}: {e.msg}")
        except Exception as e:
            errors.append(f"{safe}: {str(e)[:120]}")
    return errors


def _dev_build_diff(files_map: dict, workspace: str, orig_contents: dict) -> str:
    """生成统一 diff 文本（改动文件 + 新内容；orig_contents 里有的是修改，否则为新增）。"""
    lines = []
    for rel, content in (files_map or {}).items():
        exists = rel in orig_contents
        lines.append(f"--- a/{rel}")
        lines.append(f"+++ b/{rel}  {'(新增文件)' if not exists else '(修改文件)'}")
        if not exists:
            body = content.splitlines()
        else:
            old = (orig_contents.get(rel) or "").splitlines()
            added = len(content.splitlines()) - len(old)
            lines.append(f"# 改动行数: +{max(added, 0)} 行" + ("" if added >= 0 else f" -{-added} 行"))
            body = content.splitlines()
        for i, ln in enumerate(body, 1):
            lines.append(f"{i:5d} | {ln}")
        lines.append("")
    return "\n".join(lines)


def _find_hunk_pos(lines: list[str], old_seq: list[str], hint_start: int) -> int | None:
    """在 lines 中定位 old_seq 的起始位置（0-based）。优先按行号验证，行号对不上时用 difflib 内容匹配。

    容忍模型行号算错：先试 hint_start-1 处是否精确匹配；不行就在全文中找最长公共块推导位置，
    再用 ratio 验证相似度足够才接受（防误匹配）。
    """
    import difflib as _dl
    if not old_seq:
        return max(0, min(hint_start - 1, len(lines)))  # 纯新增：插入在行号处
    idx = hint_start - 1
    if 0 <= idx <= len(lines) - len(old_seq) and lines[idx:idx + len(old_seq)] == old_seq:
        return idx
    sm = _dl.SequenceMatcher(None, lines, old_seq, autojunk=False)
    best_a, best_b, best_n = -1, -1, 0
    for block in sm.get_matching_blocks():
        if block.size > best_n:
            best_a, best_b, best_n = block.a, block.b, block.size
    if best_n < max(1, int(len(old_seq) * 0.5)):
        return None  # 最长公共块太小，不可信
    start = best_a - best_b
    if start < 0 or start + len(old_seq) > len(lines):
        return None
    seg = lines[start:start + len(old_seq)]
    if _dl.SequenceMatcher(None, seg, old_seq, autojunk=False).ratio() < 0.6:
        return None
    return start


def _dev_apply_patch(patch_text: str, workspace: str) -> tuple[dict, list[str]]:
    """把模型输出的 diff（patch）应用到工作区文件，返回 (files_map, 错误列表)。

    patch 支持两种形态（模型输出数组形式最稳，无需转义）：
    A. 字符串 unified diff（标准 git diff 子集）：
        --- a/相对路径
        +++ b/相对路径
        @@ -旧行号,旧行数 +新行号,新行数 @@
         上下文行（空格开头，必须与源码一致）
        -删除行
        +新增行
    B. 数组形式（推荐，每行一个字符串元素，无转义问题）：
        "patch": {"相对路径": ["@@ -1,3 +1,4 @@", " print('hello')", "-print('old')", "+print('new')"]}
    新文件用 files 字段给完整内容（patch 不表达新文件）。
    定位策略：优先按行号验证，行号对不上时内容模糊匹配（容忍模型行号算错）。
    """
    import difflib as _dl

    files_map: dict = {}
    errors: list[str] = []
    # ---- 1. 归一化 patch 为按文件分组的 diff 行 ----
    file_diffs: dict[str, list[str]] = {}
    if isinstance(patch_text, dict):
        # 数组形式：{"相对路径": [diff 行...]}
        for rel, rows in patch_text.items():
            if isinstance(rows, str):
                rows = rows.splitlines()
            if isinstance(rows, list):
                file_diffs[str(rel)] = [str(r) for r in rows]
    else:
        # 字符串 unified diff：按 --- / +++ 文件头切分
        cur_rel: str | None = None
        cur_lines: list[str] = []
        for ln in (patch_text or "").splitlines():
            m = re.match(r"^---\s+(?:a/)?(.+)$", ln)
            m2 = re.match(r"^\+\+\+\s+(?:b/)?(.+)$", ln)
            if m:
                if cur_rel and cur_lines:
                    file_diffs.setdefault(cur_rel, []).extend(cur_lines)
                rel = m.group(1).strip()
                cur_rel = None if rel == "/dev/null" else rel
                cur_lines = [ln]
            elif m2:
                rel = m2.group(1).strip()
                if rel != "/dev/null" and cur_rel is None:
                    cur_rel = rel
                cur_lines.append(ln)
            elif cur_rel is not None:
                cur_lines.append(ln)
        if cur_rel and cur_lines:
            file_diffs.setdefault(cur_rel, []).extend(cur_lines)

    # ---- 2. 逐文件解析 hunk 并应用 ----
    for rel, diff_lines in file_diffs.items():
        hunks: list[dict] = []
        cur_hunk: dict | None = None
        for ln in diff_lines:
            m = re.match(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@", ln)
            if m:
                cur_hunk = {"old_start": int(m.group(1)), "old": [], "new": []}
                hunks.append(cur_hunk)
                continue
            if cur_hunk is not None:
                if ln.startswith(" "):
                    cur_hunk["old"].append(ln[1:])
                    cur_hunk["new"].append(ln[1:])
                elif ln.startswith("-"):
                    cur_hunk["old"].append(ln[1:])
                elif ln.startswith("+"):
                    cur_hunk["new"].append(ln[1:])
                # 其他（---/+++/\ No newline 等）忽略
        if not hunks:
            errors.append(f"patch 中文件 {rel} 没有有效 hunk")
            continue
        safe = _safe_dev_rel(rel, workspace)
        if safe is None:
            errors.append(f"patch 中非法路径: {rel}")
            continue
        fp = os.path.join(workspace, safe)
        if os.path.isfile(fp):
            try:
                with open(fp, encoding="utf-8-sig") as f:
                    old_text = f.read()
            except Exception as e:
                errors.append(f"{safe}: 读取原文件失败 {str(e)[:80]}")
                continue
            old_lines = old_text.splitlines()
        else:
            old_lines = []  # 新文件（patch 中无上下文，纯 + 行）
        new_lines = list(old_lines)
        ok = True
        for hunk in hunks:
            if not old_lines:
                # 新文件：直接把所有 + 行作为内容（跳过定位）
                new_lines.extend(hunk["new"])
                continue
            pos = _find_hunk_pos(new_lines, hunk["old"], hunk["old_start"])
            if pos is None:
                errors.append(f"{safe}: 第 {hunk['old_start']} 行附近找不到匹配内容（patch 与源码不一致）")
                ok = False
                break
            new_lines = new_lines[:pos] + hunk["new"] + new_lines[pos + len(hunk["old"]):]
        if not ok:
            continue
        if not old_lines and new_lines:
            files_map[safe] = "\n".join(new_lines) + "\n"  # 新文件补结尾换行
            continue
        trailing = "\n" if (os.path.isfile(fp) and old_text.endswith("\n")) else ""
        files_map[safe] = "\n".join(new_lines) + trailing
    return files_map, errors


def _dev_errors_context(errors: list[str], workspace: str, max_chars: int = 6000) -> str:
    """从校验错误提取涉及的文件，返回其当前内容（供 reasoner 精确定位 patch 行号）。"""
    parts: list[str] = []
    seen: set[str] = set()
    for err in (errors or []):
        # 错误格式: "相对路径: 语法错误 line N: msg" 或 "相对路径: xxx"
        rel = str(err).split(":", 1)[0].strip()
        if not rel or rel in seen:
            continue
        safe = _safe_dev_rel(rel, workspace)
        if safe is None:
            continue  # 非法路径：绝不越出项目目录读取
        seen.add(safe)
        p = os.path.join(workspace, safe)
        try:
            with open(p, encoding="utf-8-sig") as f:
                content = f.read()
        except Exception:
            continue
        parts.append(f"### {safe}\n{content[:max_chars]}" + ("\n...(截断)" if len(content) > max_chars else ""))
    return "\n\n".join(parts) if parts else "(无法读取文件内容)"


def _dev_files_from_info(info: dict, workspace: str) -> tuple[dict, list[str]]:
    """从模型 JSON 提取文件改动：patch（unified diff，省 token）+ files（完整内容）合并。

    返回 (files_map, 错误列表)。patch 应用失败时 errors 非空（调用方应回退让模型改用 files）。
    """
    files_map: dict = {}
    errors: list[str] = []
    patch_raw = info.get("patch") or ""
    if patch_raw:
        patched, perr = _dev_apply_patch(patch_raw, workspace)
        if perr:
            errors.extend(perr)
        else:
            files_map.update(patched)
    for rel, content in (info.get("files") or {}).items():
        if isinstance(rel, str) and isinstance(content, str):
            files_map[rel] = content
    return files_map, errors


_DEV_SHELL_BAD = [";", "|", ">", "<", "`", "$(", "rm ", "del ", "rmdir", "format ",
                  "shutdown", "taskkill", "格式化", "删除全部", "rd /s"]


def _check_dev_command_safety(command: str) -> str | None:
    """校验命令安全性：拒绝危险命令前缀与危险 shell 元字符。返回错误信息或 None。

    Windows cmd 分隔符是 &（等价 bash 的 ;），必须拦截单 &，但放行 &&（步骤串联）。
    """
    low = (command or "").lower()
    for bad in ("rm ", "del ", "rmdir", "format ", "shutdown", "taskkill", "格式化", "删除全部", "rd /s"):
        if low.startswith(bad):
            return f"命令被安全拦截（危险操作）: {command[:80]}"
    for ch in _DEV_SHELL_BAD:
        if ch in (command or ""):
            return f"命令包含禁止的 shell 字符（{ch}）: {command[:80]}"
    # 拦截单 &（cmd 分隔符）：去掉 && 后若还有 & 则拒绝
    stripped = (command or "").replace("&&", "")
    if "&" in stripped:
        return f"命令包含禁止的 shell 字符（&）: {command[:80]}"
    return None


def _split_command_steps(command: str) -> list[str]:
    """把命令按 && 拆成多条依次执行（不拆 ||，避免隐藏失败）。"""
    return [s.strip() for s in (command or "").split("&&") if s.strip()]


def _run_dev_command_background(workspace: str, command: str, probe_seconds: int = 6) -> dict:
    """后台启动长驻命令（web 服务器等），探测进程存活后返回，不等待结束。

    支持 && 串联（拆分为多条：安装依赖等前置步骤等待完成，最后的长驻命令后台运行）；
    Windows 上用 cmd shell 执行（npm/pip 等 .cmd 包装器需要）。
    """
    import shlex
    import subprocess as _sp
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": False, "error": "空命令"}
    err = _check_dev_command_safety(cmd)
    if err:
        return {"ok": False, "error": err}
    steps = _split_command_steps(cmd)
    log_path = os.path.join(workspace, f".dev_run_{uuid.uuid4().hex[:8]}.log")
    procs: list = []
    try:
        logf = open(log_path, "w", encoding="utf-8")
        for i, step in enumerate(steps):
            if os.name == "nt":
                proc = _sp.Popen(step, shell=True, cwd=workspace, stdout=logf, stderr=_sp.STDOUT,
                                 encoding="utf-8", errors="replace", creationflags=0x08000000)
            else:
                proc = _sp.Popen(shlex.split(step), cwd=workspace, stdout=logf, stderr=_sp.STDOUT,
                                 encoding="utf-8", errors="replace")
            procs.append(proc)
            if i < len(steps) - 1:
                proc.wait(timeout=300)
                if proc.returncode != 0:
                    logf.close()
                    with open(log_path, encoding="utf-8", errors="replace") as lf:
                        out = lf.read()
                    return {"ok": False, "exit_code": proc.returncode,
                            "output": (out or f"前置步骤失败: {step[:100]}")[:4000]}
        import time as _t
        _t.sleep(probe_seconds)
        last = procs[-1]
        if last.poll() is not None:
            logf.close()
            with open(log_path, encoding="utf-8", errors="replace") as lf:
                out = lf.read()
            try:
                os.unlink(log_path)
            except OSError:
                pass
            return {"ok": False, "exit_code": last.returncode, "output": (out or "(无输出)")[:4000]}
        logf.close()
        return {"ok": True, "pid": last.pid, "output": f"进程已启动（日志: {os.path.basename(log_path)}）"}
    except _sp.TimeoutExpired:
        try:
            logf.close()
        except Exception:
            pass
        return {"ok": False, "error": "前置步骤执行超时（300s）"}
    except FileNotFoundError as e:
        return {"ok": False, "error": f"命令不存在: {e}"}
    except Exception as e:
        return {"ok": False, "error": f"启动失败: {str(e)[:120]}"}


def _run_dev_command(workspace: str, command: str, timeout: int = 300) -> dict:
    """在项目目录执行模型返回的命令（操作型需求：启动/运行/测试/安装依赖）。

    安全：支持 && 串联（拆分成多条依次执行，任一失败即停）、危险命令/字符黑名单、
    超时限制、输出截断。Windows 上用 cmd shell 执行（npm/pip 等 .cmd 包装器需要）。
    """
    import shlex
    import subprocess as _sp
    cmd = (command or "").strip()
    if not cmd:
        return {"ok": True, "output": ""}
    err = _check_dev_command_safety(cmd)
    if err:
        return {"ok": False, "error": err}
    steps = _split_command_steps(cmd)
    outputs: list[str] = []
    for step in steps:
        try:
            if os.name == "nt":
                r = _sp.run(step, shell=True, cwd=workspace, capture_output=True, text=True,
                            timeout=timeout, encoding="utf-8", errors="replace")
            else:
                args = shlex.split(step)
                r = _sp.run(args, cwd=workspace, capture_output=True, text=True,
                            timeout=timeout, encoding="utf-8", errors="replace")
        except _sp.TimeoutExpired:
            return {"ok": False, "error": f"命令执行超时（{timeout}s）: {step[:100]}"}
        except FileNotFoundError as e:
            return {"ok": False, "error": f"命令不存在: {e}"}
        except Exception as e:
            return {"ok": False, "error": f"命令执行失败: {str(e)[:120]}"}
        out = (r.stdout or "") + ("\n" + r.stderr if r.stderr else "")
        outputs.append("$ " + step + "\n" + out.strip())
        if r.returncode != 0:
            joined = "\n".join(outputs)
            return {"ok": False, "exit_code": r.returncode, "output": joined[:8000],
                    "error": f"命令失败（退出码 {r.returncode}）: {step[:100]}"}
    joined = "\n".join(outputs)
    return {"ok": True, "exit_code": 0, "output": joined[:8000]}


async def _run_dev_task(task_id: str, requirement: str, code_dir: str, plan: str | None = None,
                        feedback: str | None = None) -> dict:
    """开发任务：外部代码目录（API/CLI 上传解压的隔离副本）→ DeepSeek 改码 → 校验 → diff。

    plan/feedback: 用户确认的修改方案与调整意见（交互式流程第二步）。
    产物：dev_diff（diff 文本）、dev_files（改动清单）、dev_summary、dev_modified_zip（修改后文件 base64）、output_file（.diff）
    """
    import base64 as _b64
    import io as _io
    import shutil as _shutil
    import zipfile as _zip
    from app.services.llm_client import chat_completion_json

    started = time.time()
    workspace = os.path.normpath(code_dir)  # 外部代码目录本身就是隔离副本
    try:
        files = _walk_files(workspace)
        # 空文件夹也允许：AI 从零创建项目
        if not files:
            logger.info("[dev_task:%s] 空项目目录，从零创建", task_id)
        # 改前文件内容快照（diff 对比用；写文件前取；utf-8-sig 去 BOM，与上下文/patch 定位一致）
        orig_contents: dict[str, str] = {}
        for rel in _walk_files(workspace):
            try:
                with open(os.path.join(workspace, rel), encoding="utf-8-sig") as _f:
                    orig_contents[rel] = _f.read()
            except Exception:
                pass

        tree = _dev_context(workspace, files, requirement=requirement)
        plan_part = ""
        if plan:
            plan_part = (
                f"\n【已确认的修改方案】\n{plan}\n"
                f"【用户调整意见】\n{feedback or '无，按方案执行'}\n"
                "请严格按此方案实现，不要擅自扩大改动范围。"
            )
        # 两阶段上下文：项目文件多时，先让模型从清单里挑要读的文件（省无关文件全文的输入 token）
        two_stage = len(files) > 12
        if two_stage:
            index_text = _dev_file_index(workspace, files)
            try:
                sel = await chat_completion_json(
                    DEV_SELECT_PROMPT.format(requirement=requirement[:8000], plan_part=plan_part, index=index_text),
                    requirement, temperature=0.2, max_tokens=3000,
                    model=get_settings().dev_modify_model or get_settings().ai_model,
                )
                rels = [r for r in (sel.get("files_to_read") or []) if isinstance(r, str) and _safe_dev_rel(r, workspace)]
                # grep：搜索模型要定位的符号，命中文件自动并入读取清单（找调用点/定义点，不再靠猜）
                grep_text, grep_files = _dev_grep(workspace, files, sel.get("grep") or [])
                for g in grep_files:
                    if g not in rels:
                        rels.append(g)
                if rels:
                    tree = _dev_read_files(workspace, rels)
                    if grep_text:
                        tree += "\n\n" + grep_text
                    # 附上完整清单：模型知道还有哪些文件没读（需要时可在 files_to_read 里要求）
                    tree += "\n\n【项目全部文件清单（未读取的文件未显示内容）】\n" + index_text
                    logger.warning("[dev_task:%s] 两阶段：选中 %d/%d 个文件读取(grep %d 个): %s", task_id,
                                   len(rels), len(files), len(grep_files), rels[:12])
                else:
                    tree = _dev_context(workspace, files, requirement=requirement)  # 回退全给
                    logger.warning("[dev_task:%s] 两阶段：模型未选择文件，回退全量上下文", task_id)
            except Exception as e:
                tree = _dev_context(workspace, files, requirement=requirement)  # 选择失败回退全给
                logger.warning("[dev_task:%s] 两阶段选择失败，回退全量上下文: %s", task_id, str(e)[:100])
        files_map: dict = {}
        summary = ""
        errors: list[str] = []
        retry_hint = ""
        for attempt in range(3):
            try:
                # 每次尝试前恢复工作区到初始快照：patch/files 始终基于同一份源码应用，
                # 防止上次尝试写盘的内容污染本次（否则同一 patch 会被重复应用产生重复行）
                for _rel in _walk_files(workspace):
                    if _rel not in orig_contents:
                        try:
                            os.remove(os.path.join(workspace, _rel))
                        except Exception:
                            pass
                for _rel, _content in orig_contents.items():
                    _p = os.path.join(workspace, _rel)
                    try:
                        os.makedirs(os.path.dirname(_p), exist_ok=True)
                        with open(_p, "w", encoding="utf-8") as _f:
                            _f.write(_content)
                    except Exception:
                        pass
                prompt = DEV_MODIFY_PROMPT.format(requirement=requirement[:8000], context=tree, plan_part=plan_part)
                if attempt > 0:
                    # 上轮失败：把原因反馈给模型，强制纠正输出格式
                    prompt += ("\n\n【上次输出不符合要求】" + (retry_hint or (
                        "你没有返回合法 JSON（可能输出了目录树/说明文字或被截断）。"
                        "请这次**只输出一个完整闭合的 JSON 对象**（{\"patch\": \"...\", \"files\": {...}, \"summary\": \"...\"}），"
                        "绝对不要输出目录树、文件名列表或任何解释文字。")))
                info = await chat_completion_json(
                    prompt,
                    requirement, temperature=0.2, max_tokens=32000,  # 大项目/长文件：防 JSON 截断
                    model=get_settings().dev_modify_model or get_settings().ai_model,
                )
            except Exception as e:
                if attempt < 2:
                    retry_hint = ("你没有返回合法 JSON。请这次**只输出一个完整闭合的 JSON 对象**"
                                  "（{\"patch\": \"...\", \"files\": {...}, \"summary\": \"...\"}），"
                                  "绝对不要输出目录树、markdown 围栏或任何解释文字。")
                    await asyncio.sleep(2)  # 偶发网络/JSON 错误 → 重试（带格式纠正提示）
                    continue
                return {"status": "failed", "error": f"开发模型调用失败: {str(e)[:120]}", "elapsed": round(time.time() - started, 1)}
            files_map, patch_errors = _dev_files_from_info(info, workspace)
            logger.info("[dev_task:%s] 模型返回 keys=%s patch_type=%s files=%s", task_id,
                        sorted(info.keys()), type(info.get("patch")).__name__,
                        list((info.get("files") or {}).keys()))
            if patch_errors:
                logger.warning("[dev_task:%s] patch 应用失败: %s | patch 内容: %s", task_id,
                               patch_errors[:3], str(info.get("patch"))[:800])
            summary = str(info.get("summary") or "")
            command = str(info.get("command") or "").strip()
            analysis = str(info.get("analysis") or "").strip()
            background = bool(info.get("background"))
            if patch_errors:
                # patch 应用失败 → 提示模型改用 files（完整内容）重试；3 次仍失败才报错
                if attempt < 2:
                    retry_hint = ("你返回的 patch 无法应用：" + "；".join(patch_errors)[:200] +
                                  "。请这次**改用 files 字段输出每个改动文件的完整内容**（不要再输出 patch），"
                                  "files 的值必须是文件完整内容。")
                    await asyncio.sleep(2)
                    continue
                return {"status": "failed", "error": f"patch 应用失败: {'；'.join(patch_errors)[:200]}",
                        "elapsed": round(time.time() - started, 1)}
            if not files_map and not command and not analysis:
                # 模型什么都没返回：带纠正提示重试，最后才失败
                if attempt < 2:
                    retry_hint = "你没有返回任何文件改动（patch/files）、命令或分析结果。请至少返回一个（改动已有文件用 patch，新增文件用 files）。"
                    await asyncio.sleep(2)
                    continue
                return {"status": "failed", "error": "模型未返回文件改动/命令/分析结果（已重试 3 次，请尝试更明确的需求）",
                        "elapsed": round(time.time() - started, 1)}
            if files_map:
                errors = _dev_validate(files_map, workspace)
                if errors:
                    # 校验失败 → 把错误反馈给模型修复（reasoner），附上报错文件当前内容供精确定位
                    try:
                        files_context = _dev_errors_context(errors, workspace)
                        info2 = await chat_completion_json(
                            DEV_VALIDATE_PROMPT.format(requirement=requirement[:8000], errors="\n".join(errors),
                                                       files_context=files_context),
                            requirement, temperature=0.2, max_tokens=32000,  # 防大文件 JSON 截断
                            model=get_settings().ai_model_reasoning,
                        )
                        fixed_map, _ = _dev_files_from_info(info2, workspace)
                        if fixed_map:
                            files_map = {**files_map, **fixed_map}
                        errors2 = _dev_validate(files_map, workspace)
                        if not errors2:
                            errors = []
                            break
                    except Exception:
                        break
                else:
                    break  # 校验通过 → 结束尝试（避免重复调用 LLM / 重复应用 patch）
            else:
                # 操作型需求（启动/运行/分析）：无文件改动，直接进入命令/分析处理
                break

        if errors:
            return {"status": "failed", "error": f"代码校验未通过: {'；'.join(errors)[:300]}", "elapsed": round(time.time() - started, 1)}

        # 只保留路径合法的文件（防 LLM 返回 ../ 或绝对路径），并用归一化路径作 key（防 \ 与 / 不一致）
        safe_files: dict[str, str] = {}
        for rel, content in (files_map or {}).items():
            safe = _safe_dev_rel(rel, workspace)
            if safe is not None:
                safe_files[safe] = content
        files_map = safe_files
        diff = _dev_build_diff(files_map, workspace, orig_contents)
        # diff 落 web/ 便于下载/查看（产物域）
        os.makedirs(_WEB_DIR, exist_ok=True)
        diff_path = os.path.join(_WEB_DIR, f"dev_{task_id}.diff")
        with open(diff_path, "w", encoding="utf-8") as f:
            f.write(f"# 需求: {requirement}\n# 改动说明: {summary}\n\n" + diff)

        dev_files = []
        for rel, content in (files_map or {}).items():
            dev_files.append({"path": rel, "status": "新增" if rel not in orig_contents else "修改", "size": len(content)})

        # 修改后的文件打包 zip（base64）——CLI 用它应用改动到本地项目
        buf = _io.BytesIO()
        with _zip.ZipFile(buf, "w", _zip.ZIP_DEFLATED) as zf:
            for rel, content in (files_map or {}).items():
                zf.writestr(rel, content)
        modified_zip_b64 = _b64.b64encode(buf.getvalue()).decode()

# 操作型需求：执行模型返回的命令（在项目目录运行），失败则让模型自我修复（最多 5 轮；
# 模型可中途换实现方案或 give_up 放弃，5 轮仍失败说明方案本身不行）
        dev_command = command or ""
        dev_output = ""
        dev_output_ok = True
        dev_keep_dir = False  # 后台进程在运行 → 保留项目目录（否则进程的文件会被清理）
        last_error = ""
        for fix_round in range(5):
            if not dev_command:
                break
            if background:
                # 长驻服务（web 服务器等）：后台启动 + 存活探测，不等待结束；保留目录让进程持续运行
                bg = await asyncio.to_thread(_run_dev_command_background, workspace, dev_command)
                dev_output_ok = bool(bg.get("ok"))
                dev_output = bg.get("output") or bg.get("error") or ""
                if bg.get("pid"):
                    dev_keep_dir = True
                    dev_output = (f"✅ 服务器已在后台运行（PID {bg['pid']}）\n" + dev_output).strip()
            else:
                cmd_result = await asyncio.to_thread(_run_dev_command, workspace, dev_command)
                dev_output_ok = bool(cmd_result.get("ok"))
                dev_output = cmd_result.get("output") or cmd_result.get("error") or ""
            if dev_output_ok:
                break
            last_error = dev_output
            logger.warning("[dev_task:%s] 命令第 %d 次失败: %s", task_id, fix_round + 1, last_error[:150])
            if fix_round >= 4:
                break
            # 失败 → 让模型自我修复：改代码（换实现方案）或换命令；模型可判断该继续还是放弃
            try:
                fix_prompt = (
                    f"你在项目里执行命令 `{dev_command}` 失败了（第 {fix_round + 1} 次，共可尝试 {5 - fix_round} 次）。\n"
                    f"命令输出/错误如下（重点看依赖安装、编译、端口占用等）:\n{last_error[:2500]}\n\n"
                    "【请自我修复】\n"
                    "1. 如果依赖安装失败（如 better-sqlite3 等需要 C++ 编译工具链/node-gyp/Visual Studio，"
                    "或 prebuild-install 下载失败）：**修改代码改用不需要编译的替代方案**"
                    "（如 sql.js 纯 JS、node:sqlite 内置模块、JSON 文件存储等），并同步更新 package.json 依赖；\n"
                    "2. 如果是代码/端口/路径问题：修改相关文件（patch 或 files）或给出修正后的命令（command）；\n"
                    "3. 必须给出修复：patch（unified diff，只写改动行）/ files（完整内容）或 command（新命令），至少一个。\n"
                    "4. 如果你认为**换个实现思路**更好（当前方案本身有缺陷），直接给出新方案的文件改动，不要硬修。\n"
                    "5. 如果这个问题**无法用改代码/换命令解决**（如环境本身缺失、需求与现有代码根本冲突），"
                    "输出 {\"give_up\": true, \"reason\": \"简短中文原因\"} 明确放弃，不要瞎改。\n"
                    "只输出 JSON（patch/files/command/summary 或 give_up/reason）。")
                fix_info = await chat_completion_json(
                    fix_prompt, requirement, temperature=0.2, max_tokens=32000,
                    model=get_settings().ai_model_reasoning,
                )
                fixed_any = False
                fix_map, fix_patch_err = _dev_files_from_info(fix_info, workspace)
                if fix_patch_err and not fix_map:
                    logger.warning("[dev_task:%s] 修复 patch 无法应用: %s", task_id, "；".join(fix_patch_err)[:150])
                if fix_map:
                    safe_map = {}
                    for rel, content in fix_map.items():
                        safe = _safe_dev_rel(rel, workspace)
                        if safe is not None:
                            safe_map[safe] = content
                    if safe_map:
                        _dev_validate(safe_map, workspace)  # 写盘
                        files_map = {**files_map, **safe_map}
                        # 重新打包（改动后）
                        _buf2 = _io.BytesIO()
                        with _zip.ZipFile(_buf2, "w", _zip.ZIP_DEFLATED) as zf2:
                            for rel2, c2 in files_map.items():
                                zf2.writestr(rel2, c2)
                        modified_zip_b64 = _b64.b64encode(_buf2.getvalue()).decode()
                        for rel2, c2 in safe_map.items():
                            entry = {"path": rel2, "status": "新增" if rel2 not in orig_contents else "修改", "size": len(c2)}
                            if entry not in dev_files:
                                dev_files.append(entry)
                        fixed_any = True
                new_cmd = str(fix_info.get("command") or "").strip()
                if new_cmd:
                    dev_command = new_cmd
                    fixed_any = True
                if fix_info.get("give_up"):
                    # 模型明确放弃（环境问题/需求冲突）：不再瞎试，把原因返回给用户
                    logger.warning("[dev_task:%s] 模型放弃修复: %s", task_id, str(fix_info.get("reason") or "")[:150])
                    dev_output = dev_output + "\n\n⚠️ AI 判断此问题无法通过改代码/换命令解决: " + str(fix_info.get("reason") or "")
                    break
                if not fixed_any:
                    break  # 模型没给出任何修复
                await asyncio.sleep(2)  # 给文件系统/进程一点时间
            except Exception as e:
                logger.warning("[dev_task:%s] 命令修复重试异常: %s", task_id, str(e)[:120])
                break
        if not dev_output_ok:
            logger.warning("[dev_task:%s] 命令最终失败: %s", task_id, last_error[:150])

        return {
            "status": "ok",
            "dev_diff": diff[:20000],
            "dev_diff_url": f"/dev_{task_id}.diff",
            "dev_files": dev_files,
            "dev_summary": summary,
            "dev_modified_zip": modified_zip_b64,
            "dev_command": dev_command,
            "dev_output": dev_output[:8000],
            "dev_output_ok": dev_output_ok,
            "dev_running": dev_keep_dir,
            "dev_analysis": analysis,
            "output_file": diff_path,
            "elapsed": round(time.time() - started, 1),
            "error": None,
        }
    except Exception as e:
        logger.exception("[dev_task:%s] failed", task_id)
        return {"status": "failed", "error": f"开发任务执行出错: {str(e)[:200]}", "elapsed": round(time.time() - started, 1)}


async def _plan_dev_task(requirement: str, code_dir: str, feedback: str | None = None) -> dict:
    """开发任务第一步：读代码 → 修改方案（不写文件），供用户确认/调整。

    feedback: 用户对上一版方案的意见（带意见重新规划）。
    """
    from app.services.llm_client import chat_completion_json

    workspace = os.path.normpath(code_dir)
    files = _walk_files(workspace)
    # 空文件夹也允许：AI 从零创建项目
    if not files:
        logger.info("[dev_plan] 空项目目录，从零规划")
    tree = _dev_context(workspace, files, requirement=requirement)
    # 两阶段：文件多时先让模型挑要读的文件（方案阶段同样省输入 token）
    if len(files) > 12:
        index_text = _dev_file_index(workspace, files)
        try:
            sel = await chat_completion_json(
                DEV_SELECT_PROMPT.format(requirement=requirement[:8000], plan_part="", index=index_text),
                requirement, temperature=0.2, max_tokens=3000,
                model=get_settings().ai_model,
            )
            rels = [r for r in (sel.get("files_to_read") or []) if isinstance(r, str) and _safe_dev_rel(r, workspace)]
            grep_text, grep_files = _dev_grep(workspace, files, sel.get("grep") or [])
            for g in grep_files:
                if g not in rels:
                    rels.append(g)
            if rels:
                tree = _dev_read_files(workspace, rels)
                if grep_text:
                    tree += "\n\n" + grep_text
                tree += "\n\n【项目全部文件清单（未读取的文件未显示内容）】\n" + index_text
                logger.warning("[dev_plan] 两阶段：选中 %d/%d 个文件读取(grep %d 个): %s", len(rels), len(files),
                               len(grep_files), rels[:12])
            else:
                tree = _dev_context(workspace, files, requirement=requirement)  # 回退全给
        except Exception as e:
            tree = _dev_context(workspace, files, requirement=requirement)
            logger.warning("[dev_plan] 两阶段选择失败，回退全量上下文: %s", str(e)[:100])
    fb_part = f"\n【用户对上一版方案的意见】\n{feedback}\n请根据意见重新调整方案。" if feedback else ""
    info = None
    for attempt in range(3):
        try:
            prompt = DEV_PLAN_PROMPT.format(requirement=requirement[:8000], context=tree) + fb_part
            if attempt > 0:
                prompt += ("\n\n【上次输出不符合要求】你没有返回合法 JSON。请这次**只输出一个完整闭合的 JSON 对象**"
                           "（{\"plan\": \"...\", \"files\": [...], \"questions\": [...]}），"
                           "不要输出目录树、markdown 围栏或任何解释文字。")
            info = await chat_completion_json(
                prompt,
                requirement, temperature=0.2, max_tokens=5000,
                model=get_settings().ai_model,  # 方案用便宜模型；改码/修复才用 reasoner
            )
            break
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(2)  # 偶发网络/JSON 错误 → 重试（带格式纠正提示）
                continue
            return {"status": "failed", "error": f"方案生成失败: {str(e)[:120]}"}
    return {
        "status": "ok",
        "plan": str(info.get("plan") or ""),
        "files": info.get("files") or [],
        "questions": info.get("questions") or [],
    }


def _walk_files(root: str) -> list[str]:
    """列出目录下所有相对文件路径（跳过忽略目录）。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in DEV_EXCLUDE_DIRS]
        for fn in filenames:
            out.append(os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/"))
    return out


async def _run_report_task(task_id: str, requirement: str, url: str | None) -> dict:
    """报告模式：LLM 生成脚本 → 沙箱执行 → report.html 落 web 目录（公网可访问）。"""
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

    # 2. 沙箱执行（隔离环境 + 静态扫描 + 超时/资源限制），失败时带 stderr 定向修复（最多 2 轮）
    from app.sandbox.docker_executor import execute_in_sandbox
    result = None
    stdout = stderr = ""
    for round_no in range(3):  # 首次执行 + 最多 2 次定向修复
        if round_no > 0:
            logger.info("报告脚本第 %d 轮失败，带错误定向修复...", round_no)
            try:
                heal = await chat_completion(
                    REPORT_HEAL_PROMPT,
                    f"【用户需求】{requirement}\n\n【当前脚本】\n```python\n{code}\n```\n\n【运行报错】\n{(stderr or stdout)[-1500:]}\n\n请修复脚本（保持 report.html 输出），只输出修复后的完整代码。",
                    temperature=0.2, max_tokens=8000,
                    model=get_settings().ai_model_reasoning,  # 报告修复也用推理模型
                )
            except Exception as e:
                logger.warning("报告修复调用失败: %s", str(e)[:120])
                break  # LLM 不可用，跳过修复
            heal = heal.strip()
            if heal.startswith("```python"):
                heal = heal[9:]
            elif heal.startswith("```"):
                heal = heal[3:]
            if heal.endswith("```"):
                heal = heal[:-3]
            heal = heal.strip()
            try:
                compile(heal, "<script>", "exec")
                if "report.html" in heal:
                    code = heal
                else:
                    break
            except Exception:
                break  # 修复代码不合格，放弃
        result = await execute_in_sandbox(code, timeout=180, preview_mode=False, task_id=task_id)
        stdout = result.stdout or ""
        stderr = result.stderr or ""
        report_ok = result.success and result.output_file_path and os.path.exists(result.output_file_path)
        if report_ok:
            break

    if not result or not result.success:
        return {
            "status": "failed",
            "error": f"脚本执行失败: {(stderr or stdout)[-300:]}",
            "elapsed": round(time.time() - started, 1),
            "script": code,
        }

    report_path = os.path.join(out_dir, report_name)
    if result.output_file_path and os.path.exists(result.output_file_path) and _safe_output_src(result.output_file_path):
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
    # pandas 同步读取放线程池，避免阻塞事件循环
    import asyncio as _a
    loop = _a.get_running_loop()

    def _read_summary(fp: str) -> dict:
        if fp.lower().endswith((".xlsx", ".xls")):
            import pandas as pd
            return {"df": pd.read_excel(fp)}
        if fp.lower().endswith(".csv"):
            import pandas as pd
            return {"df": pd.read_csv(fp)}
        return {}

    for fp in data_paths or []:
        if not os.path.exists(fp):
            continue
        try:
            loaded = await loop.run_in_executor(None, _read_summary, fp)
            df = loaded.get("df")
            if df is None:
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
        # LLM 失败不能伪装成成功：标记 failed 并把错误写入 error 字段
        merged = {
            "status": "failed",
            "answer": "",
            "rows": summary["rows"],
            "columns": summary["columns"],
            "elapsed": round(time.time() - started, 1),
            "error": f"分析失败: {str(e)[:200]}",
        }
        record["result"] = merged
        record["status"] = "done"
        record["message"] = "完成（分析失败）"
        record["progress"] = 100
        await update_mini_task(task_id, status="done", message=record["message"],
                               result=_json.dumps(merged, ensure_ascii=False), error=merged["error"])
        return

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
    result = await execute_in_sandbox(code, timeout=180, preview_mode=False, task_id=task_id)
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
    if result.output_file_path and os.path.exists(result.output_file_path) and _safe_output_src(result.output_file_path):
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
    if user_id and rec.get("user_id") != user_id:  # 空 user_id 也不放行（防越权）
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


def delete_task(task_id: str, user_id: str = "") -> bool:
    """删除任务记录（内存 + SQLite）；user_id 非空时校验归属。"""
    if user_id:
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
    # 运行中的任务先取消
    if is_running(task_id):
        cancel(task_id)
    _TASKS.pop(task_id, None)
    try:
        from app.database import _get_conn
        with _get_conn() as conn:
            conn.execute("DELETE FROM mini_tasks WHERE id=?", (task_id,))
    except Exception:
        pass
    return True
