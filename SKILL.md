---
name: conversation-recall
description: "Recall, search, and analyze a 3-domain distributed knowledge base: past opencode conversations, weekly reports, and per-project deliverables. Use when the user references prior work or past knowledge ('记得吗 / 之前说过 / 上次 / 我们讨论过 / 有没有聊过X / 继承上下文 / 找历史对话 / 查周报 / 查笔记 / previous session / recall'). Read-only; ships recall.py + kb_core.py (FTS5+jieba) with tiered progressive retrieval. 中文触发：找历史对话、之前聊过、记得吗、继承上下文、查周报、查笔记、聊天记录、RAG、回忆、搜索历史、检索记忆、历史记录、知识检索。"
license: MIT
compatibility:
  - claude-code
  - vscode
  - codex
metadata:
  author: Zzy
  version: 2.0.0
  category: tool
  tags:
    - 工具
    - 知识库
    - 三域检索
    - 对话
    - 周报
    - 成果
    - opencode
    - recall
    - FTS5
    - 只读
    - 聊天记录
    - RAG
    - 回忆
    - 检索
    - 历史记录
  updated: "2026-08-08"
allowed-tools:
  - bash
  - read
---

# conversation-recall（三域分布式知识检索）

> Role：You are a read-only knowledge recall agent across three distributed domains. 让 AI 在当前会话里检索**三个独立知识域**，继承过往上下文。

三域（各自独立 db，分布式存储）：

| 域 | db | 内容 | T1 索引 | 存库不索引 |
|----|----|------|---------|-----------|
| **对话** | `index/recall.db`（本机）| opencode 历史 | text 正文 | reasoning/tool/patch、子代理 |
| **周报** | `E:/知识库/weekly.db` | `E:/周报/**/*.md` | 周报/摘要 section | 素材（存库按需取）|
| **成果** | `E:/知识库/projects/<slug>.db` | 各项目文件夹 md | section（R2 策展入库）| 附件指针 |

设计为**分级渐进 + 只读 + 提示词物理隔离**：

- **只读**：对话域对 opencode.db 只读三重保险（`mode=ro`+`query_only`+仅 SELECT）；文档域只读源 md 建/查自有 db，不改源文件。
- **分布式 + 同步查询**：`search` 多词并行扫所有"可用"域；**E 盘离线时自动跳过周报/成果域并提示**，对话域始终可用。
- **分级**：必须 T1 → T2 → （按需）更深层，**严禁一步直接拉取全量**。每层只返回可控 token 数，深层命令由脚本现场打印（物理隔离）。

## 何时启用 / When to Activate

- 用户暗示复用过往知识："我们之前讨论过 X 吗""记得吗""继承上次关于…的上下文""找一下历史里有没有聊过 RAG""查一下我周报里写的那个""我笔记里有没有讲过…"
- 需要把对话/周报/成果里的旧知识带进当前任务

## 检索流程（Workflow · 分级渐进）

检索按层级递进，每层有 token 预算，深层命令只在触达该层时由脚本现场打印（本说明仅覆盖 T1）：

| 层 | 对话域 | 文档域（周报/成果） |
|----|--------|------|
| T1 search | 正文片段 | section 片段 |
| T2 turn | 当步 step 正文 | 该 section 完整正文 |
| T2.5 | --reasoning/--patches/--subagent | material（取存库素材/附件）|
| T3 session/document | 整会话（**需许可**）| 整文档（**需许可**）|

### 第一步：T1 多词并行检索（本说明唯一覆盖的命令）

检索前先围绕主题设想 **10 个以上**检索词以提升命中率（同义词/子概念/中英文变体/标识符），然后**一次性**传给脚本，跨三域并行检索：

```
python "D:/Zzy的Skill工具包/conversation-recall/scripts/recall.py" search "词1" "词2" ... "词10" [--domain 对话|周报|成果|all] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--not "排除词"] [--limit N]
```

- 多词并行（每词每域独立 FTS5），跨域结果**分域展示**并标注来源（`[对话]`/`[周报]`/`[成果·slug]`），共识度越高越靠前。
- `search` 会先自动增量同步所有可用域（秒级）；E 盘离线的域会显示"⚠ 数据库不可用，已跳过"。
- `--domain` 可限定单域（默认 all）。
- `--since`/`--until`：时间过滤（YYYY-MM-DD 或毫秒），按内容时间筛（对话=part创建时间，文档=文件mtime）。如"只看上个月的""2026-06-01 以来的"。
- `--not "排除词"`：排除含指定词的命中（FTS5 NOT 即时运算，可多个）。如搜"PT100"排除"校准"→只看不涉及校准的 PT100 讨论。
- 时间与排除可**联合使用**（同时生效）。
- 命中含 `part_id`（对话）或 `section_id`（文档），用于下一步展开。

