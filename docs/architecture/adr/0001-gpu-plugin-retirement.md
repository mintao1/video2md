# ADR-0001：gpu_router 插件关系退役——回归纯 API 客户端

日期：2026-09-25 ｜ 状态：已决策（gpu_router 侧同步登记 ADR-0008）｜ 决策人：项目所有者

## 背景

与 gpu_router 的集成曾是双通道：① 本服务作为 HTTP 客户端调 `:9700` API
（`V2PDF_REMOTE`/`V2PDF_OCR_REMOTE`/`V2PDF_CLIPS_REMOTE`——唯一运维关系）；②
`gpu_plugins/v2md` 以插件协议被 gpu_router 装载，声明 asr/clips/ocr 能力并携带
provider 实现与 reference 节点服务。通道② 造成两仓源码级耦合：能力归属分裂、
契约实现者（reference 节点）住在消费方仓。

## 决策

**通道② 退役（方向性）**：video2pdf 回归 gpu_router 的纯 API 客户端。

- 节点实现（reference 三形态）已迁 gpu_router 仓收编为 gpu-node-agent：
  `gpu_router/node_agent.py` + `deploy/node_agent_colab.py` + `deploy/modal_node.py`
  （commit a483d9c，含 nvenc 上报与冷启动预热增强）；本仓 gpu_plugins/ 停止演进
- 迁移当刻丢弃的在途未提交改动（/ping GPU 快照上报 v1）即该工作流的迁移源，
  内容已在 gpu_router 侧落地——本地丢弃无损
- gpu_router 的 plugins.py 装载过渡期维持（`gpu_router.docker.json` 仍配
  `plugins: ["v2pdf"]`，存量部署零破坏）；能力声明内置化与 plugin_paths 移除由
  gpu_router 侧后续 PR 完成（其 ADR-0008）

## 后果

- 07 契约篇口径改为「gpu_router 为本项目提供 API」；插件装载降为过渡期现实
- gpu_plugins/ 目录冻结：不新增功能，测试（test_gpu_plugin_*）维持 HEAD 语义回归
- 上公网清单不受影响（P0-4/P0-2 仍为余项）；P2-2 成本闸的用量记账口径直接对接
  gpu_router 侧，无插件耦合
