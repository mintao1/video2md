// 插件设置：data.json 持久化（Obsidian 托管），身份 token 也在里面（本机 vault，不外出）。
export interface V2mdSettings {
  /** video2md API 基址——经 SSH 隧道指本机回环（launchd 常驻，见 README「Obsidian 插件」） */
  baseUrl: string;
  /** Google 设备流换回的 id_token；空 = 未登录 */
  idToken: string;
  /** 收件箱轮询间隔（秒） */
  pollIntervalS: number;
  /** bundle 解包进库的目标文件夹（与服务端 config.OUT_DIR 同构保 wikilink） */
  targetFolder: string;
  /** 拉取成功后自动 ACK（DELETE artifacts，云端即焚） */
  autoAck: boolean;
}

export const DEFAULT_SETTINGS: V2mdSettings = {
  baseUrl: "http://127.0.0.1:8790",
  idToken: "",
  pollIntervalS: 120,
  targetFolder: "资料库/视频笔记",
  autoAck: true,
};
