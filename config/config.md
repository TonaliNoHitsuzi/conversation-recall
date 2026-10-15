# config/config.json —— 多域分布式知识体系配置

> 本件（conversation-recall）是**独立 git 仓**（主库 gitignore），本三件套只入本仓，不入主库。

## 覆盖范围

- `config.example.json`：配置模板（键清单 + 默认值 + 内联 `_comment` 文档），入库。
- `config.json`：本机真值（多域注册表：对话/周报/成果域、灰库、meta、web 端口、阈值），本仓 `.gitignore` 的 `config.json` 模式已兜底忽略。
- 相对 `db_path` 的解析锚点是**技能根目录**（conversation-recall/，由 `scripts/recall.py` 的 `_resolve` 以 `HUB_DIR` 拼接），与 config 文件所在子目录无关。

## 配置来源与取值途径

| 键 | 来源 | 取值途径 |
|---|---|---|
| `domains.conversation` | opencode 会话库 | `opencode_db_path` 默认 `~/.local/share/opencode/opencode.db`（跨平台展开），装在别处时覆盖 |
| `domains.weekly` | 周报域 | `source_root` 指向周报根目录（本机为系统级 paths 的 `weekly_root`）；启用置 `enabled: true` |
| `projects[]` | 成果域（可多个） | 知识策展入库时登记（`kb_core.py register-project` 或 web 终端 `/api/library`），slug/source_root/db_path 三要素 |
| `gray` / `meta` / `thresholds` | 治理 | 审计淘汰归档 / 反馈勘误收藏 / db 容量警报阈值，一般保持默认 |

## 清空配置后在新环境的重建步骤

1. `cp config/config.example.json config/config.json`
2. 改 `domains.weekly.source_root` 指向本机周报目录（或保持 `enabled: false` 跳过周报域）
3. 按需向 `projects[]` 复制样例块，填本机项目 `slug`/`source_root`，`enabled: true`
4. 若 opencode 会话库不在默认位置，改 `domains.conversation.opencode_db_path`
5. 跑 `python scripts/recall.py status` 逐域确认「可用: 是」；`recall.py sync` 重建索引

## 迁移注记

- 2026-10-14：配置由技能根目录 `config.json` 迁入 `config/config.json`（skill-system v2 三件套约定）；`scripts/recall.py` 的 `CONFIG_PATH` 与 `scripts/kb_core.py` 的 `DEFAULT_CONFIG` 同步改为新路径。旧根目录备份 `config.json.bak` 未随迁，可删。
