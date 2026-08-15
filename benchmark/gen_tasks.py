#!/usr/bin/env python
"""生成 300 个 benchmark 长难任务（确定性随机组合，可复现）。

模板组合：
- 数据生成+复杂统计（约 180）：10 主题 × 多种统计操作 × 规模/参数变体
- 电商抓取（约 60）：books.toscrape 分页/分类/详情/对比
- 名言抓取（约 40）：quotes.toscrape 多页/筛选/作者统计/详情
- 组合报告（约 20）：抓取/数据 → 可视化报告

用法：python benchmark/gen_tasks.py   → 覆盖 benchmark/tasks.json
"""
from __future__ import annotations

import json
import random
from pathlib import Path

random.seed(20260816)

TASKS_FILE = Path(__file__).parent / "tasks.json"


def gen_data_tasks(n: int) -> list[dict]:
    """数据生成 + 复杂统计任务（主题 × 操作 × 参数）。"""
    themes = [
        ("销售数据", ["产品", "数量", "单价", "日期", "地区"]),
        ("员工数据", ["姓名", "部门", "工资", "入职年份", "城市"]),
        ("订单数据", ["订单号", "客户", "商品", "金额", "日期"]),
        ("库存数据", ["商品", "仓库", "库存量", "安全库存", "类别"]),
        ("学生成绩", ["姓名", "班级", "科目", "成绩", "学期"]),
        ("气温数据", ["城市", "月份", "平均气温", "最高气温", "降水"]),
        ("网站流量", ["页面", "日期", "访问量", "独立访客", "来源"]),
        ("电影票房", ["电影", "类型", "票房", "上映月份", "评分"]),
        ("餐厅评价", ["餐厅", "菜系", "评分", "人均消费", "城市"]),
        ("人口数据", ["城市", "年份", "人口", "面积", "省份"]),
        ("物流数据", ["订单号", "城市", "货物", "重量", "运输方式"]),
        ("医疗数据", ["患者", "科室", "费用", "住院天数", "年龄"]),
    ]
    ops = [
        "按{grp}分组汇总{val}，输出总金额/总量的{top}，并计算占比",
        "筛选{val}大于{thr}的记录，按{val}从高到低排序，输出前{top}条",
        "按{grp}分组统计平均值、最大值、最小值，输出{val}最高的{top}",
        "计算每月环比增长率，找出增长最快的月份，并输出全年累计占比",
        "按{grp}和{grp2}两级分组汇总{val}，输出每个组合的{val}和占比",
        "筛选出{val}低于平均值20%的记录（异常值），分析这些记录的特征",
        "按{grp}分组后计算{val}的排名，输出每组排名前{top}",
        "生成{rows}行数据，按{grp}汇总{val}，并对比两个{grp}的差异",
        "按{grp}分组，计算每组{val}的中位数和四分位差，输出离散程度最大的{top}组",
        "计算{val}的移动平均（窗口3），找出趋势转折点，输出转折点前后的记录",
        "按{grp}分组，筛选每组{val}最大的记录和最小的记录，对比输出",
        "计算{val}的累积求和和累积占比，找出贡献80%的{grp}（帕累托分析）",
        "将{val}按大小分成高/中/低三档，统计每档的记录数和占比",
        "按{grp}和{grp2}交叉汇总{val}，计算行占比和列占比，输出占比最高的组合",
        "筛选出{grp}为特定值且{val}在前10%的记录，输出并标注排名",
        "按日期拆分成周，统计每周{val}，计算周环比并输出波动最大的{top}周",
    ]
    tasks = []
    for i in range(n):
        theme, cols = themes[i % len(themes)]
        op = ops[i % len(ops)]
        grp = random.choice(cols)
        other = [c for c in cols if c != grp]
        grp2 = random.choice(other) if other else "类别"
        val = random.choice([c for c in cols if c not in (grp, grp2)] or other)
        rows = random.choice([50, 100, 200, 300])
        top = random.choice([3, 5, 10])
        thr = random.choice([100, 500, 1000, 5000])
        op_text = op.format(grp=grp, grp2=grp2, val=val, rows=rows, top=top, thr=thr)
        # 去掉句子里的格式瑕疵
        op_text = op_text.replace("总金额/总量", "数值").replace("总金额", "数值")
        req = f"生成{rows}行{theme}（列：{'、'.join(cols)}），{op_text}，导出Excel"
        tasks.append({"id": f"D{100 + i:03d}", "cat": "数据", "req": req, "expect": "统计结果"})
    return tasks


