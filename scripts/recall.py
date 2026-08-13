#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""conversation-recall —— 多域分布式知识检索（分级渐进）。

多域（分布式存储，各自独立 db）：
  - 对话域 conversation.db（本机 D盘，源：opencode.db，只读三重保险）
  - 周报域 E:/知识库/weekly.db（源：E:/周报/**/*.md，索引周报/摘要，素材存库不索引）
  - 成果域 E:/知识库/projects/<slug>.db（每项目一库，R2 策展入库填充）
查询时多词并行扫所有“可用”域；E盘离线自动跳过周报/成果域并提示。

分级渐进 + 提示词分部注入（物理隔离）：T1 search 仅本说明覆盖；
turn/session/document 的深层用法由脚本现场打印。输出强制 UTF-8 中文。
"""
import sys
import os
import json
import re
import sqlite3
import argparse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import jieba
import kb_core

HUB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(HUB_DIR, "config.json")
INDEX_DIR = os.path.join(HUB_DIR, "index")
INDEX_DB = os.path.join(INDEX_DIR, "recall.db")
OPENCODE_DB = os.path.join(os.path.expanduser("~"), ".local", "share", "opencode", "opencode.db")

BUDGET_T1 = 1200
BUDGET_T2 = 2000
BUDGET_T25 = 1500
BUDGET_T3 = 4000
CHARS_PER_TOKEN = 0.75

CONFIG = {}


def est_tokens(text):
    return max(1, int(len(text) * CHARS_PER_TOKEN))


def to_chars(tokens):
    return int(tokens / CHARS_PER_TOKEN)


def seg(text):
    return " ".join(jieba.cut(text))


_WORD_RE = re.compile(r'[A-Za-z0-9\u4e00-\u9fff]')


def build_match(query, negatives=None):
    toks = [t for t in jieba.cut(query) if t.strip() and _WORD_RE.search(t)]
    if not toks:
        return None
    expr = " AND ".join('"' + t.replace('"', '""') + '"' for t in toks)
    if negatives:
        for neg in negatives:
            neg_q = '"' + neg.replace('"', '""') + '"'
            expr = "(" + expr + " NOT " + neg_q + ")"
    return expr


def fmt_time(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def short_dir(d):
    if not d:
        return "?"
    d = d.replace("\\", "/").rstrip("/")
    parts = d.split("/")
    return "/".join(parts[-2:]) if len(parts) >= 2 else d


def short_path(p):
    if not p:
        return "?"
    p = p.replace("\\", "/")
    parts = p.split("/")
    return "/".join(parts[-3:]) if len(parts) >= 3 else p


def make_snippet(text, terms, window=80):
    if not text:
        return ""
    pos = len(text)
    for t in terms:
        p = text.find(t)
        if 0 <= p < pos:
            pos = p
    start = max(0, pos - window // 2)
    end = min(len(text), start + window)
    s = text[start:end]
    if start > 0:
        s = "…" + s
    if end < len(text):
        s = s + "…"
    return s.replace("\n", " ")


def emit_blocks(blocks, budget, offset, full, header_lines):
    full_text = "\n\n".join(
        ("[" + label + "]\n" + txt) for label, txt in blocks if txt and txt.strip()
    )
    total = est_tokens(full_text)
    for h in header_lines:
        print(h)
    off_tok = offset
    start = to_chars(offset)
    if full:
        sliced = full_text[start:]
    elif offset == 0 and budget >= total:
        sliced = full_text
    else:
        sliced = full_text[start:start + to_chars(budget)]
    print(sliced)
    shown_tok = est_tokens(sliced)
    consumed = off_tok + shown_tok
    remaining = total - consumed
    if remaining > 0:
        print()
        print("✂ 截断：此段约 {0} tok，已返回 {1} tok（跳过 {2}），剩余 {3} tok。".format(total, shown_tok, off_tok, remaining))
        print("  续取：--length N 指定读取量 / --full 完整读取 / --offset N 续翻")
    return remaining


def _resolve(p):
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(HUB_DIR, p)


def load_config():
    global CONFIG
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as e:
            print("警告：config.json 解析失败，用默认配置: " + str(e), file=sys.stderr)
    domains = cfg.get("domains", {})
    conv = domains.get("conversation", {})
    conv_cfg = {
        "kind": "conversation",
        "key": "conversation",
        "label": "对话",
        "db_path": _resolve(conv.get("db_path") or "index/recall.db"),
        "enabled": conv.get("enabled", True),
    }
    # v3: opencode session db 路径可由 config 覆盖；否则用默认 ~/.local/share/opencode/opencode.db
    global OPENCODE_DB
    if conv.get("opencode_db_path"):
        OPENCODE_DB = _resolve(conv.get("opencode_db_path"))
    doc_domains = []
    wk = domains.get("weekly", {})
    if wk:
        doc_domains.append({
            "kind": "doc", "key": "weekly", "label": wk.get("label", "周报"),
            "db_path": wk.get("db_path"), "source_root": wk.get("source_root"),
            "source_glob": wk.get("source_glob", "**/*.md"),
            "index_globs": wk.get("index_globs"), "store_only_globs": wk.get("store_only_globs"),
            "domain": "weekly", "enabled": wk.get("enabled", True),
        })
    for p in cfg.get("projects", []):
        if not p.get("enabled", True):
            continue
        slug = p.get("slug", "?")
        doc_domains.append({
            "kind": "doc", "key": "project:" + slug,
            "label": p.get("label", slug),  # v3: 不再强制加 "成果·" 前缀（前端自决显示）
            "db_path": p.get("db_path"), "source_root": p.get("source_root"),
            "source_glob": p.get("source_glob", "**/*.md"),
            "index_globs": p.get("index_globs"), "store_only_globs": p.get("store_only_globs"),
            "domain": "project:" + slug, "enabled": True,
            # v3 元信息
            "description": p.get("description", ""), "tags": p.get("tags", []),
            "icon": p.get("icon", "📁"),
        })
    gray = cfg.get("gray", {})
    meta = cfg.get("meta", {})
    CONFIG = {
        "conversation": conv_cfg,
        "doc_domains": doc_domains,
        "projects": cfg.get("projects", []),  # v3: 保留原 projects 供 /api/libraries 用
        "gray": gray,
        "meta_db": meta.get("db_path"),
        "thresholds": cfg.get("thresholds", {}),
    }


def connect_ro():
    if not os.path.exists(OPENCODE_DB):
        sys.exit("错误：找不到 opencode.db：" + OPENCODE_DB)
    import pathlib
    uri = pathlib.Path(OPENCODE_DB).resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only=ON")
    return con


def connect_idx():
    os.makedirs(INDEX_DIR, exist_ok=True)
    con = sqlite3.connect(INDEX_DB)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def ensure_schema(idx):
    idx.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    idx.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS text_idx USING fts5(
        part_id UNINDEXED, message_id UNINDEXED, session_id UNINDEXED,
        session_title UNINDEXED, directory UNINDEXED, role UNINDEXED,
        time_created UNINDEXED, text_orig UNINDEXED, text_seg)""")
    idx.commit()


