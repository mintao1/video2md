// 插件入口：生命周期接线（设置加载 → API/收件箱/状态栏 → 命令注册 → 轮询循环）。
import { Notice, Plugin, PluginSettingTab, Setting, TextComponent } from "obsidian";
import { unzipSync } from "fflate";

import { V2mdApi, V2mdError } from "./api.ts";
import { deviceLogin } from "./auth.ts";
import { Inbox } from "./inbox.ts";
import type { SyncResult } from "./inbox.ts";
import { fs as nodeFsLike, joinPath } from "./node.ts";
import { DEFAULT_SETTINGS, type V2mdSettings } from "./settings.ts";
import { extractUrl, submitUrl } from "./submit.ts";
import type { ZipEntry } from "./unpacker.ts";

export default class Video2mdPlugin extends Plugin {
  declare settings: V2mdSettings;
  api!: V2mdApi;
  private inbox!: Inbox;
  private timer: number | null = null;
  private syncing = false;
  private sbEl: HTMLElement | null = null;

  async onload(): Promise<void> {
    await this.loadSettings();
    this.api = new V2mdApi(this.settings.baseUrl, this.settings.idToken);
    this.inbox = this.buildInbox();

    this.setStatus("Video2MD · 未登录");
    this.addSettingTab(new V2mdSettingTab(this.app, this));
    this.addCommand({
      id: "submit-video-link",
      name: "提交视频链接（转笔记）",
      callback: () => this.promptSubmit(),
    });
    this.addCommand({
      id: "sync-now",
      name: "立即同步收件箱",
      callback: () => void this.runSync(true),
    });

    // 节拍：setTimeout 自续链（上一轮没完不叠跑；改间隔即时生效），外加 registerInterval
    // 兜底一个分钟级心跳防休眠后链条断掉
    this.registerInterval(window.setInterval(() => {
      if (!this.syncing && this.settings.idToken) void this.runSync(false);
    }, 60_000));
    if (this.settings.idToken) void this.runSync(false);
  }

  onunload(): void {
    if (this.timer !== null) window.clearTimeout(this.timer);
  }

  async loadSettings(): Promise<void> {
    this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
  }

  /** 设置改动后的统一收口：重建 API/Inbox（baseUrl/token/folder/autoAck 都在构造参数里） */
  async saveSettings(): Promise<void> {
    await this.saveData(this.settings);
    this.api = new V2mdApi(this.settings.baseUrl, this.settings.idToken);
    this.inbox = this.buildInbox();
  }

  private buildInbox(): Inbox {
    return new Inbox({
      api: this.api,
      fs: nodeFsLike,
      joinPath,
      vaultRoot: adapterBase(this.app),
      targetFolder: this.settings.targetFolder,
      autoAck: this.settings.autoAck,
      unzip: async (buf) => unzipEntries(buf),
      log: (m) => console.log("[video2md]", m),
    });
  }

  /** 一轮同步 + 自续下一拍。manual=true 时结果用 Notice 明示。 */
  async runSync(manual: boolean): Promise<void> {
    if (!this.settings.idToken) {
      this.setStatus(`Video2MD · 未登录${manual ? "（去设置页登录）" : ""}`);
      return;
    }
    if (this.syncing) return;
    this.syncing = true;
    try {
      const res = await this.inbox.syncOnce();
      this.render(res, manual);
    } catch (e) {
      this.setStatus(`Video2MD · ${errText(e)}`);
      if (manual) new Notice(`Video2MD 同步失败：${errText(e)}`);
    } finally {
      this.syncing = false;
      if (this.timer !== null) window.clearTimeout(this.timer);
      this.timer = window.setTimeout(
        () => void this.runSync(false),
        Math.max(this.settings.pollIntervalS, 15) * 1000,
      );
    }
  }

  private render(res: SyncResult, manual: boolean): void {
    const parts: string[] = [];
    if (res.received > 0) parts.push(`入库 ${res.received} 个 run`);
    if (res.errors.length > 0) parts.push(`异常 ${res.errors.length}`);
    this.setStatus(parts.length ? `Video2MD · ${parts.join(" · ")}` : "Video2MD · 已同步");
    for (const e of res.errors.slice(0, 3)) console.warn("[video2md]", e);
    if (manual) {
      new Notice(
        res.received > 0
          ? `Video2MD：新入库 ${res.received} 个 run${res.errors.length ? `，${res.errors.length} 个异常` : ""}`
          : "Video2MD：没有新产物",
      );
    }
  }

  private setStatus(text: string): void {
    if (!this.sbEl) this.sbEl = this.addStatusBarItem();
    this.sbEl.setText(text);
  }

