"use client";

/**
 * 本地改码助手（浏览器版）：
 * 选择本地文件夹 → 输入需求 → AI 出方案（中间聊天区）→ 确认/提意见 → 改码 → 应用回本地文件夹。
 * 复用后端 /api/dev/plan + /api/dev/apply（DeepSeek，方案用便宜模型、改码用 reasoner）。
 */
import { useCallback, useEffect, useRef, useState } from "react";
import JSZip from "jszip";
import AppNav from "@/components/AppNav";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { getAnonymousId, getAuthToken } from "@/lib/api";
import {
  FolderGit2, Send, Loader2, CheckCircle2, RefreshCw, Download,
  Wand2, FileCode2, XCircle, Trash2,
} from "lucide-react";

const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

// ---- 打包排除规则（与 CLI 一致）----
const EXCLUDE_DIRS = new Set([
  "node_modules", ".next", "__pycache__", ".git", "venv", ".venv", "dist", "build",
  "out", ".idea", ".vscode", "web", "tmp", "uploads", "data", "browser_profile", "screens",
]);
const EXCLUDE_EXTS = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".pdf", ".zip",
  ".exe", ".dll", ".so", ".dylib", ".pyc", ".lock",
]);
const MAX_FILES = 200;
const MAX_TOTAL_BYTES = 20 * 1024 * 1024;
const MAX_FILE_BYTES = 2 * 1024 * 1024;

// ---- File System Access API 最小类型（TS lib 未内置时兜底）----
interface WritableLike { write(data: unknown): Promise<void>; close(): Promise<void>; }
interface FileHandleLike {
  kind: "file";
  getFile(): Promise<File>;
  createWritable(): Promise<WritableLike>;
}
interface DirHandleLike {
  kind: "directory";
  name: string;
  entries(): AsyncIterableIterator<[string, DirHandleLike | FileHandleLike]>;
  getDirectoryHandle(name: string, opts?: { create?: boolean }): Promise<DirHandleLike>;
  getFileHandle(name: string, opts?: { create?: boolean }): Promise<FileHandleLike>;
}
declare global {
  interface Window { showDirectoryPicker?: (opts?: { mode?: "read" | "readwrite" }) => Promise<DirHandleLike>; }
}

interface DevFileInfo { path: string; status: string; size: number }

type Bubble = {
  id: number;
  role: "user" | "ai";
  kind: "requirement" | "plan" | "apply" | "error";
  text?: string;          // 需求 / 方案文本 / 改动说明
  requirement?: string;   // 方案对应的需求（确认/重规划时回传）
  planFiles?: string[];   // 预计改动文件
  questions?: string[];   // AI 待确认问题
  devFiles?: DevFileInfo[];
  diff?: string;
  zipB64?: string;
  planText?: string;      // 确认改码时要回传的方案
  applied?: number;
  devCommand?: string;    // 操作型需求：执行过的命令
  devOutput?: string;     // 命令输出
  devOutputOk?: boolean;
  devAnalysis?: string;   // 分析代码的结果文本
};

