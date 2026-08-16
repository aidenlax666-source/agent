"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Sparkles, History, QrCode, Zap, LogIn, LogOut, User as UserIcon, Coins, FolderGit2 } from "lucide-react";
import { authApi, getAuthToken, setAuthToken } from "@/lib/api";
import type { User } from "@/lib/types";

const LINKS = [
  { href: "/mini", label: "工作台", icon: Sparkles },
  { href: "/claude", label: "改码助手", icon: FolderGit2 },
  { href: "/history", label: "任务历史", icon: History },
  { href: "/gallery", label: "分享中心", icon: QrCode },
];

function displayName(user: User): string {
  if (user.name && user.name.trim()) return user.name.trim();
  return (user.email || "用户").split("@")[0];
}

/** 全局顶部导航：工作台 / 任务历史 / 分享中心 + 登录状态（账号数据独立） */
export default function AppNav() {
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [checking, setChecking] = useState(true);

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
