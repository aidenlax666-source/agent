"use client";

/**
 * 定时与监控管理页：定时提醒 / 监控任务 的 开关（启用/禁用）+ 删除 + 新建。
 * 也可在工作台用一句话创建（"每天8点提醒我打卡" / "监控打开浏览器时提醒我"）。
 */
import { useCallback, useEffect, useState } from "react";
import AppNav from "@/components/AppNav";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { notificationsApi, type ReminderItem, type MonitorItem } from "@/lib/api";
import { Clock, MonitorCheck, Plus, Trash2, Loader2, Bell } from "lucide-react";

export default function AutomationsPage() {
  const [reminders, setReminders] = useState<ReminderItem[]>([]);
  const [monitors, setMonitors] = useState<MonitorItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // 新建表单
  const [remTime, setRemTime] = useState("09:00");
  const [remText, setRemText] = useState("");
  const [monType, setMonType] = useState<"window" | "screen">("window");
  const [monKeyword, setMonKeyword] = useState("");
  const [monAction, setMonAction] = useState("仅提醒");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await notificationsApi.automations();
      setReminders(d.reminders || []);
      setMonitors(d.monitors || []);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const toggleRem = async (id: string) => {
    try {
      await notificationsApi.toggleReminder(id);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    await load();
  };
  const delRem = async (id: string) => {
    try {
      await notificationsApi.deleteReminder(id);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    await load();
  };
  const toggleMon = async (id: string) => {
    try {
      await notificationsApi.toggleMonitor(id);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    await load();
  };
  const delMon = async (id: string) => {
    try {
      await notificationsApi.deleteMonitor(id);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    await load();
  };

  const createReminder = async () => {
    if (!remText.trim() || saving) return;
    setSaving(true);
    try {
      await notificationsApi.createReminder(remTime, remText.trim());
      setRemText("");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setSaving(false); }
  };

  const createMonitor = async () => {
    if (monType === "window" && !monKeyword.trim()) { setError("窗口监控需要填写关键词（如：浏览器、微信）"); return; }
    if (saving) return;
    setSaving(true);
    try {
      await notificationsApi.createMonitor({
        type: monType,
        keywords: monType === "window" ? monKeyword.trim() : "",
        condition: monType === "screen" ? "变化" : "",
        action_requirement: monAction.trim() === "仅提醒" ? "" : monAction.trim(),
        check_interval: monType === "window" ? 20 : 30,
      });
      setMonKeyword("");
      setMonAction("仅提醒");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    finally { setSaving(false); }
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-indigo-50/60 via-white to-fuchsia-50/40 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950/40">
      <AppNav />
      <main className="max-w-4xl mx-auto px-4 py-6 space-y-5">
        <div className="flex items-center gap-2">
          <h1 className="text-xl sm:text-2xl font-extrabold tracking-tight bg-gradient-to-r from-violet-600 via-fuchsia-600 to-indigo-600 dark:from-violet-400 dark:via-fuchsia-400 dark:to-indigo-400 bg-clip-text text-transparent">
            定时与监控
          </h1>
          <span className="text-xs text-muted-foreground">也可在工作台输入「每天8点提醒我打卡」「监控打开浏览器时提醒我」自动创建</span>
        </div>

        {error && <p className="text-sm text-red-500 bg-red-50 dark:bg-red-950 rounded-xl px-4 py-2.5">{error}</p>}

        {/* ---- 定时提醒 ---- */}
        <Card className="rounded-3xl border-violet-200/70 dark:border-violet-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-violet-500/5">
          <CardContent className="p-5 sm:p-6 space-y-4">
            <div className="flex items-center gap-2">
              <Clock className="w-4 h-4 text-violet-500" />
              <span className="text-sm font-semibold">⏰ 定时提醒</span>
              <span className="text-xs text-muted-foreground">每天固定时间提醒你</span>
            </div>

            {/* 新建 */}
            <div className="flex flex-col sm:flex-row gap-2 items-stretch">
              <input type="time" value={remTime} onChange={(e) => setRemTime(e.target.value)}
                className="w-32 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-violet-400" />
              <input value={remText} onChange={(e) => setRemText(e.target.value)} placeholder="提醒内容，如：喝水 / 打卡 / 开会"
                className="flex-1 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-violet-400" />
              <Button size="sm" onClick={createReminder} disabled={saving || !remText.trim()} className="gap-1.5 rounded-xl bg-gradient-to-r from-violet-600 to-fuchsia-600 hover:opacity-90 text-white">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} 添加提醒
              </Button>
            </div>

            {/* 列表 */}
            {loading ? <p className="text-sm text-muted-foreground py-4">加载中...</p> :
              reminders.length === 0 ? (
                <p className="text-sm text-slate-400 py-4">暂无提醒。添加一条，或在工作台输入「每天8点提醒我喝水」</p>
              ) : (
                <div className="space-y-2">
                  {reminders.map((r) => (
                    <div key={r.id} className={`flex items-center gap-3 rounded-xl border px-4 py-2.5 ${r.enabled ? "border-violet-200/60 dark:border-violet-800" : "border-slate-200 dark:border-slate-700 opacity-55"}`}>
                      <Bell className={`w-4 h-4 shrink-0 ${r.enabled ? "text-violet-500" : "text-slate-400"}`} />
                      <span className={`font-mono text-sm shrink-0 ${r.enabled ? "text-violet-600 dark:text-violet-300" : "text-slate-400"}`}>每天 {r.time}</span>
                      <span className={`flex-1 text-sm ${r.enabled ? "text-slate-700 dark:text-slate-200" : "text-slate-400"}`}>{r.text}</span>
                      {/* 开关 */}
                      <button onClick={() => toggleRem(r.id)} title={r.enabled ? "停用" : "启用"}
                        className={`relative w-10 h-5 rounded-full transition-colors shrink-0 ${r.enabled ? "bg-violet-500" : "bg-slate-300 dark:bg-slate-600"}`}>
                        <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${r.enabled ? "left-5" : "left-0.5"}`} />
                      </button>
                      {/* 删除 */}
                      <button onClick={() => delRem(r.id)} title="删除" className="p-1.5 rounded-lg text-muted-foreground hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/50 transition-colors shrink-0">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
          </CardContent>
        </Card>

        {/* ---- 监控任务 ---- */}
        <Card className="rounded-3xl border-emerald-200/70 dark:border-emerald-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-emerald-500/5">
          <CardContent className="p-5 sm:p-6 space-y-4">
            <div className="flex items-center gap-2">
              <MonitorCheck className="w-4 h-4 text-emerald-500" />
              <span className="text-sm font-semibold">👁️ 监控任务</span>
              <span className="text-xs text-muted-foreground">条件满足时提醒/执行任务（低成本：窗口匹配/屏幕变化检测）</span>
            </div>

            {/* 新建 */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <button onClick={() => setMonType("window")}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${monType === "window" ? "bg-emerald-500 text-white" : "bg-slate-100 dark:bg-slate-800 text-muted-foreground"}`}>监控软件/窗口</button>
                <button onClick={() => setMonType("screen")}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${monType === "screen" ? "bg-emerald-500 text-white" : "bg-slate-100 dark:bg-slate-800 text-muted-foreground"}`}>监控屏幕变化</button>
              </div>
              <div className="flex flex-col sm:flex-row gap-2">
                {monType === "window" ? (
                  <input value={monKeyword} onChange={(e) => setMonKeyword(e.target.value)} placeholder="窗口标题关键词，如：浏览器 / 微信 / 记事本"
                    className="flex-1 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-emerald-400" />
                ) : (
                  <input value="屏幕画面发生变化" disabled className="flex-1 rounded-xl border border-slate-200 dark:border-slate-700 bg-slate-50 dark:bg-slate-800 px-3 py-2 text-sm text-slate-400" />
                )}
                <input value={monAction} onChange={(e) => setMonAction(e.target.value)} placeholder="触发后动作（默认仅提醒，可填任务需求）"
                  className="flex-1 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-emerald-400" />
                <Button size="sm" onClick={createMonitor} disabled={saving || (monType === "window" && !monKeyword.trim())} className="gap-1.5 rounded-xl bg-gradient-to-r from-emerald-600 to-teal-600 hover:opacity-90 text-white">
                  {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} 添加监控
                </Button>
              </div>
            </div>

            {/* 列表 */}
            {loading ? <p className="text-sm text-muted-foreground py-4">加载中...</p> :
              monitors.length === 0 ? (
                <p className="text-sm text-slate-400 py-4">暂无监控任务。添加一条，或在工作台输入「监控打开浏览器时提醒我」</p>
              ) : (
                <div className="space-y-2">
                  {monitors.map((m) => (
                    <div key={m.id} className={`flex items-center gap-3 rounded-xl border px-4 py-2.5 ${m.enabled ? "border-emerald-200/60 dark:border-emerald-800" : "border-slate-200 dark:border-slate-700 opacity-55"}`}>
                      <span className={`text-xs font-mono px-1.5 py-0.5 rounded-md shrink-0 ${m.enabled ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" : "bg-slate-100 text-slate-400"}`}>
                        {m.monitor_type === "window" ? "窗口" : "屏幕"}
                      </span>
                      <span className={`flex-1 text-sm truncate ${m.enabled ? "text-slate-700 dark:text-slate-200" : "text-slate-400"}`}>
                        {m.keywords || m.condition || "（条件未填）"} <span className="text-slate-400">→</span> {m.action_requirement || "仅提醒"}
                      </span>
                      <span className="text-[11px] text-muted-foreground shrink-0 hidden sm:inline">每{m.check_interval}s</span>
                      {/* 开关 */}
                      <button onClick={() => toggleMon(m.id)} title={m.enabled ? "停用" : "启用"}
                        className={`relative w-10 h-5 rounded-full transition-colors shrink-0 ${m.enabled ? "bg-emerald-500" : "bg-slate-300 dark:bg-slate-600"}`}>
                        <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${m.enabled ? "left-5" : "left-0.5"}`} />
                      </button>
                      {/* 删除 */}
                      <button onClick={() => delMon(m.id)} title="删除" className="p-1.5 rounded-lg text-muted-foreground hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/50 transition-colors shrink-0">
                        <Trash2 className="w-4 h-4" />
                      </button>
                    </div>
                  ))}
                </div>
              )}
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