def meta_get(idx, k, default=None):
    row = idx.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


def meta_set(idx, k, v):
    idx.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    idx.commit()


def sync(idx, ro, force=False):
    cur_max = ro.execute("SELECT MAX(time_created) FROM part").fetchone()[0] or 0
    stored_max = meta_get(idx, "watermark_time")
    if (not force) and stored_max is not None and int(stored_max) >= int(cur_max):
        return False
    rows = ro.execute(
        """SELECT p.id, p.message_id, p.session_id, p.time_created, p.data,
                  m.data, s.title, s.directory
           FROM part p JOIN session s ON s.id=p.session_id
           LEFT JOIN message m ON m.id=p.message_id
           WHERE s.parent_id IS NULL AND p.data LIKE '%"type":"text"%'
           ORDER BY p.time_created""").fetchall()
    idx.execute("DELETE FROM text_idx")
    batch = []
    for pid, mid, sid, tc, pdata, mdata, title, directory in rows:
        try:
            pj = json.loads(pdata)
        except Exception:
            continue
        if pj.get("type") != "text":
            continue
        text = pj.get("text", "")
        if not text:
            continue
        role = "?"
        if mdata:
            try:
                role = json.loads(mdata).get("role", "?")
            except Exception:
                pass
        batch.append((pid, mid, sid, title or "", directory or "", role, tc, text, seg(text)))
    idx.executemany(
        "INSERT INTO text_idx (part_id,message_id,session_id,session_title,directory,role,time_created,text_orig,text_seg) VALUES (?,?,?,?,?,?,?,?,?)",
        batch)
    meta_set(idx, "watermark_time", cur_max)
    meta_set(idx, "indexed_count", len(batch))
    idx.commit()
    return True


def load_timeline(ro, session_id):
    rows = ro.execute(
        "SELECT id, message_id, time_created, data FROM part WHERE session_id=? ORDER BY time_created, id",
        (session_id,)).fetchall()
    timeline = []
    for pid, mid, tc, data in rows:
        try:
            j = json.loads(data)
        except Exception:
            j = {}
        timeline.append({"id": pid, "mid": mid, "tc": tc, "j": j})
    return timeline


def load_roles(ro, session_id):
    roles = {}
    for mid, data in ro.execute("SELECT id, data FROM message WHERE session_id=?", (session_id,)).fetchall():
        try:
            roles[mid] = json.loads(data).get("role", "?")
        except Exception:
            roles[mid] = "?"
    return roles


def find_step_bounds(timeline, target_idx, target_role, roles):
    n = len(timeline)
    if target_role == "user":
        start = target_idx
        end = n - 1
        for j in range(start + 1, n):
            if timeline[j]["j"].get("type") == "text" and roles.get(timeline[j]["mid"]) == "user":
                end = j - 1
                break
        return start, end
    start = None
    for j in range(target_idx, -1, -1):
        t = timeline[j]["j"].get("type")
        if t == "step-start":
            start = j
            break
        if t == "step-finish":
            break
    if start is None:
        start = target_idx
    end = n - 1
    for j in range(start + 1, n):
        t = timeline[j]["j"].get("type")
        if t in ("step-finish", "step-start"):
            end = j - 1 if t == "step-start" else j
            break
    return start, end


def collect_step_text(timeline, roles, start, end):
    blocks = []
    for k in range(start, end + 1):
        j = timeline[k]["j"]
        if j.get("type") == "text":
            role = roles.get(timeline[k]["mid"], "?")
            blocks.append(("用户" if role == "user" else "助手", j.get("text", "")))
    return blocks


def collect_step_task_tools(timeline, start, end):
    tasks = []
    for k in range(start, end + 1):
        j = timeline[k]["j"]
        if j.get("type") == "tool" and j.get("tool") == "task":
            state = j.get("state", {}) or {}
            inp = state.get("input", {}) or {}
            out = state.get("output", "") or ""
            meta = state.get("metadata", {}) or {}
            tasks.append({"part_id": timeline[k]["id"], "subagent": inp.get("subagent_type", "?"),
                          "desc": inp.get("description", ""), "child": meta.get("sessionId") or parse_child_session(out)})
    return tasks


