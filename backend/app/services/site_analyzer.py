from __future__ import annotations
"""网站结构分析器：自动识别列表/卡片元素、内部字段选择器、跳转路径。

在生成脚本前运行，产出结构化分析报告（比原始 DOM 快照更利于 LLM 写对选择器）。
解决"LLM 盲猜选择器 → 试错"的问题：像人工分析小红书那样，把字段在哪、跨页关系查清楚再交给 LLM。
"""
import asyncio
import json
import logging

logger = logging.getLogger("app.services.site_analyzer")

_ANALYZE_JS = r"""
() => {
    // 1. 找重复 class 的"卡片/列表项"候选：同一 class 出现 >=3 次
    const classCount = {};
    document.querySelectorAll('[class]').forEach(el => {
        const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/)[0] : '';
        if (!cls || cls.length < 3) return;
        classCount[cls] = (classCount[cls] || 0) + 1;
    });
    const cardCandidates = Object.entries(classCount)
        .filter(([c, n]) => n >= 3 && n <= 200)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 8)
        .map(([c]) => c);

    const out = { cardCandidates: [], links: { detail: null, author: null }, priorityControls: [] };

    // 2. 优先控件：筛选/排序类（用户要"最新/最热/筛选"时先操作这些）
    const controlKeywords = ['筛选', '排序', '综合', '最新', '最热', '时间', '销量', '价格', '推荐', '热门', '人气'];
    const controlSelectors = ['button', 'a', 'div', 'span', '[role="button"]', '[role="tab"]', '[role="menuitem"]'];
    const seenControls = new Set();
    for (const sel of controlSelectors) {
        for (const el of document.querySelectorAll(sel)) {
            if (out.priorityControls.length >= 20) break;
            const t = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 20);
            if (!t || t.length > 15) continue;
            if (!controlKeywords.some(k => t.includes(k))) continue;
            const key = el.tagName + '|' + t;
            if (seenControls.has(key)) continue;
            seenControls.add(key);
            out.priorityControls.push({
                tag: el.tagName.toLowerCase(),
                text: t,
                cls: (typeof el.className === 'string' ? el.className : '').slice(0, 60),
            });
        }
    }

    for (const cls of cardCandidates) {
        const first = document.querySelector('.' + cls);
        if (!first) continue;
        // 卡片内部结构：带 class 的元素 + 文本示例
        const fields = [];
        first.querySelectorAll('a, span, div, h1, h2, h3, img, time').forEach(n => {
            const elCls = (n.className && typeof n.className === 'string') ? n.className.trim().split(/\s+/).slice(0, 2).join(' ') : '';
            const text = (n.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 50);
            const href = n.getAttribute('href') || '';
            // 只收有明显特征的：文本非空 / 有链接 / img
            if ((text && text.length > 1) || href || n.tagName === 'IMG') {
                fields.push({
                    tag: n.tagName.toLowerCase(),
                    cls: elCls.slice(0, 50),
                    text: text.slice(0, 40),
                    href: href.slice(0, 80),
                    img: n.tagName === 'IMG' ? (n.getAttribute('src') || '').slice(0, 80) : '',
                });
            }
        });
        // 去重（同 tag+cls+text）
        const seen = new Set();
        const uniq = fields.filter(f => {
            const k = f.tag + '|' + f.cls + '|' + f.text;
            if (seen.has(k)) return false;
            seen.add(k);
            return true;
        }).slice(0, 25);

        // 卡片内链接：标题链接（正文）、作者链接
        let detailHref = null, authorHref = null;
        const aList = first.querySelectorAll('a[href]');
        for (const a of aList) {
            const href = a.getAttribute('href') || '';
            const cls = (a.className && typeof a.className === 'string') ? a.className : '';
            if (!detailHref && (cls.includes('title') || cls.includes('cover') || /\/explore\//.test(href) || /\/item\//.test(href) || /\/search_result\//.test(href))) {
                detailHref = href;
            }
            if (!authorHref && (cls.includes('author') || cls.includes('user') || /\/user\/profile\//.test(href))) {
                authorHref = href;
            }
        }
        out.cardCandidates.push({
            class: cls,
            count: classCount[cls],
            fields: uniq,
            detailHref,
            authorHref,
        });
        if (detailHref) out.links.detail = detailHref;
        if (authorHref) out.links.author = authorHref;
    }
    return out;
}
"""


