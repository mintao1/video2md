// video2md API 客户端：fetch + Authorization: Bearer <id_token>。
// 错误分层诊断是插件的「体验缝」：连不上→十有八九隧道没起；401→没登录/凭据过期；
// 409→服务器还在 vault 模式（bundle/ACK 不可用）。每档给不同人话提示。

export class V2mdError extends Error {
  constructor(
    message: string,
    readonly kind: "offline" | "unauthorized" | "mode" | "gone" | "server",
    readonly status?: number,
  ) {
    super(message);
  }
}

export interface RunListItem {
  run_id: string;
  url: string;
  status: string | null;
  created: string | null;
  total: number | null;
  done_entries: number;
  artifacts: { mode: string; present: boolean; expires: string | null } | null;
}

export interface RunListResp {
  items: RunListItem[];
  mode: string;
}

export interface RunCreated {
  run_id: string;
  total: number;
  all_skipped: boolean;
  status: string;
}

export interface DeviceCode {
  device_code: string;
  user_code: string;
  verification_url: string;
  expires_in: number;
  interval: number;
}

export class V2mdApi {
  constructor(
    private baseUrl: string,
    private idToken: string,
  ) {}

  setToken(t: string): void {
    this.idToken = t;
  }

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (this.idToken) h["Authorization"] = `Bearer ${this.idToken}`;
    return h;
  }

  private async req<T>(path: string, init?: RequestInit): Promise<T> {
    let resp: Response;
    try {
      resp = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        headers: { ...this.headers(), ...(init?.headers as Record<string, string>) },
      });
    } catch (e) {
      throw new V2mdError(
        `连不上 ${this.baseUrl}——SSH 隧道起了吗？（ssh -N -L 8790:127.0.0.1:8790 lcode-4c，见 README）`,
        "offline",
      );
    }
    if (resp.status === 401) throw new V2mdError("未登录或 Google 凭据已过期，去设置页重新登录", "unauthorized", 401);
    if (resp.status === 409) throw new V2mdError("服务器产物模式不是 deliver（bundle/ACK 不可用）", "mode", 409);
    if (resp.status === 410) throw new V2mdError("产物已焚（ACK 或 TTL 到期）", "gone", 410);
    if (!resp.ok) {
      const body = (await resp.text()).slice(0, 200);
      throw new V2mdError(`HTTP ${resp.status}: ${body}`, "server", resp.status);
    }
    return (await resp.json()) as T;
  }

  /** GET /runs —— 收件箱发现（status 空=全部；limit 默认服务端 50） */
  listRuns(status = "", limit = 100): Promise<RunListResp> {
    const q = new URLSearchParams();
    if (status) q.set("status", status);
    q.set("limit", String(limit));
    return this.req<RunListResp>(`/runs?${q}`);
  }

  getRun(runId: string): Promise<Record<string, unknown>> {
    return this.req(`/runs/${runId}`);
  }

  postRun(url: string): Promise<RunCreated> {
    return this.req<RunCreated>("/runs", { method: "POST", body: JSON.stringify({ url }) });
  }

  /** bundle zip 原始字节（不进 JSON 通道） */
  async getBundle(runId: string): Promise<Uint8Array> {
    let resp: Response;
    try {
      resp = await fetch(`${this.baseUrl}/runs/${runId}/bundle`, { headers: this.headers() });
    } catch {
      throw new V2mdError(`连不上 ${this.baseUrl}——SSH 隧道起了吗？`, "offline");
    }
    if (resp.status === 401) throw new V2mdError("未登录或凭据过期", "unauthorized", 401);
    if (resp.status === 409) throw new V2mdError("服务器不在 deliver 模式", "mode", 409);
    if (resp.status === 410) throw new V2mdError("产物已焚", "gone", 410);
    if (!resp.ok) throw new V2mdError(`bundle 拉取失败 HTTP ${resp.status}`, "server", resp.status);
    return new Uint8Array(await resp.arrayBuffer());
  }

  ack(runId: string): Promise<{ run_id: string; status: string }> {
    return this.req(`/runs/${runId}/artifacts`, { method: "DELETE" });
  }

  // ---- 设备流登录桥（无需凭据） ----

  deviceCode(): Promise<DeviceCode> {
    return this.req<DeviceCode>("/auth/device/code", { method: "POST" });
  }

  /** 轮询换 id_token：用户还没授权时抛 unauthorized（调用方按 interval 继续轮） */
  async deviceToken(deviceCode: string): Promise<string> {
    const d = await this.req<{ id_token: string }>("/auth/device/token", {
      method: "POST",
      body: JSON.stringify({ device_code: deviceCode }),
    });
    return d.id_token;
  }

  healthz(): Promise<{ ok: boolean }> {
    return this.req("/healthz");
  }
}
