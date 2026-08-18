"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Loader2, LogIn, Sparkles, Eye, EyeOff, AlertCircle } from "lucide-react";
import { authApi, setAuthToken } from "@/lib/api";

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [fieldError, setFieldError] = useState<{ email?: string; password?: string }>({});

  const validate = () => {
    const fe: { email?: string; password?: string } = {};
    if (!email.trim()) fe.email = "请输入邮箱";
    else if (!EMAIL_RE.test(email.trim())) fe.email = "邮箱格式不正确，示例：you@example.com";
    if (!password) fe.password = "请输入密码";
    else if (password.length < 4) fe.password = "密码至少 4 位";
    setFieldError(fe);
    return Object.keys(fe).length === 0;
  };

  const handleLogin = async () => {
    if (loading) return;
    setError("");
    if (!validate()) return;
    setLoading(true);
    try {
      const result = await authApi.login(email.trim(), password);
      setAuthToken(result.access_token);
      router.push("/mini");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "登录失败，请检查邮箱和密码");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center px-4 bg-gradient-to-br from-indigo-50 via-white to-fuchsia-50 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950/60">
      <div className="pointer-events-none fixed inset-0 opacity-[0.35] dark:opacity-[0.15]"
        style={{ backgroundImage: "linear-gradient(rgba(99,102,241,0.08) 1px, transparent 1px), linear-gradient(90deg, rgba(99,102,241,0.08) 1px, transparent 1px)", backgroundSize: "44px 44px" }}
      />
      <Card className="relative w-full max-w-md rounded-3xl border-slate-200/70 dark:border-slate-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5 overflow-hidden">
        <div className="h-1.5 bg-gradient-to-r from-indigo-500 via-violet-500 to-fuchsia-500" />
        <CardContent className="p-8 space-y-5">
          <div className="text-center">
            <div className="inline-flex items-center justify-center w-12 h-12 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 mb-4 shadow-lg shadow-indigo-500/30">
              <LogIn className="w-6 h-6 text-white" />
            </div>
            <h2 className="text-xl font-bold text-slate-900 dark:text-slate-50">登录</h2>
            <p className="text-sm text-slate-500 dark:text-slate-400 mt-1">登录后，你的任务和数据只属于你的账号</p>
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
              <Input id="password" type={showPassword ? "text" : "password"} placeholder="请输入密码"
                value={password}
                onChange={(e) => { setPassword(e.target.value); if (fieldError.password) setFieldError((f) => ({ ...f, password: undefined })); }}
                disabled={loading}
                onKeyDown={(e) => e.key === "Enter" && handleLogin()}
                className={`rounded-xl pr-10 ${fieldError.password ? "border-red-400 focus-visible:ring-red-400" : ""}`} />
              <button type="button" tabIndex={-1} onClick={() => setShowPassword((v) => !v)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-400 hover:text-slate-600 dark:hover:text-slate-300 transition-colors"
                aria-label={showPassword ? "隐藏密码" : "显示密码"}>
                {showPassword ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            {fieldError.password && (
              <p className="text-xs text-red-600 dark:text-red-400 flex items-center gap-1 mt-1">
                <AlertCircle className="w-3 h-3" /> {fieldError.password}
              </p>
            )}
          </div>

          {error && (
            <div className="text-sm text-red-600 dark:text-red-400 bg-red-50 dark:bg-red-950 border border-red-200 dark:border-red-800 p-3 rounded-xl">
              {error}
            </div>
          )}

          <Button className="w-full h-11 rounded-xl bg-gradient-to-r from-indigo-600 via-violet-600 to-fuchsia-600 hover:from-indigo-700 hover:via-violet-700 hover:to-fuchsia-700 shadow-lg shadow-indigo-500/30 text-white font-medium"
            onClick={handleLogin} disabled={loading}>
            {loading ? <><Loader2 className="w-4 h-4 animate-spin" /> 登录中...</> : "登 录"}
          </Button>

          <p className="text-xs text-center text-slate-500 dark:text-slate-400">
            还没有账号？{" "}
            <Link href="/register" className="text-indigo-600 hover:underline font-medium">立即注册</Link>
          </p>

          <div className="flex items-center gap-2 justify-center text-xs text-slate-500 dark:text-slate-400">
            <Sparkles className="w-3 h-3" />
            <Link href="/mini" className="hover:text-indigo-500 hover:underline">先匿名试用，不登录也能玩</Link>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
