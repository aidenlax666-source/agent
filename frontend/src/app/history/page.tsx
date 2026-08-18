"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { miniApi } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { History as HistoryIcon, Loader2, ArrowRight, Inbox, Trash2 } from "lucide-react";
import AppNav from "@/components/AppNav";

const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  queued: { label: "排队中", cls: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300" },
  running: { label: "执行中", cls: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300" },
  done: { label: "已完成", cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" },
  confirmed: { label: "已完成", cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" },
  error: { label: "失败", cls: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300" },
  cancelled: { label: "已取消", cls: "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300" },
};

const FILTERS = ["全部", "排队中", "执行中", "已完成", "失败", "已取消"];

interface HistoryItem {
  id: string;
  requirement: string;
  status: string;
  message?: string;
  created_at?: number;
}

function fmtTime(ts?: number): string {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    });
  } catch {
    return "";
  }
}

export default function HistoryPage() {
  const [tasks, setTasks] = useState<HistoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("全部");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await miniApi.list(50);
      setTasks(r.tasks || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const [deleting, setDeleting] = useState<string | null>(null);

  const removeTask = async (id: string) => {
    if (deleting) return;
    if (!window.confirm("确定删除这条任务记录吗？删除后不可恢复。")) return;
    setDeleting(id);
    try {
      await miniApi.remove(id);
      setTasks((prev) => prev.filter((t) => t.id !== id));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(null);
    }
  };

  const shown = filter === "全部" ? tasks : tasks.filter((t) => STATUS_STYLE[t.status]?.label === filter);

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-white to-indigo-50/80 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950/40">
      <div className="pointer-events-none fixed inset-0 opacity-[0.35] dark:opacity-[0.15]"
        style={{ backgroundImage: "linear-gradient(rgba(99,102,241,0.08) 1px, transparent 1px), linear-gradient(90deg, rgba(99,102,241,0.08) 1px, transparent 1px)", backgroundSize: "44px 44px" }}
      />
      <AppNav />

      <main className="relative max-w-4xl mx-auto px-4 py-8 space-y-5">
        <div className="flex items-center gap-3 flex-wrap">
          <h1 className="text-xl font-extrabold tracking-tight flex items-center gap-2">
            <HistoryIcon className="w-5 h-5 text-indigo-500" /> 任务历史
          </h1>
          <span className="text-xs text-muted-foreground">{tasks.length} 条任务</span>
          <div className="ml-auto flex items-center gap-1 bg-white/70 dark:bg-slate-900/70 border border-slate-200/70 dark:border-slate-800/70 rounded-xl p-1">
            {FILTERS.map((f) => (
              <button
                key={f}
                onClick={() => setFilter(f)}
                className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                  filter === f
                    ? "bg-indigo-600 text-white shadow"
                    : "text-muted-foreground hover:text-foreground"
                }`}
              >
                {f}
              </button>
            ))}
          </div>
        </div>

        <Card className="rounded-3xl border-slate-200/70 dark:border-slate-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5">
          <CardContent className="p-4 sm:p-5">
            {loading ? (
              <div className="flex items-center justify-center gap-2 text-sm text-muted-foreground py-14">
                <Loader2 className="w-4 h-4 animate-spin text-indigo-500" /> 加载中…
              </div>
            ) : error ? (
              <div className="text-sm text-red-500 bg-red-50 dark:bg-red-950 rounded-xl px-4 py-3">{error}</div>
            ) : shown.length === 0 ? (
              <div className="flex flex-col items-center gap-3 py-14 text-muted-foreground">
                <Inbox className="w-8 h-8 opacity-40" />
                <p className="text-sm">{filter === "全部" ? "还没有任务记录，去提交第一个需求吧" : `没有「${filter}」的任务`}</p>
                {filter === "全部" && (
                  <Link href="/mini" className="inline-flex items-center gap-1.5 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:underline">
                    去工作台 <ArrowRight className="w-3.5 h-3.5" />
                  </Link>
                )}
              </div>
            ) : (
              <div className="space-y-2">
                {shown.map((h) => {
                  const st = STATUS_STYLE[h.status];
                  return (
                    <div key={h.id} className="group flex items-stretch gap-1 rounded-2xl border border-transparent hover:border-indigo-200/60 dark:hover:border-indigo-800/60 hover:bg-indigo-50/50 dark:hover:bg-indigo-950/30 transition-all">
                      <Link
                        href={`/mini?task=${h.id}`}
                        className="flex-1 min-w-0 block text-left rounded-2xl px-4 py-3 text-sm"
                      >
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-muted-foreground text-xs font-mono">#{h.id.slice(0, 8)}</span>
                          {st && <Badge className={`${st.cls} text-[10px]`}>{st.label}</Badge>}
                          <span className="text-xs text-muted-foreground">{fmtTime(h.created_at)}</span>
                          <ArrowRight className="w-3.5 h-3.5 ml-auto text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity" />
                        </div>
                        <div className="mt-1 text-foreground/85 line-clamp-2">{h.requirement}</div>
                        {h.message && <div className="mt-0.5 text-xs text-muted-foreground truncate">{h.message}</div>}
                      </Link>
                      <button
                        onClick={() => removeTask(h.id)}
                        disabled={deleting === h.id}
                        title="删除这条任务记录"
                        className="self-center p-2 mr-2 rounded-lg text-muted-foreground opacity-0 group-hover:opacity-100 hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/50 transition-all disabled:opacity-40 shrink-0"
                      >
                        {deleting === h.id ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        <footer className="text-center text-[11px] text-muted-foreground pb-6">
          点击任意任务可回到工作台查看结果 / 迭代修改 / 定时执行
        </footer>
      </main>
    </div>
  );
}
