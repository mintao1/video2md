// 本地账本：已领取的 run 清单（run_id → 时间戳），存 vault 根 .v2md-received.json。
// 为什么放 vault 而不是 plugin data.json：换机器/重装 Obsidian 时账本跟库走，
// 不会把云端在架产物重复领一遍（deliver TTL 内 bundle 还能拉）。

export interface Ledger {
  received: Record<string, number>; // run_id → 领取时刻（epoch ms）
}

export function emptyLedger(): Ledger {
  return { received: {} };
}

export function parseLedger(text: string): Ledger {
  try {
    const d = JSON.parse(text) as Partial<Ledger>;
    if (d && typeof d.received === "object" && d.received !== null) {
      return { received: d.received as Record<string, number> };
    }
  } catch {
    /* 坏文件按空账本处理——领取本身幂等（unpacker 同名同大小跳过），不会写坏库 */
  }
  return emptyLedger();
}

export function markReceived(led: Ledger, runId: string, ts = Date.now()): Ledger {
  return { received: { ...led.received, [runId]: ts } };
}

export function isReceived(led: Ledger, runId: string): boolean {
  return Object.prototype.hasOwnProperty.call(led.received, runId);
}
