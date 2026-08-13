# conversation-recall

> **三域分布式知识检索 · 分级渐进 · 只读 · 自主优化**
> Tiered progressive retrieval over a distributed knowledge base — read-only, with self-optimizing data governance.

让 AI 在当前会话里检索过往**对话 / 周报 / 项目成果**，并把"知识到底有没有用"交给**真实任务结果**裁定，而非 AI 当下自评。

---

## 为什么需要它 / Why

AI 会话是易失的——每次新会话从零开始，过往讨论、踩过的坑、得出的结论都消失。本工具把三类历史知识建成**可检索的分布式知识库**，让 AI 能"回忆"：

| 域 | 内容 | 入索引（高信号）| 存库不索引（按需取）|
|----|------|----------------|---------------------|
| **对话** | opencode 历史会话 | text 正文 | reasoning / tool / patch / 子代理 |
| **周报** | 周报、摘要、产出文档、计划 | section 正文 | 素材（原始草稿）|
| **成果** | 各项目文件夹的 md | section（策展入库）| 附件指针 |

四个不可妥协的设计：

- 🔒 **只读**——对话域对源库只读三重保险（`mode=ro` + `query_only` + 仅 SELECT）；文档域只读源 md 建自有 db，不改源文件。
- 🌐 **分布式 + 同步查询**——每域每项目独立 db；多词并行 fan-out 扫所有可用域，**外置盘离线时自动跳过并提示**，对话域常驻可用。
- 📊 **分级渐进**——必须 T1 → T2 →（按需）更深层，**严禁一步拉全量**；每层有 token 预算，深层命令由脚本现场打印（提示词物理隔离）。
- ⚖️ **现实支点反馈**——rank 来自"真实任务结果"，不是 AI 当下自评；勘误如 GitHub issue 在检索时自动浮现。

---

## ✨ 核心特性一：分级渐进检索

普通检索一股脑倒出全文，token 瞬间爆炸。本工具**强制分层下钻**，每层只返回可控片段，深层命令在触达该层时才由脚本现场揭示：

```
T1  search      正文/section 片段（多词并行，跨三域）   ← 唯一起点
 │
 T2  turn       当步 step 正文 / section 完整正文
 │
 T2.5           --reasoning / --patches / --subagent    material（取存库素材/附件）
 │
 T3  session / document    整会话 / 整文档（⚠ 受权限闸门保护，弹用户许可）
```

**为什么物理隔离**：把深层命令写进 SKILL.md，AI 会忍不住一步拉全量；只让 `turn` 输出在那一刻现场打印下一步，AI 拿不到指令就无法越级。

### T1 检索语法

```bash
python scripts/recall.py search "词1" "词2" ... "词10" \
    [--domain 对话|周报|成果|all] \
    [--since YYYY-MM-DD] [--until YYYY-MM-DD] \
    [--not "排除词"] [--limit N]
```

- **多词并行**：每词每域独立 FTS5，跨域结果分域展示并标来源（`[对话]`/`[周报]`/`[成果·slug]`），共识度越高越靠前。
- **`--since` / `--until`**：时间过滤（对话=part 创建时间，文档=文件 mtime）。如"只看上个月的"。
- **`--not`**：FTS5 布尔排除（即时运算，可多个）。如搜"PT100"排除"校准"。
- **浏览模式**：空关键词 + 时间/域过滤 → 按时间倒序直接浏览，不走 FTS。
- 检索前自动增量同步所有可用域（秒级）。

### 辅助命令

```bash
python scripts/recall.py status      # 各域可用性 + 索引统计（先跑这个）
python scripts/recall.py sync        # 强制重建所有域索引
python scripts/recall.py sessions    # 列出最近主会话（对话域）
python scripts/recall.py turn <id>   # 深入展开（自动识别域并路由）
```

---

## ✨ 核心特性二：自主优化的数据治理

检索不是终点——**知识"有没有用"要由真实任务结果裁定**。这是本工具区别于普通全文检索的关键。它构成一个**自我纠偏的闭环**：

