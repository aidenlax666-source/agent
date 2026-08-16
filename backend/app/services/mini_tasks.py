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
           schedule: dict | None = None) -> dict:
    """提交一个后台任务，立即返回任务信息（持久化到 SQLite，重启不丢）。

    统一入口：LLM 自动判断"数据问答"还是"任务执行"，无需用户选择。
    skip_run=True：只落库不执行（用于提醒/监控等"设置型"任务，由调度器驱动）。
    schedule={type,value}：提交时直接设置定时重跑（随 INSERT 一次写入，无竞态）。
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
    key = f"rem:{reminder['id']}:{time.strftime('%Y%m%d%H%M')}"
    if key in _fire_memory:
        return
    try:
        await add_notification(user_id, "⏰ 定时提醒",
                               f"{reminder.get('time', '')} - {reminder.get('text', '')}")
    except Exception as e:
        logger.warning("[提醒] %s 写库失败（本次不记去重，下轮重试）: %s", user_id[:8], str(e)[:120])
        return
    _fire_memory[key] = time.time()
    if len(_fire_memory) > 2000:
        for k in list(_fire_memory)[:1000]:
            _fire_memory.pop(k, None)
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
    try:
        if mtype == "window":
            titles = await asyncio.to_thread(_window_titles)  # ctypes 同步调用不阻塞事件循环
            joined = " ".join(titles).lower()
            for kw in [k for k in keywords.replace("，", ",").split(",") if k.strip()]:
                kw = kw.strip().lower()
                if kw and kw in joined:
                    key = f"mon:{mid}:{kw}"
                    if _fire_memory.get(key, 0) > now - 300:  # 5 分钟冷却
                        continue
                    _fire_memory[key] = now
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
                    _fire_memory.pop(key, None)  # 画面动了，重置计时
                else:
                    prev_t = _fire_memory.get(key, 0)
                    if prev_t and now - prev_t >= mins * 60:
                        _fire_memory.pop(key, None)
                        fired.append(f"屏幕静止超过 {mins} 分钟")
                    elif not prev_t:
                        _fire_memory[key] = now  # 开始计时
            else:  # 默认：画面变化触发（冷却 60s）
                if changed:
                    key = f"mon:{mid}:change"
                    if _fire_memory.get(key, 0) > now - 60:
                        pass
                    else:
                        _fire_memory[key] = now
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
        # "提醒/通知我xxx"语义 = 仅通知；其余才作为动作任务执行（扣 1 积分防无限免费跑）
        notify_only = (not action_req or action_req in ("仅提醒", "提醒", "通知")
                       or action_req.startswith(("提醒", "通知")))
        if action_req and not notify_only:
            try:
                from app.database import try_decrement_credits
                if not await try_decrement_credits(user_id, 1):
                    await _add_note(user_id, "额度不足",
                                    f"监控触发的动作任务因余额不足未执行：{what}")
                    continue
            except Exception:
                pass
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
    """执行一次定时重跑：扣 1 积分（余额不足跳过+通知），再提交任务。

    注册在稳定 key mini_sched:{tid} 下，运行期间调度器跳过该任务（防重叠并发）。
    """
    from app.database import add_notification as _add_note, try_decrement_credits
    user_id = mtask.get("user_id") or ""
    try:
        if user_id and not await try_decrement_credits(user_id, 1):
            await _add_note(user_id, "额度不足", "定时任务因余额不足未执行，请充值后重试")
            return
    except Exception as e:
        logger.warning("[调度器] %s 扣积分失败: %s", user_id[:8], str(e)[:100])
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
        # 沙箱稳定产物目录（auto_output_*/sandbox_output_*）：产物已复制到 web/，按 mtime 清超期目录
        _tmp_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "tmp"))
        if os.path.isdir(_tmp_dir):
            for name in os.listdir(_tmp_dir):
                p = os.path.join(_tmp_dir, name)
                if not os.path.isdir(p):
                    continue
                if not (name.startswith("auto_output_") or name.startswith("sandbox_output_")):
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
                    if not os.path.normpath(_src).startswith(os.path.normpath(_WEB_DIR)):
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
        elif merged.get("status") in ("no_data", "login_required", "robots_blocked"):
            pass  # 保留业务状态（无数据/需登录/禁止抓取），前端有对应友好展示
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


# ============================================================
# 开发类任务：外部代码目录（API/CLI 上传解压的隔离副本）→ AI 改码 → 校验 → diff
# 用 DeepSeek 驱动，隔离副本保证安全，产出 diff 供用户确认/应用
# ============================================================

# 遍历时排除的目录（大/无关）
DEV_EXCLUDE_DIRS = {"node_modules", ".next", "__pycache__", ".git", "web", "data", "tmp",
                    "uploads", "browser_profile", "screens", "benchmark"}

DEV_MODIFY_PROMPT = """你是一位资深软件工程师。用户需求：{requirement}

以下是项目源码（文件路径 + 内容）：
{context}
{plan_part}

