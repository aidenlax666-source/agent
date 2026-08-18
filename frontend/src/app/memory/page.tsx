"use client";

/**
 * 记忆管理页：查看 AI 记住了你的哪些偏好/习惯（长期记忆）。
 * AI 会从任务需求里自动提取（如"我喜欢 Excel 格式"），这里可查看/删除/手动添加。
 */
import { useCallback, useEffect, useState } from "react";
import AppNav from "@/components/AppNav";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Brain, Plus, Trash2, RefreshCw, Loader2, AlertTriangle, Sparkles } from "lucide-react";
import { memoryApi, type MemoryItem } from "@/lib/api";

const KIND_META: Record<string, { label: string; cls: string }> = {
  preference: { label: "偏好", cls: "bg-indigo-100 text-indigo-700 dark:bg-indigo-900/50 dark:text-indigo-300" },
  habit: { label: "习惯", cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300" },
  fact: { label: "事实", cls: "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300" },
};

function fmtTime(ts: number): string {
  if (!ts) return "";
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch {
    return "";
  }
}

export default function MemoryPage() {
  const [items, setItems] = useState<MemoryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [kind, setKind] = useState("preference");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const r = await memoryApi.list();
      setItems(r.items || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const add = async () => {
    const c = content.trim();
    if (!c || saving) return;
    if (c.length > 200) { setError("记忆内容最多 200 字"); return; }
    setSaving(true);
    setError(null);
    try {
      await memoryApi.add(c, kind);
      setContent("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (content: string) => {
    if (deleting) return;
    if (!window.confirm("删除这条记忆？AI 后续任务将不再使用它。")) return;
    setDeleting(content);
    try {
      await memoryApi.remove(content);
      setItems((prev) => prev.filter((m) => m.content !== content));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(null);
    }
  };

  const clearAll = async () => {
    if (items.length === 0) return;
    if (!window.confirm(`清空全部 ${items.length} 条记忆？`)) return;
    setError(null);
    try {
      await memoryApi.clear();
      setItems([]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-indigo-50 via-white to-violet-50/60 dark:from-slate-950 dark:via-slate-950 dark:to-violet-950/30">
      <AppNav />
      <main className="max-w-3xl mx-auto px-4 py-8 space-y-5">
        <div className="flex items-center gap-3">
          <div className="inline-flex items-center justify-center w-10 h-10 rounded-xl bg-gradient-to-br from-indigo-500 via-violet-500 to-fuchsia-500 shadow-lg shadow-indigo-500/30">
            <Brain className="w-5 h-5 text-white" />
          </div>
          <div className="flex-1">
            <h1 className="text-xl font-extrabold tracking-tight">AI 记忆</h1>
            <p className="text-sm text-muted-foreground mt-0.5">AI 会记住你的偏好和习惯，后续任务自动按此执行</p>
          </div>
          <Button variant="outline" size="sm" onClick={load} disabled={loading} className="gap-1.5">
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? "animate-spin" : ""}`} /> 刷新
          </Button>
        </div>

        {error && (
          <div className="flex items-start gap-2 text-sm text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950 rounded-xl px-4 py-3">
            <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" /> {error}
          </div>
        )}

        {/* 手动添加 */}
        <Card className="rounded-3xl border-violet-200/70 dark:border-violet-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-violet-500/5">
          <CardContent className="p-5 space-y-3">
            <Label className="text-slate-700 dark:text-slate-300">让 AI 记住</Label>
            <div className="flex flex-col sm:flex-row gap-2">
              <div className="flex items-center gap-1.5 shrink-0">
                {Object.entries(KIND_META).map(([k, meta]) => (
                  <button key={k} onClick={() => setKind(k)}
                    className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-colors ${kind === k ? meta.cls : "bg-slate-100 dark:bg-slate-800 text-muted-foreground"}`}>
                    {meta.label}
                  </button>
                ))}
              </div>
              <Input value={content} onChange={(e) => setContent(e.target.value)}
                placeholder="如：我喜欢用 Excel 输出 / 报告要用中文 / 我常用 xx 网站"
                onKeyDown={(e) => e.key === "Enter" && add()}
                className="flex-1 rounded-xl text-slate-900 dark:text-slate-100" />
              <Button onClick={add} disabled={saving || !content.trim()} className="gap-1.5 rounded-xl bg-gradient-to-r from-indigo-600 via-violet-600 to-fuchsia-600 hover:opacity-90 text-white whitespace-nowrap">
                {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Plus className="w-4 h-4" />} 记住
              </Button>
            </div>
          </CardContent>
        </Card>

        {/* 记忆列表 */}
        <Card className="rounded-3xl border-indigo-200/70 dark:border-indigo-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5">
          <CardContent className="p-5">
            <div className="flex items-center justify-between mb-3">
              <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">
                🧠 已记住 {items.length} 条
              </span>
              {items.length > 0 && (
                <button onClick={clearAll} className="text-xs text-red-500 hover:underline flex items-center gap-1">
                  <Trash2 className="w-3 h-3" /> 清空全部
                </button>
              )}
            </div>

            {loading ? (
              <div className="text-center text-muted-foreground py-10">加载中...</div>
            ) : items.length === 0 ? (
              <div className="text-center text-muted-foreground py-10 space-y-2">
                <Sparkles className="w-8 h-8 opacity-40 mx-auto" />
                <p className="text-sm">还没有记忆</p>
                <p className="text-xs">使用工作台时说「我喜欢 Excel 格式」等，AI 会自动记住；也可以在上面手动添加</p>
              </div>
            ) : (
              <div className="space-y-2">
                {items.map((m) => {
                  const meta = KIND_META[m.kind] || KIND_META.preference;
                  return (
                    <div key={m.content} className="flex items-center gap-3 rounded-xl border border-slate-200/60 dark:border-slate-800 bg-white/60 dark:bg-slate-900/60 px-4 py-2.5">
                      <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-md shrink-0 ${meta.cls}`}>{meta.label}</span>
                      <span className="flex-1 text-sm text-slate-700 dark:text-slate-200">{m.content}</span>
                      <span className="text-[11px] text-muted-foreground shrink-0">{fmtTime(m.updated_at)}</span>
                      <button onClick={() => remove(m.content)} disabled={deleting === m.content}
                        title="删除这条记忆"
                        className="p-1.5 rounded-lg text-muted-foreground hover:text-red-500 hover:bg-red-50 dark:hover:bg-red-950/50 transition-colors shrink-0 disabled:opacity-40">
                        {deleting === m.content ? <Loader2 className="w-4 h-4 animate-spin" /> : <Trash2 className="w-4 h-4" />}
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>

        <p className="text-center text-[11px] text-muted-foreground pb-4">
          AI 记忆仅保存在本机账号内；任务需求与记忆冲突时，以你本次明确的要求为准
        </p>
      </main>
    </div>
  );
}
