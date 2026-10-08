import assert from "node:assert/strict";
import { test } from "node:test";

import { dedupeIndexLines, planBundle, safeRelPath } from "./unpacker.ts";

const enc = new TextEncoder();

test("safeRelPath 挡 zip-slip", () => {
  assert.equal(safeRelPath("attachments/a.jpg"), "attachments/a.jpg");
  assert.equal(safeRelPath("/abs/x.md"), "abs/x.md");
  assert.equal(safeRelPath("../escape.md"), null);
  assert.equal(safeRelPath("a/../../b.md"), null);
  assert.equal(safeRelPath(""), null);
});

test("planBundle：映射目标路径 + 索引行剥离 + 同名同大小跳过", () => {
  const entries = [
    { name: "笔记A.md", data: enc.encode("# A") },
    { name: "attachments/f001.jpg", data: new Uint8Array([1, 2, 3]) },
    { name: "transcripts/笔记A-transcript.txt", data: enc.encode("转录") },
    { name: "索引-append.md", data: enc.encode("| [[笔记A]] | 0:10 | — | 2026-09-23 |\n\n说明\n") },
    { name: "../evil.md", data: enc.encode("x") },
  ];
  const existing = new Map<string, number>([["资料库/视频笔记/attachments/f001.jpg", 3]]);
  const p = planBundle(entries, "资料库/视频笔记", existing);
  assert.deepEqual([...p.files.keys()].sort(), [
    "资料库/视频笔记/transcripts/笔记A-transcript.txt",
    "资料库/视频笔记/笔记A.md",
  ]);
  assert.equal(p.skippedFiles, 1);
  assert.deepEqual(p.indexLines, ["| [[笔记A]] | 0:10 | — | 2026-09-23 |"]);
});

test("dedupeIndexLines：按首列 wikilink 去重（含跨窗重复）", () => {
  const current = "| [[旧笔记]] | x |\n| [[笔记A]] | 0:05 | — | 2026-09-20 |\n";
  const out = dedupeIndexLines(
    ["| [[笔记A]] | 0:10 | — | 2026-09-23 |", "| [[笔记B]] | 0:01 | — | 2026-09-23 |"],
    current,
  );
  assert.deepEqual(out, ["| [[笔记B]] | 0:01 | — | 2026-09-23 |"]);
});