```
        检索 → 用于真实工作 → 工作出结果 → 反馈裁决
         ↑                                    │
         │    ┌─────────── 裁决路由 ───────────┤
         │    │                                 │
         │  useful → rank↑          stale/wrong → 记勘误(issue)
         │                                    │
         │    ┌─── 下次检索该单元时 ───┐       │
         └──  ⚠ 未结勘误 自动浮现告诫块  ←──────┘
              （避免被同一份过时知识重复误导）

         持续 stale → rank 衰减 → 跌出阈值 → knowledge-auditor 抽样 → 淘汰进 gray.db
                                                                          │
                                          active 全空时 gray 兜底检索 ←──────┘
```

### 四个机制

| 机制 | 做什么 | 为什么 |
|------|--------|--------|
| **钉反馈任务** | `search`→`turn` 展开的内容将用于真实工作时，强制 todowrite 追加一条反馈提醒 | 防止"查完就忘评" |
| **展开即记录** | 每次 `turn`/`document`/`session` 把"查过哪个单元"写进 `.kb_consulted.jsonl` | 反馈时知道评哪些 |
| **延迟评估** | 工作告一段落后才跑 `recall.py feedback`，生成可填表单，AI 按真实结果填 verdict | 杜绝"刚展开就自评"的认知偏差 |
| **勘误浮现** | `stale`/`wrong` 带 note 的记为勘误；**以后再检索到该单元，正文前自动出 `⚠ 未结勘误` 块** | 类 GitHub issue，防止重复踩坑 |

### 裁决选项

| verdict | 含义 | 对 rank 的影响 |
|---------|------|---------------|
| `useful` | 真的帮到了 | 提升 |
| `neutral` | 没明显帮助也没错 | 跳过（默认不强制评）|
| `stale` | 过时了 | 下降 + 记勘误 |
| `wrong` | 错的/误导 | 重降 + 记勘误 |

### 灰库生命周期（仅成果域）

对话和周报是**永久档案**，参与排名但**永不淘汰**。只有**成果域**知识有寿命——持续 stale 会衰减、跌出阈值、被 `knowledge-auditor` 抽样淘汰进 `gray.db`；active 区全空时 gray 才兜底检索。这让知识库**自我新陈代谢**，不会被僵尸内容拖垮。

---

## 🖥 本地网页人工入口终端

命令行之外，附带一个**纯本地**的网页终端（`web/`），给不写命令的人用：

- **搜索栏 + 时间下拉 + 排除框**：覆盖 CLI 全部检索能力
- **结果展开 / 分级下钻**：点开看片段，再点拉全文
- **反馈打分 + 提勘误**：浏览器里直接裁决，路由到 meta.db
- **灰库视图**：看哪些被淘汰了，一键 revive
- **离线渲染**：Markdown / 代码高亮 / Mermaid 图 / LaTeX 公式全 vendored 本地（无需 CDN）

```bash
cd web && python server.py        # 或双击 web/run.bat
# 访问 http://127.0.0.1:8719
```

> 服务端用 Python 标准库 `http.server`，**零第三方依赖**；前端 vendored 了 marked / highlight.js / mermaid / MathJax（约 3.9 MB，离线优先）。

---

## 架构

```
 对话域 recall.db(本目录)       ┐
 周报域 weekly.db(外置盘)        ├─ recall.py 多词并行统一查询（rank 融合）
 成果域 projects/<slug>.db(外置盘)┘     ↑ 外置盘离线自动跳过，对话域常驻

 meta.db  ← rank / 反馈 / 勘误 / 审计日志
 gray.db  ← 失效归档，active 全空才兜底检索（仅成果域）
```

- **DB 是派生产物**：源 md 才是本体，所有 db 全部 gitignore，可任意 `sync` 重建。
- **文档域分层**：高信号 section 进 FTS（加速召回），素材/附件存表不索引（按需取），避免噪声稀释排名。

---

## Quick Start

### 1. 环境

