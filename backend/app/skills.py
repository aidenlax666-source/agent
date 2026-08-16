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


def list_skills() -> list[dict]:
    """返回全部技能（元信息）。"""
    return [{"name": s["name"], "description": s["description"], "keywords": s["keywords"]}
            for s in _skills.values()]


def select_skill(requirement: str) -> dict | None:
    """按关键词匹配需求命中的技能（命中数最多的；无命中返回 None）。"""
    req = (requirement or "").lower()
    best: dict | None = None
    best_hits = 0
    for s in _skills.values():
        kws = [k.strip().lower() for k in s["keywords"].split(",") if k.strip()]
        hits = sum(1 for k in kws if k and k in req)
        if hits > best_hits:
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
