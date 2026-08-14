"use client";

import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Clock, Database, Table2 } from "lucide-react";
import type { PreviewData } from "@/lib/types";

interface PreviewTableProps {
  data: PreviewData | null;
  totalEstimate?: number;
}

export function PreviewTable({ data, totalEstimate }: PreviewTableProps) {
  if (!data || !data.columns || data.columns.length === 0) {
    return (
      <Card className="w-full max-w-4xl mx-auto rounded-2xl border-2 border-slate-200/60 dark:border-slate-800/60">
        <CardContent className="p-8 text-center">
          <div className="w-12 h-12 rounded-xl bg-slate-100 dark:bg-slate-800 flex items-center justify-center mx-auto mb-3">
            <Table2 className="w-6 h-6 text-slate-400" />
          </div>
          <p className="text-muted-foreground font-medium">暂无预览数据</p>
          {data?.error && (
            <p className="text-xs text-red-500 mt-1.5 bg-red-50 dark:bg-red-950 rounded-lg p-2 max-w-md mx-auto">
              {data.error}
            </p>
          )}
        </CardContent>
      </Card>
    );
  }

  const rows = data.rows || [];
  const estimate = totalEstimate || data.total_estimate || rows.length;

  return (
    <Card className="w-full max-w-4xl mx-auto rounded-2xl border-2 border-slate-200/60 dark:border-slate-800/60 shadow-lg overflow-hidden">
      <div className="h-1 bg-gradient-to-r from-emerald-500 to-teal-500" />
      <CardHeader className="pb-4">
        <div className="flex items-center justify-between flex-wrap gap-3">
          <CardTitle className="text-lg flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-emerald-100 dark:bg-emerald-900/50 flex items-center justify-center">
              <Table2 className="w-4 h-4 text-emerald-600 dark:text-emerald-400" />
            </div>
            预览结果
          </CardTitle>
          <div className="flex items-center gap-2">
            <Badge variant="outline" className="gap-1.5 font-normal">
              <Database className="w-3 h-3" />
              {rows.length} / {estimate} 条
            </Badge>
            {data.execution_time > 0 && (
              <Badge variant="secondary" className="gap-1.5 font-normal">
                <Clock className="w-3 h-3" />
                {data.execution_time.toFixed(1)}s
              </Badge>
            )}
          </div>
        </div>
      </CardHeader>
      <CardContent className="p-0">
        <div className="overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow className="bg-slate-50 dark:bg-slate-900/50 border-b-2">
                <TableHead className="w-12 text-center font-semibold">#</TableHead>
                {data.columns.map((col, i) => (
                  <TableHead key={i} className="font-semibold">
                    {col}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.length > 0 ? (
                rows.map((row, i) => (
                  <TableRow key={i} className="hover:bg-indigo-50/50 dark:hover:bg-indigo-950/20 transition-colors">
                    <TableCell className="text-center text-muted-foreground text-xs font-medium">
                      {i + 1}
                    </TableCell>
                    {row.map((cell, j) => (
                      <TableCell
                        key={j}
                        className="max-w-xs truncate"
                        title={typeof cell === "string" ? cell : String(cell)}
                      >
                        {typeof cell === "string" && cell.length > 80
                          ? cell.slice(0, 80) + "..."
                          : String(cell)}
                      </TableCell>
                    ))}
                  </TableRow>
                ))
              ) : (
                <TableRow>
                  <TableCell
                    colSpan={data.columns.length + 1}
                    className="text-center text-muted-foreground py-8"
                  >
                    暂无数据
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