- Python 3.8+（用了 f-string / pathlib；FTS5 随 SQLite 自带）
- [jieba](https://github.com/fxsjy/jieba)（中文分词）：`pip install jieba`
- opencode（对话域数据源；可选，没有则只用文档域）

### 2. 配置

```bash
git clone <repo-url> conversation-recall
cd conversation-recall
cp config.example.json config.json    # Windows: copy config.example.json config.json
```

编辑 `config.json`：
- **开箱即用**：conversation 域默认开启，只需 opencode 历史即可工作。
- **启用周报/成果域**：把对应块的 `enabled` 改 `true`，`source_root` 指向你的目录，`db_path` 放任意位置（相对路径以本目录为根）。

#### v3 新增可配字段

| 字段 | 默认 | 说明 |
|------|------|------|
| `server.port` | 8719 | web 终端端口（CLI `--port` 优先级最高） |
| `defaults.projects_default_dir` | `data/projects/` | 通过 web 终端新建项目库时的默认 db 目录 |
| `domains.conversation.opencode_db_path` | `~/.local/share/opencode/opencode.db` | opencode session db 路径，自机安装位置不同时覆盖 |

#### 自机部署 checklist（给其他想用的人）

1. **opencode 路径**：默认 `~/.local/share/opencode/opencode.db`。Windows 上 `~` 会展开为 `C:/Users/<用户名>`。若你的 opencode 装别处（如 Scoop/Chocolatey 改了数据目录），在 `conversation.opencode_db_path` 里指明。
2. **db 目录**：所有 `db_path` 都可用相对路径（以 `config.json` 所在目录为根，eg. `"data/weekly.db"`）。不想用 `E:/知识库/` 这种绝对路径的话，全部改成 `data/...`，gitignored 不会泄露。
3. **数学公式字体**：`web/index.html` 第 ~528 行 MathJax 配置硬编码了 jsdelivr CDN 的字体路径 `https://cdn.jsdelivr.net/npm/mathjax@3.2.2/es5/output/chtml/fonts/woff-v2`。完全离线部署时需把这个 URL 换成本地路径或自托管 CDN。
4. **目录浏览器**：web 终端的「📁 浏览」按钮调 PowerShell `FolderBrowserDialog`，仅 Windows 可用。非 Windows 用户需直接手输绝对路径到「源目录」字段。
5. **端口冲突**：8719 被占时改 `server.port` 或加 CLI `--port`。
6. **代理**：从中国境内 push 到 GitHub 需配 git 代理：`git config --global http.proxy http://127.0.0.1:7897`（按你的代理端口改）。

### 3. 首次检索

```bash
python scripts/recall.py status                    # 看各域可用性
python scripts/recall.py sync                      # 建索引
python scripts/recall.py search "RAG" "检索" "知识库"   # 多词并行
python scripts/recall.py turn <命中返回的 id>       # 深入展开
```

### 4.（可选）启动网页终端

```bash
cd web && python server.py
```

---

## 配置参考

见 [`config.example.json`](config.example.json)，每块带 `_comment`。关键字段：

| 字段 | 说明 |
|------|------|
| `domains.conversation` | 对话域；`db_path` 相对路径，默认 `index/recall.db` |
| `domains.weekly` | 周报域；`source_root`=源目录，`index_globs`=入索引的 glob，`store_only_globs`=存库不索引 |
| `projects[]` | 成果域项目注册表；每项目独立 db + source_root |
| `gray` / `meta` | 灰库 / rank反馈库 |
| `thresholds.size_warn_mb` | 成果域 db 超此阈值时检索附 ⚠ 警告，提示审计 |

---

## 项目结构

```
conversation-recall/
├── README.md
├── LICENSE                          # MIT
├── SKILL.md                         # Agent Skill 提示词（T1-only，物理隔离）
├── ROADMAP.md                       # 四轮开发演进总览
├── config.example.json              # 配置模板（复制为 config.json 后改）
├── .gitignore
├── scripts/
│   ├── recall.py                    # 三域统一查询 / 分级展开 / 反馈
│   └── kb_core.py                   # 文档域共享核心（切片/sync/查询/反馈/灰库）
├── web/
│   ├── server.py                    # 本地网页终端后端（stdlib，零依赖）
│   ├── index.html                   # 前端（搜索/展开/打分/勘误/灰库）
│   ├── run.bat                      # Windows 启动器
│   └── assets/                      # vendored 离线前端库
│       ├── marked.min.js            # Markdown 渲染
│       ├── highlight.min.js         # 代码高亮
│       ├── mermaid.min.js           # 图表
│       ├── mathjax-...js            # LaTeX 公式
│       └── github-markdown.css      # GitHub 风格样式
└── index/                           # 对话域 db（gitignored，自动生成）
```

---

## 🔮 Roadmap / 待办

### 已完成（四轮闭环）

- ✅ **R1 地基**：三域分布式存储 + 统一查询 + document 闸门
- ✅ **R2 入库**：策展式入库 + 周报联动 + 文档域分层
- ✅ **R3 排名+反馈**：rank 融合 + 现实支点反馈 + 勘误浮现
- ✅ **R4 治理**：审计 + 灰库 + size 警报 + 淘汰/复活
- ✅ **检索增强**：时间过滤 / NOT 排除 / 浏览模式
- ✅ **网页终端**：本地人工入口 + 反馈打分 + 灰库视图

### 待办（数据驱动触发，当前不急）

| ID | 待办 | 触发条件 |
|----|------|---------|
| 🔮 T1 | **rank 时间衰减 + Bayesian 平滑** | 反馈样本累积到一定量后（当前频率计数已够用）|
| 🔮 T2 | **hybrid 检索**（FTS 精确 + 语义模糊融合）| 出现"搜 X 没找到但库里讲了同义 Y"≥ 3 次 |
| 🔮 T3 | **跨域联动可视化**（对话→周报→成果的引用链）| 用户有跨域追溯需求 |
| 🔮 T4 | **多用户/多机同步**（db 合并协议）| 团队协作场景出现 |
| 🔮 T5 | **导入外部知识源**（RSS / 书签 / PDF 批量）| 配套 doc2md 已就绪，等需求 |
| 🔮 T6 | **标签定向检索**（按 frontmatter 标签过滤）| 数据量够后（当前手动翻即可）|
| 🔮 T7 | **非文本资产指针 + 渲染**（图片代理 / PDF 内嵌预览）| 数据多了或需求出现后 |

> 设计哲学：**不过早优化**。0-100 数值模型听起来比离散"高级"，但输入精度不存在（AI 评分不可靠）；语义匹配听起来比关键词"智能"，但代码场景精确更重要。没数据时谈优化是空谈——每项待办都附**量化触发条件**。

---

## 设计原则

1. **分级渐进 + 提示词物理隔离**——T1-only 写进 SKILL.md，深层命令脚本现场打印，AI 无法越级。
2. **分布式存储**——每域每项目独立 db；查询 fan-out，单盘故障不全局宕。
3. **文档域分层**——高信号 section 进 FTS，素材/附件存表不索引，避免噪声稀释排名。
4. **现实支点反馈**——rank 来自真实任务结果，非 AI 当下自评；勘误如 issue 在检索时浮现。
5. **治理仅成果域**——对话/周报是永久档案，参与排名但不淘汰；成果域知识有寿命。
6. **DB 是派生产物**——源 md 才是本体，DB 全部 gitignore，可任意 sync 重建。

---

## License

[MIT](LICENSE) © Zzy

## Acknowledgements

前端 vendored 库（均在 `web/assets/`，离线优先）：
- [marked](https://github.com/markedjs/marked) · Markdown 渲染
- [highlight.js](https://github.com/highlightjs/highlight.js) · 代码高亮
- [mermaid](https://github.com/mermaid-js/mermaid) · 图表
- [MathJax](https://github.com/mathjax/MathJax) · LaTeX 公式
- [github-markdown-css](https://github.com/sindresorhus/github-markdown-css) · 样式

中文分词由 [jieba](https://github.com/fxsjy/jieba) 提供。
