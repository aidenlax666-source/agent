"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Sparkles, History, QrCode, Zap } from "lucide-react";

const LINKS = [
  { href: "/mini", label: "工作台", icon: Sparkles },
  { href: "/history", label: "任务历史", icon: History },
  { href: "/gallery", label: "分享中心", icon: QrCode },
];

/** 全局顶部导航：工作台 / 任务历史 / 分享中心，自动高亮当前页 */
export default function AppNav() {
  const pathname = usePathname();
  return (
    <header className="sticky top-0 z-20 border-b border-slate-200/60 dark:border-slate-800/60 bg-white/70 dark:bg-slate-950/70 backdrop-blur-xl">
      <div className="max-w-6xl mx-auto px-4 h-16 flex items-center gap-3">
        <Link href="/" className="flex items-center gap-2.5 group shrink-0">
          <div className="w-9 h-9 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 flex items-center justify-center shadow-lg shadow-indigo-500/30 group-hover:scale-105 transition-transform">
            <Zap className="w-5 h-5 text-white" />
          </div>
          <div className="leading-tight hidden sm:block">
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
                <span className="hidden sm:inline">{label}</span>
              </Link>
            );
          })}
        </nav>
      </div>
    </header>
  );
}
