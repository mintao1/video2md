# 10 · video2md 命名收口

项目规范名称是 **video2md**。源码包、命令入口、环境变量、容器、数据库对象、前端存储键和监控指标全部使用这一命名。

本次收口包含一次数据库对象迁移：生产库中旧前缀的表、索引、序列、约束、RLS policy、tenant helper 函数和 schema ledger 会在迁移命令持有 advisory lock 时原子改为 `v2md_*`。迁移不改数据内容；API 必须在迁移完成后再启动。LangGraph 自管的 `checkpoint*` 对象不属于项目命名空间，不改名。

迁移路径：

1. 备份 PostgreSQL，并在独立数据库执行 `python -m video2md migrate`。
2. 核对 `v2md_schema_version`、`v2md_runs`、RLS policy 和约束名称。
3. 再切换 API 与 worker；旧前缀对象不再由运行时代码读取。
4. 保留数据库备份作为回滚点；回滚必须恢复与旧镜像匹配的代码和对象名，不能只回滚镜像。

收口后的检查要求：

- `python -m video2md --help` 是唯一 CLI 入口。
- 配置只读取 `V2MD_*` 和 `~/.config/video2md.env`。
- 前端只使用 `v2md_*` localStorage 键。
- 新代码、部署清单、测试和文档不再包含旧项目包、旧环境前缀或旧数据库前缀；历史生产证据可保留原始事实。