async def analyze_site(url: str, timeout: int = 30) -> dict:
    """打开目标页面，自动分析结构，返回报告 dict。

    Returns:
        {"ok": bool, "title": str, "url": str, "cardCandidates": [...], "links": {...}, "error": str}
    """
    try:
        from app.services.page_capture import _load_storage_state
    except Exception:
        _load_storage_state = None

    def _run():
        import glob as _glob
        import os as _os
        import json as _json
        from playwright.sync_api import sync_playwright

        # 注入保存的登录态（合并 browser_profile 所有域，同沙箱）
        profile_dir = _os.path.normpath(_os.path.join(_os.path.dirname(__file__), "..", "..", "browser_profile"))
        storage_state = None
        try:
            merged = {"cookies": [], "origins": []}
            for f in _glob.glob(_os.path.join(profile_dir, "*.json")):
                try:
                    with open(f, encoding="utf-8") as fp:
                        s = _json.load(fp)
                    merged["cookies"].extend(s.get("cookies", []))
                    merged["origins"].extend(s.get("origins", []))
                except Exception:
                    pass
            if merged["cookies"]:
                storage_state = merged
        except Exception:
            storage_state = None

        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
            try:
                ctx = browser.new_context(
                    storage_state=storage_state,
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0.0.0 Safari/537.36",
                    viewport={"width": 1440, "height": 900})
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(4000)
                title = page.title()
                final_url = page.url
                result = page.evaluate(_ANALYZE_JS)
                result["ok"] = True
                result["title"] = title
                result["url"] = final_url
                return result
            finally:
                browser.close()

    loop = asyncio.get_running_loop()
    try:
        return await loop.run_in_executor(None, _run)
    except Exception as e:
        logger.warning("网站分析失败: %s", str(e)[:150])
        return {"ok": False, "error": str(e)[:200], "url": url}


def format_analysis_report(analysis: dict) -> str:
    """把分析结果格式化成给 LLM 的文本报告。"""
    if not analysis or not analysis.get("ok"):
        return f"(网站结构分析失败: {(analysis or {}).get('error', 'unknown')})"
    parts = []
    parts.append(f"网站结构分析（自动探查结果，选择器以此为准）")
    parts.append(f"页面: {analysis.get('title', '')} | URL: {analysis.get('url', '')}")
    cards = analysis.get("cardCandidates") or []
    if not cards:
        parts.append("未发现明显的列表/卡片结构（可能是单页/详情页/表单页）")
    for i, card in enumerate(cards[:5]):
        parts.append(f"\n[卡片候选{i+1}] class=\".{card.get('class')}\" (出现{card.get('count')}次)")
        for f in (card.get("fields") or [])[:20]:
            detail = f"text={f.get('text')!r}" if f.get("text") else ""
            if f.get("href"):
                detail += f" href={f.get('href')}"
            if f.get("img"):
                detail += " [图片]"
            parts.append(f"  <{f.get('tag')}> .{f.get('cls')} {detail}")
        if card.get("detailHref"):
            parts.append(f"  → 详情/正文链接: {card.get('detailHref')}")
        if card.get("authorHref"):
            parts.append(f"  → 作者主页链接: {card.get('authorHref')}")
    links = analysis.get("links") or {}
    if links.get("author"):
        parts.append(f"\n作者主页路径: {links['author']}（粉丝数等用户信息通常在作者主页）")
    # 排序/筛选控件
    controls = analysis.get("priorityControls") or []
    if controls:
        parts.append("\n排序/筛选控件（用户要'最新/最热/筛选'时必须先点击切换）:")
        for c in controls[:12]:
            parts.append(f"  <{c.get('tag')}> text={c.get('text')!r} class={c.get('cls', '')[:40]}")
    return "\n".join(parts)
