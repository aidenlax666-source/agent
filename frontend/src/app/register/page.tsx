"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Loader2, UserPlus, CheckCircle2, Eye, EyeOff, AlertCircle, Check } from "lucide-react";
import { authApi, setAuthToken } from "@/lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function passwordStrength(pwd: string): { score: number; label: string; color: string } {
  if (!pwd) return { score: 0, label: "", color: "" };
  let s = 0;
  if (pwd.length >= 6) s++;
  if (pwd.length >= 10) s++;
  if (/[A-Z]/.test(pwd) && /[a-z]/.test(pwd)) s++;
  if (/\d/.test(pwd)) s++;
  if (/[^A-Za-z0-9]/.test(pwd)) s++;
  const labels = ["", "太短", "较弱", "一般", "较强", "很强"];
  const colors = ["", "bg-red-500", "bg-orange-500", "bg-yellow-500", "bg-lime-500", "bg-emerald-500"];
  return { score: Math.min(s, 5), label: labels[Math.min(s, 5)], color: colors[Math.min(s, 5)] };
}

export default function RegisterPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [name, setName] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [fieldError, setFieldError] = useState<{ name?: string; email?: string; password?: string; confirm?: string }>({});
  const strength = passwordStrength(password);

  const validate = () => {
    const fe: { name?: string; email?: string; password?: string; confirm?: string } = {};
    if (name.trim() && name.trim().length > 20) fe.name = "昵称最多 20 个字";
    if (!email.trim()) fe.email = "请输入邮箱";
    else if (!EMAIL_RE.test(email.trim())) fe.email = "邮箱格式不正确，示例：you@example.com";
    if (!password) fe.password = "请设置密码";
    else if (password.length < 4) fe.password = "密码至少 4 位";
    else if (password.length < 6) fe.password = "建议密码至少 6 位（当前仅 " + password.length + " 位）";
    if (!confirm) fe.confirm = "请再次输入密码";
    else if (confirm !== password) fe.confirm = "两次输入的密码不一致";
    setFieldError(fe);
    return Object.keys(fe).length === 0;
  };

  const handleRegister = async () => {
    if (loading) return;
    setError("");
    if (!validate()) return;
    setLoading(true);
    try {
      const result = await authApi.register(email.trim(), password, name.trim() || undefined);
      setAuthToken(result.access_token);
      router.push("/mini");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "注册失败，请重试");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4 py-8 bg-gradient-to-br from-indigo-50 via-white to-fuchsia-50 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950/60">
      <div className="pointer-events-none fixed inset-0 opacity-[0.35] dark:opacity-[0.15]"
        style={{ backgroundImage: "linear-gradient(rgba(99,102,241,0.08) 1px, transparent 1px), linear-gradient(90deg, rgba(99,102,241,0.08) 1px, transparent 1px)", backgroundSize: "44px 44px" }}
      />
      <Card className="relative w-full max-w-md rounded-3xl border-slate-200/70 dark:border-slate-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5 overflow-hidden">
        <div className="h-1.5 bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500" />
        <CardContent className="p-8 space-y-5">
          <div className="text-center">
            <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 mb-4 shadow-lg shadow-indigo-500/30">
              <UserPlus className="w-6 h-6 text-white" />
            </div>
            <h2 className="text-xl font-bold text-slate-900 dark:text-slate-50">注册账号</h2>
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">每个账号的任务和数据互相独立，互不可见</p>
          </div>

          <div className="space-y-2">
            <Label htmlFor="name" className="text-slate-700 dark:text-slate-300">昵称 <span className="text-slate-400">（可选）</span></Label>
            <Input id="name" placeholder="你的昵称" value={name}
              onChange={(e) => { setName(e.target.value); if (fieldError.name) setFieldError((f) => ({ ...f, name: undefined })); }}
              disabled={loading} className="rounded-xl" />
            {fieldError.name && (
              <p className="text-xs text-red-600 dark:text-red-400 flex items-center gap-1 mt-1">
                <AlertCircle className="w-3 h-3" /> {fieldError.name}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="email" className="text-slate-700 dark:text-slate-300">邮箱</Label>
            <Input id="email" type="email" placeholder="you@example.com"
              value={email}
              onChange={(e) => { setEmail(e.target.value); if (fieldError.email) setFieldError((f) => ({ ...f, email: undefined })); }}
              disabled={loading}
              className={`rounded-xl ${fieldError.email ? "border-red-400 focus-visible:ring-red-400" : ""}`} />
            {fieldError.email && (
              <p className="text-xs text-red-600 dark:text-red-400 flex items-center gap-1 mt-1">
                <AlertCircle className="w-3 h-3" /> {fieldError.email}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="password" className="text-slate-700 dark:text-slate-300">密码</Label>
            <div className="relative">
              <Input id="password" type={showPassword ? "text" : "password"} placeholder="至少 6 位，含字母和数字更安全"
                value={password}
                onChange={(e) => { setPassword(e.target.value); if (fieldError.password) setFieldError((f) => ({ ...f, password: undefined })); }}
                disabled={loading}
                className={`rounded-xl pr-10 ${fieldError.password ? "border-red-400 focus-visible:ring-red-400" : ""}`} />
              <button type="button" tabIndex={-1} onClick={() => setShowPassword((v) => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 transition-colors"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}>
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            {password && (
              <div className="mt-1.5">
                <div className="flex gap-1">
                  {[1, 2, 3, 4, 5].map((i) => (
                    <div key={i} className={`h-1 flex-1 rounded-full transition-colors ${i <= strength.score ? strength.color : "bg-slate-200 dark:bg-slate-700"}`} />
                  ))}
                </div>
                {strength.label && (
                  <p className="text-xs text-slate-500 dark:text-slate-400 mt-1">
                    密码强度：<span className="font-medium">{strength.label}</span>
                  </p>
                )}
              </div>
            )}
            {fieldError.password && (
              <p className="text-xs text-red-600 dark:text-red-400 flex items-center gap-1 mt-1">
                <AlertCircle className="w-3 h-3" /> {fieldError.password}
              </p>
            )}
          </div>

          <div className="space-y-2">
            <Label htmlFor="confirm" className="text-slate-700 dark:text-slate-300">确认密码</Label>
            <Input id="confirm" type={showPassword ? "text" : "password"} placeholder="再次输入密码"
              value={confirm}
              onChange={(e) => { setConfirm(e.target.value); if (fieldError.confirm) setFieldError((f) => ({ ...f, confirm: undefined })); }}
              disabled={loading}
              className={`rounded-xl ${fieldError.confirm ? "border-red-400 focus-visible:ring-red-400" : ""}`}
              onKeyDown={(e) => e.key === "Enter" && handleRegister()} />
            {confirm && confirm === password && password.length >= 4 && (
              <p className="text-xs text-emerald-600 dark:text-emerald-400 flex items-center gap-1 mt-1">
                <Check className="w-3 h-3" /> 两次密码一致
              </p>
            )}
            {fieldError.confirm && (
              <p className="text-xs text-red-600 dark:text-red-400 flex items-center gap-1 mt-1">
                <AlertCircle className="w-3 h-3" /> {fieldError.confirm}
              </p>
            )}
          </div>

          {error && (
            <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 p-3 rounded-xl">
              {error}
            </div>
          )}

          <Button className="w-full h-11 rounded-xl bg-gradient-to-r from-indigo-600 via-violet-600 to-fuchsia-600 hover:from-indigo-700 hover:via-violet-700 hover:to-fuchsia-700 shadow-lg shadow-indigo-500/30 text-white font-medium"
            onClick={handleRegister} disabled={loading}>
            {loading ? <><Loader2 className="w-4 h-4 animate-spin" /> 注册中...</> : "创建账号"}
          </Button>

          <div className="flex items-center gap-1.5 justify-center text-[11px] text-slate-500 dark:text-slate-400">
            <CheckCircle2 className="w-3 h-3 text-emerald-500" />
            注册即送 10 积分，每次任务消耗 1 积分
          </div>

          <p className="text-xs text-center text-slate-500 dark:text-slate-400">
            已有账号？{" "}
            <Link href="/login" className="text-indigo-600 hover:underline font-medium">去登录</Link>
          </p>
        </CardContent>
      </Card>
    </div>
  );
}