def gen_books_tasks(n: int) -> list[dict]:
    """电商抓取任务（books.toscrape）。"""
    pages = ["第1页", "前2页", "前3页", "前5页"]
    cats = ["Travel", "Mystery", "Classics", "Romance", "Poetry", "Historical Fiction"]
    ops = [
        "抓取{scope}书籍的标题和价格，按价格从低到高排序，输出最便宜的{top}本",
        "抓取{scope}书籍的标题、价格和星级，输出星级为4星及以上的所有书",
        "抓取{scope}书籍，统计不同星级的数量分布，输出各星级数量",
        "抓取{scope}书籍的标题、价格和库存，找出库存最少的{top}本",
        "抓取{scope}书籍并进入详情页抓取描述，统计描述长度，输出最长的{top}本",
        "抓取{scope}书籍，计算平均价格和中位数价格，输出高于平均价的{top}本",
    ]
    tasks = []
    for i in range(n):
        op = ops[i % len(ops)]
        use_cat = i % 3 == 0
        if use_cat:
            scope = f"{cats[i % len(cats)]}分类"
        else:
            scope = pages[i % len(pages)]
        top = random.choice([3, 5, 10])
        req = f"从books.toscrape.com{op.format(scope=scope, top=top)}，导出Excel"
        tasks.append({"id": f"B{200 + i:03d}", "cat": "电商", "req": req, "expect": "电商数据"})
    return tasks


def gen_quotes_tasks(n: int) -> list[dict]:
    """名言抓取任务（quotes.toscrape）。"""
    ops = [
        "爬取{scope}的名言（引用、作者、标签），统计每位作者的名言数量，输出最多的{top}位作者",
        "爬取{scope}的名言，筛选出含{tag}标签的名言，输出引用和标签",
        "爬取{scope}的名言，统计每个标签出现的次数，输出出现最多的{top}个标签",
        "爬取{scope}的名言，进入作者详情页抓取作者简介，统计简介字数，输出最长的{top}位作者",
        "爬取{scope}的名言，按作者分组，输出每位作者的完整名言列表",
    ]
    tasks = []
    for i in range(n):
        op = ops[i % len(ops)]
        scope = random.choice(["前2页", "前3页", "前5页", "第1页到第3页", "全部10页"])
        tag = random.choice(["life", "wisdom", "love", "truth", "inspirational"])
        top = random.choice([3, 5, 10])
        req = f"从quotes.toscrape.com{op.format(scope=scope, tag=tag, top=top)}，导出Excel"
        tasks.append({"id": f"Q{300 + i:03d}", "cat": "名言", "req": req, "expect": "名言统计"})
    return tasks


def gen_combo_tasks(n: int) -> list[dict]:
    """组合报告任务（抓取/数据 → 可视化报告）。"""
    sources = [
        "从books.toscrape.com抓取前2页书籍的标题和价格",
        "从quotes.toscrape.com爬取前3页名言，统计每个作者的名言数量",
        "生成100行销售数据（列：月份、产品、销售额）",
        "从books.toscrape.com抓取前3页书籍的价格和星级",
        "生成50行学生成绩（列：班级、科目、成绩）",
    ]
    analyses = [
        "分析数据分布特征，生成一份精美的可视化分析报告",
        "统计关键指标并对比，生成可视化分析报告",
        "分析趋势和占比，生成一份可视化分析报告",
    ]
    tasks = []
    for i in range(n):
        src = sources[i % len(sources)]
        ana = analyses[i % len(analyses)]
        req = f"{src}，{ana}"
        tasks.append({"id": f"C{400 + i:03d}", "cat": "组合", "req": req, "expect": "可视化报告"})
    return tasks


def main() -> None:
    tasks = []
    tasks += gen_data_tasks(260)
    tasks += gen_books_tasks(60)
    tasks += gen_quotes_tasks(40)
    tasks += gen_combo_tasks(20)
    # 去重（保留首个）
    seen = set()
    uniq = []
    for t in tasks:
        if t["req"] not in seen:
            seen.add(t["req"])
            uniq.append(t)
    print(f"生成任务: {len(uniq)} 个（去重前 {len(tasks)}）", flush=True)
    cat_count: dict[str, int] = {}
    for t in uniq:
        cat_count[t["cat"]] = cat_count.get(t["cat"], 0) + 1
    print("分类分布:", cat_count, flush=True)
    TASKS_FILE.write_text(json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已写入 {TASKS_FILE}", flush=True)


if __name__ == "__main__":
    main()
