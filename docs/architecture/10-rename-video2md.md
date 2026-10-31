# 10 · 项目命名与兼容策略（video2md）

当前项目的规范名称是 **video2md**。源码包、命令入口、容器镜像、环境变量和前端存储键均以 video2md 为主；旧的 video2pdf 标识只作为平滑升级兼容层保留。

## 当前规范

| 项目 | 规范名称 | 兼容保留 |
| --- | --- | --- |
| Python 包 / 入口 | `video2md`、`python -m video2md` | `video2pdf` import 与 CLI 垫片 |
| 环境变量 | `V2MD_*` | `V2PDF_*` 仅在对应 `V2MD_*` 未设置时补齐 |
| 凭证文件 | `~/.config/video2md.env` | `~/.config/video2pdf.env` 旧文件兜底 |
| 前端 localStorage | `v2md_*` | 读取一次并迁移旧 `v2pdf_*` 键 |
| 容器与部署 | `video2md` | 旧镜像/脚本可通过兼容入口回滚 |
| PostgreSQL 现有表 | `v2pdf_*` | 生产数据表暂不改名，避免破坏已有数据与回滚能力 |

`video2pdf` 兼容层不得成为新代码的 import、命令、用户可见文案或部署默认值。新功能只使用规范名称。

## 数据与迁移边界

生产库已经使用 `v2pdf_*` 表名。此次命名恢复不执行在线表重命名；应用层继续通过内部表名访问现有数据。若未来确需把表名改为 `v2md_*`，必须单独设计可回滚的双读/双写迁移，并完成备份、演练和生产验收后再切换。

历史测试证据、trace 前缀和 gpu_plugins 外部资源名称可保留原值，因为它们承担跨版本关联或外部协议身份；新生成的数据和文档使用 video2md。

## 收尾检查

- `rg -n 'video2pdf|V2PDF|v2pdf'` 的命中只能出现在兼容垫片、兼容测试、生产表名边界或历史证据中。
- `python -m video2md --help` 与 `python -m video2pdf --help` 都能启动，后者输出弃用兼容提示。
- 生产部署使用 `V2MD_*` 和 `python -m video2md`，旧变量只用于回滚。