请基于上述真实源码完成需求：理解现有代码逻辑，然后**修改/新增文件**实现该需求。

只输出一个 JSON（不要输出其他内容）：
{{"files": {{"相对路径": "该文件的完整新内容"}}, "summary": "一句话说明你改了什么"}}

【严格要求】
1. files 里只列出你需要**新增或修改**的文件；未列出的文件保持原样
2. 每个文件内容必须**完整**（整个文件的全部代码，禁止用省略号/注释代替中间部分）
3. 改动必须真实可用：import、函数定义、调用关系完整，逻辑自洽
4. 优先小步改动：能加一个函数/文件解决的不要大改
5. 中文 summary 说明改动内容
"""

# 第一步：只读代码、出修改方案（不写文件）——让用户确认后再动手
DEV_PLAN_PROMPT = """你是一位资深软件工程师。用户需求：{requirement}

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
4. 中文
"""

DEV_VALIDATE_PROMPT = """你是软件工程师。下面是上一个 AI 按需求 {requirement} 改的代码，但校验报错：
{errors}

请修复：只输出 JSON {{"files": {{"相对路径": "修复后的完整文件内容"}}}}，
只列出需要修改的文件（其他文件不用重复输出）。直接输出修复后的完整代码。"""


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


def _dev_context(workspace: str, files: list[str], requirement: str | None = None,
                 max_items: int = 80, per_file: int = 4000, total_cap: int = 40000) -> str:
    """文件树 + 每个文件的内容（Claude Code 风格：让模型看到真实代码而非只看到文件名）。

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
            with open(os.path.join(workspace, rel), encoding="utf-8") as f:
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
    """校验改动的 .py 文件语法，返回错误列表（空=通过）。"""
    errors = []
    for rel, content in (files_map or {}).items():
        if not rel.endswith(".py"):
            continue
        safe = _safe_dev_rel(rel, workspace)
        if safe is None:
            errors.append(f"{rel}: 非法路径（不允许越出项目目录）")
            continue
        p = os.path.join(workspace, safe)
        try:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
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
        # 空文件夹也允许：AI 从零创建项目（像 Claude Code 在空目录建项目）
        if not files:
            logger.info("[dev_task:%s] 空项目目录，从零创建", task_id)
        # 改前文件内容快照（diff 对比用；写文件前取）
        orig_contents: dict[str, str] = {}
        for rel in _walk_files(workspace):
            try:
                with open(os.path.join(workspace, rel), encoding="utf-8") as _f:
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
        files_map: dict = {}
        summary = ""
        errors: list[str] = []
        for attempt in range(3):
            try:
                info = await chat_completion_json(
                    DEV_MODIFY_PROMPT.format(requirement=requirement[:8000], context=tree, plan_part=plan_part),
                    requirement, temperature=0.2, max_tokens=32000,  # 大项目/长文件：防 JSON 截断
                    model=get_settings().dev_modify_model or get_settings().ai_model,
                )
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(2)  # 偶发网络/JSON 错误 → 重试
                    continue
                return {"status": "failed", "error": f"开发模型调用失败: {str(e)[:120]}", "elapsed": round(time.time() - started, 1)}
            files_map = info.get("files") or {}
            summary = str(info.get("summary") or "")
            if not files_map:
                return {"status": "failed", "error": "模型未返回任何文件改动", "elapsed": round(time.time() - started, 1)}
            errors = _dev_validate(files_map, workspace)
            if not errors:
                break
            # 校验失败 → 把错误反馈给模型修复（reasoner）
            try:
                info2 = await chat_completion_json(
                    DEV_VALIDATE_PROMPT.format(requirement=requirement[:8000], errors="\n".join(errors)),
                    requirement, temperature=0.2, max_tokens=32000,  # 防大文件 JSON 截断
                    model=get_settings().ai_model_reasoning,
                )
                files_map = info2.get("files") or files_map
                errors2 = _dev_validate(files_map, workspace)
                if not errors2:
                    errors = []
                    break
            except Exception:
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

        return {
            "status": "ok",
            "dev_diff": diff[:20000],
            "dev_diff_url": f"/dev_{task_id}.diff",
            "dev_files": dev_files,
            "dev_summary": summary,
            "dev_modified_zip": modified_zip_b64,
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
    fb_part = f"\n【用户对上一版方案的意见】\n{feedback}\n请根据意见重新调整方案。" if feedback else ""
    info = None
    for attempt in range(3):
        try:
            info = await chat_completion_json(
                DEV_PLAN_PROMPT.format(requirement=requirement[:8000], context=tree) + fb_part,
                requirement, temperature=0.2, max_tokens=5000,
                model=get_settings().ai_model,  # 方案用便宜模型；改码/修复才用 reasoner
            )
            break
        except Exception as e:
            if attempt < 2:
                await asyncio.sleep(2)  # 偶发网络/JSON 错误 → 重试
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
        result = await execute_in_sandbox(code, timeout=180, preview_mode=False)
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
