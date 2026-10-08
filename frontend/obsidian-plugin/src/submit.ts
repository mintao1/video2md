// 提交通道：贴视频链接（兼容抖音口令文本——正则抽内嵌短链，同 batch_csv.py 口径）→ POST /runs。

export function extractUrl(text: string): string {
  const m = text.match(/https?:\/\/[^\s，,、)）]+/);
  const u = (m ? m[0] : text).trim();
  if (!u) return "";
  return /^https?:\/\//.test(u) ? u : "https://" + u;
}

export interface SubmitResult {
  runId: string;
  total: number;
  allSkipped: boolean;
}

import type { V2mdApi } from "./api.ts";

export async function submitUrl(api: V2mdApi, raw: string): Promise<SubmitResult> {
  const url = extractUrl(raw);
  if (!url) throw new Error("没找到有效链接");
  const d = await api.postRun(url);
  return { runId: d.run_id, total: d.total, allSkipped: d.all_skipped };
}
