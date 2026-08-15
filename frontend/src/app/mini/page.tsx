"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { miniApi, uploadApi, type MiniTaskStatus } from "@/lib/api";
import type { PreviewData } from "@/lib/types";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { PreviewTable } from "@/components/PreviewTable";
import { Loader2, Sparkles, Play, CheckCircle2, XCircle, AlertTriangle, Download, RefreshCw, History, Upload } from "lucide-react";

const STATUS_COLOR: Record<string, string> = {
  queued: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
  running: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300",
  done: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300",
  error: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300",
  cancelled: "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300",
};

const RESULT_STATUS_TEXT: Record<string, { label: string; color: string }> = {
  ok: { label: "✅ 通过（执行+数量+字段+覆盖+值 全校验）", color: "bg-emerald-100 text-emerald-700" },
  insufficient_count: { label: "⚠️ 数量不足", color: "bg-amber-100 text-amber-700" },
  missing_fields: { label: "⚠️ 缺少字段", color: "bg-amber-100 text-amber-700" },
  coverage_gap: { label: "⚠️ 需求未覆盖", color: "bg-amber-100 text-amber-700" },
  value_suspect: { label: "⚠️ 数据值可疑", color: "bg-amber-100 text-amber-700" },
  login_required: { label: "🔒 需要登录", color: "bg-blue-100 text-blue-700" },
  no_data: { label: "⚠️ 无数据", color: "bg-amber-100 text-amber-700" },
  robots_blocked: { label: "🚫 网站禁止抓取", color: "bg-red-100 text-red-700" },
  generate_failed: { label: "❌ 生成失败", color: "bg-red-100 text-red-700" },
  failed: { label: "❌ 执行失败", color: "bg-red-100 text-red-700" },
};

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
  const [url, setUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [task, setTask] = useState<MiniTaskStatus | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [history, setHistory] = useState<{ id: string; requirement: string; status: string; message?: string }[]>([]);
  const [feedback, setFeedback] = useState("");
  const [acting, setActing] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [imagePaths, setImagePaths] = useState<string[]>([]);
  const [imagePreviews, setImagePreviews] = useState<string[]>([]);
  const [qaMode, setQaMode] = useState(false);
  const [qaFile, setQaFile] = useState<string | null>(null);
  const [qaFileName, setQaFileName] = useState("");
  const [qaQuestion, setQaQuestion] = useState("");
  const [qaAnswer, setQaAnswer] = useState("");
  const [qaLoading, setQaLoading] = useState(false);
  const [scheduleValue, setScheduleValue] = useState("30");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  useEffect(() => stopPolling, [stopPolling]);

  const loadHistory = useCallback(async () => {
    try {
      const r = await miniApi.list(10);
      setHistory(r.tasks);
    } catch {
      // 忽略历史加载失败
    }
  }, []);

  useEffect(() => {
    loadHistory();
  }, [loadHistory]);

  const pollTask = useCallback((taskId: string) => {
    stopPolling();
    pollRef.current = setInterval(async () => {
      try {
        const s = await miniApi.get(taskId);
        setTask(s);
        if (s.status === "done" || s.status === "error" || s.status === "cancelled" || s.status === "confirmed") {
          stopPolling();
          loadHistory();
        }
      } catch {
        stopPolling();
      }
    }, 2000);
  }, [stopPolling, loadHistory]);

  const handleSubmit = async () => {
    if (!requirement.trim() || submitting) return;
    setSubmitting(true);
    setSubmitError(null);
    setTask(null);
    stopPolling();
    try {
      const res = await miniApi.submit(requirement.trim(), url.trim() || undefined, imagePaths);
      const initial = await miniApi.get(res.task_id);
      setTask(initial);
      pollTask(res.task_id);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
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

  const removeImage = (idx: number) => {
    setImagePaths((prev) => prev.filter((_, i) => i !== idx));
    setImagePreviews((prev) => prev.filter((_, i) => i !== idx));
  };

  const handleCancel = async () => {
    if (!task) return;
    try {
      await miniApi.cancel(task.id);
      stopPolling();
      setTask({ ...task, status: "cancelled", message: "已取消" });
      loadHistory();
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

  const handleQaFile = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setQaLoading(true);
    try {
      const res = await uploadApi.upload(files[0]);
      setQaFile(res.path);
      setQaFileName(res.filename);
    } catch (e) {
      setSubmitError(e instanceof Error ? e.message : String(e));
    } finally {
      setQaLoading(false);
    }
  };

  const handleQa = async () => {
    if (!qaFile || !qaQuestion.trim()) return;
    setQaLoading(true);
    setQaAnswer("");
    try {
      const r = await miniApi.qa(qaFile, qaQuestion.trim());
      setQaAnswer(r.answer);
    } catch (e) {
      setQaAnswer(e instanceof Error ? e.message : String(e));
    } finally {
      setQaLoading(false);
    }
  };

  const handleLoadTask = async (taskId: string) => {
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
  };

  const handleConfirm = async () => {
    if (!task || acting) return;
    setActing(true);
    try {
      await miniApi.confirm(task.id);
      setTask({ ...task, status: "confirmed", message: "已确认" });
      loadHistory();
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
  const statusStyle = RESULT_STATUS_TEXT[result?.status ?? ""];

  return (
    <div className="min-h-screen">
      <header className="border-b bg-white/80 backdrop-blur-sm dark:bg-slate-950/80">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center gap-3">
          <div className="w-8 h-8 rounded-lg bg-indigo-100 dark:bg-indigo-900/50 flex items-center justify-center">
            <Sparkles className="w-4 h-4 text-indigo-600 dark:text-indigo-400" />
          </div>
          <span className="text-lg font-semibold">一句话自动化</span>
          <Badge variant="outline" className="ml-auto font-normal text-xs">
            自然语言 → 脚本 → 四重校验
          </Badge>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-10 space-y-6">
        <Card className="rounded-2xl border-2 border-slate-200/60 dark:border-slate-800/60">
          <CardHeader className="pb-3">
            <div className="flex items-center gap-4 flex-wrap">
              <CardTitle className="text-base">描述你要做的事</CardTitle>
              <div className="ml-auto flex items-center gap-1 bg-slate-100 dark:bg-slate-800 rounded-xl p-1 text-sm">
                <button
                  onClick={() => setQaMode(false)}
                  className={`px-3 py-1 rounded-lg transition-colors ${!qaMode ? "bg-white dark:bg-slate-700 shadow font-medium" : "text-muted-foreground"}`}
                >
                  任务
                </button>
                <button
                  onClick={() => setQaMode(true)}
                  className={`px-3 py-1 rounded-lg transition-colors ${qaMode ? "bg-white dark:bg-slate-700 shadow font-medium" : "text-muted-foreground"}`}
                >
                  数据问答
                </button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="space-y-4">
            {qaMode ? (
              <>
                <p className="text-sm text-muted-foreground">
                  上传 Excel/CSV 数据文件，用自然语言提问（如“哪个产品销量最高？”），AI 直接分析回答。
                </p>
                <div className="flex items-center gap-3 flex-wrap">
                  <label className="inline-flex items-center gap-2 text-sm text-muted-foreground cursor-pointer hover:text-foreground transition-colors">
                    <Upload className="w-4 h-4" />
                    上传数据文件（xlsx/csv）
                    <input type="file" accept=".xlsx,.xls,.csv" className="hidden" onChange={(e) => handleQaFile(e.target.files)} />
                  </label>
                  {qaLoading && <Loader2 className="w-4 h-4 animate-spin text-indigo-500" />}
                  {qaFileName && <Badge variant="outline">{qaFileName}</Badge>}
                </div>
                <div className="flex flex-col sm:flex-row gap-2">
                  <input
                    value={qaQuestion}
                    onChange={(e) => setQaQuestion(e.target.value)}
                    placeholder="你的问题，如：哪个产品的销售额最高？"
                    className="flex-1 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-indigo-400"
                    onKeyDown={(e) => e.key === "Enter" && handleQa()}
                  />
                  <Button onClick={handleQa} disabled={!qaFile || !qaQuestion.trim() || qaLoading} className="gap-2">
                    {qaLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Sparkles className="w-4 h-4" />}
                    分析
                  </Button>
                </div>
                {qaAnswer && (
                  <div className="rounded-xl bg-slate-50 dark:bg-slate-900 p-4 text-sm leading-relaxed whitespace-pre-wrap">
                    {qaAnswer}
                  </div>
                )}
              </>
            ) : (
              <>
            <textarea
              value={requirement}
              onChange={(e) => setRequirement(e.target.value)}
              placeholder={'例如：\n生成100行销售数据（列：产品、数量、单价、日期），按产品汇总总金额，导出Excel\n\n从quotes.toscrape.com抓取前10条名言和作者，导出Excel'}
              className="w-full min-h-32 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 p-4 text-sm outline-none focus:ring-2 focus:ring-indigo-400 resize-y"
            />
            <div className="flex flex-col sm:flex-row gap-3 items-start sm:items-center">
              <input
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="目标 URL（网页任务可填，留空自动识别）"
                className="flex-1 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-4 py-2.5 text-sm outline-none focus:ring-2 focus:ring-indigo-400"
              />
              <Button onClick={handleSubmit} disabled={!requirement.trim() || submitting || !!isActive} className="gap-2">
                {submitting || isActive ? <Loader2 className="w-4 h-4 animate-spin" /> : <Play className="w-4 h-4" />}
                {isActive ? "执行中..." : "生成并执行"}
              </Button>
            </div>
            <div className="flex items-center gap-3 flex-wrap">
              <label className="inline-flex items-center gap-2 text-sm text-muted-foreground cursor-pointer hover:text-foreground transition-colors">
                <Upload className="w-4 h-4" />
                上传图片（需求截图/参考图，最多4张，AI 自动识别）
                <input
                  type="file"
                  accept="image/*"
                  multiple
                  className="hidden"
                  disabled={uploading}
                  onChange={(e) => handleImages(e.target.files)}
                />
              </label>
              {uploading && <Loader2 className="w-4 h-4 animate-spin text-indigo-500" />}
              {imagePreviews.map((src, i) => (
                <div key={i} className="relative">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img src={src} alt={`upload-${i}`} className="w-16 h-16 object-cover rounded-lg border border-slate-200 dark:border-slate-700" />
                  <button
                    onClick={() => removeImage(i)}
                    className="absolute -top-1.5 -right-1.5 w-5 h-5 rounded-full bg-red-500 text-white text-xs flex items-center justify-center shadow"
                    title="移除"
                  >
                    ×
                  </button>
                </div>
              ))}
            </div>
            {submitError && (
              <p className="text-sm text-red-500 bg-red-50 dark:bg-red-950 rounded-lg px-3 py-2">{submitError}</p>
            )}
            <p className="text-xs text-muted-foreground">
              支持：网页抓取（含登录态/排序筛选）、Excel/Word/PPT、文件操作、数据处理、API 调用、图片/PDF，
              以及 🎮 联机游戏、📊 可视化报告、📄 网页内容、🎬 AI 视频（Seedance）、🖼️ AI 图片（Seedream）。
              系统自动执行并做数量、字段、功能覆盖、数据值四重校验，发现问题自动修复。
            </p>
              </>
            )}
          </CardContent>
        </Card>

        {task && (
          <Card className="rounded-2xl border-2 border-slate-200/60 dark:border-slate-800/60">
            <CardHeader className="pb-3">
              <div className="flex items-center gap-3 flex-wrap">
                <CardTitle className="text-base">任务 #{task.id.slice(0, 8)}</CardTitle>
                <Badge className={STATUS_COLOR[task.status] ?? ""}>{task.message || task.status}</Badge>
                {isActive && <Loader2 className="w-4 h-4 text-blue-500 animate-spin" />}
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <p className="text-sm text-muted-foreground break-words">{task.requirement}</p>

              {result && (
                <>
                  <div className="flex items-center gap-3 flex-wrap">
                    {statusStyle ? (
                      <Badge className={statusStyle.color}>{statusStyle.label}</Badge>
                    ) : (
                      <Badge variant="outline">状态: {result.status}</Badge>
                    )}
                    {result.rows > 0 && <Badge variant="outline">共 {result.rows} 行</Badge>}
                    {typeof result.elapsed === "number" && (
                      <Badge variant="secondary">{result.elapsed.toFixed(1)}s</Badge>
                    )}
                    {typeof result.count_heals === "number" && result.count_heals > 0 && (
                      <Badge variant="secondary">数量自愈×{result.count_heals}</Badge>
                    )}
                    {typeof result.field_heals === "number" && result.field_heals > 0 && (
                      <Badge variant="secondary">字段自愈×{result.field_heals}</Badge>
                    )}
                    {typeof result.coverage_heals === "number" && result.coverage_heals > 0 && (
                      <Badge variant="secondary">覆盖自愈×{result.coverage_heals}</Badge>
                    )}
                    {typeof result.value_heals === "number" && result.value_heals > 0 && (
                      <Badge variant="secondary">值自愈×{result.value_heals}</Badge>
                    )}
                  </div>

                  {result.error && (
                    <div className="flex items-start gap-2 text-sm text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950 rounded-xl px-4 py-3">
                      <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
                      <span>{result.error}</span>
                    </div>
                  )}

                  {result.missing_fields && result.missing_fields.length > 0 && (
                    <p className="text-xs text-amber-600">缺少字段: {result.missing_fields.join("、")}</p>
                  )}
                  {result.coverage_missing && result.coverage_missing.length > 0 && (
                    <p className="text-xs text-amber-600">未覆盖功能: {result.coverage_missing.join("、")}</p>
                  )}
                  {result.value_issues && result.value_issues.length > 0 && (
                    <p className="text-xs text-amber-600">数据值问题: {result.value_issues.join("；")}</p>
                  )}

                  <PreviewTable data={toPreviewData(result.preview)} totalEstimate={result.rows} />

                  {result.output_file && (
                    <a
                      href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/mini/tasks/${task.id}/download`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-2 text-sm font-medium text-indigo-600 dark:text-indigo-400 hover:underline"
                    >
                      <Download className="w-4 h-4" />
                      下载结果文件（{result.output_file.split(/[\\/]/).pop()}）
                    </a>
                  )}
                  {result.report_url && (
                    <a
                      href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}/api/mini/tasks/${task.id}/download`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-2 text-sm font-medium text-emerald-600 dark:text-emerald-400 hover:underline"
                    >
                      <Sparkles className="w-4 h-4" />
                      📊 查看可视化报告
                    </a>
                  )}
                  {result.game_url && (
                    <a
                      href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}${result.game_url}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-2 text-sm font-medium text-violet-600 dark:text-violet-400 hover:underline"
                    >
                      <Play className="w-4 h-4" />
                      🎮 开始联机游戏（建房分享给好友一起玩）
                    </a>
                  )}
                  {result.content_url && (
                    <a
                      href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}${result.content_url}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-2 text-sm font-medium text-sky-600 dark:text-sky-400 hover:underline"
                    >
                      <Play className="w-4 h-4" />
                      📄 打开生成的内容
                    </a>
                  )}
                  {result.video_url && (
                    <a
                      href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}${result.video_url}`}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-2 text-sm font-medium text-rose-600 dark:text-rose-400 hover:underline"
                    >
                      <Play className="w-4 h-4" />
                      🎬 播放生成的视频
                    </a>
                  )}
                  {result.image_urls && result.image_urls.length > 0 && (
                    <div className="flex items-center gap-3 flex-wrap">
                      <span className="text-sm font-medium text-pink-600 dark:text-pink-400">
                        🖼️ 生成的图片（{result.image_urls.length} 张）：
                      </span>
                      {result.image_urls.map((u, i) => (
                        <a key={u} href={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}${u}`} target="_blank" rel="noreferrer">
                          {/* eslint-disable-next-line @next/next/no-img-element */}
                          <img
                            src={`${process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000"}${u}`}
                            alt={`generated-${i}`}
                            className="w-24 h-24 object-cover rounded-lg border border-slate-200 dark:border-slate-700 hover:opacity-80 transition-opacity"
                          />
                        </a>
                      ))}
                    </div>
                  )}

                  {result.script && (
                    <details className="text-sm">
                      <summary className="cursor-pointer text-muted-foreground hover:text-foreground select-none">
                        查看生成的脚本（{result.script.length} 字符）
                      </summary>
                      <pre className="mt-3 max-h-96 overflow-auto rounded-xl bg-slate-950 text-slate-100 p-4 text-xs leading-relaxed whitespace-pre-wrap">
                        {result.script}
                      </pre>
                    </details>
                  )}
                </>
              )}

              {!result && !task.error && (
                <div className="flex items-center gap-2 text-sm text-muted-foreground py-6">
                  {isActive ? (
                    <>
                      <Loader2 className="w-4 h-4 animate-spin" />
                      系统正在生成脚本、沙箱试跑并做四重校验，请稍候…
                      <Button variant="outline" size="sm" onClick={handleCancel} className="ml-2 gap-1.5 text-red-500">
                        <XCircle className="w-3.5 h-3.5" />
                        取消
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

              {result?.status === "ok" && (
                <div className="flex items-center gap-2 text-sm text-emerald-600">
                  <CheckCircle2 className="w-4 h-4" />
                  四重校验全部通过，数据可信。
                </div>
              )}

              {result && task.status === "done" && (
                <div className="pt-2 border-t border-slate-100 dark:border-slate-800 space-y-3">
                  <div className="flex items-center gap-2">
                    <Button size="sm" className="gap-2" onClick={handleConfirm} disabled={acting}>
                      <CheckCircle2 className="w-4 h-4" />
                      满意，确认结果
                    </Button>
                    <span className="text-xs text-muted-foreground">
                      不满意？在下方提出修改意见，AI 会在原任务上迭代重跑
                    </span>
                  </div>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-xs text-muted-foreground">定时执行：</span>
                    <input
                      value={scheduleValue}
                      onChange={(e) => setScheduleValue(e.target.value)}
                      className="w-20 rounded-lg border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-2 py-1 text-xs outline-none focus:ring-2 focus:ring-indigo-400"
                      placeholder="分钟"
                    />
                    <span className="text-xs text-muted-foreground">分钟</span>
                    <Button variant="outline" size="sm" onClick={() => handleSchedule(true)} className="text-xs">
                      开启定时
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => handleSchedule(false)} className="text-xs text-red-500">
                      关闭
                    </Button>
                  </div>
                  <div className="flex flex-col sm:flex-row gap-2">
                    <textarea
                      value={feedback}
                      onChange={(e) => setFeedback(e.target.value)}
                      placeholder="修改意见，如：只要产品名和金额两列 / 换成按月份统计 / 数据太多只要前5条…"
                      className="flex-1 min-h-16 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-indigo-400 resize-y"
                    />
                    <Button variant="outline" size="sm" onClick={handleIterate} disabled={!feedback.trim() || acting} className="gap-2">
                      <RefreshCw className="w-4 h-4" />
                      迭代修改
                    </Button>
                  </div>
                </div>
              )}

              {task.status === "confirmed" && (
                <div className="flex items-center gap-2 text-sm text-emerald-600">
                  <CheckCircle2 className="w-4 h-4" />
                  已确认完成，结果即最终版。
                </div>
              )}
            </CardContent>
          </Card>
        )}

        {/* 历史任务 */}
        <Card className="rounded-2xl border-2 border-slate-200/60 dark:border-slate-800/60">
          <CardHeader className="pb-3">
            <CardTitle className="text-base flex items-center gap-2">
              <History className="w-4 h-4" />
              任务历史
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5">
            {history.length === 0 ? (
              <p className="text-sm text-muted-foreground py-3">暂无历史任务，提交第一个需求吧</p>
            ) : (
              history.map((h) => (
                <button
                  key={h.id}
                  onClick={() => handleLoadTask(h.id)}
                  className={`w-full text-left rounded-xl px-3 py-2 text-sm transition-colors hover:bg-slate-50 dark:hover:bg-slate-800/50 ${
                    task?.id === h.id ? "bg-indigo-50 dark:bg-indigo-950/30 ring-1 ring-indigo-200 dark:ring-indigo-800" : ""
                  }`}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge variant="outline" className="text-[10px] font-normal">{h.status}</Badge>
                    <span className="text-muted-foreground text-xs font-mono">#{h.id.slice(0, 8)}</span>
                    <span className="text-xs text-muted-foreground">{h.message || ""}</span>
                  </div>
                  <div className="mt-0.5 text-foreground/80 truncate">{h.requirement}</div>
                </button>
              ))
            )}
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
