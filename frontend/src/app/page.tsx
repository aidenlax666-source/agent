"use client";

import { Button } from "@/components/ui/button";
import Link from "next/link";
import AppNav from "@/components/AppNav";
import {
  Sparkles,
  Wrench,
  CheckCircle2,
  Eye,
  RefreshCw,
  FileSpreadsheet,
  Shield,
  Code2,
  ArrowRight,
} from "lucide-react";

const features = [
  {
    icon: <Sparkles className="w-6 h-6" />,
    title: "AI 脚本生成",
    desc: "自然语言描述需求，自动生成 Python + Playwright 自动化代码",
    color: "from-amber-500 to-orange-600",
  },
  {
    icon: <Wrench className="w-6 h-6" />,
    title: "自动修复引擎",
    desc: "脚本出错时自动分析、自动修复，用户无需了解代码细节",
    color: "from-emerald-500 to-teal-600",
  },
  {
    icon: <CheckCircle2 className="w-6 h-6" />,
    title: "需求覆盖验证",
    desc: "自动检查脚本是否遗漏功能，确保交付结果与需求一致",
    color: "from-blue-500 to-indigo-600",
  },
  {
    icon: <Eye className="w-6 h-6" />,
    title: "结果预览确认",
    desc: "生成产物自动预览（示例数据/文件链接），确认满意后一键下载",
    color: "from-violet-500 to-purple-600",
  },
  {
    icon: <RefreshCw className="w-6 h-6" />,
    title: "迭代修改",
    desc: "对结果不满意？直接描述修改意见，AI 自动调整脚本",
    color: "from-pink-500 to-rose-600",
  },
  {
    icon: <FileSpreadsheet className="w-6 h-6" />,
    title: "Excel 导出",
    desc: "完整执行后自动导出为 Excel 文件，一键下载结果",
    color: "from-cyan-500 to-sky-600",
  },
];

export default function Home() {
  return (
    <div className="flex flex-col min-h-screen">
      <AppNav />

      {/* Hero section */}
      <section className="relative overflow-hidden">
        {/* Background gradient */}
        <div className="absolute inset-0 bg-gradient-to-br from-indigo-50 via-white to-violet-50 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950" />
        <div className="absolute top-0 right-0 w-[600px] h-[600px] bg-gradient-to-bl from-indigo-200/40 to-violet-200/20 dark:from-indigo-500/10 dark:to-violet-500/5 rounded-full blur-3xl -translate-y-1/2 translate-x-1/4" />
        <div className="absolute bottom-0 left-0 w-[500px] h-[500px] bg-gradient-to-tr from-blue-200/30 to-cyan-200/20 dark:from-blue-500/10 dark:to-cyan-500/5 rounded-full blur-3xl translate-y-1/2 -translate-x-1/4" />

        <div className="relative py-24 px-4">
          <div className="text-center mb-12">
            <div className="inline-flex items-center gap-2 px-4 py-1.5 rounded-full bg-indigo-100 dark:bg-indigo-900/50 text-indigo-700 dark:text-indigo-300 text-sm font-medium mb-6">
              <Sparkles className="w-4 h-4" />
              基于 DeepSeek + Playwright
            </div>
            <h1 className="text-5xl font-extrabold tracking-tight mb-4 bg-gradient-to-r from-gray-900 via-indigo-800 to-violet-800 dark:from-white dark:via-indigo-200 dark:to-violet-200 bg-clip-text text-transparent">
              用自然语言生成自动化脚本
            </h1>
            <p className="text-lg text-muted-foreground max-w-2xl mx-auto leading-relaxed">
              只需描述你想做什么，AI 自动编写 Python + Playwright 脚本，
              <br />
              自动修 bug、自动验证需求，你只需确认预览结果
            </p>
          </div>

          <div className="mt-8 flex justify-center gap-4 flex-wrap">
            <Link href="/mini">
              <Button size="lg" className="gap-2 text-base px-8 py-6">
                <Sparkles className="w-5 h-5" />
                开始：描述你的需求
                <ArrowRight className="w-5 h-5" />
              </Button>
            </Link>
            <Link href="/assistant">
              <Button size="lg" variant="outline" className="gap-2 text-base px-8 py-6">
                <Code2 className="w-5 h-5" />
                改码助手：AI 改你的项目
              </Button>
            </Link>
          </div>
        </div>
      </section>

      {/* Features */}
      <section className="py-24 px-4 bg-white dark:bg-slate-950">
        <div className="max-w-6xl mx-auto">
          <div className="text-center mb-16">
            <h2 className="text-3xl font-bold mb-4">核心能力</h2>
            <p className="text-muted-foreground max-w-xl mx-auto">
              三层过滤机制，确保你拿到的是正确的、可运行的结果
            </p>
          </div>
          <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-6">
            {features.map((feature, i) => (
              <div
                key={i}
                className="group relative p-6 rounded-2xl border bg-card hover:shadow-lg hover:-translate-y-1 transition-all duration-300 cursor-default"
              >
                <div
                  className={`w-12 h-12 rounded-xl bg-gradient-to-br ${feature.color} flex items-center justify-center mb-4 text-white shadow-lg group-hover:scale-110 transition-transform duration-300`}
                >
                  {feature.icon}
                </div>
                <h3 className="font-semibold text-lg mb-2">{feature.title}</h3>
                <p className="text-sm text-muted-foreground leading-relaxed">
                  {feature.desc}
                </p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* How it works */}
      <section className="py-24 px-4 bg-slate-50 dark:bg-slate-900/50">
        <div className="max-w-4xl mx-auto text-center">
          <h2 className="text-3xl font-bold mb-4">三步完成</h2>
          <p className="text-muted-foreground mb-16">像聊天一样简单</p>
          <div className="grid md:grid-cols-3 gap-8">
            {[
              {
                step: "01",
                icon: <Code2 className="w-8 h-8" />,
                title: "描述需求",
                desc: "用一句话描述你想做的事，AI 自动判断目标网站与执行方式",
              },
              {
                step: "02",
                icon: <Shield className="w-8 h-8" />,
                title: "AI 自动处理",
                desc: "系统自动生成脚本、测试、修复、验证，全程无需干预",
              },
              {
                step: "03",
                icon: <FileSpreadsheet className="w-8 h-8" />,
                title: "下载结果",
                desc: "预览确认后，后台运行完整任务，下载 Excel 结果",
              },
            ].map((item, i) => (
              <div key={i} className="relative">
                <div className="text-6xl font-bold text-indigo-100 dark:text-indigo-900/50 mb-4">
                  {item.step}
                </div>
                <div className="w-14 h-14 rounded-2xl bg-gradient-to-br from-indigo-500 to-violet-600 flex items-center justify-center mx-auto mb-4 text-white shadow-lg">
                  {item.icon}
                </div>
                <h3 className="font-semibold text-lg mb-2">{item.title}</h3>
                <p className="text-sm text-muted-foreground">{item.desc}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="mt-auto py-8 text-center text-sm text-muted-foreground border-t bg-white dark:bg-slate-950">
        <p>AI 自动化脚本生成器 — Powered by DeepSeek + Playwright + Edge</p>
        <p className="text-xs mt-1 opacity-60">Python · FastAPI · Next.js · shadcn/ui</p>
      </footer>
    </div>
  );
}
