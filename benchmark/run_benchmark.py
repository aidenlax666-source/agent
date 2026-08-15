#!/usr/bin/env python
"""Benchmark 自动并行测试：提交任务集 → 并行轮询 → 汇总通过率与假通过检测。

用法：
    python benchmark/run_benchmark.py                 # 全量任务（并发 3）
    python benchmark/run_benchmark.py --tasks B01,Q06 # 只跑指定任务
    python benchmark/run_benchmark.py --concurrency 5 # 调整提交并发
    python benchmark/run_benchmark.py --timeout 1800  # 总等待上限（秒）

前置：后端已在 8000 运行。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).parent.parent
TASKS_FILE = Path(__file__).parent / "tasks.json"
RESULT_FILE = ROOT / "bench_result.json"
API = "http://127.0.0.1:8000"
DB_PATH = ROOT / "backend" / "data" / "automation.db"

# 判定"有产物"的键（与后端 merged 判定一致；output_file 是数据任务的 Excel/文件产物）
PRODUCT_KEYS = ("game_url", "report_url", "content_url", "video_url", "image_url", "image_urls", "music_url", "tts_url", "output_file")


def load_tasks(only: list[str] | None = None) -> list[dict]:
    tasks = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    if only:
        want = set(only)
        tasks = [t for t in tasks if t["id"] in want]
    return tasks


async def register_and_topup(client: httpx.AsyncClient, credits: int = 200) -> str:
    """注册一个基准测试账号并直连 DB 充值，返回 token。"""
    email = f"bench_{uuid.uuid4().hex[:8]}@bench.local"
    r = await client.post(f"{API}/api/auth/register", json={"email": email, "password": "bench1234", "name": "Bench"})
    r.raise_for_status()
    data = r.json()
    uid = data["user"]["id"]
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("UPDATE users SET credits = ? WHERE id=?", (credits, uid))
    conn.commit()
    conn.close()
    return data["access_token"]


async def submit_all(client: httpx.AsyncClient, token: str, tasks: list[dict], concurrency: int) -> dict:
    """并行提交任务，返回 {task_id: task}。"""
    sem = asyncio.Semaphore(concurrency)
    headers = {"Authorization": f"Bearer {token}"}

    async def submit(t: dict) -> tuple[str, dict] | None:
        async with sem:
            try:
                r = await client.post(f"{API}/api/mini/tasks",
                                      json={"requirement": t["req"], "url": "", "image_paths": [], "data_paths": []},
                                      headers=headers, timeout=30)
                if r.status_code == 200:
                    tid = r.json()["task_id"]
                    print(f"  [submit] {t['id']} -> {tid[:8]}", flush=True)
                    return tid, t
                print(f"  [submit-FAIL] {t['id']}: HTTP {r.status_code} {r.text[:100]}", flush=True)
                return None
            except Exception as e:
                print(f"  [submit-ERR] {t['id']}: {e}", flush=True)
                return None

    results = await asyncio.gather(*(submit(t) for t in tasks))
    return {tid: t for tid, t in results if tid}


async def poll_all(client: httpx.AsyncClient, token: str, mapping: dict, timeout_s: int, poll_interval: int = 15) -> dict:
    """并行轮询所有任务直到完成，返回 {task_id: (task, status_dict)}。"""
    headers = {"Authorization": f"Bearer {token}"}
    remaining = dict(mapping)
    out: dict[str, tuple[dict, dict]] = {}
    deadline = time.time() + timeout_s

    while remaining and time.time() < deadline:
        async def fetch(tid: str):
            try:
                r = await client.get(f"{API}/api/mini/tasks/{tid}", headers=headers, timeout=15)
                if r.status_code == 200:
                    return tid, r.json()
            except Exception:
                pass
            return tid, None

        states = await asyncio.gather(*(fetch(tid) for tid in list(remaining.keys())))
        for tid, st in states:
            if st and st.get("status") in ("done", "error", "cancelled"):
                out[tid] = (remaining.pop(tid), st)
        if remaining:
            await asyncio.sleep(poll_interval)

    for tid, t in remaining.items():  # 超时未完成
        out[tid] = (t, {"status": "timeout", "result": None})
    return out


def has_product(res: dict) -> bool:
    return any(res.get(k) for k in PRODUCT_KEYS)


def summarize(results: dict) -> dict:
    rows = []
    passed = suspicious = 0
    for tid, (task, st) in sorted(results.items(), key=lambda kv: kv[1][0]["id"]):
        res = st.get("result") or {}
        rstatus = res.get("status")
        ok = st["status"] == "done" and rstatus == "ok" and has_product(res)
        # 假通过检测：标记 ok 但既无产物文件、又 0 行数据（报告类有 report_url 不算）
        fake = st["status"] == "done" and rstatus == "ok" and not has_product(res) and (res.get("rows") or 0) == 0
        if ok:
            passed += 1
        if fake:
            suspicious += 1
        rows.append({
            "id": task["id"], "cat": task.get("cat", ""),
            "task_status": st["status"], "result_status": rstatus,
            "rows": res.get("rows"), "elapsed": res.get("elapsed"),
            "product": next((k for k in PRODUCT_KEYS if res.get(k)), None),
            "error": (res.get("error") or "")[:120],
            "fake_pass": fake,
        })
    total = len(rows)
    return {
        "total": total, "passed": passed, "fake_pass": suspicious,
        "pass_rate": f"{passed}/{total} ({passed / total * 100:.0f}%)" if total else "0/0",
        "rows": rows,
    }


def print_report(summary: dict) -> None:
    print("\n" + "=" * 78, flush=True)
    print(f"BENCHMARK RESULT  {summary['pass_rate']}  (fake-pass: {summary['fake_pass']})", flush=True)
    print("=" * 78, flush=True)
    for r in summary["rows"]:
        mark = "PASS" if r["result_status"] == "ok" and not r["fake_pass"] else ("FAKE" if r["fake_pass"] else "FAIL")
        print(f"[{r['id']:<5}] {mark:<4} st={r['task_status']:<8} rs={r['result_status'] or '-':<12} "
              f"rows={r['rows']} t={r['elapsed']}s prod={r['product'] or '-'} err={r['error'][:60]}", flush=True)
    print("=" * 78, flush=True)


async def main() -> None:
    ap = argparse.ArgumentParser(description="benchmark 自动并行测试")
    ap.add_argument("--tasks", help="逗号分隔的任务 id，默认全部")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--credits", type=int, default=200)
    args = ap.parse_args()

    tasks = load_tasks([s.strip() for s in args.tasks.split(",") if s.strip()] if args.tasks else None)
    print(f"任务数: {len(tasks)} | 并发: {args.concurrency} | 超时: {args.timeout}s", flush=True)

    async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
        token = await register_and_topup(client, args.credits)
        print("测试账号已注册并充值", flush=True)
        mapping = await submit_all(client, token, tasks, args.concurrency)
        print(f"提交成功: {len(mapping)}/{len(tasks)}，开始轮询...", flush=True)
        results = await poll_all(client, token, mapping, args.timeout)
        summary = summarize(results)
        print_report(summary)
        RESULT_FILE.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已保存: {RESULT_FILE}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
