// Google 设备流登录：经服务器 /auth/device/* 桥（client_secret 不落客户端）。
// UI 由调用方给回调（显示 user_code、打开链接、轮询节奏），这里只管状态机。

import { V2mdApi, V2mdError } from "./api.ts";

export interface DeviceLoginUi {
  /** 拿到授权码对：展示 ABBD-1234 样式代码 + verification_url */
  onCode(code: string, url: string): void;
  /** 每次轮询的进度提示（可选） */
  onTick?(attempt: number): void;
}

export interface DeviceLoginResult {
  idToken: string;
}

const MAX_POLLS = 80; // interval≥5s × 80 ≈ 6min+，覆盖 Google 15min 窗口内的正常手速

/** 走完整个设备流：申请 → 轮询到用户授权 → 回 id_token。取消信号中断等待。 */
export async function deviceLogin(
  api: V2mdApi,
  ui: DeviceLoginUi,
  signal?: AbortSignal,
): Promise<DeviceLoginResult> {
  const dc = await api.deviceCode();
  ui.onCode(dc.user_code, dc.verification_url);
  const intervalMs = Math.max(dc.interval || 5, 5) * 1000;
  for (let i = 0; i < MAX_POLLS; i++) {
    if (signal?.aborted) throw new Error("登录已取消");
    await sleep(intervalMs, signal);
    ui.onTick?.(i + 1);
    try {
      const idToken = await api.deviceToken(dc.device_code);
      return { idToken };
    } catch (e) {
      // 401 = authorization_pending/slow_down/expired——pending 继续轮，其余两档早退
      if (e instanceof V2mdError && e.kind === "unauthorized") {
        const msg = String(e.message);
        if (msg.includes("expired") || msg.includes("invalid_grant") || msg.includes("access_denied")) {
          throw new Error("设备码已失效或被拒绝授权，请重新点登录");
        }
        continue;
      }
      throw e; // offline/server/mode 类错误重试无意义
    }
  }
  throw new Error("等待授权超时，请重新点登录");
}

function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const t = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      reject(new Error("登录已取消"));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}
