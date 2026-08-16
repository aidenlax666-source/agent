"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { miniApi, uploadApi, ASSETS_BASE, type MiniTaskStatus } from "@/lib/api";
import type { PreviewData } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { PreviewTable } from "@/components/PreviewTable";
import AppNav from "@/components/AppNav";
import {
  Loader2, Sparkles, Play, CheckCircle2, XCircle, AlertTriangle, Download,
  RefreshCw, Upload, Send, Wand2, FileSpreadsheet, Trash2, History,
} from "lucide-react";

const STATUS_STYLE: Record<string, { label: string; cls: string }> = {
  queued: { label: "排队中", cls: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300" },
  running: { label: "执行中", cls: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300" },
  done: { label: "已完成", cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" },
  error: { label: "失败", cls: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300" },
  cancelled: { label: "已取消", cls: "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300" },
};

// 业务状态友好文案（无数据/需登录/禁止抓取等是如实告知，不是执行失败）
const RESULT_BADGE: Record<string, string> = {
  no_data: "⚠️ 无符合条件的数据",
  login_required: "🔒 需要登录",
  robots_blocked: "🚫 网站禁止抓取",
  insufficient_count: "⚠️ 数量不足",
  missing_fields: "⚠️ 缺少字段",
  coverage_gap: "⚠️ 需求未覆盖",
  value_suspect: "⚠️ 数据值可疑",
  failed: "❌ 执行失败",
};

// 一键试用示例（点击填入输入框）
const EXAMPLES = [
  { icon: "🎬", label: "短视频", text: "生成一段橘猫在花园追蝴蝶的短视频" },
  { icon: "🖼️", label: "图片", text: "生成一张赛博朋克机械猫壁纸" },
  { icon: "🎵", label: "音乐", text: "生成一首星空主题的背景音乐" },
  { icon: "📊", label: "报告", text: "从quotes.toscrape.com抓取名言，生成可视化分析报告" },
  { icon: "🎮", label: "联机游戏", text: "做一个联机五子棋" },
  { icon: "📄", label: "网页", text: "生成一个带倒计时的个人主页" },
  { icon: "📋", label: "数据", text: "生成100行销售数据（列：产品、数量、单价、日期），按产品汇总总金额，导出Excel" },
  { icon: "💬", label: "数据问答", text: "分析我上传的销售数据，哪个产品销量最高？" },
  { icon: "🔊", label: "配音", text: "给这段文字配音：「欢迎使用 AI 自动化工作台，一切尽在掌握」" },
  { icon: "🎙️", label: "男声配音", text: "用男声开心地朗读：「大家好，欢迎收听今天的新闻快报」" },
];

function toPreviewData(p: Record<string, unknown>[] | undefined): PreviewData | null {
  if (!p || p.length === 0) return null;
  const columns = Object.keys(p[0]);
  return {
    columns,
    rows: p.map((r) => columns.map((c) => String(r[c] ?? ""))),
    total_estimate: p.length,
    execution_time: 0,
  };
}

export default function MiniPage() {
  const [requirement, setRequirement] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [task, setTask] = useState<MiniTaskStatus | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState("");
  const [acting, setActing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [imagePaths, setImagePaths] = useState<string[]>([]);
  const [imagePreviews, setImagePreviews] = useState<string[]>([]);
  const [dataPaths, setDataPaths] = useState<string[]>([]);
  const [dataFiles, setDataFiles] = useState<{ name: string; path: string }[]>([]);
  const [scheduleValue, setScheduleValue] = useState("30");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const pollTask = useCallback((taskId: string) => {
    stopPolling();
    let consecutiveErrors = 0;
    pollRef.current = setInterval(async () => {
      try {
        const s = await miniApi.get(taskId);
        // 竞态防护：只接受当前任务的响应（防旧任务在途响应覆盖新状态）
        if (s.id !== taskId) return;
        consecutiveErrors = 0;
        setTask(s);
        if (s.status === "done" || s.status === "error" || s.status === "cancelled" || s.status === "confirmed") {
          stopPolling();
        }
      } catch (e) {
        // 瞬时错误不停止轮询（否则界面永久卡死在旧状态）；连续 10 次失败才放弃
        consecutiveErrors += 1;
        if (consecutiveErrors >= 10) {
          stopPolling();
          setSubmitError("与服务器连接中断，请刷新页面重试");
        }
      }
    }, 2000);
  }, [stopPolling]);

  const handleLoadTask = useCallback(async (taskId: string) => {
    setFeedback("");
    try {
      const s = await miniApi.get(taskId);
      setTask(s);
      if (s.status === "done" || s.status === "running" || s.status === "queued") {
        pollTask(taskId);
      }
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    }
  }, [pollTask]);

  // 从 /history 跳转过来时（/mini?task=xxx）自动加载任务；task 参数变化时重新加载
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const tid = params.get("task");
    if (tid) {
      handleLoadTask(tid);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [handleLoadTask]);

  const handleSubmit = async () => {
    if (!requirement.trim() || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    setTask(null);
    stopPolling();
    try {
      const res = await miniApi.submit(requirement.trim(), undefined, imagePaths, dataPaths);
      const initial = await miniApi.get(res.task_id);
      setTask(initial);
      pollTask(res.task_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  };

  // 下载任务结果文件（fetch + 鉴权头 → blob，替代裸 <a href> 直链）
  const handleDownloadOutput = async (taskId: string) => {
    try {
      const blob = await miniApi.download(taskId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `result_${taskId.slice(0, 8)}.xlsx`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);  // 等下载启动后再 revoke
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleImages = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setSubmitError(null);
    try {
      const paths: string[] = [];
      const previews: string[] = [];
      for (const file of Array.from(files).slice(0, 4)) {
        const res = await uploadApi.upload(file);
        paths.push(res.path);
        previews.push(URL.createObjectURL(file));
      }
      setImagePaths((prev) => [...prev, ...paths]);
      setImagePreviews((prev) => [...prev, ...previews]);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  const handleDataFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setUploading(true);
    setSubmitError(null);
    try {
      const added: { name: string; path: string }[] = [];
      for (const file of Array.from(files).slice(0, 3)) {
        const res = await uploadApi.upload(file);
        added.push({ name: res.filename, path: res.path });
      }
      setDataFiles((prev) => [...prev, ...added]);
      setDataPaths((prev) => [...prev, ...added.map((a) => a.path)]);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  const removeImage = (idx: number) => {
    setImagePaths((prev) => prev.filter((_, i) => i !== idx));
    setImagePreviews((prev) => {
      const next = prev.filter((_, i) => i !== idx);
      // revoke 被移除的 blob URL（防内存泄漏）
      const removed = prev[idx];
      if (removed) URL.revokeObjectURL(removed);
      return next;
    });
  };

  // 组件卸载时统一 revoke 全部图片预览 blob URL
  useEffect(() => {
    return () => {
      imagePreviews.forEach((src) => URL.revokeObjectURL(src));
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const removeDataFile = (idx: number) => {
    setDataFiles((prev) => prev.filter((_, i) => i !== idx));
    setDataPaths((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleCancel = async () => {
    if (!task) return;
    try {
      await miniApi.cancel(task.id);
      stopPolling();
      setTask({ ...task, status: "cancelled", message: "已取消" });
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleSchedule = async (enabled: boolean) => {
    if (!task) return;
    try {
      await miniApi.schedule(task.id, "interval", scheduleValue || "30", enabled);
      setSubmitError(null);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    }
  };

  const handleConfirm = async () => {
    if (!task || acting) return;
    setActing(true);
    try {
      await miniApi.confirm(task.id);
      setTask({ ...task, status: "confirmed", message: "已确认" });
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setActing(false);
    }
  };

  const handleIterate = async () => {
    if (!task || !feedback.trim() || acting) return;
    setActing(true);
    try {
      await miniApi.iterate(task.id, feedback.trim());
      setFeedback("");
      const s = await miniApi.get(task.id);
      setTask(s);
      pollTask(task.id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setActing(false);
    }
  };

  const isActive = task && (task.status === "queued" || task.status === "running");
  const result = task?.result;
  const statusStyle = STATUS_STYLE[task?.status ?? ""];
  const isQa = !!result?.answer;

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-white to-indigo-50/80 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950/40">
      {/* 顶部装饰网格 */}
      <div className="pointer-events-none fixed inset-0 opacity-[0.35] dark:opacity-[0.15]"
        style={{ backgroundImage: "linear-gradient(rgba(99,102,241,0.08) 1px, transparent 1px), linear-gradient(90deg, rgba(99,102,241,0.08) 1px, transparent 1px)", backgroundSize: "44px 44px" }}
      />

      <AppNav />

      <main className="relative max-w-5xl mx-auto px-4 py-8 space-y-6">
        {/* 提交区 */}
        <Card className="rounded-3xl border-slate-200/70 dark:border-slate-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5 overflow-visible">
          <CardContent className="p-5 sm:p-7 space-y-5">
            <div className="flex items-start gap-3">
              <div className="flex-1">
                <h1 className="text-2xl sm:text-3xl font-extrabold tracking-tight">
                  <span className="bg-gradient-to-r from-indigo-600 via-violet-600 to-fuchsia-600 dark:from-indigo-400 dark:via-violet-400 dark:to-fuchsia-400 bg-clip-text text-transparent">
                    一句话，AI 帮你搞定一切
                  </span>
                </h1>
                <p className="mt-1.5 text-sm text-muted-foreground">
                  抓数据、出报告、做游戏、生成视频/图片/音乐——或上传 Excel/CSV 直接提问，AI 自动判断怎么执行。
                </p>
              </div>
              <Link href="/history" className="shrink-0 hidden sm:inline-flex items-center gap-1.5 text-xs font-medium text-indigo-600 dark:text-indigo-400 hover:underline mt-1">
                <History className="w-3.5 h-3.5" /> 全部历史
              </Link>
            </div>

            <div className="relative">
              <textarea
                value={requirement}
                onChange={(e) => setRequirement(e.target.value)}
                placeholder={"描述你要做的事，例如：\n「生成100行销售数据（列：产品、数量、单价、日期），按产品汇总总金额，导出Excel」\n「分析我上传的数据，哪个产品销量最高？」\n「生成一段橘猫在花园追蝴蝶的短视频」"}
                className="w-full min-h-36 rounded-2xl border border-slate-200 dark:border-slate-700 bg-white/90 dark:bg-slate-900/90 p-4 text-sm shadow-inner outline-none focus:ring-4 focus:ring-indigo-400/20 focus:border-indigo-400 transition-all resize-y"
              />
              <div className="absolute right-3 bottom-3 flex items-center gap-2">
                {uploading && <Loader2 className="w-4 h-4 animate-spin text-indigo-500" />}
                <label className="inline-flex items-center gap-1.5 text-xs font-medium text-indigo-600 dark:text-indigo-400 cursor-pointer hover:bg-indigo-50 dark:hover:bg-indigo-950 rounded-lg px-2.5 py-1.5 transition-colors">
                  <Upload className="w-3.5 h-3.5" /> 图片
                  <input type="file" accept="image/*" multiple className="hidden" disabled={uploading} onChange={(e) => { handleImages(e.target.files); e.target.value = ""; }} />
                </label>
                <label className="inline-flex items-center gap-1.5 text-xs font-medium text-emerald-600 dark:text-emerald-400 cursor-pointer hover:bg-emerald-50 dark:hover:bg-emerald-950 rounded-lg px-2.5 py-1.5 transition-colors">
                  <FileSpreadsheet className="w-3.5 h-3.5" /> 数据文件
                  <input type="file" accept=".xlsx,.xls,.csv" multiple className="hidden" disabled={uploading} onChange={(e) => { handleDataFiles(e.target.files); e.target.value = ""; }} />
                </label>
              </div>
            </div>

            {/* 已上传文件 */}
            {(imagePreviews.length > 0 || dataFiles.length > 0) && (
              <div className="flex items-center gap-3 flex-wrap">
                {imagePreviews.map((src, i) => (
                  <div key={i} className="relative group">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img src={src} alt={`upload-${i}`} className="w-14 h-14 object-cover rounded-xl border border-slate-200 dark:border-slate-700 shadow-sm" />
                    <button onClick={() => removeImage(i)} className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-red-500 text-white text-xs flex items-center justify-center shadow opacity-0 group-hover:opacity-100 transition-opacity" title="移除">
                      ×
                    </button>
                  </div>
                ))}
                {dataFiles.map((f, i) => (
                  <div key={i} className="inline-flex items-center gap-2 rounded-xl border border-emerald-200 dark:border-emerald-800 bg-emerald-50/70 dark:bg-emerald-950/50 px-3 py-1.5 text-xs text-emerald-700 dark:text-emerald-300">
                    <FileSpreadsheet className="w-3.5 h-3.5" />
                    <span className="max-w-40 truncate">{f.name}</span>
                    <button onClick={() => removeDataFile(i)} className="text-emerald-500 hover:text-red-500 transition-colors" title="移除">
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                ))}
              </div>
            )}

            <div className="flex flex-col sm:flex-row gap-3 items-stretch sm:items-center">
              <Button
                onClick={handleSubmit}
                disabled={!requirement.trim() || submitting || !!isActive}
                className="gap-2 h-12 px-8 rounded-2xl bg-gradient-to-r from-indigo-600 via-violet-600 to-fuchsia-600 hover:opacity-90 text-white font-semibold shadow-lg shadow-indigo-500/30 disabled:opacity-50 w-full sm:w-auto"
              >
                {submitting || isActive ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
                {isActive ? "执行中..." : "让 AI 搞定"}
              </Button>
              <span className="text-xs text-muted-foreground sm:ml-1">
                网页抓取无需填 URL，AI 会根据需求自动定位目标网站
              </span>
            </div>

            {/* 能力示例 chips */}
            <div>
              <p className="text-[11px] font-medium text-muted-foreground mb-2 flex items-center gap-1.5">
                <Wand2 className="w-3 h-3" /> 试试这些（点击填入）
              </p>
              <div className="flex flex-wrap gap-2">
                {EXAMPLES.map((ex) => (
                  <button
                    key={ex.label}
                    onClick={() => setRequirement(ex.text)}
                    className="inline-flex items-center gap-1.5 rounded-full border border-slate-200 dark:border-slate-700 bg-white/70 dark:bg-slate-900/70 px-3 py-1.5 text-xs text-slate-600 dark:text-slate-300 hover:border-indigo-300 hover:text-indigo-600 dark:hover:text-indigo-300 hover:shadow-sm transition-all"
                  >
                    <span>{ex.icon}</span>
                    {ex.label}
                  </button>
                ))}
              </div>
            </div>

            {submitError && (
              <p className="text-sm text-red-500 bg-red-50 dark:bg-red-950 rounded-xl px-4 py-2.5">{submitError}</p>
            )}
          </CardContent>
        </Card>

        {/* 结果区 */}
        {task && (
          <Card className="rounded-3xl border-slate-200/70 dark:border-slate-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5">
            <CardContent className="p-5 sm:p-6 space-y-4">
              <div className="flex items-center gap-3 flex-wrap">
                <span className="text-sm font-semibold font-mono">#{task.id.slice(0, 8)}</span>
                {statusStyle && <Badge className={statusStyle.cls}>{statusStyle.label}</Badge>}
                <span className="text-xs text-muted-foreground flex-1 min-w-0 truncate">{task.message || ""}</span>
                {isActive && <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />}
              </div>
              <p className="text-sm text-muted-foreground break-words bg-slate-50 dark:bg-slate-900 rounded-xl px-4 py-3">{task.requirement}</p>

              {!result && !task.error && (
                <div className="flex items-center gap-3 text-sm text-muted-foreground py-6 justify-center">
                  {isActive ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin text-indigo-500" />
                      <span>AI 正在识别意图并执行，请稍候…</span>
                      <Button variant="outline" size="sm" onClick={handleCancel} className="gap-1.5 text-red-500">
                        <XCircle className="w-3.5 h-3.5" /> 取消
                      </Button>
                    </>
                  ) : (
                    <span>任务未开始或已结束</span>
                  )}
                </div>
              )}

              {task.error && !result && (
                <div className="flex items-start gap-2 text-sm text-red-600 bg-red-50 dark:bg-red-950 rounded-xl px-4 py-3">
                  <XCircle className="w-4 h-4 mt-0.5 shrink-0" />
                  <span>{task.error}</span>
                </div>
              )}

              {result && (
                <>
                  {/* QA 答案 */}
                  {isQa && (
                    <div className="rounded-2xl bg-gradient-to-br from-indigo-50/80 to-fuchsia-50/60 dark:from-indigo-950/40 dark:to-fuchsia-950/30 border border-indigo-100 dark:border-indigo-900/50 p-5">
                      <div className="flex items-center gap-2 text-sm font-semibold text-indigo-700 dark:text-indigo-300 mb-2">
                        <Sparkles className="w-4 h-4" /> AI 数据分析结果
                        {typeof result.rows === "number" && <Badge variant="outline" className="ml-auto text-xs font-normal">{result.rows} 行数据</Badge>}
                      </div>
                      <div className="text-sm leading-relaxed whitespace-pre-wrap text-slate-700 dark:text-slate-200">{result.answer}</div>
                    </div>
                  )}

                  {/* 状态与校验徽章（非 QA） */}
                  {!isQa && (
                    <div className="flex items-center gap-2 flex-wrap">
                      <Badge className={result.status === "ok" ? "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" : "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300"}>
                        {result.status === "ok" ? "✅ 校验通过" : (RESULT_BADGE[result.status] || `⚠️ ${result.status}`)}
                      </Badge>
                      {result.rows > 0 && <Badge variant="outline">{result.rows} 行</Badge>}
                      {typeof result.elapsed === "number" && <Badge variant="secondary">{result.elapsed.toFixed(1)}s</Badge>}
                      {typeof result.count_heals === "number" && result.count_heals > 0 && <Badge variant="secondary">数量自愈×{result.count_heals}</Badge>}
                      {typeof result.field_heals === "number" && result.field_heals > 0 && <Badge variant="secondary">字段自愈×{result.field_heals}</Badge>}
                      {typeof result.coverage_heals === "number" && result.coverage_heals > 0 && <Badge variant="secondary">覆盖自愈×{result.coverage_heals}</Badge>}
                      {typeof result.value_heals === "number" && result.value_heals > 0 && <Badge variant="secondary">值自愈×{result.value_heals}</Badge>}
                    </div>
                  )}

                  {result.error && (
                    <div className="flex items-start gap-2 text-sm text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950 rounded-xl px-4 py-3">
                      <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                      <span>{result.error}</span>
                    </div>
                  )}

                  {!isQa && <PreviewTable data={toPreviewData(result.preview)} totalEstimate={result.rows} />}

                  {/* 媒体产物（产物域，与 API 不同源） */}
                  {result.video_url && (
                    <div className="rounded-2xl overflow-hidden border border-slate-200 dark:border-slate-700">
                      <video src={`${ASSETS_BASE}${result.video_url}`} controls className="w-full max-h-96 bg-black" />
                    </div>
                  )}
                  {result.image_urls && result.image_urls.length > 0 && (
                    <div className="flex items-start gap-3 flex-wrap">
                      {result.image_urls.map((u, i) => (
                        <a key={u} href={`${ASSETS_BASE}${u}`} target="_blank" rel="noreferrer" className="group relative">
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img src={`${ASSETS_BASE}${u}`} alt={`generated-${i}`} className="w-40 h-40 object-cover rounded-2xl border border-slate-200 dark:border-slate-700 shadow-sm group-hover:opacity-85 group-hover:scale-[1.02] transition-all" />
                          <span className="absolute inset-0 rounded-2xl opacity-0 group-hover:opacity-100 bg-black/30 flex items-center justify-center text-white text-xs font-medium transition-opacity">查看大图</span>
                        </a>
                      ))}
                    </div>
                  )}
                  {result.music_url && (
                    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-gradient-to-r from-amber-50/60 to-rose-50/60 dark:from-amber-950/30 dark:to-rose-950/30 p-4">
                      <audio src={`${ASSETS_BASE}${result.music_url}`} controls className="w-full" />
                    </div>
                  )}
                  {result.tts_url && (
                    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-gradient-to-r from-sky-50/60 to-indigo-50/60 dark:from-sky-950/30 dark:to-indigo-950/30 p-4">
                      <p className="text-xs font-medium text-sky-700 dark:text-sky-300 mb-2 flex items-center gap-1.5">
                        🔊 AI 配音
                      </p>
                      <audio src={`${ASSETS_BASE}${result.tts_url}`} controls className="w-full" />
                    </div>
                  )}

                  {/* 链接类产物 */}
                  <div className="flex flex-wrap gap-2.5">
                    {result.output_file && (
                      <button onClick={() => handleDownloadOutput(task.id)}
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:underline rounded-xl border border-indigo-200/60 dark:border-indigo-800 px-3.5 py-2 hover:bg-indigo-50 dark:hover:bg-indigo-950/50 transition-colors">
                        <Download className="w-4 h-4" /> 下载结果文件
                      </button>
                    )}
                    {result.report_url && (
                      <a href={`${ASSETS_BASE}${result.report_url}`} target="_blank" rel="noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-emerald-600 dark:text-emerald-400 hover:underline rounded-xl border border-emerald-200/60 dark:border-emerald-800 px-3.5 py-2 hover:bg-emerald-50 dark:hover:bg-emerald-950/50 transition-colors">
                        <Sparkles className="w-4 h-4" /> 📊 查看可视化报告
                      </a>
                    )}
                    {result.game_url && (
                      <a href={`${ASSETS_BASE}${result.game_url}`} target="_blank" rel="noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-violet-600 dark:text-violet-400 hover:underline rounded-xl border border-violet-200/60 dark:border-violet-800 px-3.5 py-2 hover:bg-violet-50 dark:hover:bg-violet-950/50 transition-colors">
                        <Play className="w-4 h-4" /> 🎮 开始联机游戏
                      </a>
                    )}
                    {result.content_url && (
                      <a href={`${ASSETS_BASE}${result.content_url}`} target="_blank" rel="noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm font-medium text-sky-600 dark:text-sky-400 hover:underline rounded-xl border border-sky-200/60 dark:border-sky-800 px-3.5 py-2 hover:bg-sky-50 dark:hover:bg-sky-950/50 transition-colors">
                        <Play className="w-4 h-4" /> 📄 打开生成的内容
                      </a>
                    )}
                  </div>

                  {result.script && (
                    <details className="text-sm">
                      <summary className="cursor-pointer text-muted-foreground hover:text-foreground select-none">查看生成的脚本（{result.script.length} 字符）</summary>
                      <pre className="mt-3 max-h-96 overflow-auto rounded-xl bg-slate-950 text-slate-100 p-4 text-xs leading-relaxed whitespace-pre-wrap">{result.script}</pre>
                    </details>
                  )}

                  {/* 开发任务结果（AI 改代码） */}
                  {result.dev_files && result.dev_files.length > 0 && (
                    <div className="rounded-2xl border border-slate-200 dark:border-slate-700 bg-gradient-to-br from-violet-50/60 to-indigo-50/60 dark:from-violet-950/30 dark:to-indigo-950/30 p-4 space-y-3">
                      <p className="text-xs font-medium text-violet-700 dark:text-violet-300 flex items-center gap-1.5">
                        💻 AI 代码改动 {result.dev_summary ? `· ${result.dev_summary}` : ""}
                      </p>
                      <div className="flex flex-wrap gap-1.5">
                        {result.dev_files.map((f) => (
                          <span key={f.path} className="inline-flex items-center gap-1 rounded-lg bg-white/70 dark:bg-slate-900/70 border border-violet-200/60 dark:border-violet-800 px-2 py-1 text-[11px] text-slate-600 dark:text-slate-300 font-mono">
                            {f.status === "新增" ? "🆕" : "✏️"} {f.path}
                          </span>
                        ))}
                      </div>
                      {result.dev_diff && (
                        <details className="text-sm">
                          <summary className="cursor-pointer text-muted-foreground hover:text-foreground select-none">查看代码改动 diff（{result.dev_diff.length} 字符）</summary>
                          <pre className="mt-2 max-h-96 overflow-auto rounded-xl bg-slate-950 text-slate-100 p-4 text-[11px] leading-relaxed whitespace-pre-wrap">{result.dev_diff}</pre>
                        </details>
                      )}
                      {result.dev_diff_url && (
                        <a href={`${ASSETS_BASE}${result.dev_diff_url}`} target="_blank" rel="noreferrer"
                          className="inline-flex items-center gap-1.5 text-xs font-medium text-violet-600 dark:text-violet-400 hover:underline">
                          <Download className="w-3.5 h-3.5" /> 下载 diff 文件
                        </a>
                      )}
                    </div>
                  )}
                </>
              )}

              {result && task.status === "done" && (
                <div className="pt-3 border-t border-slate-100 dark:border-slate-800 space-y-3">
                  <div className="flex items-center gap-2 flex-wrap">
                    <Button size="sm" className="gap-2 rounded-xl" onClick={handleConfirm} disabled={acting}>
                      <CheckCircle2 className="w-4 h-4" /> 满意，确认结果
                    </Button>
                    <span className="text-xs text-muted-foreground">不满意？下方提出修改意见，AI 会在原任务上迭代重跑</span>
                  </div>
                  <div className="flex items-center gap-2 flex-wrap text-xs">
                    <span className="text-muted-foreground">定时执行：</span>
                    <input value={scheduleValue} onChange={(e) => setScheduleValue(e.target.value)}
                      className="w-20 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1 text-xs outline-none focus:ring-2 focus:ring-indigo-400" placeholder="分钟" />
                    <span className="text-muted-foreground">分钟</span>
                    <Button variant="outline" size="sm" onClick={() => handleSchedule(true)}>开启</Button>
                    <Button variant="ghost" size="sm" onClick={() => handleSchedule(false)} className="text-red-500">关闭</Button>
                  </div>
                  <div className="flex flex-col sm:flex-row gap-2">
                    <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)}
                      placeholder="修改意见，如：只要产品名和金额两列 / 换成按月份统计 / 数据太多只要前5条…"
                      className="flex-1 min-h-16 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-indigo-400 resize-y" />
                    <Button variant="outline" size="sm" onClick={handleIterate} disabled={!feedback.trim() || acting} className="gap-2 rounded-xl">
                      <RefreshCw className="w-4 h-4" /> 迭代修改
                    </Button>
                  </div>
                </div>
              )}

              {task.status === "confirmed" && (
                <div className="flex items-center gap-2 text-sm text-emerald-600">
                  <CheckCircle2 className="w-4 h-4" /> 已确认完成，结果即最终版。
                </div>
              )}
            </CardContent>
          </Card>
        )}

        {/* 开发任务结果（上传 zip 改代码）已迁移到 /claude 改码助手（选文件夹），此处移除 */}

        <footer className="text-center text-[11px] text-muted-foreground pb-6">
          AI 自动化工作台 · 任务与数据问答自动识别 · 生成产物自动校验（数量 / 字段 / 覆盖 / 数值）
        </footer>
      </main>
    </div>
  );
}
