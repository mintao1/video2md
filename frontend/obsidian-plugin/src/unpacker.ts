// bundle zip → 本机 vault 写入计划的纯逻辑（零 obsidian/node 依赖，可单测）。
// 服务端 outbox 形状（graph._relocate_outbox + artsink）：
//   <标题>.md                    —— 笔记正文（合集在子文件夹 collection/<标题>.md）
//   attachments/*.{jpg,mp4}      —— 帧图/节段视频
//   transcripts/<标题>-transcript.txt
//   索引-append.md               —— 本 run 新增的索引行（按首列 wikilink 去重后并进目标索引）

export interface ZipEntry {
  /** zip 内相对路径（正斜杠） */
  name: string;
  data: Uint8Array;
}

export interface WritePlan {
  /** 落盘文件：vault 相对路径 → 内容 */
  files: Map<string, Uint8Array>;
  /** 要 append 进目标索引的行（已按现有索引去重） */
  indexLines: string[];
  /** 被跳过（内容已存在同名同大小）的文件数——统计展示用 */
  skippedFiles: number;
}

/** zip 名安全闸：拒绝绝对路径与 ..（zip-slip），返回清洗后的相对路径或 null */
export function safeRelPath(name: string): string | null {
  const n = name.replace(/\\/g, "/").replace(/^\/+/, "");
  if (!n || n === "." || n.split("/").some((s) => s === ".." || s === "")) return null;
  return n;
}

/** 把 zip 条目映射为「目标文件夹下」的写入计划。existing：目标文件夹里已有的
 * 相对路径 → 字节数（幂等防重复写：同名同大小视为已入库，除非 force）。 */
export function planBundle(
  entries: ZipEntry[],
  targetFolder: string,
  existing: Map<string, number>,
  force = false,
): WritePlan {
  const files = new Map<string, Uint8Array>();
  const indexLines: string[] = [];
  let skippedFiles = 0;
  const prefix = targetFolder.replace(/\/+$/, "") + "/";

  // 先收集索引行（对全部条目算完再决定 md 写不写：索引去重要看整包）
  for (const e of entries) {
    const rel = safeRelPath(e.name);
    if (rel === null) continue;
    if (rel === "索引-append.md") {
      const text = new TextDecoder().decode(e.data);
      indexLines.push(...text.split("\n").map((l) => l.trim()).filter((l) => l.startsWith("|")));
      continue;
    }
    const dest = prefix + rel;
    if (!force && existing.get(dest) === e.data.byteLength) {
      skippedFiles += 1;
      continue;
    }
    files.set(dest, e.data);
  }
  return { files, indexLines, skippedFiles };
}

/** 索引表按首列 wikilink 去重：返回该追加到索引末尾的行（现索引没有的键） */
export function dedupeIndexLines(newLines: string[], currentIndexText: string): string[] {
  const seen = new Set<string>();
  for (const line of currentIndexText.split("\n")) {
    const k = indexKey(line);
    if (k) seen.add(k);
  }
  const out: string[] = [];
  for (const line of newLines) {
    const k = indexKey(line);
    if (!k || seen.has(k)) continue;
    seen.add(k);
    out.push(line);
  }
  return out;
}

/** 首列 `[[标题]]` 作去重键（与服务端索引表口径一致） */
function indexKey(line: string): string | null {
  if (!line.trim().startsWith("|")) return null;
  const m = line.match(/\[\[([^\]|]+)\]\]/);
  return m ? m[1].trim() : null;
}
