"use client";

import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ExternalLink, QrCode, RefreshCw, Link2, Download } from "lucide-react";
import Link from "next/link";
import AppNav from "@/components/AppNav";
import { getAuthToken, getAnonymousId, ASSETS_BASE } from "@/lib/api";

const API_BASE = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface GalleryItem {
  path: string;
  filename: string;
  name: string;
  type: string;
  desc: string;
}

const TYPE_STYLE: Record<string, string> = {
  game: "bg-indigo-100 text-indigo-700",
  manga: "bg-violet-100 text-violet-700",
  music: "bg-cyan-100 text-cyan-700",
  video: "bg-rose-100 text-rose-700",
  report: "bg-emerald-100 text-emerald-700",
  html: "bg-slate-100 text-slate-600",
};

const TYPE_LABEL: Record<string, string> = {
  game: "游戏", manga: "漫剧", music: "音乐", video: "视频", report: "报告", html: "网页",
};

export default function GalleryPage() {
  const [items, setItems] = useState<GalleryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [origin, setOrigin] = useState("");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const headers: Record<string, string> = { "X-Anonymous-Id": getAnonymousId() };
      const token = getAuthToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const r = await fetch(`${API_BASE}/api/gallery`, { headers });
      if (!r.ok) {
        throw new Error(`加载失败: ${r.status}`);
      }
      const data = await r.json();
      setItems(data.items || []);
      // 分享链接指向产物域（与 API 不同源）
      setOrigin(ASSETS_BASE);
    } catch (e) {
      setItems([]);
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const [downloading, setDownloading] = useState(false);

  const downloadZip = async () => {
    if (downloading) return;
    setDownloading(true);
    try {
      const headers: Record<string, string> = { "X-Anonymous-Id": getAnonymousId() };
      const token = getAuthToken();
      if (token) headers["Authorization"] = `Bearer ${token}`;
      const r = await fetch(`${API_BASE}/api/gallery/download-zip`, { headers });
      if (!r.ok) throw new Error(`下载失败: ${r.status}`);
      const blob = await r.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "works.zip";
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setDownloading(false);
    }
  };

  const fullUrl = (path: string) => `${origin}${path}`;
  const qrUrl = (path: string) =>
    `https://api.qrserver.com/v1/create-qr-code/?size=180x180&margin=8&data=${encodeURIComponent(fullUrl(path))}`;

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-50 via-white to-emerald-50/60 dark:from-slate-950 dark:via-slate-950 dark:to-emerald-950/30">
      <AppNav />

      <main className="max-w-6xl mx-auto px-4 py-10">
        <div className="mb-8 flex items-center gap-3 flex-wrap">
          <div className="flex-1">
            <h1 className="text-2xl font-bold flex items-center gap-2">
              <QrCode className="w-6 h-6 text-emerald-500" /> 你的可分享作品
            </h1>
            <p className="text-sm text-muted-foreground mt-1">
              游戏 / 漫剧 / 音乐 / 视频 / 报告 / 图片，扫码或复制链接即可转发分享
              {origin ? `（当前站点：${origin}）` : ""}
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={load} className="gap-2">
            <RefreshCw className="w-4 h-4" /> 刷新
          </Button>
          <Button variant="outline" size="sm" onClick={downloadZip} disabled={downloading || items.length === 0} className="gap-2">
            {downloading ? <RefreshCw className="w-4 h-4 animate-spin" /> : <Download className="w-4 h-4" />}
            打包下载
          </Button>
        </div>

        {loading ? (
          <p className="text-center text-muted-foreground py-12">加载中...</p>
        ) : error ? (
          <Card className="rounded-2xl">
            <CardContent className="p-10 text-center text-red-500">{error}（点击刷新重试）</CardContent>
          </Card>
        ) : items.length === 0 ? (
          <Card className="rounded-2xl">
            <CardContent className="p-10 text-center text-muted-foreground">
              暂无作品。先去 <Link href="/mini" className="text-indigo-500 hover:underline">一句话自动化</Link> 生成报告/漫剧/视频吧
            </CardContent>
          </Card>
        ) : (
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-5">
            {items.map((item, i) => (
              <Card key={i} className="rounded-2xl overflow-hidden border-2 border-slate-200/60 dark:border-slate-800/60">
                <div className="h-1 bg-gradient-to-r from-emerald-500 to-teal-500" />
                <CardHeader className="pb-2">
                  <div className="flex items-center justify-between gap-2">
                    <CardTitle className="text-base flex items-center gap-2">
                      <span className="truncate">{item.name}</span>
                    </CardTitle>
                    <Badge className={TYPE_STYLE[item.type] || TYPE_STYLE.html}>{TYPE_LABEL[item.type] || "网页"}</Badge>
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  <p className="text-xs text-muted-foreground">{item.desc}</p>
                  <div className="flex items-center gap-3">
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={qrUrl(item.path)}
                      alt="二维码"
                      width={88}
                      height={88}
                      className="rounded-lg border border-slate-200 dark:border-slate-700 shrink-0"
                    />
                    <div className="flex flex-col gap-1.5 min-w-0">
                      <a
                        href={fullUrl(item.path)}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1.5 text-sm text-indigo-600 dark:text-indigo-400 hover:underline truncate"
                      >
                        <ExternalLink className="w-3.5 h-3.5 shrink-0" />
                        打开
                      </a>
                      <button
                        onClick={() => navigator.clipboard?.writeText(fullUrl(item.path))}
                        className="inline-flex items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
                      >
                        <Link2 className="w-3.5 h-3.5 shrink-0" />
                        复制链接
                      </button>
                    </div>
                  </div>
                  <p className="text-[10px] text-muted-foreground truncate" title={fullUrl(item.path)}>
                    {fullUrl(item.path)}
                  </p>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}