def parse_child_session(output_text):
    if not output_text:
        return None
    s = str(output_text)
    m = re.search(r'<task id="(ses_\w+)"', s) or re.search(r"task_id:\s*(ses_\w+)", s)
    return m.group(1) if m else None


def fetch_session_text_blocks(ro, session_id):
    roles = load_roles(ro, session_id)
    timeline = load_timeline(ro, session_id)
    blocks = []
    for x in timeline:
        j = x["j"]
        if j.get("type") == "text":
            role = roles.get(x["mid"], "?")
            blocks.append(("用户" if role == "user" else "助手", j.get("text", "")))
    return blocks


def _want_domain(domfilter, kind, key, label):
    if not domfilter or domfilter.lower() in ("all", "全部", "*", "0"):
        return True
    d = domfilter.lower().strip()
    if d in ("对话", "conversation", "conv") and kind == "conversation":
        return True
    if d in ("周报", "weekly", "周") and key == "weekly":
        return True
    if d in ("成果", "project", "deliverable", "projects") and key.startswith("project:"):
        return True
    return d in (label or "").lower() or d in (key or "").lower()


def _conv_search(idx, queries, role, sid, limit, pool=50, since=None, until=None, negatives=None):
    planned = [(q, build_match(q, negatives)) for q in queries]
    where_extra = ""
    params_extra = []
    if role:
        r = "user" if role.lower().startswith("u") else "assistant"
        where_extra += " AND role = ?"
        params_extra.append(r)
    if sid:
        where_extra += " AND session_id = ?"
        params_extra.append(sid)
    if since is not None:
        where_extra += " AND time_created >= ?"
        params_extra.append(since)
    if until is not None:
        where_extra += " AND time_created <= ?"
        params_extra.append(until)

    def run_one(q, match):
        con = sqlite3.connect(INDEX_DB)
        try:
            con.execute("PRAGMA query_only=ON")
            try:
                rows = con.execute(
                    "SELECT part_id, session_id, session_title, directory, role, time_created, text_orig, "
                    "bm25(text_idx) score FROM text_idx WHERE text_seg MATCH ? " + where_extra +
                    " ORDER BY score LIMIT ?", [match] + params_extra + [pool]).fetchall()
            except sqlite3.OperationalError:
                rows = []
        finally:
            con.close()
        return (q, rows)

    tasks = [(q, m) for q, m in planned if m]
    results = []
    if tasks:
        with ThreadPoolExecutor(min(max(1, len(tasks)), 8)) as ex:
            results = list(ex.map(lambda t: run_one(t[0], t[1]), tasks))
    for q, m in planned:
        if m is None:
            results.append((q, []))
    agg = {}
    counts = {q: 0 for q, _ in results}
    for q, rows in results:
        for (pid, s, title, directory, rl, tc, text, score) in rows:
            counts[q] += 1
            if pid not in agg:
                agg[pid] = {"ref": pid, "sid": s, "title": title, "directory": directory,
                            "role": rl, "tc": tc, "text": text, "best": score, "matched": [q]}
            else:
                a = agg[pid]
                if q not in a["matched"]:
                    a["matched"].append(q)
                if score < a["best"]:
                    a["best"] = score
    ranked = sorted(agg.values(), key=lambda a: (-len(a["matched"]), a["best"]))[:limit]
    return ranked, counts


def _all_terms(queries):
    terms = []
    for q in queries:
        terms.extend([t for t in jieba.cut(q) if t.strip()])
    return terms


def _quality_badge(domain, unit_id):
    """命中单元的质量徽章：有用★/勘误⚠（来自 meta.db 的 rank 与 open errata）。无则空。"""
    meta_db = CONFIG.get("meta_db")
    if not meta_db:
        return ""
    parts = []
    try:
        rk = kb_core.unit_rank(meta_db, domain, unit_id)
    except Exception:
        rk = 0.0
    if rk >= 1.0:
        parts.append("★有用" + str(int(rk)))
    elif rk < 0:
        parts.append("↓负评")
    try:
        if kb_core.open_errata(meta_db, domain, unit_id):
            parts.append("⚠勘误")
    except Exception:
        pass
    return ("  " + " ".join(parts)) if parts else ""


