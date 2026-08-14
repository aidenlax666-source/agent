"use client";

import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { AlertTriangle, RefreshCw } from "lucide-react";

export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error("Page error:", error);
  }, [error]);

  return (
    <div className="min-h-screen flex items-center justify-center p-4">
      <div className="text-center max-w-md">
        <div className="w-16 h-16 rounded-2xl bg-red-100 dark:bg-red-900/50 flex items-center justify-center mx-auto mb-4">
          <AlertTriangle className="w-8 h-8 text-red-500" />
        </div>
        <h2 className="text-xl font-bold mb-2">页面加载失败</h2>
        <p className="text-sm text-muted-foreground mb-6">
          {error.message || "发生了意外错误，请重试"}
        </p>
        <Button onClick={reset} className="gap-2">
          <RefreshCw className="w-4 h-4" />
          重新加载
        </Button>
      </div>
    </div>
  );
}
