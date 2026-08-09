# 知识库系统 ROADMAP

> conversation-recall 从"单域对话检索"演进为"三域分布式知识生命周期系统"的总览。
> 开发自检表单在 `dev_log/roundN_done.md`（gitignored），跨会话续接读最新的。

## 架构（三域分布式 + 治理）

```
对话域 conversation.db(D盘)  ┐
周报域 weekly.db(E盘)         ├─ recall.py 多词并行统一查询（rank 融合 R3）
成果域 projects/<slug>.db(E盘)┘     ↑ E盘离线自动跳过，对话域常驻

meta.db(E盘)  ← rank/反馈/勘误/审计日志（R3）
gray.db(E盘)  ← 失效归档，active 全空才兜底检索（R4）
```

## 四轮开发计划

| 轮 | 目标 | 主要产物 | 状态 |
|----|------|---------|------|
| **R1 地基** | 三域分布式存储 + 统一查询（无治理） | kb_core.py、config.json、recall.py 三域化、SKILL.md、document 闸门 | ✅ 完成 |
| **R2 入库** | 策展式入库 + 周报联动 | knowledge-curator skill（注册项目+AI筛选自己产/参考文献+doc2md清洗）、office-weekly-status 加 sync-weekly 挂钩、kb_core ingest/upload+folder共存、探照灯特用库 | ✅ 完成 |
| **R3 排名+反馈** | rank 融合 + 现实支点反馈 | meta.db、rank 计算、recall.py feedback（consulted日志+表单生成+路由）、勘误浮现、SKILL.md 钉反馈任务、searchlight-curator 闭环 | ✅ 完成 |
| **R4 治理** | 审计+灰库+size 警报 | gray.db、decay/evict（仅成果域）、knowledge-auditor skill、查询时 size 警报、gray 兜底、revive、build_match 词元修复 | ✅ 完成 |

> 🎉 四轮全部完成（2026-08-08）。系统已闭环：查询(conversation-recall) ↔ 入库(knowledge-curator) ↔ 治理(knowledge-auditor)，周报/探照灯各自联动。后续增强见 dev_log/round4_done.md。

## 关键设计原则

1. **分级渐进 + 提示词物理隔离**：T1-only 在 SKILL.md，深层命令脚本现场打印。
2. **分布式存储**：每域每项目独立 db；查询 fan-out。
3. **文档域分层**：高信号 section 进 FTS（加速召回），素材/附件存表不索引（按需取）。
4. **现实支点反馈（R3）**：rank 来自"真实任务结果"，非 AI 当下自评；勘误如 GitHub issue 在检索时浮现。
5. **治理仅成果域（R4）**：对话/周报是永久档案，参与排名但不淘汰；成果域知识有寿命。
6. **DB 是派生产物**：源 md 才是本体，DB 全部 gitignore，可任意 sync 重建。

## 状态判定

每轮完成判据 = 功能跑通 + check-skill 0 错 + update-readme/check-all 通过 + dev_log 自检表单写好。