  private promptSubmit(): void {
    const host = this.app.workspace.containerEl.createDiv({ cls: "v2md-submit" });
    host.addClass("modal-ish");
    host.style.cssText =
      "position:fixed;inset:auto 16px 64px 16px;z-index:999;background:var(--background-primary);" +
      "border:1px solid var(--background-modifier-border);border-radius:8px;padding:12px;box-shadow:var(--shadow-l)";
    host.createDiv({ text: "提交视频链接（B站/抖音/YouTube/小红书，支持粘贴整段分享口令文本）",
      attr: { style: "margin-bottom:8px;font-weight:600" } });
    const tc = new TextComponent(host);
    tc.setPlaceholder("https://v.douyin.com/… 或整段抖音口令");
    tc.inputEl.style.width = "100%";
    const btn = host.createEl("button", { text: "发起处理" });
    const close = () => host.remove();
    btn.addEventListener("click", async () => {
      const raw = tc.getValue();
      if (!extractUrl(raw)) {
        new Notice("没找到有效链接");
        return;
      }
      btn.disabled = true;
      btn.setText("提交中…");
      try {
        const r = await submitUrl(this.api, raw);
        new Notice(r.allSkipped
          ? "该链接已全部处理过（幂等跳过），不重复烧"
          : `已发起 run ${r.runId}（${r.total} 条），完成后自动入库`);
        close();
        void this.runSync(false);
      } catch (e) {
        new Notice(`提交失败：${errText(e)}`);
        btn.disabled = false;
        btn.setText("发起处理");
      }
    });
    this.registerDomEvent(document, "keydown", (ev) => {
      if (ev.key === "Escape") close();
    });
    tc.inputEl.focus();
  }

  /** 设备流登录：由设置页「登录」按钮触发 */
  async loginWithDeviceFlow(): Promise<boolean> {
    try {
      const r = await deviceLogin(this.api, {
        onCode: (code, url) => {
          new Notice(`打开 ${url} 输入验证码 ${code} 完成 Google 授权（此处自动等待）`, 12000);
          window.open(url);
        },
      });
      this.settings.idToken = r.idToken;
      await this.saveSettings();
      new Notice("已登录 video2md ✅");
      void this.runSync(false);
      return true;
    } catch (e) {
      new Notice(`登录失败：${errText(e)}`);
      return false;
    }
  }

  logout(): void {
    this.settings.idToken = "";
    void this.saveSettings().then(() => {
      this.setStatus("Video2MD · 未登录");
      new Notice("已退出登录");
    });
  }
}

/** Obsidian DesktopAdapter 的绝对根路径（内部字段，isDesktopOnly 前提下稳定存在） */
function adapterBase(app: Video2mdPlugin["app"]): string {
  const a = app.vault.adapter as unknown as { basePath?: string; base?: string };
  return a.basePath ?? a.base ?? "";
}

function unzipEntries(buf: Uint8Array): ZipEntry[] {
  const files = unzipSync(buf);
  return Object.entries(files).map(([name, data]) => ({ name, data }));
}

function errText(e: unknown): string {
  if (e instanceof V2mdError) return e.message;
  return e instanceof Error ? e.message : String(e);
}

// ---------------------------------------------------------------- 设置页

class V2mdSettingTab extends PluginSettingTab {
  plugin: Video2mdPlugin;

  constructor(app: Video2mdPlugin["app"], plugin: Video2mdPlugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display(): void {
    const { containerEl } = this;
    containerEl.empty();
    const s = this.plugin.settings;

    new Setting(containerEl)
      .setName("API 地址")
      .setDesc("经 SSH 隧道指向服务器回环（launchd 常驻模板见 README「Obsidian 插件」节）")
      .addText((t) => t.setValue(s.baseUrl).onChange(async (v) => {
        s.baseUrl = v.trim() || DEFAULT_SETTINGS.baseUrl;
        await this.plugin.saveSettings();
      }));

    new Setting(containerEl)
      .setName("Google 登录")
      .setDesc(s.idToken ? "已登录（id_token 存本机 data.json）" : "未登录——设备流：点登录 → 浏览器输码授权")
      .addButton((b) => b.setButtonText(s.idToken ? "重新登录" : "登录").onClick(async () => {
        if (await this.plugin.loginWithDeviceFlow()) this.display();
      }))
      .addButton((b) => b.setButtonText("退出").setCta().onClick(() => {
        this.plugin.logout();
        this.display();
      }));

    new Setting(containerEl)
      .setName("轮询间隔（秒）")
      .setDesc("收件箱发现新产物的节拍（下限 15s）")
      .addText((t) => t.setValue(String(s.pollIntervalS)).onChange(async (v) => {
        const n = Number(v);
        if (Number.isFinite(n) && n >= 15) {
          s.pollIntervalS = Math.round(n);
          await this.plugin.saveSettings();
        }
      }));

    new Setting(containerEl)
      .setName("目标文件夹")
      .setDesc("bundle 解包进库的位置；保持默认与服务端目录同构（wikilink 才连得上）")
      .addText((t) => t.setValue(s.targetFolder).onChange(async (v) => {
        s.targetFolder = v.trim() || DEFAULT_SETTINGS.targetFolder;
        await this.plugin.saveSettings();
      }));

    new Setting(containerEl)
      .setName("拉取后自动 ACK")
      .setDesc("ACK = 通知云端即焚产物（本地已入库，服务器不留存；关掉的留待 24h TTL 自焚）")
      .addToggle((tg) => tg.setValue(s.autoAck).onChange(async (v) => {
        s.autoAck = v;
        await this.plugin.saveSettings();
      }));

    new Setting(containerEl)
      .setName("连接测试")
      .setDesc("探 /healthz + GET /runs（需先登录）")
      .addButton((b) => b.setButtonText("测试").onClick(async () => {
        try {
          await this.plugin.api.healthz();
          const l = await this.plugin.api.listRuns("", 5);
          new Notice(`连通 ✅ mode=${l.mode}，名下可见 ${l.items.length} run`);
        } catch (e) {
          new Notice(`不通 ❌ ${errText(e)}`);
        }
      }));
  }
}
