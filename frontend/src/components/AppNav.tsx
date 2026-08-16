"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Sparkles, History, QrCode, Zap, LogIn, LogOut, User as UserIcon, Coins, FolderGit2, Bell, X, Trash2, Clock, MonitorCheck, AlarmClock } from "lucide-react";
import { authApi, getAuthToken, setAuthToken, notificationsApi, type NotificationItem, type ReminderItem, type MonitorItem } from "@/lib/api";
import type { User } from "@/lib/types";

const LINKS = [
  { href: "/mini", label: "工作台", icon: Sparkles },
  { href: "/claude", label: "改码助手", icon: FolderGit2 },
  { href: "/automations", label: "定时监控", icon: AlarmClock },
  { href: "/history", label: "任务历史", icon: History },
  { href: "/gallery", label: "分享中心", icon: QrCode },
];

function displayName(user: User): string {
  if (user.name && user.name.trim()) return user.name.trim();
  return (user.email || "用户").split("@")[0];
}

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** 全局顶部导航：工作台 / 改码助手 / 任务历史 / 分享中心 + 消息中心 + 登录状态 */
export default function AppNav() {
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);

  // ---- 消息中心 ----
  const [panelOpen, setPanelOpen] = useState(false);
  const [tab, setTab] = useState<"msg" | "auto">("msg");
  const [items, setItems] = useState<NotificationItem[]>([]);
  const [unread, setUnread] = useState(0);
  const [reminders, setReminders] = useState<ReminderItem[]>([]);
  const [monitors, setMonitors] = useState<MonitorItem[]>([]);
  const [panelError, setPanelError] = useState<string | null>(null);
  const lastUnread = useRef(0);

  const loadNotifs = useCallback(async () => {
    try {
      const d = await notificationsApi.list();
      setItems(d.items || []);
      setUnread(d.unread || 0);
      // 有新增未读且页面不可见 → 浏览器通知
      if (d.unread > lastUnread.current && typeof document !== "undefined" && document.hidden
          && typeof Notification !== "undefined" && Notification.permission === "granted") {
        const n = (d.items || [])[0];
        if (n) new Notification(n.title || "新消息", { body: n.content || "" });
      }
      lastUnread.current = d.unread || 0;
    } catch { /* 静默 */ }
  }, []);

  const loadAutomations = useCallback(async () => {
    try {
      const d = await notificationsApi.automations();
      setReminders(d.reminders || []);
      setMonitors(d.monitors || []);
    } catch { /* 静默 */ }
  }, []);

  useEffect(() => {
    loadNotifs();
    const t = setInterval(loadNotifs, 30000); // 每 30 秒轮询未读消息
    return () => clearInterval(t);
  }, [loadNotifs]);

  // 打开面板时刷新
  const openPanel = async () => {
    setPanelOpen((v) => !v);
    if (!panelOpen) {
      // 用户手势内请求通知权限（Chrome 要求）
      if (typeof Notification !== "undefined" && Notification.permission === "default") {
        try { await Notification.requestPermission(); } catch { /* 忽略 */ }
      }
      setPanelError(null);
      await loadNotifs();
      await loadAutomations();
    }
  };

  const markAllRead = async () => {
    try {
      await notificationsApi.markRead();
      setUnread(0);
      setItems((prev) => prev.map((i) => ({ ...i, read: 1 })));
    } catch { /* 忽略 */ }
  };

  const removeReminder = async (id: string) => {
    try {
      await notificationsApi.deleteReminder(id);
      setPanelError(null);
    } catch (e) {
      setPanelError(e instanceof Error ? e.message : "删除失败");
    }
    await loadAutomations(); // 以服务端为准刷新，避免 stale 列表
  };

  const toggleReminder = async (id: string) => {
    try {
      await notificationsApi.toggleReminder(id);
      setPanelError(null);
    } catch (e) {
      setPanelError(e instanceof Error ? e.message : "操作失败");
    }
    await loadAutomations();
  };

  const removeMonitor = async (id: string) => {
    try {
      await notificationsApi.deleteMonitor(id);
      setPanelError(null);
    } catch (e) {
      setPanelError(e instanceof Error ? e.message : "删除失败");
    }
    await loadAutomations();
  };

  const toggleMonitor = async (id: string) => {
    try {
      await notificationsApi.toggleMonitor(id);
      setPanelError(null);
    } catch (e) {
      setPanelError(e instanceof Error ? e.message : "操作失败");
    }
    await loadAutomations();
  };

  const loadUser = useCallback(async () => {
    setChecking(true);
    try {
      if (getAuthToken()) {
        const me = await authApi.getMe();
        // 匿名会话（anon_*@auto.local）不算登录用户
        if (me.email && !me.email.startsWith("anon_")) {
          setUser(me);
        } else {
          setUser(null);
        }
      } else {
        setUser(null);
      }
    } catch {
      // token 失效 → 清除
      setAuthToken(null);
      setUser(null);
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    loadUser();
  }, [loadUser]);

  const handleLogout = () => {
    setAuthToken(null);
    setUser(null);
    window.location.href = "/mini";
  };

  return (
    <header className="sticky top-0 z-20 border-b border-slate-200/60 dark:border-slate-800/60 bg-white/70 dark:bg-slate-950/70 backdrop-blur-xl">
      <div className="max-w-6xl mx-auto px-4 h-16 flex items-center gap-3">
        <Link href="/" className="flex items-center gap-2.5 group shrink-0">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 flex items-center justify-center shadow-lg shadow-indigo-500/30 group-hover:scale-105 transition-transform">
            <Zap className="w-5 h-5 text-white" />
          </div>
          <div className="leading-tight hidden md:block">
            <span className="text-base font-bold tracking-tight">AI 自动化工作台</span>
            <span className="block text-[11px] text-muted-foreground">一句话 → 生成 · 执行 · 校验 · 迭代</span>
          </div>
        </Link>

        <nav className="ml-auto flex items-center gap-1">
          {LINKS.map(({ href, label, icon: Icon }) => {
            const active = pathname === href;
            return (
              <Link
                key={href}
                href={href}
                className={`inline-flex items-center gap-1.5 rounded-xl px-3 py-2 text-sm font-medium transition-colors ${
                  active
                    ? "bg-indigo-50 text-indigo-600 dark:bg-indigo-950/60 dark:text-indigo-300"
                    : "text-muted-foreground hover:text-foreground hover:bg-slate-100 dark:hover:bg-slate-800/60"
                }`}
              >
                <Icon className="w-4 h-4" />
                <span className="hidden lg:inline">{label}</span>
              </Link>
            );
          })}

          {/* 消息中心（铃铛） */}
          <div className="relative">
            <button
              onClick={openPanel}
              title="消息与定时/监控"
              className="relative inline-flex items-center justify-center w-9 h-9 rounded-xl text-muted-foreground hover:text-foreground hover:bg-slate-100 dark:hover:bg-slate-800/60 transition-colors"
            >
              <Bell className="w-4.5 h-4.5" />
              {unread > 0 && (
                <span className="absolute -top-0.5 -right-0.5 min-w-[16px] h-4 px-1 rounded-full bg-red-500 text-white text-[10px] font-bold flex items-center justify-center">
                  {unread > 99 ? "99+" : unread}
                </span>
              )}
            </button>

            {panelOpen && (
              <div className="absolute right-0 mt-2 w-80 sm:w-96 rounded-2xl border border-slate-200 dark:border-slate-700 bg-white/95 dark:bg-slate-900/95 backdrop-blur-xl shadow-2xl overflow-hidden z-30">
                <div className="flex items-center gap-1 border-b border-slate-100 dark:border-slate-800 px-3 py-2">
                  <button
                    onClick={() => setTab("msg")}
                    className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${tab === "msg" ? "bg-indigo-50 text-indigo-600 dark:bg-indigo-950/60 dark:text-indigo-300" : "text-muted-foreground hover:bg-slate-100 dark:hover:bg-slate-800/60"}`}
                  >
                    消息{unread > 0 ? ` (${unread})` : ""}
                  </button>
                  <button
                    onClick={() => { setTab("auto"); loadAutomations(); }}
                    className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition-colors ${tab === "auto" ? "bg-violet-50 text-violet-600 dark:bg-violet-950/60 dark:text-violet-300" : "text-muted-foreground hover:bg-slate-100 dark:hover:bg-slate-800/60"}`}
                  >
                    定时与监控
                  </button>
                  <button onClick={() => setPanelOpen(false)} className="p-1 text-muted-foreground hover:text-foreground" title="关闭">
                    <X className="w-4 h-4" />
                  </button>
                </div>

                {tab === "msg" ? (
                  <div className="max-h-80 overflow-auto">
                    {items.length === 0 ? (
                      <p className="text-center text-xs text-muted-foreground py-8">暂无消息</p>
                    ) : (                      <>
                        {items.map((n) => (
                          <div key={n.id} className={`px-4 py-2.5 border-b border-slate-50 dark:border-slate-800/60 ${n.read ? "opacity-60" : ""}`}>
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-medium text-slate-700 dark:text-slate-200">{n.title}</span>
                              {!n.read && <span className="w-1.5 h-1.5 rounded-full bg-red-500" />}
                              <span className="ml-auto text-[10px] text-muted-foreground">{fmtTime(n.created_at)}</span>
                            </div>
                            <p className="mt-0.5 text-xs text-slate-500 dark:text-slate-400 break-words">{n.content}</p>
                          </div>
                        ))}
                        <button onClick={markAllRead} className="w-full py-2 text-xs text-indigo-600 dark:text-indigo-400 hover:bg-indigo-50 dark:hover:bg-indigo-950/40 transition-colors">
                          全部标为已读
                        </button>
                      </>
                    )}
                  </div>
                ) : (
                  <div className="max-h-80 overflow-auto p-3 space-y-3">
                    {panelError && (
                      <p className="text-xs text-red-500 bg-red-50 dark:bg-red-950 rounded-lg px-3 py-2">{panelError}</p>
                    )}
                    <div>
                      <p className="text-[11px] font-medium text-muted-foreground mb-1.5 flex items-center gap-1">
                        <Clock className="w-3 h-3" /> 定时提醒
                      </p>
                      {reminders.length === 0 ? (
                        <p className="text-xs text-slate-400">无。可在工作台输入「每天8点提醒我打卡」</p>
                      ) : (
                        reminders.map((r) => (
                          <div key={r.id} className={`flex items-center gap-2 rounded-xl border px-2.5 py-1.5 mb-1 text-xs ${r.enabled ? "border-violet-200/60 dark:border-violet-800" : "border-slate-200 dark:border-slate-700 opacity-55"}`}>
                            <span className={`font-mono shrink-0 ${r.enabled ? "text-violet-600 dark:text-violet-300" : "text-slate-400"}`}>每天 {r.time}</span>
                            <span className="flex-1 truncate text-slate-600 dark:text-slate-300">{r.text}</span>
                            <button
                              onClick={() => toggleReminder(r.id)}
                              title={r.enabled ? "停用" : "启用"}
                              className={`relative w-8 h-4.5 rounded-full transition-colors shrink-0 ${r.enabled ? "bg-violet-500" : "bg-slate-300 dark:bg-slate-600"}`}
                            >
                              <span className={`absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-all ${r.enabled ? "left-4" : "left-0.5"}`} />
                            </button>
                            <button onClick={() => removeReminder(r.id)} className="text-muted-foreground hover:text-red-500 shrink-0" title="删除">
                              <Trash2 className="w-3 h-3" />
                            </button>
                          </div>
                        ))
                      )}
                    </div>
                    <div>
                      <p className="text-[11px] font-medium text-muted-foreground mb-1.5 flex items-center gap-1">
                        <MonitorCheck className="w-3 h-3" /> 监控任务
                      </p>
                      {monitors.length === 0 ? (
                        <p className="text-xs text-slate-400">无。可输入「监控打开浏览器时提醒我」</p>
                      ) : (
                        monitors.map((m) => (
                          <div key={m.id} className={`flex items-center gap-2 rounded-xl border px-2.5 py-1.5 mb-1 text-xs ${m.enabled ? "border-emerald-200/60 dark:border-emerald-800" : "border-slate-200 dark:border-slate-700 opacity-55"}`}>
                            <span className={`font-mono shrink-0 ${m.enabled ? "text-emerald-600 dark:text-emerald-300" : "text-slate-400"}`}>{m.monitor_type === "window" ? "窗口" : "屏幕"}</span>
                            <span className="flex-1 truncate text-slate-600 dark:text-slate-300">
                              {m.keywords || m.condition || "（条件未填）"} → {m.action_requirement || "仅提醒"}
                            </span>
                            <button
                              onClick={() => toggleMonitor(m.id)}
                              title={m.enabled ? "停用" : "启用"}
                              className={`relative w-8 h-4.5 rounded-full transition-colors shrink-0 ${m.enabled ? "bg-emerald-500" : "bg-slate-300 dark:bg-slate-600"}`}
                            >
                              <span className={`absolute top-0.5 w-3.5 h-3.5 rounded-full bg-white shadow transition-all ${m.enabled ? "left-4" : "left-0.5"}`} />
                            </button>
                            <button onClick={() => removeMonitor(m.id)} className="text-muted-foreground hover:text-red-500 shrink-0" title="删除">
                              <Trash2 className="w-3 h-3" />
                            </button>
                          </div>
                        ))
                      )}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>

          {/* 登录状态 */}
          <div className="ml-2 pl-2 border-l border-slate-200 dark:border-slate-700 flex items-center gap-1.5">
            {checking ? (
              <span className="w-8 h-8 rounded-full bg-slate-100 dark:bg-slate-800 animate-pulse" />
            ) : user ? (
              <>
                <span className="hidden sm:inline-flex items-center gap-1.5 text-xs font-medium text-slate-600 dark:text-slate-300">
                  <UserIcon className="w-3.5 h-3.5 text-indigo-500" />
                  {displayName(user)}
                </span>
                {typeof user.credits === "number" && (
                  <span className="hidden sm:inline-flex items-center gap-1 text-[11px] text-amber-600 dark:text-amber-400 font-medium">
                    <Coins className="w-3 h-3" /> {user.credits}
                  </span>
                )}
                <button
                  onClick={handleLogout}
                  title="退出登录"
                  className="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-xs text-muted-foreground hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/50 transition-colors"
                >
                  <LogOut className="w-3.5 h-3.5" />
                  <span className="hidden sm:inline">退出</span>
                </button>
              </>
            ) : (
              <>
                <Link
                  href="/register"
                  className="inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs font-medium text-slate-600 dark:text-slate-300 hover:bg-slate-100 dark:hover:bg-slate-800/60 transition-colors"
                >
                  注册
                </Link>
                <Link
                  href="/login"
                  className="inline-flex items-center gap-1.5 rounded-xl px-3 py-1.5 text-xs font-semibold text-white bg-gradient-to-r from-indigo-600 to-violet-600 hover:opacity-90 shadow-sm shadow-indigo-500/30 transition-all"
                >
                  <LogIn className="w-3.5 h-3.5" /> 登录
                </Link>
              </>
            )}
          </div>
        </nav>
      </div>
    </header>
  );
}
