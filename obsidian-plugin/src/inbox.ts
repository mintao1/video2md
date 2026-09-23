// 收件箱轮询器：发现 done run → 拉 bundle → 解包写 vault → 记账本 →（可选）ACK。
// 纯编排，Obsidian 依赖全部从 deps 注入（vault fs / adapter），单测可换假实现。

import { V2mdApi, V2mdError } from "./api.ts";
import type { FsLike } from "./node.ts";
import { isReceived, markReceived, parseLedger, type Ledger } from "./ledger.ts";
import { dedupeIndexLines, planBundle, type ZipEntry } from "./unpacker.ts";

export interface InboxDeps {
  api: V2mdApi;
  fs: FsLike;
  joinPath: (...segs: string[]) => string;
  /** vault 绝对根路径 */
  vaultRoot: string;
  targetFolder: string; // 相对 vault 根，如 资料库/视频笔记
  autoAck: boolean;
  unzip: (buf: Uint8Array) => Promise<ZipEntry[]>;
  log: (msg: string) => void;
}

export interface SyncResult {
  received: number; // 本轮新入库的 run 数
  skippedAlready: number; // 账本命中直接跳过的 run 数
  errors: string[];
}

const LEDGER_NAME = ".v2md-received.json";

export class Inbox {
  private led: Ledger = { received: {} };
  private loaded = false;

  constructor(private d: InboxDeps) {}

  private ledgerPath(): string {
    return this.d.joinPath(this.d.vaultRoot, LEDGER_NAME);
  }

  async loadLedger(): Promise<void> {
    const text = await this.d.fs.readFileBuffer(this.ledgerPath()).then(
      (b) => new TextDecoder().decode(b),
      () => "",
    );
    this.led = text ? parseLedger(text) : { received: {} };
    this.loaded = true;
  }

  private async saveLedger(): Promise<void> {
    await this.d.fs.writeFile(this.ledgerPath(), JSON.stringify(this.led, null, 1));
  }

  receivedCount(): number {
    return Object.keys(this.led.received).length;
  }

  /** 一轮同步：列 done → 差集账本 → 逐个领取。单个 run 失败不阻断其余。 */
  async syncOnce(): Promise<SyncResult> {
    if (!this.loaded) await this.loadLedger();
    const res: SyncResult = { received: 0, skippedAlready: 0, errors: [] };
    let list;
    try {
      list = await this.d.api.listRuns("done", 100);
    } catch (e) {
      res.errors.push(errText(e));
      return res;
    }
    if (list.mode !== "deliver") {
      res.errors.push(`服务器产物模式=${list.mode}：bundle 领取仅 deliver 可用`);
      return res;
    }
    for (const run of list.items) {
      if (isReceived(this.led, run.run_id)) {
        res.skippedAlready += 1;
        continue;
      }
      if (!run.artifacts?.present) continue; // 不在架（已焚/未搬家）——不可领也不报错
      try {
        await this.receive(run.run_id);
        res.received += 1;
      } catch (e) {
        if (e instanceof V2mdError && e.kind === "gone") continue; // 竞态被 TTL 烧了：正常
        res.errors.push(`${run.run_id}: ${errText(e)}`);
      }
    }
    return res;
  }

  /** 领取单个 run：拉 zip → 计划 → 写盘 → 并索引 → 记账 → ACK。 */
  async receive(runId: string): Promise<void> {
    const buf = await this.d.api.getBundle(runId);
    const entries = await this.d.unzip(buf);
    const destAbs = this.d.joinPath(this.d.vaultRoot, this.d.targetFolder);
    // existing 快照：目标文件夹下所有文件的大小（幂等防重复写）
    const existing = await this.walkSizes(destAbs, this.d.targetFolder);
    const plan = planBundle(entries, this.d.targetFolder, existing);

    for (const [rel, data] of plan.files) {
      const abs = this.d.joinPath(this.d.vaultRoot, rel);
      await this.d.fs.mkdirp(abs.slice(0, abs.lastIndexOf("/")));
      await this.d.fs.writeFile(abs, data);
    }

    if (plan.indexLines.length > 0) {
      await this.mergeIndex(plan.indexLines);
    }

    this.led = markReceived(this.led, runId);
    await this.saveLedger();
    this.d.log(`已领取 run ${runId}（${plan.files.size} 文件、跳过 ${plan.skippedFiles} 个同名同大小）`);

    if (this.d.autoAck) {
      try {
        await this.d.api.ack(runId);
        this.d.log(`run ${runId} 已 ACK（云端即焚）`);
      } catch (e) {
        // ACK 失败不影响本地已入库的事实；TTL 兜底会烧
        this.d.log(`run ${runId} ACK 失败（留待 TTL）：${errText(e)}`);
      }
    }
  }

  /** 索引合并：读现索引全文 → 去重 → 追加缺失行（走 fs 直写，不经 vault API——
   * 大索引 append 频繁，且插件写入的文件 Obsidian 会自动索引）。 */
  private async mergeIndex(lines: string[]): Promise<void> {
    const idxAbs = this.d.joinPath(this.d.vaultRoot, this.d.targetFolder, "索引.md");
    const cur = await this.d.fs.readFileBuffer(idxAbs).then(
      (b) => new TextDecoder().decode(b),
      () => "",
    );
    const add = dedupeIndexLines(lines, cur);
    if (add.length === 0) return;
    const body = cur.trimEnd() + (cur ? "\n" : "") + add.join("\n") + "\n";
    await this.d.fs.writeFile(idxAbs, body);
  }

  /** 目标文件夹递归 → Map<vault相对路径, 字节数> */
  private async walkSizes(absDir: string, relPrefix: string): Promise<Map<string, number>> {
    const out = new Map<string, number>();
    const stack = [absDir];
    while (stack.length > 0) {
      const dir = stack.pop() as string;
      const names = await this.d.fs.readdir(dir).catch(() => [] as string[]);
      for (const name of names) {
        const childAbs = this.d.joinPath(dir, name);
        const st = await this.d.fs.stat(childAbs);
        if (!st) continue;
        if (st.isDirectory()) {
          stack.push(childAbs);
        } else {
          const rel = relPrefix + "/" + childAbs.slice(absDir.length + 1);
          out.set(rel, st.size);
        }
      }
    }
    return out;
  }
}

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