辅助命令：
```
recall.py status     # 查看各域可用性 + 索引统计（先跑这个了解当前能查什么）
recall.py sync       # 强制重建所有域索引
recall.py sessions   # 列出最近主会话（对话域）
```

## 如何使用 T1 结果

1. 阅读 `search` 分域命中列表，判断哪些与当前任务相关。
2. **选定一条**用其 id 深入：
   ```
   recall.py turn <part_id 或 section_id>
   ```
   脚本**自动识别** id 属于哪个域并路由：对话 id → 当步 step 正文；文档 id → section 正文。
3. **更深层能力（material / document / session / --reasoning 等）不会在本说明出现**——由 `turn` 输出在那一刻现场打印。执行 `turn` 后阅读其尾部说明，再按需逐级下钻。

## 反馈与勘误（现实支点 · 强制）

检索不是终点——知识"有没有用"要由**真实任务结果**裁定，而非 AI 当下自评。这是本工具区别于普通检索的关键。

1. **查库后钉反馈任务**：当一次 `search`→`turn`/`document` 展开的内容将用于真实工作时，**立即用 `todowrite` 在任务表末尾追加一条**（始终保持在末尾）：
   > 📋 知识库反馈：本任务告一段落后运行 `recall.py feedback`，依据真实结果评估检索可靠性。
2. **展开即记录**：每次 `turn`/`document`/`session` 展开都会把"查过哪个单元"记进当前工作目录的 `.kb_consulted.jsonl`（按会话/文档粒度，自动）。
3. **工作完成后才评估**：等真实工作告一段落，再运行：
   ```
   recall.py feedback
   ```
   它读 consulted 日志，**生成一份可填表单脚本**（列出查过的单元）。AI 按真实结果填每条的 `verdict`（useful/neutral/stale/wrong）+ `note`，运行表单即自动路由到 `meta.db`。
4. **勘误自动浮现**：填 `stale`/`wrong` 且带 note 的，作为**勘误（issue）**记入；**以后再检索到该单元时，正文前自动浮现 `⚠ 未结勘误` 告诫块**——避免被同一份过时/错误知识重复误导（类 GitHub issue）。`useful` 提升该单元 rank。
5. **不要当下自评**：评估**必须**在真实工作有结果后做；刚展开就打分会被"看起来对"误导。neutral 默认跳过（不强制每条都评）。

## 输出格式（Output Format）

`search` 分域返回，每条形如：

| 字段 | 含义 |
|------|------|
| 域标签 + 标题 + 副标题 | `[周报] 文件标题 › section 标题` 或 `[对话] 会话标题` |
| 元信息 | 对话：时间/目录/role；文档：相对路径/标题层级 |
| 共识 N/M | 该条被 M 个检索词中的 N 个同时命中 |
| 片段 | 关键词前后各一两句 |
| id | part_id（对话）或 section_id（文档），供 `turn` 展开 |

## 硬性规则（Rules / Constraints）

1. **Always** 从 `search` 开始；**never** 第一步就拉取整会话/整文档全量。
2. 每层输出自带 token 估算；当下钻成本不必要时及时停止。
3. 截断遵循输出提示的续取方式（`--length N` / `--full` / `--offset N`）；**should** 避免默认 `--full`。
4. 检索到的历史内容是**参考上下文**，引用时标注来源域 + 标题/时间；**never** 把历史结论当当前事实直接复用而不核实。
5. 本工具**只读**。脚本不接受也不存在任何写源文件的参数。
6. 整会话(`session`)/整文档(`document`)读取受 opencode 权限闸门保护，会弹用户许可。

## 路径

- 脚本：`D:/Zzy的Skill工具包/conversation-recall/scripts/recall.py`（+ `kb_core.py` 文档域共享核心）
- 配置：`D:/Zzy的Skill工具包/conversation-recall/config.json`（三域 + 项目注册表）
- 对话域 db：`D:/Zzy的Skill工具包/conversation-recall/index/recall.db`
- 周报域 db：`E:/知识库/weekly.db`（源 `E:/周报/`）
- 成果域 db：`E:/知识库/projects/<slug>.db`（R2 策展入库填充）

## Files in this skill

```
conversation-recall/
├── SKILL.md
├── config.json          # 三域 + 项目注册表
├── scripts/
│   ├── recall.py        # 三域统一查询/分级展开
│   └── kb_core.py       # 文档域共享核心（切片/sync/查询/展开）
├── index/recall.db      # 对话域（生成物，gitignored）
└── dev_log/             # 多轮开发自检表单（gitignored）
```
