"use client";

import { useCallback, useEffect, useState } from "react";
import { Download, Pencil, RefreshCw, Trash2, FileText, FolderOpen, AlertTriangle } from "lucide-react";
import { filesApi, type FileItem } from "@/lib/api";
import { ASSETS_BASE } from "@/lib/api";
import AppNav from "@/components/AppNav";

function sizeHuman(size: number): string {
  if (size < 1024) return `${size}B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)}KB`;
  return `${(size / 1024 / 1024).toFixed(1)}MB`;
}

function timeHuman(ts: number): string {
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return "";
  }
}

function typeIcon(type: string) {
  switch (type) {
    case "video": return "🎬";
    case "music": return "🎵";
    case "image": return "🖼️";
    case "report": return "📊";
    case "game": return "🎮";
    default: return "📄";
  }
}

export default function FilesPage() {
  const [items, setItems] = useState<FileItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await filesApi.list();
      setItems(res.items || []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const startRename = (f: FileItem) => {
    setRenaming(f.filename);
    setNewName(f.filename);
  };

  const confirmRename = async () => {
    if (!renaming || !newName.trim() || newName === renaming || busy) return;
    setBusy(true);
    try {
      await filesApi.rename(renaming, newName.trim());
      setRenaming(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (filename: string) => {
    if (!window.confirm(`删除文件 ${filename}？此操作不可恢复。`)) return;
    setBusy(true);
    try {
      await filesApi.remove(filename);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="min-h-screen bg-gradient-to-b from-slate-50 via-white to-indigo-50/40 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950/30">
      <AppNav />
      <div className="max-w-4xl mx-auto px-4 py-8">
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center gap-3">
          <FolderOpen className="w-6 h-6 text-indigo-500" />
          <h1 className="text-2xl font-bold">我的文件</h1>
        </div>
        <button
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground disabled:opacity-50"
        >
          <RefreshCw className={`w-4 h-4 ${loading ? "animate-spin" : ""}`} /> 刷新
        </button>
      </div>

      {error && (
        <div className="flex items-start gap-2 text-sm text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950 rounded-xl px-4 py-3 mb-4">
          <AlertTriangle className="w-4 h-4 mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {loading && items.length === 0 ? (
        <div className="text-center text-muted-foreground py-16">加载中...</div>
      ) : items.length === 0 ? (
        <div className="text-center text-muted-foreground py-16">
          <div className="text-4xl mb-3">📂</div>
          <p>还没有产物文件</p>
          <p className="text-sm mt-1">在工作台生成内容后，文件会出现在这里，可重命名或删除</p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map((f) => (
            <div key={f.filename} className="flex items-center gap-3 rounded-xl border border-slate-200 dark:border-slate-800 bg-white dark:bg-slate-900 px-4 py-3">
              <span className="text-lg">{typeIcon(f.type)}</span>
              <div className="flex-1 min-w-0">
                {renaming === f.filename ? (
                  <input
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") confirmRename(); if (e.key === "Escape") setRenaming(null); }}
                    autoFocus
                    className="w-full text-sm bg-transparent border-b border-indigo-400 outline-none font-mono"
                  />
                ) : (
                  <div className="font-medium text-sm truncate font-mono">{f.filename}</div>
                )}
                <div className="text-xs text-muted-foreground mt-0.5">
                  {sizeHuman(f.size)} · {timeHuman(f.modified)}
                </div>
              </div>
              {renaming === f.filename ? (
                <div className="flex gap-1.5">
                  <button onClick={confirmRename} disabled={busy} className="text-xs text-emerald-600 hover:text-emerald-500 px-2 py-1 rounded-lg bg-emerald-50 dark:bg-emerald-950">保存</button>
                  <button onClick={() => setRenaming(null)} className="text-xs text-muted-foreground hover:text-foreground px-2 py-1">取消</button>
                </div>
              ) : (
                <div className="flex gap-1.5 shrink-0">
                  <a href={`${ASSETS_BASE}${f.path}`} target="_blank" rel="noreferrer" className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-muted-foreground hover:text-foreground" title="打开">
                    <FileText className="w-4 h-4" />
                  </a>
                  <button onClick={() => startRename(f)} className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-muted-foreground hover:text-foreground" title="重命名">
                    <Pencil className="w-4 h-4" />
                  </button>
                  <button onClick={() => remove(f.filename)} disabled={busy} className="p-1.5 rounded-lg hover:bg-red-50 dark:hover:bg-red-950 text-muted-foreground hover:text-red-500" title="删除">
                    <Trash2 className="w-4 h-4" />
                  </button>
                  <a href={`${ASSETS_BASE}${f.path}`} download className="p-1.5 rounded-lg hover:bg-slate-100 dark:hover:bg-slate-800 text-muted-foreground hover:text-foreground" title="下载">
                    <Download className="w-4 h-4" />
                  </a>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      </div>
    </div>
  );
}
