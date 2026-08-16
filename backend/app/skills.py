from __future__ import annotations
"""技能系统（Skill）：预置可复用的能力包（视频剪辑 / CAD 制图 / ...）。

每个技能是 backend/app/skills/<name>/SKILL.md：
  --- frontmatter ---
  name / description / keywords（逗号分隔，用于匹配需求）
  ---
  正文 = 给 LLM 的专家指南（工具、命令模板、输出规范）。

任务生成时按需求关键词自动加载命中的技能，把指南注入生成 prompt
（类 Claude Code / Codex 的 skill 机制，零 LLM 成本、按需生效）。
"""

import logging
import os
import re

logger = logging.getLogger("app.skills")

_SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")


def _load_skills() -> dict[str, dict]:
    skills: dict[str, dict] = {}
    if not os.path.isdir(_SKILLS_DIR):
        return skills
    for name in sorted(os.listdir(_SKILLS_DIR)):
        md = os.path.join(_SKILLS_DIR, name, "SKILL.md")
        if not os.path.isfile(md):
            continue
        try:
            with open(md, encoding="utf-8") as f:
                text = f.read()
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.S)
            if not m:
                logger.warning("skill %s 缺少 frontmatter", name)
                continue
            meta: dict[str, str] = {}
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            skills[name] = {
                "name": name,
                "description": meta.get("description", ""),
                "keywords": meta.get("keywords", ""),
                "content": m.group(2).strip(),
            }
        except Exception as e:
            logger.warning("skill 加载失败 %s: %s", name, str(e)[:100])
    return skills


_skills = _load_skills()

# 歧义词排除：命中这些词的场景不算对应技能（防"合并PDF/裁剪图片/机械键盘/配音"误判）
_SKILL_EXCLUSIONS = {
    "video-edit": ("pdf", "图片", "zip", "压缩包", "文件合并", "文档", "作曲", "生成音乐", "写歌", "配乐生成",
                   "生成一首", "创作一首", "唱一首", "配音", "朗读"),
    "cad-drawing": ("键盘", "鼠标", "scada", "架构图", "流程图", "网站", "页面", "电路", "scad"),
}


def list_skills() -> list[dict]:
    """返回全部技能（元信息）。"""
    return [{"name": s["name"], "description": s["description"], "keywords": s["keywords"]}
            for s in _skills.values()]


def _matches_skill(s: dict, req: str) -> int:
    """技能关键词命中数（ASCII 用词边界，避免 cad 命中 scada 这类子串）。"""
    hits = 0
    for kw in s["keywords"].split(","):
        kw = kw.strip().lower()
        if not kw:
            continue
        if kw.isascii() and kw.isalnum():
            import re as _re
            if _re.search(rf"\b{_re.escape(kw)}\b", req):
                hits += 1
        elif kw in req:
            hits += 1
    return hits


def select_skill(requirement: str) -> dict | None:
    """按关键词匹配需求命中的技能（命中数最多且 ≥2 个泛词或 ≥1 个强词；有排除词则跳过）。

    泛词（1 个字如"画"或常见动词）需要 ≥2 命中才生效，降低"合并/裁剪/机械"劫持无关任务的假阳性。
    """
    req = (requirement or "").lower()
    best: dict | None = None
    best_hits = 0
    for s in _skills.values():
        # 排除规则：命中排除词且未命中强标识词 → 跳过该技能
        excl = _SKILL_EXCLUSIONS.get(s["name"], ())
        if excl and any(e in req for e in excl):
            continue
        hits = _matches_skill(s, req)
        # 强标识词（词长 ≥3 且非通用动词）单个即命中；否则需要 ≥2 个关键词
        strong = [k.strip().lower() for k in s["keywords"].split(",")
                  if len(k.strip()) >= 3 and k.strip().lower() not in ("画", "合并", "压缩", "裁剪")]
        is_strong = any(kw in req for kw in strong) or hits >= 2
        if is_strong and hits > best_hits:
            best_hits = hits
            best = s
    return best if best_hits > 0 else None


def skill_guide_for(requirement: str) -> str:
    """命中技能时返回注入生成 prompt 的指南文本；未命中返回空串。"""
    s = select_skill(requirement)
    if not s:
        return ""
    return (
        f"\n\n【技能加载：{s['name']}】{s['description']}\n"
        f"{s['content']}\n"
        "（本任务使用该技能处理，不需要网页采集；按上面指南执行并输出结果）"
    )