function base64ToUint8(b64: string): Uint8Array {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function uint8ToBlob(bytes: Uint8Array, type: string): Blob {
  // 兼容 TS 的 ArrayBufferLike 泛型：取精确的 ArrayBuffer 切片
  const buf = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  return new Blob([buf], { type });
}

export default function ClaudePage() {
  const [dir, setDir] = useState<DirHandleLike | null>(null);
  const [dirName, setDirName] = useState("");
  const [fileCount, setFileCount] = useState(0);
  const [zipBlob, setZipBlob] = useState<Blob | null>(null);
  const [legacyFiles, setLegacyFiles] = useState<File[] | null>(null); // 无 showDirectoryPicker 的兼容模式
  const [bubbles, setBubbles] = useState<Bubble[]>([]);
  const [requirement, setRequirement] = useState("");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const idRef = useRef(0);
  const scrollRef = useRef<HTMLDivElement>(null);

  const nextId = () => ++idRef.current;

  const push = useCallback((b: Omit<Bubble, "id">) => {
    setBubbles((prev) => [...prev, { ...b, id: nextId() }]);
  }, []);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [bubbles]);

  // buildZip 接受显式数据源（避免 React state 闭包旧值问题：选完文件夹立即打包必须用新句柄）
  const buildZip = useCallback(async (dirArg?: DirHandleLike | null, filesArg?: File[] | null): Promise<{ blob: Blob; count: number }> => {
    const d = dirArg !== undefined ? dirArg : dir;
    const lf = filesArg !== undefined ? filesArg : legacyFiles;
    // 兼容模式：来自 <input webkitdirectory> 的只读文件列表
    if (lf && lf.length > 0 && !d) {
      const zip = new JSZip();
      let total = 0;
      let count = 0;
      for (const f of lf) {
        if (count >= MAX_FILES || total >= MAX_TOTAL_BYTES) break;
        if (f.size > MAX_FILE_BYTES) continue;
        const ext = "." + f.name.split(".").pop()!.toLowerCase();
        if (EXCLUDE_EXTS.has(ext)) continue;
        const rel = (f as File & { webkitRelativePath?: string }).webkitRelativePath || f.name;
        if (rel.split("/").some((p) => EXCLUDE_DIRS.has(p))) continue;
        total += f.size;
        count++;
        zip.file(rel, await f.arrayBuffer());
      }
      return { blob: await zip.generateAsync({ type: "blob", compression: "DEFLATE" }), count };
    }
    if (!d) throw new Error("请先选择文件夹");
    const zip = new JSZip();
    const counter = { files: 0, total: 0 };
    async function walk(dirH: DirHandleLike, base: string) {
      for await (const [name, handle] of dirH.entries()) {
        if (handle.kind === "directory") {
          if (!EXCLUDE_DIRS.has(name)) await walk(handle, base + name + "/");
        } else {
          const ext = "." + name.split(".").pop()!.toLowerCase();
          if (EXCLUDE_EXTS.has(ext)) continue;
          if (counter.files >= MAX_FILES || counter.total >= MAX_TOTAL_BYTES) continue;
          try {
            const file = await handle.getFile();
            if (file.size > MAX_FILE_BYTES) continue;
            counter.files++;
            counter.total += file.size;
            zip.file(base + name, await file.arrayBuffer());
          } catch { /* 不可读文件跳过 */ }
        }
      }
    }
    await walk(d, "");
    const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
    return { blob, count: counter.files };
  }, [dir, legacyFiles]);

  const pickFolder = useCallback(async () => {
    setError(null);
    try {
      if (window.showDirectoryPicker) {
        const d = await window.showDirectoryPicker({ mode: "readwrite" });
        setDir(d);
        setDirName(d.name);
        setLegacyFiles(null);
        // 用新句柄直接打包（不等 React 重渲染）
        const { blob, count } = await buildZipRef.current(d, null);
        setZipBlob(blob);
        setFileCount(count);
      } else {
        setError("当前浏览器不支持直接读写文件夹，请使用 Chrome/Edge；已提供文件列表兼容模式");
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  // 让 pickFolder 能拿到最新的 buildZip（避免 useCallback 依赖链）
  const buildZipRef = useRef(buildZip);
  buildZipRef.current = buildZip;

  const onLegacyFiles = useCallback(async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    setError(null);
    setLegacyFiles(Array.from(files));
    setDir(null);
    setDirName(Array.from(files)[0]?.webkitRelativePath?.split("/")[0] || "所选文件夹");
    const { blob, count } = await buildZipRef.current(null, Array.from(files));
    setZipBlob(blob);
    setFileCount(count);
  }, []);

  const postDev = useCallback(async (path: string, fd: FormData, blob: Blob) => {
    fd.append("file", blob, "project.zip");
    const headers: Record<string, string> = { "X-Anonymous-Id": getAnonymousId() };
    const token = getAuthToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;  // 登录用户按本人积分/配额
    const res = await fetch(`${API}${path}`, {
      method: "POST",
      body: fd,
      headers,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || `${path} 失败: ${res.status}`);
    return data;
  }, []);

  const sendRequirement = useCallback(async () => {
    const req = requirement.trim();
    if (!req || busy) return;
    if (!zipBlob) {
      setError("请先选择文件夹");
      return;
    }
    setError(null);
    setFeedback("");
    push({ role: "user", kind: "requirement", text: req });
    setRequirement("");
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("requirement", req);
      const data = await postDev("/api/dev/plan", fd, zipBlob);
      push({
        role: "ai", kind: "plan",
        requirement: req,  // 关键：方案气泡记住需求文本，确认/重规划用它回传
        text: (data.plan || "").trim() || "(AI 未给出方案文本)",
        planFiles: (data.files || []).map((f: unknown) => (typeof f === "string" ? f : (f as { path?: string })?.path || String(f))),
        questions: data.questions || [],
        planText: (data.plan || "").trim(),
      });
    } catch (e) {
      push({ role: "ai", kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }, [requirement, busy, zipBlob, postDev, push]);

  const replan = useCallback(async (planText: string, req: string) => {
    if (busy || !zipBlob) return;
    const fb = feedback.trim();
    if (!fb || !req) return;
    setError(null);
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("requirement", req);
      fd.append("feedback", fb);
      const data = await postDev("/api/dev/plan", fd, zipBlob);
      setFeedback("");
      push({
        role: "ai", kind: "plan",
        requirement: req,
        text: (data.plan || "").trim() || "(AI 未给出方案文本)",
        planFiles: (data.files || []).map((f: unknown) => (typeof f === "string" ? f : (f as { path?: string })?.path || String(f))),
        questions: data.questions || [],
        planText: (data.plan || "").trim(),
      });
    } catch (e) {
      push({ role: "ai", kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }, [busy, zipBlob, feedback, postDev, push]);

  const confirmPlan = useCallback(async (planText: string, req: string) => {
    if (busy || !zipBlob) return;
    if (!req && !planText) return;
    setError(null);
    setBusy(true);
    try {
      const fd = new FormData();
      fd.append("requirement", req);
      fd.append("plan", planText);
      if (feedback.trim()) fd.append("feedback", feedback.trim());
      const data = await postDev("/api/dev/apply", fd, zipBlob);
      setFeedback("");
      push({
        role: "ai", kind: "apply",
        text: data.dev_summary || "(无改动说明)",
        devFiles: data.dev_files || [],
        diff: data.dev_diff || "",
        zipB64: data.dev_modified_zip || "",
        devCommand: data.dev_command || "",
        devOutput: data.dev_output || "",
        devOutputOk: data.dev_output_ok !== false,
        devAnalysis: data.dev_analysis || "",
      });
      // 改码成功后直接应用回本地文件夹（无需再次确认）；兼容模式提示下载
      if (data.dev_modified_zip && data.dev_files && data.dev_files.length > 0) {
        if (dir) {
          const applied = await writeZipToFolderRef.current(data.dev_modified_zip);
          // 重新打包（下一轮包含本次改动）
          const { blob, count: c2 } = await buildZipRef.current(dir, null);
          setZipBlob(blob);
          setFileCount(c2);
          push({ role: "ai", kind: "apply", text: `✅ 已自动应用 ${applied} 个文件到本地文件夹（${dirName}）`, applied });
        } else {
          push({ role: "ai", kind: "apply", text: "改码完成（兼容模式不能写回文件夹，可用下方「下载修改后 zip」应用）" });
        }
      }
    } catch (e) {
      push({ role: "ai", kind: "error", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setBusy(false);
    }
  }, [busy, zipBlob, feedback, postDev, push, dir, dirName]);

  // 把修改后 zip 写回本地文件夹，返回写入文件数
  const writeZipToFolder = useCallback(async (zipB64: string): Promise<number> => {
    if (!dir) throw new Error("兼容模式下不能写回文件夹，请用「下载修改后 zip」");
    const zip = await JSZip.loadAsync(base64ToUint8(zipB64));
    let count = 0;
    for (const rel of Object.keys(zip.files)) {
      const entry = zip.files[rel];
      if (entry.dir) continue;
      if (rel.split("/").includes("..") || /^[A-Za-z]:/.test(rel)) continue; // 防穿越
      const parts = rel.split("/").filter(Boolean);
      if (parts.length === 0) continue;
      let cur = dir;
      for (let i = 0; i < parts.length - 1; i++) {
        cur = await cur.getDirectoryHandle(parts[i], { create: true });
      }
      const fh = await cur.getFileHandle(parts[parts.length - 1], { create: true });
      const w = await fh.createWritable();
      try {
        await w.write(await entry.async("arraybuffer"));
      } finally {
        try { await w.close(); } catch { /* 关闭失败不阻塞 */ }
      }
      count++;
    }
    return count;
  }, [dir]);
  const writeZipToFolderRef = useRef(writeZipToFolder);
  writeZipToFolderRef.current = writeZipToFolder;

  const clearAll = () => {
    setBubbles([]);
    setRequirement("");
    setFeedback("");
    setError(null);
  };

  const lastPlanId = [...bubbles].reverse().find((b) => b.kind === "plan")?.id;

  return (
    <div className="min-h-screen bg-gradient-to-b from-indigo-50/60 via-white to-fuchsia-50/40 dark:from-slate-950 dark:via-slate-950 dark:to-indigo-950/40">
      <AppNav />
      <main className="max-w-4xl mx-auto px-4 py-6 space-y-4">
        {/* 顶部：选文件夹 + 状态 */}
        <Card className="rounded-3xl border-violet-200/70 dark:border-violet-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-violet-500/5">
          <CardContent className="p-5 sm:p-6 space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <div className="flex-1 min-w-0">
                <h1 className="text-xl sm:text-2xl font-extrabold tracking-tight bg-gradient-to-r from-violet-600 via-fuchsia-600 to-indigo-600 dark:from-violet-400 dark:via-fuchsia-400 dark:to-indigo-400 bg-clip-text text-transparent">
                  AI 改码助手
                </h1>
                <p className="mt-1 text-xs text-muted-foreground">
                  选一个本地文件夹，用自然语言让它改代码：先出方案 → 你确认或提意见 → 改码 → 应用回文件夹。
                </p>
              </div>
              <div className="flex items-center gap-2">
                <Button size="sm" onClick={pickFolder} disabled={busy} className="gap-1.5 rounded-xl bg-gradient-to-r from-violet-600 to-fuchsia-600 hover:opacity-90 text-white">
                  <FolderGit2 className="w-4 h-4" /> 选择文件夹
                </Button>
                <label className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-500 hover:text-violet-600 cursor-pointer rounded-lg px-2 py-1.5 hover:bg-violet-50 dark:hover:bg-violet-950/50 transition-colors" title="兼容模式（不能写回，只能下载修改后 zip）">
                  兼容选择
                  <input type="file" multiple className="hidden" disabled={busy}
                    {...({ webkitdirectory: "" } as Record<string, unknown>)}
                    onChange={(e) => { onLegacyFiles(e.target.files); e.target.value = ""; }} />
                </label>
              </div>
            </div>
            {dirName && (
              <div className="inline-flex items-center gap-2 rounded-xl border border-violet-200 dark:border-violet-800 bg-violet-50/70 dark:bg-violet-950/50 px-3 py-1.5 text-xs text-violet-700 dark:text-violet-300">
                <FolderGit2 className="w-3.5 h-3.5" />
                <span className="max-w-56 truncate">{dirName}</span>
                <span className="text-violet-400">· {fileCount} 个文件</span>
                {!dir && <span className="text-amber-500">（兼容模式，仅可下载）</span>}
              </div>
            )}
          </CardContent>
        </Card>

        {/* 中间：聊天区 */}
        <Card className="rounded-3xl border-slate-200/70 dark:border-slate-800/70 bg-white/80 dark:bg-slate-900/70 backdrop-blur-xl shadow-xl shadow-indigo-500/5">
          <CardContent className="p-4 sm:p-5">
            <div ref={scrollRef} className="h-[52vh] overflow-auto space-y-4 pr-1">
              {bubbles.length === 0 && (
                <div className="h-full flex flex-col items-center justify-center text-center text-muted-foreground gap-2 py-16">
                  <Wand2 className="w-8 h-8 text-violet-400" />
                  <p className="text-sm">选择文件夹后，在这里输入需求，例如：</p>
                  <p className="text-xs text-slate-400">「给 todo.py 加一个删除任务的功能」</p>
                </div>
              )}

              {bubbles.map((b) => (
                <div key={b.id} className={b.role === "user" ? "flex justify-end" : "flex justify-start"}>
                  {b.role === "user" ? (
                    <div className="max-w-[85%] rounded-2xl rounded-br-md bg-gradient-to-r from-indigo-600 to-violet-600 text-white px-4 py-2.5 text-sm shadow-md shadow-indigo-500/20 whitespace-pre-wrap">
                      {b.text}
                    </div>
                  ) : b.kind === "error" ? (
                    <div className="max-w-[90%] w-full rounded-2xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950/50 text-red-600 dark:text-red-300 px-4 py-3 text-sm">
                      <div className="flex items-start gap-2">
                        <XCircle className="w-4 h-4 mt-0.5 shrink-0" />
                        <span className="break-words">{b.text}</span>
                      </div>
                    </div>
                  ) : b.kind === "plan" ? (
                    <div className="max-w-[90%] w-full rounded-2xl border border-violet-200/70 dark:border-violet-800/70 bg-violet-50/50 dark:bg-violet-950/30 px-4 py-3 space-y-3">
                      <div className="flex items-center gap-1.5 text-xs font-semibold text-violet-600 dark:text-violet-300">
                        <Wand2 className="w-3.5 h-3.5" /> AI 修改方案（先确认，不改代码）
                      </div>
                      <p className="text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap max-h-56 overflow-auto">{b.text}</p>
                      {b.planFiles && b.planFiles.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {b.planFiles.map((f, i) => (
                            <span key={i} className="inline-flex items-center gap-1 rounded-lg bg-white/70 dark:bg-slate-900/70 border border-violet-200/60 dark:border-violet-800 px-2 py-1 text-[11px] text-slate-600 dark:text-slate-300 font-mono">
                              <FileCode2 className="w-3 h-3 text-violet-400" /> {f}
                            </span>
                          ))}
                        </div>
                      )}
                      {b.questions && b.questions.length > 0 && (
                        <div className="space-y-1">
                          <p className="text-[11px] font-medium text-amber-600 dark:text-amber-400">AI 需要你确认</p>
                          {b.questions.map((q, i) => (
                            <p key={i} className="text-xs text-amber-700 dark:text-amber-300 bg-amber-50 dark:bg-amber-950/50 rounded-lg px-3 py-1.5">? {q}</p>
                          ))}
                        </div>
                      )}
                      {/* 只有最新方案才显示操作区 */}
                      {b.id === lastPlanId && (
                        <div className="space-y-2">
                          <div className="flex flex-col sm:flex-row gap-2">
                            <textarea value={feedback} onChange={(e) => setFeedback(e.target.value)}
                              placeholder="对方案有意见？在这里输入，AI 会重新规划。没意见直接点「确认并改码」"
                              className="flex-1 min-h-12 rounded-xl border border-slate-200 dark:border-slate-700 bg-white dark:bg-slate-900 px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-violet-400 resize-y" />
                            <Button variant="outline" size="sm" onClick={() => replan(b.planText || "", b.requirement || "")}
                              disabled={!feedback.trim() || busy} className="gap-1.5 rounded-xl text-violet-600 dark:text-violet-400">
                              <RefreshCw className="w-3.5 h-3.5" /> 重新规划
                            </Button>
                          </div>
                          <div className="flex flex-wrap gap-2">
                            <Button size="sm" onClick={() => confirmPlan(b.planText || "", b.requirement || "")} disabled={busy}
                              className="gap-1.5 rounded-xl bg-gradient-to-r from-violet-600 to-fuchsia-600 hover:opacity-90 text-white">
                              {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle2 className="w-4 h-4" />}
                              {busy ? "AI 正在生成代码…（大项目约 1-3 分钟）" : "确认并改码"}
                            </Button>
                            <Button size="sm" variant="ghost" onClick={clearAll} disabled={busy}>清空对话</Button>
                          </div>
                        </div>
                      )}
                    </div>
                  ) : b.kind === "apply" ? (
                    <div className="max-w-[90%] w-full rounded-2xl border border-emerald-200/70 dark:border-emerald-800/70 bg-emerald-50/50 dark:bg-emerald-950/30 px-4 py-3 space-y-3">
                      <div className="flex items-center gap-1.5 text-xs font-semibold text-emerald-600 dark:text-emerald-300">
                        <CheckCircle2 className="w-3.5 h-3.5" /> AI 改动{b.applied !== undefined ? "（已应用）" : ""}
                      </div>
                      <p className="text-sm text-slate-700 dark:text-slate-200">{b.text}</p>
                      {b.devFiles && b.devFiles.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                          {b.devFiles.map((f, i) => (
                            <span key={i} className="inline-flex items-center gap-1 rounded-lg bg-white/70 dark:bg-slate-900/70 border border-emerald-200/60 dark:border-emerald-800 px-2 py-1 text-[11px] text-slate-600 dark:text-slate-300 font-mono">
                              {f.status === "新增" ? "🆕" : "✏️"} {f.path}
                            </span>
                          ))}
                        </div>
                      )}
                      {b.diff && (
                        <details className="text-sm">
                          <summary className="cursor-pointer text-muted-foreground hover:text-foreground select-none">
                            查看代码改动 diff（{b.diff.length} 字符）
                          </summary>
                          <pre className="mt-2 max-h-72 overflow-auto rounded-xl bg-slate-950 text-slate-100 p-4 text-[11px] leading-relaxed whitespace-pre-wrap">{b.diff}</pre>
                        </details>
                      )}
                      {b.devAnalysis && (
                        <div className="rounded-xl border border-sky-200/60 dark:border-sky-800 bg-sky-50/60 dark:bg-sky-950/30 px-3 py-2">
                          <p className="text-[11px] font-semibold text-sky-600 dark:text-sky-300 mb-1">🔍 分析结果</p>
                          <p className="text-sm text-slate-700 dark:text-slate-200 whitespace-pre-wrap max-h-72 overflow-auto">{b.devAnalysis}</p>
                        </div>
                      )}
                      {b.devCommand && (
                        <div className={`rounded-xl border px-3 py-2 text-xs ${b.devOutputOk ? "border-emerald-200/60 dark:border-emerald-800 bg-emerald-50/60 dark:bg-emerald-950/30" : "border-red-200/60 dark:border-red-800 bg-red-50/60 dark:bg-red-950/30"}`}>
                          <p className={`font-mono mb-1 ${b.devOutputOk ? "text-emerald-700 dark:text-emerald-300" : "text-red-600 dark:text-red-300"}`}>
                            {b.devOutputOk ? "▶ 已执行：" : "⚠ 执行失败："}{b.devCommand}
                          </p>
                          {b.devOutput && (
                            <pre className="max-h-48 overflow-auto whitespace-pre-wrap text-[11px] text-slate-600 dark:text-slate-300">{b.devOutput}</pre>
                          )}
                        </div>
                      )}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>

            {error && (
              <p className="mt-3 text-sm text-red-500 bg-red-50 dark:bg-red-950 rounded-xl px-4 py-2.5">{error}</p>
            )}

            {/* 底部输入框 */}
            <div className="mt-4 flex items-end gap-2">
              <textarea
                value={requirement}
                onChange={(e) => setRequirement(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    sendRequirement();
                  }
                }}
                placeholder={"输入需求，如：给 todo.py 加一个删除任务的功能（Enter 发送 / Shift+Enter 换行）"}
                className="flex-1 min-h-[60px] rounded-2xl border border-slate-200 dark:border-slate-700 bg-white/90 dark:bg-slate-900/90 px-4 py-3 text-sm shadow-inner outline-none focus:ring-4 focus:ring-violet-400/20 focus:border-violet-400 transition-all resize-y"
              />
              <Button onClick={sendRequirement} disabled={!requirement.trim() || busy}
                className="gap-2 h-[60px] px-6 rounded-2xl bg-gradient-to-r from-violet-600 via-fuchsia-600 to-indigo-600 hover:opacity-90 text-white font-semibold shadow-lg shadow-violet-500/30 disabled:opacity-50">
                {busy ? <Loader2 className="w-5 h-5 animate-spin" /> : <Send className="w-5 h-5" />}
                <span className="hidden sm:inline">{busy ? "处理中…" : "发送"}</span>
              </Button>
            </div>
            <div className="mt-2 flex items-center justify-between text-[11px] text-muted-foreground">
              <span>流程：方案（便宜模型）→ 确认/意见 → 改码（reasoner）→ 应用到本地文件夹</span>
              <button onClick={clearAll} className="inline-flex items-center gap-1 hover:text-red-500 transition-colors">
                <Trash2 className="w-3 h-3" /> 清空
              </button>
            </div>
          </CardContent>
        </Card>
      </main>
    </div>
  );
}