def _consulted_append(domain, unit_id, unit_label, ref_id=""):
    """展开某单元后，把"查过它"记进当前工作目录的 consulted 日志，供 feedback 表单读取。"""
    try:
        path = os.path.join(os.getcwd(), ".kb_consulted.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": int(datetime.now().timestamp() * 1000),
                "domain": domain, "unit_id": unit_id,
                "unit_label": unit_label, "ref_id": ref_id,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _show_errata(domain, unit_id):
    """展开前查 meta.db 该单元的未结勘误（GitHub-issue 同构），有则在正文前告诫。"""
    meta_db = CONFIG.get("meta_db")
    if not meta_db:
        return
    try:
        errs = kb_core.open_errata(meta_db, domain, unit_id)
    except Exception:
        errs = []
    if not errs:
        return
    print("⚠ 本单元有 {0} 条未结勘误（检索时自动浮现，避免重复误导）：".format(len(errs)))
    for note, opened in errs[:3]:
        try:
            when = datetime.fromtimestamp(int(opened) / 1000).strftime("%Y-%m-%d")
        except Exception:
            when = "?"
        print("  • {0}  （{1}）".format(note.replace("\n", " ")[:120], when))
    print()


def _print_section_header(label, n_idx, n_store, note=""):
    extra = ""
    if n_store:
        extra = "（另存素材/附件 {0} 篇未索引）".format(n_store)
    print("\n──── [{0}] {1} 条命中{2} {3} ────".format(label, n_idx, extra, note))


def _parse_time(s, end=False):
    """把 'YYYY-MM-DD' 或毫秒数字 解析为毫秒时间戳。end=True 时取当日 23:59:59。None/空→None。"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        if end:
            d = d.replace(hour=23, minute=59, second=59)
        return int(d.timestamp() * 1000)
    except ValueError:
        return None


def _conv_browse(idx, since, until, limit):
    """无关键词浏览对话域：每个 session_id 只取一条最新 part，按 time_created 倒序取 limit 个 session。

    dedup 下沉到 SQL（window function），避免「先 LIMIT 再 dedup」时被高频 session 占满配额。
    """
    where = ["1=1"]
    params = []
    if since is not None:
        where.append("time_created >= ?")
        params.append(since)
    if until is not None:
        where.append("time_created <= ?")
        params.append(until)
    where_sql = " AND ".join(where)
    sql = (
        "WITH ranked AS ("
        " SELECT part_id,session_id,session_title,directory,role,time_created,text_orig,"
        "        ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY time_created DESC, part_id) AS rn"
        " FROM text_idx WHERE " + where_sql +
        ") SELECT part_id,session_id,session_title,directory,role,time_created,text_orig"
        " FROM ranked WHERE rn=1 ORDER BY time_created DESC LIMIT ?"
    )
    params.append(limit)
    rows = idx.execute(sql, params).fetchall()
    return [{"ref": pid, "sid": sid, "title": title, "directory": directory,
             "role": role, "tc": tc, "text": text, "matched": ["(浏览)"], "best": 0}
            for pid, sid, title, directory, role, tc, text in rows]


def cmd_search(args, idx, ro):
    queries = [q for q in args.queries if q.strip()]
    since = _parse_time(getattr(args, "since", None))
    until = _parse_time(getattr(args, "until", None), end=True)
    negatives = []
    for n in getattr(args, "not", []) or []:
        negatives.extend([t for t in jieba.cut(n) if t.strip() and _WORD_RE.search(t)])
    sync(idx, ro)
    for d in CONFIG["doc_domains"]:
        if d.get("enabled") and d.get("db_path") and d.get("source_root"):
            try:
                kb_core.sync_doc_domain(d)
            except Exception:
                pass
    domfilter = args.domain
    budget = BUDGET_T1
    used = 0
    tnote = ""
    if since is not None or until is not None:
        s_ = datetime.fromtimestamp(since / 1000).strftime("%Y-%m-%d") if since is not None else "起"
        u_ = datetime.fromtimestamp(until / 1000).strftime("%Y-%m-%d") if until is not None else "今"
        tnote = " · 时间 {0}~{1}".format(s_, u_)

    # 浏览模式：无关键词 → 按时间倒序直接 SELECT
    if not queries:
        print("═══ 浏览模式（无关键词{0}）═══".format(tnote))
        if _want_domain(domfilter, "conversation", "conversation", "对话") and CONFIG["conversation"]["enabled"]:
            hits = _conv_browse(idx, since, until, args.limit)
            print("\n──── [对话] {0} 条（按时间倒序）────".format(len(hits)))
            for a in hits:
                badge = _quality_badge("conversation", a["sid"])
                block = ("【对话】{0}{1}  ({2} · role={3})\n   {4}\n"
                         "   → part_id: {5}").format(
                    a["title"] or "(无标题)", badge, fmt_time(a["tc"]), a["role"],
                    (a["text"][:80] + "…").replace("\n", " "), a["ref"])
                print(block); print()
        for d in CONFIG["doc_domains"]:
            if not d.get("enabled") or not _want_domain(domfilter, "doc", d["key"], d["label"]):
                continue
            if not d.get("db_path") or not kb_core.available(d["db_path"]):
                continue
            hits = kb_core.browse_doc_db(d["db_path"], since, until, args.limit)
            print("\n──── [{0}] {1} 条（按时间倒序）────".format(d["label"], len(hits)))
            for h in hits:
                b = _quality_badge(d["domain"], h["doc_id"])
                block = ("【{0}】{1}{2} › {3}\n   {4}\n"
                         "   → section_id: {5}").format(
                    d["label"], h["file_title"] or "(无标题)", b, h["heading"][:40],
                    (h["text"][:80] + "…").replace("\n", " "), h["section_id"])
                print(block); print()
        print("选定某条深入：recall.py turn <part_id 或 section_id>")
        return

    terms = _all_terms(queries)
    total_hits = 0
    print("═══ 多域并行检索（T1·{0} 个词{1}）═══".format(len(queries), tnote))

    if _want_domain(domfilter, "conversation", "conversation", "对话") and CONFIG["conversation"]["enabled"]:
        hits, counts = _conv_search(idx, queries, args.role, args.sid, args.limit, since=since, until=until, negatives=negatives)
        total_hits += len(hits)
        _print_section_header("对话", len(hits), 0)
        if not hits:
            print("  未命中。")
        else:
            for a in hits:
                badge = _quality_badge("conversation", a["sid"])
                block = ("【对话】{0}{1}  ({2} · {3} · role={4})\n"
                         "   共识 {5}/{6}：{7}\n   {8}\n"
                         "   → part_id: {9} | session: {10}").format(
                    a["title"] or "(无标题)", badge, fmt_time(a["tc"]), short_dir(a["directory"]),
                    a["role"], len(a["matched"]), len(queries),
                    ", ".join(a["matched"][:4]), make_snippet(a["text"], terms), a["ref"], a["sid"])
                btok = est_tokens(block)
                if used + btok > budget and used > 0:
                    print("   …（达预算，更多命中需 --limit 或精简词）")
                    break
                print(block)
                print()
                used += btok

    for d in CONFIG["doc_domains"]:
        if not d.get("enabled"):
            continue
        if not _want_domain(domfilter, "doc", d["key"], d["label"]):
            continue
        if not d.get("db_path") or not kb_core.available(d["db_path"]):
            print("\n──── [{0}] ⚠ 数据库不可用（E盘未连接？），已跳过 ────".format(d["label"]))
            print("  路径：" + str(d.get("db_path")))
            continue
        hits, counts = kb_core.query_doc_db(d["db_path"], queries, args.limit, since=since, until=until, negatives=negatives)
        total_hits += len(hits)
        st = kb_core.domain_stats(d["db_path"])
        _print_section_header(d["label"], len(hits), st.get("stored", 0))
        if not hits:
            print("  未命中（该域索引 section 数 {0}）。".format(st.get("indexed", 0)))
        else:
            shown = 0
            for h in hits:
                if shown >= args.limit:
                    print("   …（达 --limit，调大或精简词）")
                    break
                badge = _quality_badge(d["domain"], h["doc_id"])
                block = ("【{0}】{1}{2} › {3}  ({4} · L{5})\n"
                         "   共识 {6}/{7}：{8}\n   {9}\n"
                         "   → section_id: {10} | recall.py turn {10}").format(
                    d["label"], h["file_title"] or "(无标题)", badge, h["heading"][:50], short_path(h["file_path"]),
                    h["level"], len(h["matched"]), len(queries),
                    ", ".join(h["matched"][:4]), make_snippet(h["text"], terms), h["section_id"])
                btok = est_tokens(block)
                if used + btok > budget and used > 0:
                    print("   …（达预算，更多命中需 --limit 或精简词）")
                    break
                print(block)
                print()
                used += btok
                shown += 1

    if total_hits == 0:
        gray = CONFIG.get("gray") or {}
        gdb = gray.get("db_path")
        if gray.get("enabled") and gdb and kb_core.available(gdb):
            ghits, gcounts = kb_core.query_doc_db(gdb, queries, args.limit, since=since, until=until, negatives=negatives)
            print("\n──── [灰库·兜底] {0} 条命中（active 域全空，自动回退失效归档库）────".format(len(ghits)))
            for h in ghits:
                block = ("【灰库·{0}】{1} › {2}\n"
                         "   共识 {3}/{4}：{5}\n   {6}\n"
                         "   → section_id: {7}（灰库文档，原文档已归档）").format(
                    h["domain"], h["file_title"] or "(无标题)", h["heading"][:50],
                    len(h["matched"]), len(queries), ", ".join(h["matched"][:4]),
                    make_snippet(h["text"], terms), h["section_id"])
                btok = est_tokens(block)
                if used + btok > budget and used > 0:
                    break
                print(block)
                print()
                used += btok

    print("\n已用 {0}/{1} tok".format(used, budget))
    print("═══ 下一步（深层命令仅在你执行 turn 后才会显示）═══")
    print("  选定某条深入：recall.py turn <part_id 或 section_id>")


def cmd_turn(args, idx, ro):
    pid = args.part_id
    row = ro.execute("SELECT session_id FROM part WHERE id=?", (pid,)).fetchone()
    if row:
        _turn_conversation(args, ro, idx, pid, row[0])
        return
    for d in CONFIG["doc_domains"]:
        if not d.get("enabled") or not d.get("db_path") or not kb_core.available(d["db_path"]):
            continue
        r = kb_core.expand_section(d["db_path"], pid)
        if r:
            _turn_doc_section(args, d, r)
            return
    sys.exit("错误：在所有可用域都找不到 id=" + pid)


def _turn_conversation(args, ro, idx, part_id, session_id):
    trow = ro.execute("SELECT title FROM session WHERE id=?", (session_id,)).fetchone()
    session_title = trow[0] if trow else session_id
    _show_errata("conversation", session_id)
    _consulted_append("conversation", session_id, session_title or session_id, part_id)
    timeline = load_timeline(ro, session_id)
    roles = load_roles(ro, session_id)
    target_idx = next((i for i, x in enumerate(timeline) if x["id"] == part_id), None)
    if target_idx is None:
        sys.exit("错误：part 不在所属会话时间线内。")
    target_role = roles.get(timeline[target_idx]["mid"], "?")
    start, end = find_step_bounds(timeline, target_idx, target_role, roles)
    is_t25 = args.reasoning or args.patches or args.subagent is not None
    budget = BUDGET_T25 if is_t25 else BUDGET_T2
    if args.length is not None:
        budget = args.length
    if not is_t25:
        blocks = collect_step_text(timeline, roles, start, end)
        tasks = collect_step_task_tools(timeline, start, end)
        emit_blocks(blocks, budget, args.offset, args.full,
                    ["═══ [对话] 当步正文（step·一个子问题）· 会话 {0} ═══".format(session_id)])
        if tasks:
            print()
            print("─── 本步委派的子代理（内容未取，T2.5 可取）───")
            for n, t in enumerate(tasks, 1):
                child = t["child"] or "(未解析到子会话)"
                print("  [{0}] @{1}: {2}  → 子会话 {3}".format(n, t["subagent"], t["desc"], child))
                print("      取正文：recall.py turn {0} --subagent {1}".format(part_id, n))
        print()
        print("═══ 如需深入（按需取用，勿一次全取）═══")
        print("  recall.py turn {0} --reasoning     追加本步思维链   (≤{1} tok)".format(part_id, BUDGET_T25))
        print("  recall.py turn {0} --patches       追加本步文件改动 (≤{1} tok)".format(part_id, BUDGET_T25))
        print("  recall.py turn {0} --subagent [N]  取本步委派子代理正文 (≤{1} tok)".format(part_id, BUDGET_T25))
        print("  recall.py session {0}              整会话/整轮正文  (≤{1} tok，最后手段·需用户许可)".format(session_id, BUDGET_T3))
    else:
        if args.reasoning:
            blocks = [("思维链", timeline[k]["j"].get("text", ""))
                      for k in range(start, end + 1) if timeline[k]["j"].get("type") == "reasoning"]
            emit_blocks(blocks, budget, args.offset, args.full, ["═══ [对话] 本步思维链（T2.5）═══"])
        if args.patches:
            blocks = []
            for k in range(start, end + 1):
                j = timeline[k]["j"]
                if j.get("type") == "patch":
                    h = j.get("hash", "")
                    blocks.append(("改动·commit " + str(h)[:12], "\n".join(j.get("files", [])) + "\n（diff 需在仓库目录 git show " + str(h) + "）"))
            emit_blocks(blocks, budget, args.offset, args.full, ["═══ [对话] 本步文件改动（T2.5）═══"])
        if args.subagent is not None:
            tasks = collect_step_task_tools(timeline, start, end)
            if not tasks:
                print("本步无子代理委派。")
                return
            n = args.subagent if args.subagent >= 1 else 1
            if n > len(tasks):
                print("本步只有 {0} 个子代理委派。".format(len(tasks)))
                return
            t = tasks[n - 1]
            if not t["child"]:
                print("未解析到子会话 id，无法取正文。")
                return
            cblocks = fetch_session_text_blocks(ro, t["child"])
            print("═══ [对话] 子代理 @{0} 正文（T2.5·子会话 {1}）═══".format(t["subagent"], t["child"]))
            emit_blocks(cblocks, budget, args.offset, args.full, [])
            print()
            print("═══ 如需进一步 ═══")
            print("  recall.py session {0}   整会话正文 (≤{1} tok，需用户许可)".format(session_id, BUDGET_T3))


def _turn_doc_section(args, dcfg, row):
    section_id, doc_id, file_path, file_title, heading, level, text = row
    _show_errata(dcfg["domain"], doc_id)
    _consulted_append(dcfg["domain"], doc_id, file_title or doc_id, section_id)
    budget = BUDGET_T25 if (args.reasoning or args.patches or args.subagent is not None) else BUDGET_T2
    if args.length is not None:
        budget = args.length
    emit_blocks([(heading or "(导言)", text)], budget, args.offset, args.full,
                ["═══ [{0}] section 正文 · {1} ═══".format(dcfg["label"], file_title)])
    has_material = False
    st = kb_core.domain_stats(dcfg["db_path"])
    if st.get("stored", 0):
        has_material = True
    print()
    print("═══ 如需深入（按需取用）═══")
    if has_material:
        print("  recall.py material <doc_id>        取该域存库未索引的素材/附件（如 {0} 篇）".format(st.get("stored", 0)))
    print("  recall.py document {0}              整文档正文 (≤{1} tok，需用户许可)".format(doc_id, BUDGET_T3))
    print("✂ 若截断：--length N 指定 / --full 完整 / --offset N 翻页")


def cmd_document(args, idx, ro):
    for d in CONFIG["doc_domains"]:
        if not d.get("enabled") or not d.get("db_path") or not kb_core.available(d["db_path"]):
            continue
        title, blocks = kb_core.expand_document(d["db_path"], args.doc_id)
        if blocks is not None:
            _show_errata(d["domain"], args.doc_id)
            _consulted_append(d["domain"], args.doc_id, title or args.doc_id, args.doc_id)
            budget = BUDGET_T3 if args.length is None else args.length
            emit_blocks(blocks, budget, args.offset, args.full,
                        ["═══ [{0}] 整文档【{1}】正文（T3·已经过用户许可）═══".format(d["label"], title)])
            return
    sys.exit("错误：在可用文档域找不到 doc_id=" + args.doc_id)


def cmd_material(args, idx, ro):
    for d in CONFIG["doc_domains"]:
        if not d.get("enabled") or not d.get("db_path") or not kb_core.available(d["db_path"]):
            continue
        row = kb_core.fetch_material(d["db_path"], args.doc_id)
        if row:
            doc_id, file_path, file_title, frontmatter, text = row
            budget = BUDGET_T3 if args.length is None else args.length
            blocks = []
            if frontmatter:
                blocks.append(("frontmatter", frontmatter))
            blocks.append((file_title or "(素材)", text))
            emit_blocks(blocks, budget, args.offset, args.full,
                        ["═══ [{0}] 素材/附件【{1}】（存库未索引）═══".format(d["label"], file_title)])
            print("  源文件：" + file_path)
            return
    sys.exit("错误：在可用文档域找不到 material doc_id=" + args.doc_id)


def cmd_session(args, idx, ro):
    row = ro.execute("SELECT title, directory FROM session WHERE id=?", (args.sid,)).fetchone()
    title = row[0] if row else "?"
    _show_errata("conversation", args.sid)
    _consulted_append("conversation", args.sid, title or args.sid, args.sid)
    blocks_all = fetch_session_text_blocks(ro, args.sid)
    if args.turn is not None:
        groups, cur = [], []
        for label, txt in blocks_all:
            cur.append((label, txt))
            if label == "用户" and len(cur) > 1:
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        if 1 <= args.turn <= len(groups):
            blocks = groups[args.turn - 1]
        else:
            print("该会话共 {0} 个用户轮，--turn 超出范围。".format(len(groups)))
            return
        header = ["═══ [对话] 会话【{0}】· 第 {1} 轮正文（T3·已经过用户许可）═══".format(title, args.turn)]
    else:
        blocks = blocks_all
        header = ["═══ [对话] 会话【{0}】· 整会话正文（T3·已经过用户许可）═══".format(title)]
    budget = BUDGET_T3 if args.length is None else args.length
    emit_blocks(blocks, budget, args.offset, args.full, header)


def cmd_sessions(args, idx, ro):
    rows = ro.execute(
        "SELECT id, title, time_created, directory, agent FROM session WHERE parent_id IS NULL "
        "ORDER BY time_created DESC LIMIT ?", (max(1, args.limit),)).fetchall()
    print("═══ [对话] 主会话列表（最近 {0}）═══".format(len(rows)))
    for i, (sid, title, tc, directory, agent) in enumerate(rows, 1):
        print("{0:>3}. [{1}] {2}  ({3} · {4}{5})".format(
            i, sid, title or "(无标题)", fmt_time(tc), short_dir(directory),
            (" · @" + agent) if agent else ""))


def cmd_status(args, idx, ro):
    print("═══ 知识库状态 ═══")
    conv = CONFIG["conversation"]
    cnt = meta_get(idx, "indexed_count", "0")
    wm = meta_get(idx, "watermark_time", "0")
    print("[对话] db: {0}".format(conv["db_path"]))
    print("       索引 section(text part): {0}  水位线: {1}  可用: 是".format(cnt, wm))
    for d in CONFIG["doc_domains"]:
        avail = bool(d.get("db_path")) and kb_core.available(d["db_path"])
        print("[{0}] db: {1}".format(d["label"], d.get("db_path")))
        if avail:
            st = kb_core.domain_stats(d["db_path"])
            print("     可用: 是 | 索引 section: {0} | 存库素材: {1} | 源文件: {2}".format(
                st.get("indexed", 0), st.get("stored", 0), st.get("files", 0)))
        else:
            print("     可用: 否（E盘未连接或未同步）")
    dbs = [CONFIG["conversation"]["db_path"]] + [d["db_path"] for d in CONFIG["doc_domains"] if d.get("db_path")]
    gray = CONFIG.get("gray") or {}
    if gray.get("db_path"):
        dbs.append(gray["db_path"])
    sz = kb_core.kb_size_info([p for p in dbs if p])
    th = CONFIG.get("thresholds") or {}
    warn = th.get("size_warn_mb", 256)
    print("")
    print("知识库总大小：{0} MB（警报阈值 {1} MB）".format(sz["total_mb"], warn))
    if sz["total_mb"] >= warn:
        print("⚠ 已超警报阈值！建议触发 knowledge-auditor 清理到 {0} MB（低 rank 文档移入灰库）。".format(th.get("size_target_mb", 216)))


def cmd_sync(args, idx, ro):
    ch = sync(idx, ro, force=True)
    cnt = meta_get(idx, "indexed_count", "0")
    print("[对话] {0}  section: {1}".format("已重建" if ch else "已是最新", cnt))
    for d in CONFIG["doc_domains"]:
        if not d.get("enabled") or not d.get("db_path") or not d.get("source_root"):
            continue
        try:
            ch, st = kb_core.sync_doc_domain(d, force=True)
            if not st.get("available"):
                print("[{0}] 跳过：{1}".format(d["label"], st.get("reason")))
            else:
                print("[{0}] {1}  索引 section: {2}  存库素材: {3}".format(
                    d["label"], "已重建" if ch else "已是最新", st.get("indexed", 0), st.get("stored", 0)))
        except Exception as e:
            print("[{0}] 同步失败: {1}".format(d["label"], e))


def cmd_feedback(args, idx, ro):
    log = os.path.join(os.getcwd(), ".kb_consulted.jsonl")
    if not os.path.exists(log):
        print("没有 consulted 日志：" + log)
        print("先用 recall.py turn/document/session 展开过内容才会记录。日志在当前工作目录。")
        return
    entries = []
    seen = set()
    with open(log, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            if not e.get("unit_id"):
                continue
            key = e.get("domain", "") + "|" + e.get("unit_id", "")
            if key in seen:
                continue
            seen.add(key)
            entries.append(e)
    if not entries:
        print("consulted 日志为空。")
        return
    meta_db = CONFIG.get("meta_db")
    if not meta_db:
        sys.exit("错误：config.json 未配置 meta.db_path")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    form_path = os.path.join(os.getcwd(), ".kb_feedback_" + ts + ".py")
    scripts_dir = os.path.dirname(os.path.abspath(__file__))
    lines = [
        "# -*- coding: utf-8 -*-",
        "# 知识库反馈表单（recall.py feedback 自动生成）。",
        "# 填好 VERDICTS 后运行本脚本，自动路由到 meta.db。",
        "# verdict: useful(有用) / neutral(一般,默认跳过) / stale(过时) / wrong(误导)",
        "# stale/wrong 且填了 note → 作为勘误(issue)记入，未来检索该单元时自动浮现告诫。",
        "# 评估依据：真实任务结果——这些知识到底有没有帮上忙（现实支点，非当下自评）。",
        "import sys",
        'sys.path.insert(0, r"' + scripts_dir + '")',
        "import kb_core",
        'META_DB = r"' + meta_db + '"',
        "",
        "VERDICTS = {",
    ]
    for e in entries:
        key = e["domain"] + "|" + e["unit_id"]
        label = (e.get("unit_label") or "?").replace("\n", " ")[:70]
        lines.append("    " + json.dumps(key, ensure_ascii=False) + ': {"verdict": "neutral", "note": ""},  # ' + label)
    lines.append("}")
    lines.append("")
    lines.append("if __name__ == '__main__':")
    lines.append("    n = 0")
    lines.append("    for k, v in VERDICTS.items():")
    lines.append("        if v['verdict'] == 'neutral' and not v.get('note'):")
    lines.append("            continue")
    lines.append("        domain, unit_id = k.split('|', 1)")
    lines.append("        r = kb_core.record_feedback(META_DB, domain, unit_id, v['verdict'], v.get('note', ''))")
    lines.append("        n += 1")
    lines.append("        print('  ', domain, unit_id, '->', r)")
    lines.append("    print('已提交 %d 条反馈到 meta.db' % n)")
    open(form_path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print("已生成反馈表单：" + form_path)
    print("填好 VERDICTS（每条改 verdict + 写 note）后运行：python \"" + os.path.basename(form_path) + "\"")
    print("（评估须在真实工作告一段落后做，依据知识是否真的帮上忙）")
    print("本次 consulted 单元（{0} 个）：".format(len(entries)))
    for e in entries:
        print("  - " + e["domain"] + " | " + (e.get("unit_label") or e.get("unit_id", "?")))


def build_parser():
    p = argparse.ArgumentParser(prog="recall.py", description="多域分布式知识检索（分级渐进）")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("search", help="T1 多词并行检索（多域）")
    sp.add_argument("queries", nargs="*", help="查询词（建议联想 10+ 个；留空 + 时间过滤 = 浏览模式）")
    sp.add_argument("--role", help="对话域筛选角色：user/assistant")
    sp.add_argument("--sid", help="对话域限定会话 id")
    sp.add_argument("--domain", help="限定域：对话/周报/成果 或 all（默认 all）")
    sp.add_argument("--since", help="时间过滤起点：YYYY-MM-DD 或毫秒")
    sp.add_argument("--until", help="时间过滤止点：YYYY-MM-DD 或毫秒（含当日）")
    sp.add_argument("--not", nargs="*", default=[], help="排除词（含这些词的命中被过滤掉）")
    sp.add_argument("--limit", type=int, default=12, help="每域最大展示命中条数")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("turn", help="T2 展开命中（自动识别对话 part_id / 文档 section_id）")
    sp.add_argument("part_id", help="part_id 或 section_id")
    sp.add_argument("--length", type=int, help="本次读取 token 预算")
    sp.add_argument("--offset", type=int, default=0, help="跳过前 N tok")
    sp.add_argument("--full", action="store_true", help="完整读取")
    sp.add_argument("--reasoning", action="store_true", help="对话域·追加本步思维链")
    sp.add_argument("--patches", action="store_true", help="对话域·追加本步文件改动")
    sp.add_argument("--subagent", type=int, nargs="?", const=1, default=None, help="对话域·取本步第 N 个子代理正文")
    sp.set_defaults(func=cmd_turn)

    sp = sub.add_parser("document", help="T3 文档域整文档正文（需用户许可）")
    sp.add_argument("doc_id", help="文档 id")
    sp.add_argument("--length", type=int, help="本次读取 token 预算")
    sp.add_argument("--offset", type=int, default=0, help="跳过前 N tok")
    sp.add_argument("--full", action="store_true", help="完整读取")
    sp.set_defaults(func=cmd_document)

    sp = sub.add_parser("material", help="取文档域存库未索引的素材/附件")
    sp.add_argument("doc_id", help="素材 doc_id")
    sp.add_argument("--length", type=int, help="本次读取 token 预算")
    sp.add_argument("--offset", type=int, default=0, help="跳过前 N tok")
    sp.add_argument("--full", action="store_true", help="完整读取")
    sp.set_defaults(func=cmd_material)

    sp = sub.add_parser("session", help="T3 对话域整会话/整轮正文（需用户许可）")
    sp.add_argument("sid", help="会话 id")
    sp.add_argument("--turn", type=int, help="只取第 N 个用户轮")
    sp.add_argument("--length", type=int, help="本次读取 token 预算")
    sp.add_argument("--offset", type=int, default=0, help="跳过前 N tok")
    sp.add_argument("--full", action="store_true", help="完整读取")
    sp.set_defaults(func=cmd_session)

    sp = sub.add_parser("sessions", help="列出最近的主会话（对话域）")
    sp.add_argument("--limit", type=int, default=20)
    sp.set_defaults(func=cmd_sessions)

    sp = sub.add_parser("status", help="各域可用性与索引统计")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("sync", help="强制重建所有域索引")
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("feedback", help="生成现实支点反馈表单（基于当前目录 consulted 日志）")
    sp.set_defaults(func=cmd_feedback)

    return p


def main():
    load_config()
    args = build_parser().parse_args()
    idx = connect_idx()
    ensure_schema(idx)
    ro = connect_ro()
    try:
        args.func(args, idx, ro)
    finally:
        idx.close()
        ro.close()


if __name__ == "__main__":
    main()
