"use client";

/**
 * 系统监控页：任务统计（总量/今日/成功率/平均耗时）+ worker 健康 + 队列深度。
 * 运维视角：看系统是否健康、worker 是否在跑、队列是否积压。
 */
import { useCallback, useEffect, useState } from "react";
import AppNav from "@/components/AppNav";
import { Card, CardContent } from "@/components/ui/card";
import { RefreshCw, Activity, Cpu, Layers, CheckCircle2, XCircle, Timer, Server, Radio, Loader2 } from "lucide-react";
import { systemApi, type SystemStats } from "@/lib/api";

function fmtTime(ts: number): string {
  if (!ts) return "-";
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "-";
  }
}

const STATUS_META: Record<string, { label: string; cls: string }> = {
  done: { label: "成功", cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" },
  failed: { label: "失败", cls: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300" },
  running: { label: "执行中", cls: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300" },
  queued: { label: "排队中", cls: "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300" },
};

export default function MonitorPage() {
  const [stats, setStats] = useState<SystemStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    setRefreshing(true);
    setError(null);
    try {
      const s = await systemApi.stats();
      setStats(s);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(() => load(true), 15000); // 每 15s 静默刷新
    return () => clearInterval(t);
  }, [load]);

  const pct = (v: number | null | undefined): string => (v === null || v === undefined ? "-" : `${(v * 100).toFixed(0)}%`);

  return (
    <main className="min-h-screen bg-slate-50 dark:bg-slate-950">
      <AppNav />
      <div className="max-w-6xl mx-auto px-4 py-8">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <Activity className="w-6 h-6 text-indigo-500" /> 系统监控
            </h1>
            <p className="text-sm text-muted-foreground mt-1">任务执行统计 · worker 健康 · 队列深度（自动刷新）</p>
          </div>
          <button
            onClick={() => load()}
            disabled={refreshing}
            className="inline-flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-medium bg-indigo-50 text-indigo-600 dark:bg-indigo-950/60 dark:text-indigo-300 hover:bg-indigo-100 dark:hover:bg-indigo-900/60 transition-colors disabled:opacity-50"
          >
            <RefreshCw className={`w-4 h-4 ${refreshing ? "animate-spin" : ""}`} /> 刷新
          </button>
        </div>

        {error && (
          <div className="mb-4 rounded-xl border border-red-200 dark:border-red-800 bg-red-50 dark:bg-red-950/50 px-4 py-3 text-sm text-red-600 dark:text-red-300">
            {error}
          </div>
        )}

        {loading && !stats ? (
          <div className="flex items-center justify-center py-24 text-muted-foreground">
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> 加载中...
          </div>
        ) : stats ? (
          <div className="space-y-6">
            {/* 核心指标 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              <Card>
                <CardContent className="p-5">
                  <p className="text-xs text-muted-foreground flex items-center gap-1"><Layers className="w-3.5 h-3.5" /> 任务总量</p>
                  <p className="mt-1 text-3xl font-bold">{stats.total}</p>
                  <p className="text-xs text-muted-foreground mt-1">今日新增 {stats.today}</p>
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-5">
                  <p className="text-xs text-muted-foreground flex items-center gap-1"><CheckCircle2 className="w-3.5 h-3.5" /> 今日成功率</p>
                  <p className="mt-1 text-3xl font-bold text-emerald-600 dark:text-emerald-400">{pct(stats.success_rate_today)}</p>
                  <p className="text-xs text-muted-foreground mt-1">
                    <span className="text-emerald-500">{stats.done_today} 成功</span> · <span className="text-red-500">{stats.failed_today} 失败</span>
                  </p>
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-5">
                  <p className="text-xs text-muted-foreground flex items-center gap-1"><Timer className="w-3.5 h-3.5" /> 平均耗时</p>
                  <p className="mt-1 text-3xl font-bold">
                    {stats.avg_elapsed_today === null || stats.avg_elapsed_today === undefined ? "-" : `${stats.avg_elapsed_today}s`}
                  </p>
                  <p className="text-xs text-muted-foreground mt-1">今日完成任务的耗时均值</p>
                </CardContent>
              </Card>
              <Card>
                <CardContent className="p-5">
                  <p className="text-xs text-muted-foreground flex items-center gap-1"><Radio className="w-3.5 h-3.5" /> 运行模式</p>
                  <p className="mt-1 text-3xl font-bold">{stats.redis_enabled ? "云架构" : "单机"}</p>
                  <p className="text-xs text-muted-foreground mt-1">{stats.redis_enabled ? "Redis 多实例" : "进程内调度"}</p>
                </CardContent>
              </Card>
            </div>

            {/* 状态分布 */}
            <Card>
              <CardContent className="p-5">
                <p className="text-sm font-semibold mb-3">任务状态分布</p>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(stats.by_status || {}).length === 0 ? (
                    <p className="text-xs text-muted-foreground">暂无任务</p>
                  ) : (
                    Object.entries(stats.by_status).map(([status, count]) => {
                      const meta = STATUS_META[status] || { label: status, cls: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300" };
                      return (
                        <span key={status} className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium ${meta.cls}`}>
                          {meta.label} <b>{count}</b>
                        </span>
                      );
                    })
                  )}
                </div>
              </CardContent>
            </Card>

            {/* worker + 队列 */}
            <div className="grid md:grid-cols-2 gap-4">
              <Card>
                <CardContent className="p-5">
                  <p className="text-sm font-semibold mb-3 flex items-center gap-1.5">
                    <Server className="w-4 h-4 text-indigo-500" /> Worker 健康
                  </p>
                  {stats.redis_enabled ? (
                    stats.workers.length === 0 ? (
                      <p className="text-xs text-amber-600 dark:text-amber-400">⚠️ 没有活跃 worker——队列任务不会被消费！</p>
                    ) : (
                      <div className="space-y-2">
                        {stats.workers.map((w) => (
                          <div key={w.id} className="flex items-center gap-2 text-xs rounded-lg border border-slate-100 dark:border-slate-800 px-3 py-2">
                            <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
                            <span className="font-mono text-slate-600 dark:text-slate-300">{w.id}</span>
                            <span className="ml-auto text-muted-foreground">心跳 {fmtTime(w.last_heartbeat)}</span>
                          </div>
                        ))}
                      </div>
                    )
                  ) : (
                    <p className="text-xs text-muted-foreground">单机模式：任务由本实例进程内调度，无独立 worker 心跳。</p>
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardContent className="p-5">
                  <p className="text-sm font-semibold mb-3 flex items-center gap-1.5">
                    <Cpu className="w-4 h-4 text-violet-500" /> 队列深度
                  </p>
                  {stats.redis_enabled ? (
                    <div className="grid grid-cols-2 gap-3">
                      <div className="rounded-xl border border-slate-100 dark:border-slate-800 px-4 py-3 text-center">
                        <p className="text-2xl font-bold">{stats.queue_depth?.normal ?? 0}</p>
                        <p className="text-xs text-muted-foreground">普通队列</p>
                      </div>
                      <div className="rounded-xl border border-violet-100 dark:border-violet-900 px-4 py-3 text-center">
                        <p className="text-2xl font-bold text-violet-600 dark:text-violet-300">{stats.queue_depth?.high ?? 0}</p>
                        <p className="text-xs text-muted-foreground">高优队列</p>
                      </div>
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground">单机模式：任务直接进程内执行，无 Redis 队列。</p>
                  )}
                </CardContent>
              </Card>
            </div>
          </div>
        ) : null}
      </div>
    </main>
  );
}
