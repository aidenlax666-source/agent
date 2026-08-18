"use client";

/**
 * 登录态管理页：保存你在目标网站的登录状态（微信/淘宝/小红书等），
 * 之后抓取任务会用这个登录态访问，避免被反爬拦截。
 * 原理：打开真实浏览器窗口 → 你手动登录 → 系统保存 Cookie → 任务复用。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import AppNav from "@/components/AppNav";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { KeyRound, Loader2, ExternalLink, CheckCircle2, AlertTriangle, Globe } from "lucide-react";
import { sessionsApi } from "@/lib/api";

const DEFAULT_URL = "https://www.baidu.com";

export default function SessionsPage() {
  const [url, setUrl] = useState(DEFAULT_URL);
  const [status, setStatus] = useState("idle"); // idle | opening | waiting | saved | error
  const [message, setMessage] = useState("");
  const [hasProfile, setHasProfile] = useState(false);
  const [busy, setBusy] = useState(false);
  const [expiresAt, setExpiresAt] = useState(0);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPoll = () => {
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  };

  const loadCheck = useCallback(async () => {
    try {
      const r = await sessionsApi.check();
      setHasProfile(Boolean(r.has_profile));
      setExpiresAt(r.expires_at || 0);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => {
    loadCheck();
    return stopPoll;
  }, [loadCheck]);

  const startLogin = async () => {
    if (busy) return;
    const target = (url || "").trim();
    if (!/^https?:\/\//.test(target)) {
      setStatus("error");
      setMessage("请输入完整的网址，如 https://www.example.com");
      return;
    }
    setBusy(true);
    setStatus("opening");
    setMessage(`正在打开浏览器（${target}），请在弹窗中登录...`);
    try {
      const r = await sessionsApi.startLogin(target);
      setStatus("waiting");
      setMessage("浏览器已打开，请登录。登录完成后点「我已完成登录」保存。");
      // 轮询登录状态（最多 4 分钟）
      stopPoll();
      let waited = 0;
      pollRef.current = setInterval(async () => {
        waited += 3;
        try {
          const s = await sessionsApi.status();
          if (s.status === "closed" || s.status === "saved") {
            stopPoll();
            setStatus("saved");
            setMessage(s.message || "登录状态已保存");
            setHasProfile(true);
          } else if (s.status === "timeout") {
            stopPoll();
            setStatus("error");
            setMessage("登录窗口超时，请重试");
          } else if (s.status === "error") {
            stopPoll();
            setStatus("error");
            setMessage(s.message || "登录失败");
          } else if (waited > 240) {
            stopPoll();
            setStatus("error");
            setMessage("等待超时，请重试");
          }
        } catch { /* 窗口未就绪，继续等 */ }
      }, 3000);
    } catch (e) {
      setStatus("error");
      setMessage(e instanceof Error ? e.message : "打开浏览器失败");
    } finally {
      setBusy(false);
    }
  };

  const finishLogin = async () => {
    setBusy(true);
    try {
      await sessionsApi.continueAfterLogin();
      setStatus("saved");
      setMessage("登录状态已保存");
      setHasProfile(true);
    } catch (e) {
      setStatus("error");
      setMessage(e instanceof Error ? e.message : "保存失败");
    } finally {
      setBusy(false);
    }
  };

  const statusBadge = () => {
    switch (status) {
      case "opening":
      case "waiting":
        return <span className="inline-flex items-center gap-1.5 text-sm text-blue-600 dark:text-blue-400"><Loader2 className="w-4 h-4 animate-spin" /> {message}</span>;
      case "saved":
        return <span className="inline-flex items-center gap-1.5 text-sm text-emerald-600 dark:text-emerald-400"><CheckCircle2 className="w-4 h-4" /> {message}</span>;
      case "error":
        return <span className="inline-flex items-center gap-1.5 text-sm text-red-600 dark:text-red-400"><AlertTriangle className="w-4 h-4" /> {message}</span>;
      default: {
        if (hasProfile && expiresAt > 0) {
          const remaining = Math.max(0, Math.round((expiresAt - Date.now() / 1000) / 60));
          if (remaining <= 0) {
            return <span className="inline-flex items-center gap-1.5 text-sm text-amber-600 dark:text-amber-400"><AlertTriangle className="w-4 h-4" /> 登录态已过期，需要重新登录</span>;
          }
          const exp = new Date(expiresAt * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
          return <span className="inline-flex items-center gap-1.5 text-sm text-emerald-600 dark:text-emerald-400"><CheckCircle2 className="w-4 h-4" /> 登录态有效，约 {exp} 过期（剩 {remaining} 分钟）</span>;
        }
        return <span className="text-sm text-muted-foreground">{hasProfile ? "已有登录态" : "尚未保存登录态（有效期 2 小时，用于登录抓取）"}</span>;
      }
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-indigo-50 via-white to-violet-50/60 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950/30">
      <AppNav />
      <main className="max-w-2xl mx-auto px-4 py-10 space-y-5">
        <div className="flex items-center gap-3">
          <div className="inline-flex items-center justify-center w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-indigo-500/30">
            <KeyRound className="w-5 h-5 text-white" />
          </div>
          <div>
            <h1 className="text-xl font-extrabold tracking-tight">登录态管理</h1>
            <p className="text-sm text-muted-foreground mt-0.5">保存目标网站的登录状态，抓取任务自动复用</p>
          </div>
        </div>

        <Card className="rounded-3xl border-indigo-200/70 dark:border-indigo-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5">
          <CardContent className="p-6 space-y-5">
            <div className="space-y-2">
              <Label htmlFor="url" className="text-slate-700 dark:text-slate-300">目标网站网址</Label>
              <div className="flex gap-2">
                <div className="relative flex-1">
                  <Globe className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                  <Input id="url" value={url} onChange={(e) => setUrl(e.target.value)}
                    placeholder="https://www.example.com"
                    className="rounded-xl pl-9 text-slate-900 dark:text-slate-100" />
                </div>
                <Button onClick={startLogin} disabled={busy} className="gap-1.5 rounded-xl bg-gradient-to-r from-indigo-600 via-violet-600 to-fuchsia-600 hover:opacity-90 text-white whitespace-nowrap">
                  {busy && status !== "waiting" ? <Loader2 className="w-4 h-4 animate-spin" /> : <ExternalLink className="w-4 h-4" />}
                  打开浏览器登录
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                如：https://weixin.qq.com / https://www.taobao.com / https://www.xiaohongshu.com（需要反爬的站点）
              </p>
            </div>

            <div className="rounded-xl bg-slate-50 dark:bg-slate-950/50 border border-slate-200 dark:border-slate-800 p-4">
              {statusBadge()}
            </div>

            {status === "waiting" && (
              <Button onClick={finishLogin} disabled={busy} variant="outline" className="w-full rounded-xl gap-1.5">
                <CheckCircle2 className="w-4 h-4 text-emerald-500" /> 我已完成登录，保存登录态
              </Button>
            )}

            <div className="text-xs text-muted-foreground space-y-1.5 leading-relaxed">
              <p className="font-medium text-slate-600 dark:text-slate-300">使用说明</p>
              <p>1. 输入需要登录的网站（如电商/社交平台），点「打开浏览器登录」</p>
              <p>2. 在弹出的真实浏览器窗口里手动登录（输账号密码 / 扫码）</p>
              <p>3. 点「我已完成登录」→ 登录态短时保存，抓取任务自动复用</p>
              <p className="text-amber-600 dark:text-amber-400">⏱️ 登录态有效期 2 小时（第三方 Cookie 会过期），过期需重新登录；仅保存在本机，不会上传</p>
            </div>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
