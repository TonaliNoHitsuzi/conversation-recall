#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kb_core —— 知识库共享核心（文档域：周报 / 成果项目 / 探照灯认知）。

每个文档域一个独立 db（分布式）。db 内：
  - doc_idx(FTS5)：高信号 section（按 md 标题切片，FTS5+jieba）
  - material_store：存库不索引的素材/附件（按需取）
  - doc_source：doc_id → source_kind(folder|upload)，让 folder 批量同步与 upload 增量共存

两类入库：
  - folder 批量同步（sync_doc_domain）：项目终结库 / 周报域；只刷新 source_kind=folder 的条目
  - upload 增量（ingest_file）：少量文件上传到既有库；source_kind=upload，不会被 folder 同步抹掉

被 recall.py / knowledge-curator / office-weekly-status / knowledge-auditor(R4) 共用。
"""
import os
import re
import time
import glob
import json
import sqlite3
import hashlib
import argparse
from fnmatch import fnmatch
from concurrent.futures import ThreadPoolExecutor

import jieba

CHARS_PER_TOKEN = 0.75
HEADING_RE = re.compile(r'^(#{1,6})\s+(.*?)\s*$', re.MULTILINE)
FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n?(.*)$', re.DOTALL)

DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


def est_tokens(t):
    return max(1, int(len(t) * CHARS_PER_TOKEN))


def to_chars(tok):
    return int(tok / CHARS_PER_TOKEN)


def seg(t):
    return " ".join(jieba.cut(t))


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


def _hash(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:16]


def doc_id_of(path):
    return _hash(os.path.abspath(path).replace("\\", "/"))


def section_id_of(path, heading):
    return _hash(os.path.abspath(path).replace("\\", "/") + "|" + heading)


def _fm_title(fm):
    if not fm:
        return None
    m = re.search(r'^title:\s*(.+)$', fm, re.MULTILINE)
    return m.group(1).strip().strip('"').strip("'") if m else None


def _fm_title_text(txt):
    m = FM_RE.match(txt)
    return _fm_title(m.group(1)) if m else None


def split_md(text):
    fm = ""
    body = text
    m = FM_RE.match(text)
    if m:
        fm = m.group(1)
        body = m.group(2)
    marks = [(mt.start(), len(mt.group(1)), mt.group(2)) for mt in HEADING_RE.finditer(body)]
    secs = []
    if not marks:
        title = _fm_title(fm) or "(无标题)"
        secs.append({"heading": title, "level": 0, "body": (fm + "\n" if fm else "") + body.strip(), "fm": fm})
        return secs
    pre = body[:marks[0][0]].strip()
    if pre or fm:
        secs.append({"heading": _fm_title(fm) or "(导言)", "level": 0, "body": (fm + "\n" if fm else "") + pre, "fm": fm})
    stack = []
    for i, (pos, lvl, htext) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
        while stack and stack[-1][0] >= lvl:
            stack.pop()
        stack.append((lvl, htext))
        secs.append({"heading": " > ".join(t for _, t in stack), "level": lvl, "body": body[pos:end].strip(), "fm": fm})
    return secs


def ensure_doc_schema(con):
    con.execute("CREATE TABLE IF NOT EXISTS doc_meta (k TEXT PRIMARY KEY, v TEXT)")
    con.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS doc_idx USING fts5(
        section_id UNINDEXED, doc_id UNINDEXED, domain UNINDEXED,
        file_path UNINDEXED, file_title UNINDEXED, heading_path UNINDEXED,
        level UNINDEXED, mtime UNINDEXED, text_orig UNINDEXED, text_seg)""")
    con.execute("""CREATE TABLE IF NOT EXISTS material_store (
        doc_id TEXT PRIMARY KEY, file_path TEXT, file_title TEXT,
        frontmatter TEXT, mtime INTEGER, text_orig TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS doc_source (
        doc_id TEXT PRIMARY KEY, source_kind TEXT, origin TEXT)""")
    # v3: sidecar 表，存每个 doc 的 genre/tags（不动 FTS5 虚拟表）
    con.execute("""CREATE TABLE IF NOT EXISTS doc_genre (
        doc_id TEXT PRIMARY KEY, genre TEXT, tags TEXT)""")
    con.commit()


def infer_genre(file_path, index_glob_hit=None):
    """根据文件相对路径推断 genre。返回字符串。
    规则按周报/项目典型布局；未命中返回 '其他'。
    file_path: 相对 source_root 的路径（正斜杠）。
    index_glob_hit: 命中的 glob 字符串（可选，用于辅助判断）。
    """
    p = (file_path or "").replace("\\", "/").lower()
    base = p.rsplit("/", 1)[-1]
    # 周报正文
    if base.startswith("周报.md") or base.startswith("weekly-report") or base == "周报.md":
        return "周报正文"
    # 周报摘要
    if base.startswith("周报摘要") or base.startswith("leader-summary"):
        return "周报摘要"
    # 修改记录
    if base.startswith("修改记录"):
        return "周报元数据"
    # 素材（按路径或 basename）
    if "/素材/" in p or base.startswith("素材"):
        return "素材"
    # 产出/文档
    if "/产出/文档/" in p:
        return "技术文档"
    # 产出/图片（理论上 .md 不会在这，但保险）
    if "/产出/图片/" in p or "/产出/images/" in p or "/images/" in p:
        return "图片"
    # 计划
    if "/计划/" in p:
        return "计划"
    return "其他"


def _meta(con, k, d=None):
    r = con.execute("SELECT v FROM doc_meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else d


def _metaset(con, k, v):
    con.execute("INSERT INTO doc_meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def _split_globs(s):
    if not s:
        return []
    return [x.strip() for x in s.split(",") if x.strip()]


def _match_any(name, globs):
    """fnmatch 友好版：**/foo 也匹配顶层 foo（无目录前缀）。"""
    for g in globs:
        if fnmatch(name, g):
            return True
        # v3: **/foo 模式也匹配顶层 foo（避免新库根目录文件漏索引）
        if g.startswith("**/") and fnmatch(name, g[3:]):
            return True
    return False


def _excluded(relpath, basename, excludes):
    relpath = relpath.replace("\\", "/")
    for g in excludes:
        if fnmatch(basename, g) or fnmatch(relpath, g):
            return True
    return False


def available(db_path):
    if not db_path or not os.path.exists(db_path):
        return False
    try:
        con = sqlite3.connect(db_path)
        con.close()
        return True
    except Exception:
        return False


def sync_doc_domain(cfg, force=False):
    """folder 批量同步。只刷新 source_kind=folder 的条目，不动 upload。
    cfg: db_path, source_root, source_glob, index_globs, store_only_globs, exclude_globs, domain。"""
    db_path = cfg["db_path"]
    src = cfg.get("source_root")
    if not src or not os.path.isdir(src):
        return False, {"available": False, "reason": "source_root 不可用: " + str(src)}
    index_globs = cfg.get("index_globs") or ["*.md"]
    store_globs = cfg.get("store_only_globs") or []
    excludes = cfg.get("exclude_globs") or []
    domain = cfg.get("domain", "doc")
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    files = glob.glob(os.path.join(src, cfg.get("source_glob", "**/*.md")), recursive=True)
    idx_files, sto_files = [], []
    for f in files:
        rel = os.path.relpath(f, src)
        if _excluded(rel, os.path.basename(f), excludes):
            continue
        if _match_any(os.path.basename(f), index_globs) or _match_any(rel, index_globs):
            idx_files.append(f)
        elif _match_any(os.path.basename(f), store_globs) or _match_any(rel, store_globs):
            sto_files.append(f)
    folder_doc_max = max([int(os.path.getmtime(f) * 1000) for f in files] + [0])
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    ensure_doc_schema(con)
    stored = _meta(con, "watermark_mtime")
    stored_files = int(_meta(con, "files_count", "0"))
    if (not force) and stored is not None and int(stored) >= folder_doc_max and stored_files == len(files):
        con.close()
        return False, {"available": True, "changed": False,
                       "indexed": int(_meta(con, "indexed_count", "0")),
                       "stored": int(_meta(con, "material_count", "0")), "files": len(files)}
    con.execute("DELETE FROM doc_idx WHERE doc_id NOT IN (SELECT doc_id FROM doc_source WHERE source_kind IN ('upload','revived'))")
    con.execute("DELETE FROM doc_source WHERE source_kind='folder'")
    con.execute("DELETE FROM material_store")
    con.execute("DELETE FROM doc_genre WHERE doc_id NOT IN (SELECT doc_id FROM doc_source WHERE source_kind IN ('upload','revived'))")
    idx_batch, sto_batch, src_batch, genre_batch = [], [], [], []
    for f in idx_files:
        try:
            txt = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        mt = int(os.path.getmtime(f) * 1000)
        did = doc_id_of(f)
        rel = os.path.relpath(f, src).replace("\\", "/")
        title = _fm_title_text(txt) or os.path.splitext(os.path.basename(f))[0]
        for s in split_md(txt):
            body = s["body"]
            if not body.strip():
                continue
            idx_batch.append((section_id_of(f, s["heading"]), did, domain, rel, title,
                              s["heading"], s["level"], mt, body, seg(body)))
        src_batch.append((did, "folder", rel))
        genre_batch.append((did, infer_genre(rel), ""))
    for f in sto_files:
        try:
            txt = open(f, encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        mt = int(os.path.getmtime(f) * 1000)
        did = doc_id_of(f)
        rel = os.path.relpath(f, src).replace("\\", "/")
        fm = ""
        m = FM_RE.match(txt)
        if m:
            fm = m.group(1)
        sto_batch.append((did, rel, _fm_title(fm) or os.path.splitext(os.path.basename(f))[0], fm, mt, txt))
    if idx_batch:
        con.executemany("INSERT INTO doc_idx (section_id,doc_id,domain,file_path,file_title,heading_path,level,mtime,text_orig,text_seg) VALUES (?,?,?,?,?,?,?,?,?,?)", idx_batch)
    if sto_batch:
        con.executemany("INSERT INTO material_store (doc_id,file_path,file_title,frontmatter,mtime,text_orig) VALUES (?,?,?,?,?,?)", sto_batch)
    if src_batch:
        con.executemany("INSERT OR REPLACE INTO doc_source(doc_id,source_kind,origin) VALUES (?,?,?)", src_batch)
    if genre_batch:
        con.executemany("INSERT OR REPLACE INTO doc_genre(doc_id,genre,tags) VALUES (?,?,?)", genre_batch)
    _metaset(con, "watermark_mtime", folder_doc_max)
    _metaset(con, "indexed_count", len(idx_batch))
    _metaset(con, "material_count", len(sto_batch))
    _metaset(con, "files_count", len(files))
    con.commit()
    con.close()
    return True, {"available": True, "changed": True, "indexed": len(idx_batch),
                  "stored": len(sto_batch), "files": len(files)}


def ingest_file(db_path, domain, md_text, source_path=None, title=None, mtime=None, genre=None, tags=None):
    """增量上传：把一段 md 文本（通常来自 doc2md 或现成 md）入库为 source_kind=upload。
    按 doc_id upsert（同源覆盖）。返回 {doc_id, sections, title, genre}。
    genre 不传则按 source_path 自动推断；tags 为可选逗号分隔字符串。"""
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    ensure_doc_schema(con)
    if source_path:
        src = source_path.replace("\\", "/")
        did = doc_id_of(source_path)
    else:
        src = "upload://" + _hash(md_text)
        did = _hash(src)
    if title is None:
        title = _fm_title_text(md_text) or "(上传文档)"
    if mtime is None:
        mtime = int(time.time() * 1000)
    if genre is None:
        genre = infer_genre(src)
    con.execute("DELETE FROM doc_idx WHERE doc_id=?", (did,))
    con.execute("DELETE FROM doc_source WHERE doc_id=?", (did,))
    secs = split_md(md_text)
    batch = []
    for s in secs:
        body = s["body"]
        if not body.strip():
            continue
        batch.append((section_id_of(src, s["heading"]), did, domain, src, title,
                      s["heading"], s["level"], mtime, body, seg(body)))
    if batch:
        con.executemany("INSERT INTO doc_idx (section_id,doc_id,domain,file_path,file_title,heading_path,level,mtime,text_orig,text_seg) VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    con.execute("INSERT OR REPLACE INTO doc_source(doc_id,source_kind,origin) VALUES (?,?,?)", (did, "upload", src))
    con.execute("INSERT OR REPLACE INTO doc_genre(doc_id,genre,tags) VALUES (?,?,?)",
                (did, genre, tags or ""))
    con.commit()
    con.close()
    return {"doc_id": did, "sections": len(batch), "title": title, "genre": genre}


def query_doc_db(db_path, queries, limit=12, pool=50, since=None, until=None, negatives=None, genre=None):
    if not available(db_path):
        return [], {q: 0 for q in queries}
    planned = [(q, build_match(q, negatives)) for q in queries]

    def run_one(q, match):
        con = sqlite3.connect(db_path)
        try:
            con.execute("PRAGMA query_only=ON")
            sql = ("SELECT section_id,doc_id,domain,file_path,file_title,heading_path,level,text_orig,"
                   "bm25(doc_idx) score FROM doc_idx WHERE text_seg MATCH ?")
            params = [match]
            if genre:
                sql += " AND doc_id IN (SELECT doc_id FROM doc_genre WHERE genre=?)"
                params.append(genre)
            if since is not None:
                sql += " AND mtime >= ?"
                params.append(since)
            if until is not None:
                sql += " AND mtime <= ?"
                params.append(until)
            sql += " ORDER BY score LIMIT ?"
            params.append(pool)
            try:
                rows = con.execute(sql, params).fetchall()
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
        for (sid, did, dom, fp, title, hp, lvl, text, score) in rows:
            counts[q] += 1
            if sid not in agg:
                agg[sid] = {"section_id": sid, "doc_id": did, "domain": dom, "file_path": fp,
                            "file_title": title, "heading": hp, "level": lvl, "text": text,
                            "best": score, "matched": [q]}
            else:
                a = agg[sid]
                if q not in a["matched"]:
                    a["matched"].append(q)
                if score < a["best"]:
                    a["best"] = score
    ranked = sorted(agg.values(), key=lambda a: (-len(a["matched"]), a["best"]))[:limit]
    return ranked, counts


def expand_section(db_path, section_id):
    if not available(db_path):
        return None
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute(
            "SELECT section_id,doc_id,file_path,file_title,heading_path,level,text_orig "
            "FROM doc_idx WHERE section_id=?", (section_id,)).fetchone()
    finally:
        con.close()


def expand_document(db_path, doc_id):
    if not available(db_path):
        return None, None
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        t = con.execute("SELECT file_title FROM doc_idx WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone()
        title = t[0] if t else "?"
        rows = con.execute("SELECT heading_path, text_orig FROM doc_idx WHERE doc_id=? ORDER BY rowid",
                           (doc_id,)).fetchall()
        return title, [(r[0], r[1]) for r in rows]
    finally:
        con.close()


def fetch_material(db_path, doc_id):
    if not available(db_path):
        return None
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute(
            "SELECT doc_id,file_path,file_title,frontmatter,text_orig FROM material_store WHERE doc_id=?",
            (doc_id,)).fetchone()
    finally:
        con.close()


def list_materials(db_path, limit=100):
    if not available(db_path):
        return []
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        return con.execute("SELECT doc_id,file_path,file_title FROM material_store ORDER BY file_path LIMIT ?",
                           (limit,)).fetchall()
    finally:
        con.close()


def browse_doc_db(db_path, since=None, until=None, limit=12, genre=None):
    """无关键词浏览：每个 doc_id 只取一条最新 section，按 mtime 倒序取 limit 个 doc。

    dedup 下沉到 SQL（window function），避免「先 LIMIT 再 dedup」时被高频 doc 占满配额。
    genre: 可选，按 doc_genre.genre 过滤。
    """
    if not available(db_path):
        return []
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        where = ["1=1"]
        params = []
        if since is not None:
            where.append("mtime >= ?")
            params.append(since)
        if until is not None:
            where.append("mtime <= ?")
            params.append(until)
        if genre:
            where.append("doc_id IN (SELECT doc_id FROM doc_genre WHERE genre=?)")
            params.append(genre)
        where_sql = " AND ".join(where)
        sql = (
            "WITH ranked AS ("
            " SELECT section_id,doc_id,domain,file_path,file_title,heading_path,level,text_orig,mtime,"
            "        ROW_NUMBER() OVER (PARTITION BY doc_id ORDER BY mtime DESC, section_id) AS rn"
            " FROM doc_idx WHERE " + where_sql +
            ") SELECT section_id,doc_id,domain,file_path,file_title,heading_path,level,text_orig,mtime"
            " FROM ranked WHERE rn=1 ORDER BY mtime DESC LIMIT ?"
        )
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        return [{"section_id": r[0], "doc_id": r[1], "domain": r[2], "file_path": r[3],
                 "file_title": r[4], "heading": r[5], "level": r[6], "text": r[7],
                 "matched": ["(浏览)"]} for r in rows]
    finally:
        con.close()


def domain_stats(db_path):
    if not available(db_path):
        return {"available": False, "indexed": 0, "stored": 0, "files": 0, "uploaded": 0, "docs": 0}
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        up = con.execute("SELECT COUNT(*) FROM doc_source WHERE source_kind='upload'").fetchone()[0]
        docs = con.execute("SELECT COUNT(DISTINCT doc_id) FROM doc_idx").fetchone()[0]
        return {"available": True, "indexed": int(_meta(con, "indexed_count", "0")),
                "stored": int(_meta(con, "material_count", "0")),
                "files": int(_meta(con, "files_count", "0")), "uploaded": up, "docs": docs}
    finally:
        con.close()


def register_project(config_path, slug, label, source_root, db_path=None,
                     index_globs=None, store_only_globs=None, exclude_globs=None):
    """把一个项目登记进 config.json 的 projects[]（去重 by slug）。"""
    cfg = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    projects = cfg.get("projects", [])
    projects = [p for p in projects if p.get("slug") != slug]
    entry = {
        "slug": slug,
        "label": label,
        "db_path": db_path or ("E:/知识库/projects/" + slug + ".db"),
        "source_root": source_root.replace("\\", "/"),
        "source_glob": "**/*.md",
        "index_globs": index_globs or ["**/*.md"],
        "store_only_globs": store_only_globs or [],
        "exclude_globs": exclude_globs or [],
        "enabled": True,
    }
    projects.append(entry)
    cfg["projects"] = projects
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return entry


def ensure_meta(con):
    con.execute("""CREATE TABLE IF NOT EXISTS feedback (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, unit_id TEXT,
            verdict TEXT, note TEXT, ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS errata (
            id INTEGER PRIMARY KEY AUTOINCREMENT, domain TEXT, unit_id TEXT,
            note TEXT, status TEXT DEFAULT 'open', opened_ts INTEGER, resolved_ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS rank (
            domain TEXT, unit_id TEXT, score REAL DEFAULT 0,
            useful INTEGER DEFAULT 0, negative INTEGER DEFAULT 0, last_ts INTEGER,
            PRIMARY KEY (domain, unit_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS gray_evict (
            domain TEXT, unit_id TEXT, reason TEXT, evicted_ts INTEGER, status TEXT DEFAULT 'gray',
            PRIMARY KEY (domain, unit_id))""")
    # v3: 收藏（跨库，不另建库；复用 meta.db）
    con.execute("""CREATE TABLE IF NOT EXISTS favorite (
            domain TEXT, unit_id TEXT, added_ts INTEGER,
            PRIMARY KEY (domain, unit_id))""")
    # v3: 库分组（Edge 收藏夹式管理多库）
    con.execute("""CREATE TABLE IF NOT EXISTS library_folder (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            parent_id INTEGER,
            created_ts INTEGER,
            sort_order INTEGER DEFAULT 0)""")
    con.execute("""CREATE TABLE IF NOT EXISTS library_folder_member (
            folder_id INTEGER, library_slug TEXT,
            PRIMARY KEY (folder_id, library_slug))""")
    con.commit()


def record_feedback(meta_db, domain, unit_id, verdict, note=""):
    """记录一条反馈（verdict=useful/neutral/stale/wrong + note）。
    useful→rank+；stale/wrong→rank- 且有 note 时开一条 errata(issue)。
    返回更新后的 rank 摘要。"""
    ts = int(time.time() * 1000)
    os.makedirs(os.path.dirname(meta_db) or ".", exist_ok=True)
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        con.execute("INSERT INTO feedback(domain,unit_id,verdict,note,ts) VALUES (?,?,?,?,?)",
                    (domain, unit_id, verdict, note, ts))
        if verdict in ("wrong", "stale") and note.strip():
            con.execute("INSERT INTO errata(domain,unit_id,note,status,opened_ts) VALUES (?,?,?,'open',?)",
                        (domain, unit_id, note, ts))
        r = con.execute("SELECT useful,negative FROM rank WHERE domain=? AND unit_id=?",
                        (domain, unit_id)).fetchone()
        useful, negative = (r[0], r[1]) if r else (0, 0)
        if verdict == "useful":
            useful += 1
        if verdict in ("stale", "wrong"):
            negative += 1
        score = useful * 1.0 - negative * 1.5
        con.execute("INSERT INTO rank(domain,unit_id,score,useful,negative,last_ts) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(domain,unit_id) DO UPDATE SET score=excluded.score,useful=excluded.useful,"
                    "negative=excluded.negative,last_ts=excluded.last_ts",
                    (domain, unit_id, score, useful, negative, ts))
        con.commit()
        return {"verdict": verdict, "score": score, "useful": useful, "negative": negative}
    finally:
        con.close()


def open_errata(meta_db, domain, unit_id):
    if not meta_db or not available(meta_db):
        return []
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        return con.execute(
            "SELECT note,opened_ts FROM errata WHERE domain=? AND unit_id=? AND status='open' "
            "ORDER BY opened_ts DESC", (domain, unit_id)).fetchall()
    finally:
        con.close()


def open_errata(meta_db, domain, unit_id):
    if not meta_db or not available(meta_db):
        return []
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        return con.execute(
            "SELECT note,opened_ts FROM errata WHERE domain=? AND unit_id=? AND status='open' "
            "ORDER BY opened_ts DESC", (domain, unit_id)).fetchall()
    finally:
        con.close()


def add_errata(meta_db, domain, unit_id, note):
    """直接挂一条勘误（issue），不依赖评分 verdict。供人工终端用。返回 {id}。"""
    ts = int(time.time() * 1000)
    os.makedirs(os.path.dirname(meta_db) or ".", exist_ok=True)
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        cur = con.execute("INSERT INTO errata(domain,unit_id,note,status,opened_ts) VALUES(?,?,?,'open',?)",
                          (domain, unit_id, note, ts))
        con.commit()
        return {"id": cur.lastrowid, "domain": domain, "unit_id": unit_id, "note": note}
    finally:
        con.close()


def list_errata(meta_db, domain=None, unit_id=None, status=None, limit=200):
    """列勘误（可按 domain/unit_id/status 过滤）。人工终端 issue 浏览用。"""
    if not meta_db or not available(meta_db):
        return []
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        sql = "SELECT id,domain,unit_id,note,status,opened_ts,resolved_ts FROM errata WHERE 1=1"
        params = []
        if domain:
            sql += " AND domain=?"
            params.append(domain)
        if unit_id:
            sql += " AND unit_id=?"
            params.append(unit_id)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY opened_ts DESC LIMIT ?"
        params.append(limit)
        rows = con.execute(sql, params).fetchall()
        return [{"id": r[0], "domain": r[1], "unit_id": r[2], "note": r[3], "status": r[4],
                 "opened_ts": r[5], "resolved_ts": r[6]} for r in rows]
    finally:
        con.close()


def close_errata(meta_db, errata_id):
    """按 issue id 关闭（resolve）。返回受影响行数。"""
    if not meta_db or not available(meta_db):
        return 0
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        cur = con.cursor()
        cur.execute("UPDATE errata SET status='resolved', resolved_ts=? WHERE id=?",
                    (int(time.time() * 1000), errata_id))
        n = cur.rowcount
        con.commit()
        return n
    finally:
        con.close()


def get_rank(meta_db, domain, unit_id):
    """读 rank 行（审计 before 快照用）。"""
    if not meta_db or not available(meta_db):
        return None
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        r = con.execute("SELECT score,useful,negative,last_ts FROM rank WHERE domain=? AND unit_id=?",
                        (domain, unit_id)).fetchone()
        return {"score": r[0], "useful": r[1], "negative": r[2], "last_ts": r[3]} if r else None
    finally:
        con.close()


def set_rank(meta_db, domain, unit_id, score, useful, negative):
    """覆盖 rank（回滚 rate 用）。"""
    if not meta_db or not available(meta_db):
        return False
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        con.execute("INSERT INTO rank(domain,unit_id,score,useful,negative,last_ts) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(domain,unit_id) DO UPDATE SET score=excluded.score,useful=excluded.useful,negative=excluded.negative",
                    (domain, unit_id, score, useful, negative, int(time.time() * 1000)))
        con.commit()
        return True
    finally:
        con.close()


def get_errata(meta_db, errata_id):
    """读一条 errata（审计 before 用）。"""
    if not meta_db or not available(meta_db):
        return None
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        r = con.execute("SELECT id,domain,unit_id,note,status,opened_ts,resolved_ts FROM errata WHERE id=?",
                        (errata_id,)).fetchone()
        return {"id": r[0], "domain": r[1], "unit_id": r[2], "note": r[3], "status": r[4],
                "opened_ts": r[5], "resolved_ts": r[6]} if r else None
    finally:
        con.close()


def delete_errata(meta_db, errata_id):
    """删除一条 errata（回滚 issue_add 用）。"""
    if not meta_db or not available(meta_db):
        return 0
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        cur = con.cursor()
        cur.execute("DELETE FROM errata WHERE id=?", (errata_id,))
        n = cur.rowcount
        con.commit()
        return n
    finally:
        con.close()


def reopen_errata(meta_db, errata_id):
    """重开 errata（回滚 issue_close 用）。"""
    if not meta_db or not available(meta_db):
        return 0
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        cur = con.cursor()
        cur.execute("UPDATE errata SET status='open', resolved_ts=NULL WHERE id=?", (errata_id,))
        n = cur.rowcount
        con.commit()
        return n
    finally:
        con.close()


def unit_rank(meta_db, domain, unit_id):
    if not meta_db or not available(meta_db):
        return 0.0
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        r = con.execute("SELECT score FROM rank WHERE domain=? AND unit_id=?",
                        (domain, unit_id)).fetchone()
        return r[0] if r else 0.0
    finally:
        con.close()


def resolve_errata(meta_db, domain, unit_id):
    """curator 重策展（文档变化）时调用，关闭该 unit 的 open errata。返回关闭数。"""
    if not meta_db or not available(meta_db):
        return 0
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        cur = con.cursor()
        cur.execute("UPDATE errata SET status='resolved', resolved_ts=? WHERE domain=? AND unit_id=? AND status='open'",
                    (int(time.time() * 1000), domain, unit_id))
        n = cur.rowcount
        con.commit()
        return n
    finally:
        con.close()


def evict_to_gray(project_db, gray_db, domain, doc_id, meta_db=None, reason=""):
    """把一个文档（按 doc_id）从项目库移到 gray.db（失效归档）。
    成果域专用。sections 整体搬迁；meta.db 记 gray_evict。"""
    con = sqlite3.connect(project_db)
    rows = con.execute(
        "SELECT section_id,doc_id,domain,file_path,file_title,heading_path,level,mtime,text_orig,text_seg "
        "FROM doc_idx WHERE doc_id=?", (doc_id,)).fetchall()
    con.execute("DELETE FROM doc_idx WHERE doc_id=?", (doc_id,))
    con.execute("DELETE FROM doc_source WHERE doc_id=?", (doc_id,))
    con.commit()
    con.close()
    if not rows:
        return {"evicted": 0, "doc_id": doc_id}
    os.makedirs(os.path.dirname(gray_db) or ".", exist_ok=True)
    g = sqlite3.connect(gray_db)
    g.execute("PRAGMA journal_mode=WAL")
    ensure_doc_schema(g)
    g.execute("DELETE FROM doc_idx WHERE doc_id=?", (doc_id,))
    g.executemany(
        "INSERT INTO doc_idx (section_id,doc_id,domain,file_path,file_title,heading_path,level,mtime,text_orig,text_seg) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    g.execute("INSERT OR REPLACE INTO doc_source(doc_id,source_kind,origin) VALUES (?, 'gray', ?)", (doc_id, domain))
    g.commit()
    g.close()
    if meta_db:
        m = sqlite3.connect(meta_db)
        ensure_meta(m)
        m.execute("INSERT OR REPLACE INTO gray_evict(domain,unit_id,reason,evicted_ts,status) VALUES (?,?,?,?, 'gray')",
                  (domain, doc_id, reason, int(time.time() * 1000)))
        m.commit()
        m.close()
    return {"evicted": len(rows), "doc_id": doc_id, "domain": domain}


def revive_from_gray(gray_db, project_db, domain, doc_id, meta_db=None):
    """gray 中的文档获正面评价后提回项目库（source_kind=revived，不被 folder 同步抹掉）。"""
    g = sqlite3.connect(gray_db)
    rows = g.execute(
        "SELECT section_id,doc_id,domain,file_path,file_title,heading_path,level,mtime,text_orig,text_seg "
        "FROM doc_idx WHERE doc_id=?", (doc_id,)).fetchall()
    g.execute("DELETE FROM doc_idx WHERE doc_id=?", (doc_id,))
    g.execute("DELETE FROM doc_source WHERE doc_id=?", (doc_id,))
    g.commit()
    g.close()
    if not rows:
        return {"revived": 0, "doc_id": doc_id}
    con = sqlite3.connect(project_db)
    con.execute("PRAGMA journal_mode=WAL")
    ensure_doc_schema(con)
    con.execute("DELETE FROM doc_idx WHERE doc_id=?", (doc_id,))
    con.executemany(
        "INSERT INTO doc_idx (section_id,doc_id,domain,file_path,file_title,heading_path,level,mtime,text_orig,text_seg) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute("INSERT OR REPLACE INTO doc_source(doc_id,source_kind,origin) VALUES (?, 'revived', ?)", (doc_id, domain))
    con.commit()
    con.close()
    if meta_db:
        m = sqlite3.connect(meta_db)
        ensure_meta(m)
        m.execute("UPDATE gray_evict SET status='revived' WHERE domain=? AND unit_id=?", (domain, doc_id))
        m.commit()
        m.close()
    return {"revived": len(rows), "doc_id": doc_id, "domain": domain}


def low_rank_units(meta_db, domain_glob="project:%", limit=20):
    """低 rank 单元（默认只看成果域 project:%），按 score 升序。供 auditor 抽查。"""
    if not meta_db or not available(meta_db):
        return []
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        rows = con.execute(
            "SELECT domain,unit_id,score,useful,negative FROM rank WHERE domain LIKE ? "
            "ORDER BY score ASC, negative DESC LIMIT ?", (domain_glob, limit)).fetchall()
        return [{"domain": r[0], "unit_id": r[1], "score": r[2], "useful": r[3], "negative": r[4]} for r in rows]
    finally:
        con.close()


def gray_list(gray_db, domain_glob=None, limit=100):
    """列出 gray.db 中的文档。"""
    if not gray_db or not available(gray_db):
        return []
    con = sqlite3.connect(gray_db)
    try:
        if domain_glob:
            rows = con.execute(
                "SELECT doc_id,domain,file_title FROM doc_idx WHERE domain LIKE ? GROUP BY doc_id LIMIT ?",
                (domain_glob, limit)).fetchall()
        else:
            rows = con.execute("SELECT doc_id,domain,file_title FROM doc_idx GROUP BY doc_id LIMIT ?", (limit,)).fetchall()
        return [{"doc_id": r[0], "domain": r[1], "title": r[2]} for r in rows]
    finally:
        con.close()


def kb_size_info(db_paths):
    """汇总给定 db 文件总大小。"""
    total = 0
    parts = []
    for p in db_paths:
        if p and os.path.exists(p):
            sz = os.path.getsize(p)
            total += sz
            parts.append({"path": p.replace("\\", "/"), "bytes": sz, "mb": round(sz / 1048576, 2)})
    return {"total_bytes": total, "total_mb": round(total / 1048576, 2), "parts": parts}


# ============ v3: genre ============

def list_genres(db_path):
    """列出库内所有 genre 及其 doc 数（按 doc 数倒序）。"""
    if not available(db_path):
        return []
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        rows = con.execute(
            "SELECT genre, COUNT(*) FROM doc_genre GROUP BY genre ORDER BY 2 DESC, 1"
        ).fetchall()
        return [{"genre": r[0] or "其他", "docs": r[1]} for r in rows]
    finally:
        con.close()


def get_doc_genre(db_path, doc_id):
    """取单个 doc 的 {genre, tags}；未找到返回 None。"""
    if not available(db_path):
        return None
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        r = con.execute("SELECT genre, tags FROM doc_genre WHERE doc_id=?", (doc_id,)).fetchone()
        return {"genre": r[0], "tags": r[1] or ""} if r else None
    finally:
        con.close()


def get_genres_batch(db_path, doc_ids):
    """批量取多个 doc_id 的 genre。返回 {doc_id: genre}。"""
    if not available(db_path) or not doc_ids:
        return {}
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        placeholders = ",".join(["?"] * len(doc_ids))
        rows = con.execute(
            "SELECT doc_id, genre FROM doc_genre WHERE doc_id IN (" + placeholders + ")",
            list(doc_ids)
        ).fetchall()
        return {r[0]: r[1] for r in rows}
    finally:
        con.close()


# ============ v3: favorites ============

def toggle_favorite(meta_db, domain, unit_id):
    """切换收藏状态。返回 {is_favorite: bool}。"""
    if not meta_db:
        return {"is_favorite": False, "err": "meta_db 不可用"}
    os.makedirs(os.path.dirname(meta_db) or ".", exist_ok=True)
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        existing = con.execute(
            "SELECT 1 FROM favorite WHERE domain=? AND unit_id=?", (domain, unit_id)
        ).fetchone()
        if existing:
            con.execute("DELETE FROM favorite WHERE domain=? AND unit_id=?", (domain, unit_id))
            is_fav = False
        else:
            con.execute("INSERT INTO favorite(domain,unit_id,added_ts) VALUES (?,?,?)",
                        (domain, unit_id, int(time.time() * 1000)))
            is_fav = True
        con.commit()
        return {"is_favorite": is_fav}
    finally:
        con.close()


def list_favorites(meta_db, domain_filter=None):
    """列出全部收藏（或按域过滤）。返回 [{domain, unit_id, added_ts}, ...]。"""
    if not meta_db or not available(meta_db):
        return []
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        if domain_filter:
            rows = con.execute(
                "SELECT domain, unit_id, added_ts FROM favorite WHERE domain=? ORDER BY added_ts DESC",
                (domain_filter,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT domain, unit_id, added_ts FROM favorite ORDER BY added_ts DESC"
            ).fetchall()
        return [{"domain": r[0], "unit_id": r[1], "added_ts": r[2]} for r in rows]
    finally:
        con.close()


def is_favorites_batch(meta_db, pairs):
    """批量查询哪些 (domain, unit_id) 已收藏。pairs 是 iterable of (domain, unit_id)。
    返回 set of (domain, unit_id) tuples。"""
    if not meta_db or not available(meta_db) or not pairs:
        return set()
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        result = set()
        # 一次查全部，再 Python filter
        rows = con.execute("SELECT domain, unit_id FROM favorite").fetchall()
        all_favs = {(r[0], r[1]) for r in rows}
        for p in pairs:
            if p in all_favs:
                result.add(p)
        return result
    finally:
        con.close()


# ============ v3: library management ============

def register_library(config_path, slug, label, source_root, description="", tags=None,
                     icon="", db_path=None, index_globs=None, store_only_globs=None,
                     exclude_globs=None):
    """v3 扩展：登记一个新库到 config.json projects[]，含元信息（description/tags/icon）。
    兼容老 register_project（向后兼容）。"""
    cfg = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    projects = cfg.get("projects", [])
    # 去重 by slug
    projects = [p for p in projects if p.get("slug") != slug]
    entry = {
        "slug": slug,
        "label": label,
        "description": description or "",
        "tags": tags or [],
        "icon": icon or "",
        "db_path": db_path or ("E:/知识库/projects/" + slug + ".db"),
        "source_root": source_root.replace("\\", "/"),
        "source_glob": "**/*.md",
        "index_globs": index_globs or ["**/*.md"],
        "store_only_globs": store_only_globs or [],
        "exclude_globs": exclude_globs or [],
        "enabled": True,
    }
    projects.append(entry)
    cfg["projects"] = projects
    # 备份原 config（防意外）
    if os.path.exists(config_path):
        bak = config_path + ".bak"
        try:
            import shutil
            shutil.copy2(config_path, bak)
        except Exception:
            pass
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    return entry


def update_library(config_path, slug, **fields):
    """更新库元信息（label/description/tags/icon 等任意字段）。"""
    cfg = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    projects = cfg.get("projects", [])
    updated = None
    for p in projects:
        if p.get("slug") == slug:
            for k, v in fields.items():
                if v is not None:
                    p[k] = v
            updated = p
            break
    if updated:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    return updated


def delete_library(config_path, slug, keep_db=True):
    """从 config 移除库。keep_db=True 时保留 db 文件（防误删）。"""
    cfg = {}
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
    projects = cfg.get("projects", [])
    removed = next((p for p in projects if p.get("slug") == slug), None)
    cfg["projects"] = [p for p in projects if p.get("slug") != slug]
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    db_deleted = False
    if removed and not keep_db:
        db_p = removed.get("db_path")
        if db_p and os.path.exists(db_p):
            try:
                os.remove(db_p)
                db_deleted = True
            except Exception:
                pass
    return {"removed": removed, "db_deleted": db_deleted}


# ============ v3: library folders (Edge-style) ============

def create_folder(meta_db, name, parent_id=None):
    """创建一个库分组（可嵌套）。返回 {id, name, parent_id}。"""
    if not meta_db:
        return {"err": "meta_db 不可用"}
    os.makedirs(os.path.dirname(meta_db) or ".", exist_ok=True)
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        cur = con.execute(
            "INSERT INTO library_folder(name, parent_id, created_ts, sort_order) VALUES (?,?,?,0)",
            (name, parent_id, int(time.time() * 1000))
        )
        con.commit()
        return {"id": cur.lastrowid, "name": name, "parent_id": parent_id}
    finally:
        con.close()


def list_folders(meta_db):
    """列出全部分组及成员。返回 {folders: [...], unassigned: [slug, ...]}。"""
    if not meta_db or not available(meta_db):
        return {"folders": [], "unassigned": []}
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        folders = con.execute(
            "SELECT id, name, parent_id, sort_order FROM library_folder ORDER BY sort_order, id"
        ).fetchall()
        members = con.execute(
            "SELECT folder_id, library_slug FROM library_folder_member"
        ).fetchall()
        # 按文件夹聚合
        member_map = {}
        for folder_id, slug in members:
            member_map.setdefault(folder_id, []).append(slug)
        return {
            "folders": [
                {"id": f[0], "name": f[1], "parent_id": f[2], "sort_order": f[3],
                 "members": member_map.get(f[0], [])}
                for f in folders
            ],
            # unassigned 由调用方对照 config 得出（meta.db 不知道有哪些库）
        }
    finally:
        con.close()


def add_folder_member(meta_db, folder_id, library_slug):
    """把库加入分组。"""
    if not meta_db:
        return {"err": "meta_db 不可用"}
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        con.execute(
            "INSERT OR IGNORE INTO library_folder_member(folder_id, library_slug) VALUES (?,?)",
            (folder_id, library_slug)
        )
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def remove_folder_member(meta_db, folder_id, library_slug):
    """把库移出分组。"""
    if not meta_db:
        return {"err": "meta_db 不可用"}
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        con.execute(
            "DELETE FROM library_folder_member WHERE folder_id=? AND library_slug=?",
            (folder_id, library_slug)
        )
        con.commit()
        return {"ok": True}
    finally:
        con.close()


def delete_folder(meta_db, folder_id):
    """删除分组（含成员关系；不删库本身）。"""
    if not meta_db:
        return {"err": "meta_db 不可用"}
    con = sqlite3.connect(meta_db)
    try:
        ensure_meta(con)
        con.execute("DELETE FROM library_folder_member WHERE folder_id=?", (folder_id,))
        con.execute("DELETE FROM library_folder WHERE id=?", (folder_id,))
        # 把子文件夹的 parent_id 置空（避免悬空引用）
        con.execute("UPDATE library_folder SET parent_id=NULL WHERE parent_id=?", (folder_id,))
        con.commit()
        return {"ok": True}
    finally:
        con.close()


# ============ v3: siblings ============

_MD_IMG_RE = re.compile(r'!\[[^\]]*\]\(([^)\s]+)')


def list_siblings(db_path, doc_id, source_root=None, max_refs=20):
    """列出一个 doc 的关联文件：
    - same_folder: 同父目录（如同一周容器）的其他已索引 doc
    - referenced_images: doc 正文里 ![](path) 引用的图片文件（绝对或相对路径）

    返回 {same_folder: [...], referenced_images: [...], week_mate: [...]}。
    week_mate 是更宽泛的"同周容器"匹配（YYYYMMDD-MMDD 前缀）。
    """
    if not available(db_path):
        return {"same_folder": [], "referenced_images": [], "week_mate": []}
    con = sqlite3.connect(db_path)
    try:
        con.execute("PRAGMA query_only=ON")
        # 1. 取 doc 的 file_path
        r = con.execute(
            "SELECT file_path FROM doc_idx WHERE doc_id=? LIMIT 1", (doc_id,)
        ).fetchone()
        if not r:
            return {"same_folder": [], "referenced_images": [], "week_mate": []}
        file_path = (r[0] or "").replace("\\", "/")
        # 2. 同父目录的其他 doc（按 doc_id 去重）
        parent = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
        same_folder = []
        if parent:
            rows = con.execute(
                "SELECT DISTINCT doc_id, file_path, file_title FROM doc_idx "
                "WHERE file_path LIKE ? AND doc_id != ? LIMIT 50",
                (parent + "/%", doc_id)
            ).fetchall()
            same_folder = [{"doc_id": r[0], "file_path": r[1], "title": r[2]} for r in rows]
        # 3. 同周容器（YYYYMMDD-MMDD 前缀）
        week_mate = []
        m = re.match(r'^.*?(\d{8}-\d{4})/', file_path)
        if m:
            week_prefix = m.group(1)
            rows = con.execute(
                "SELECT DISTINCT doc_id, file_path, file_title FROM doc_idx "
                "WHERE file_path LIKE ? AND doc_id != ? LIMIT 50",
                ("%" + week_prefix + "/%", doc_id)
            ).fetchall()
            # 排除已在 same_folder 里的
            same_ids = {s["doc_id"] for s in same_folder}
            week_mate = [{"doc_id": r[0], "file_path": r[1], "title": r[2]}
                         for r in rows if r[0] not in same_ids]
        # 4. 解析正文图片引用
        text_rows = con.execute(
            "SELECT text_orig FROM doc_idx WHERE doc_id=?", (doc_id,)
        ).fetchall()
        full_text = "\n".join(t[0] for t in text_rows)
        img_refs = []
        seen_refs = set()
        for match in _MD_IMG_RE.finditer(full_text):
            ref = match.group(1)
            if ref in seen_refs or ref.startswith("http://") or ref.startswith("https://"):
                continue
            seen_refs.add(ref)
            img_refs.append(ref)
            if len(img_refs) >= max_refs:
                break
        # 解析为相对 source_root 的可下载路径
        referenced_images = []
        if source_root and img_refs:
            # 图片相对路径是相对 md 文件所在目录
            md_dir = parent if parent else ""
            for ref in img_refs:
                # 规范化：去掉 ./ 和 ../
                clean = ref.lstrip("./")
                if not clean:
                    continue
                # 解析为相对 source_root 的路径
                if md_dir:
                    candidate_rel = (md_dir + "/" + clean).replace("\\", "/")
                else:
                    candidate_rel = clean
                # 简化处理：去除 ./，标准化（不做完整 path resolution）
                candidate_rel = re.sub(r'/+', '/', candidate_rel)
                abs_check = os.path.join(source_root, candidate_rel.replace("/", os.sep))
                if os.path.exists(abs_check):
                    referenced_images.append({"ref": ref, "resolved_path": candidate_rel})
                else:
                    # 也试一下直接当 source_root 下路径
                    abs_check2 = os.path.join(source_root, clean.replace("/", os.sep))
                    if os.path.exists(abs_check2):
                        referenced_images.append({"ref": ref, "resolved_path": clean})
        return {"same_folder": same_folder, "referenced_images": referenced_images,
                "week_mate": week_mate}
    finally:
        con.close()


def _cli():
    try:
        import sys
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(prog="kb_core.py", description="文档域知识库核心 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("ingest", help="增量上传单个 md 文本到一个库（source_kind=upload）")
    sp.add_argument("--db", required=True)
    sp.add_argument("--domain", required=True)
    sp.add_argument("--md-file", required=True, help="要入库的 md 文件（非 md 请先用 tool-doc2md 转换）")
    sp.add_argument("--source", help="原始文件路径（溯源用，默认同 md-file）")
    sp.add_argument("--title", help="文档标题（默认从 frontmatter/H1 推断）")

    sp = sub.add_parser("build-project", help="folder 批量同步一个项目文件夹到一个库")
    sp.add_argument("--db", required=True)
    sp.add_argument("--domain", required=True)
    sp.add_argument("--source-root", required=True)
    sp.add_argument("--source-glob", default="**/*.md")
    sp.add_argument("--index-globs", default="**/*.md")
    sp.add_argument("--store-only-globs", default="")
    sp.add_argument("--exclude-globs", default="")
    sp.add_argument("--force", action="store_true")

    sp = sub.add_parser("register-project", help="把项目登记进 conversation-recall/config.json")
    sp.add_argument("--config", default=DEFAULT_CONFIG)
    sp.add_argument("--slug", required=True)
    sp.add_argument("--label", required=True)
    sp.add_argument("--source-root", required=True)
    sp.add_argument("--db-path")
    sp.add_argument("--index-globs", default="**/*.md")
    sp.add_argument("--store-only-globs", default="")
    sp.add_argument("--exclude-globs", default="")

    sp = sub.add_parser("query", help="检索单个库")
    sp.add_argument("--db", required=True)
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("words", nargs="+")

    sp = sub.add_parser("stats", help="单库统计")
    sp.add_argument("--db", required=True)

    sp = sub.add_parser("evict", help="把文档从项目库移入灰库（失效归档，成果域专用）")
    sp.add_argument("--project-db", required=True)
    sp.add_argument("--gray-db", required=True)
    sp.add_argument("--domain", required=True)
    sp.add_argument("--doc-id", required=True)
    sp.add_argument("--meta-db")
    sp.add_argument("--reason", default="auditor 判定失效")

    sp = sub.add_parser("revive", help="把文档从灰库提回项目库")
    sp.add_argument("--gray-db", required=True)
    sp.add_argument("--project-db", required=True)
    sp.add_argument("--domain", required=True)
    sp.add_argument("--doc-id", required=True)
    sp.add_argument("--meta-db")

    sp = sub.add_parser("audit-list", help="列出低 rank 单元（默认成果域）")
    sp.add_argument("--meta-db", required=True)
    sp.add_argument("--domain-glob", default="project:%")
    sp.add_argument("--limit", type=int, default=20)

    sp = sub.add_parser("gray-list", help="列出灰库文档")
    sp.add_argument("--gray-db", required=True)
    sp.add_argument("--limit", type=int, default=100)

    sp = sub.add_parser("size", help="汇总库文件总大小（MB）")
    sp.add_argument("--dbs", nargs="+", required=True)

    args = ap.parse_args()
    if args.cmd == "ingest":
        md = open(args.md_file, encoding="utf-8", errors="replace").read()
        r = ingest_file(args.db, args.domain, md, source_path=args.source or args.md_file, title=args.title)
        print("已上传：doc_id={doc_id}  sections={sections}  title={title}".format(**r))
    elif args.cmd == "build-project":
        cfg = {"db_path": args.db, "domain": args.domain, "source_root": args.source_root,
               "source_glob": args.source_glob, "index_globs": _split_globs(args.index_globs),
               "store_only_globs": _split_globs(args.store_only_globs),
               "exclude_globs": _split_globs(args.exclude_globs)}
        ch, st = sync_doc_domain(cfg, force=args.force)
        print(json.dumps(st, ensure_ascii=False))
    elif args.cmd == "register-project":
        entry = register_project(args.config, args.slug, args.label, args.source_root, args.db_path,
                                 _split_globs(args.index_globs), _split_globs(args.store_only_globs),
                                 _split_globs(args.exclude_globs))
        print("已登记项目：" + json.dumps(entry, ensure_ascii=False))
    elif args.cmd == "query":
        hits, counts = query_doc_db(args.db, args.words, limit=args.limit)
        print("命中 {0} 条 · 词分布 {1}".format(len(hits), counts))
        for h in hits:
            print("  [{0}] {1} > {2}  共识{3}".format(h["domain"], h["file_title"], h["heading"][:40], len(h["matched"])))
    elif args.cmd == "stats":
        print(json.dumps(domain_stats(args.db), ensure_ascii=False))
    elif args.cmd == "evict":
        print(json.dumps(evict_to_gray(args.project_db, args.gray_db, args.domain, args.doc_id, args.meta_db, args.reason), ensure_ascii=False))
    elif args.cmd == "revive":
        print(json.dumps(revive_from_gray(args.gray_db, args.project_db, args.domain, args.doc_id, args.meta_db), ensure_ascii=False))
    elif args.cmd == "audit-list":
        print(json.dumps(low_rank_units(args.meta_db, args.domain_glob, args.limit), ensure_ascii=False, indent=2))
    elif args.cmd == "gray-list":
        print(json.dumps(gray_list(args.gray_db, limit=args.limit), ensure_ascii=False, indent=2))
    elif args.cmd == "size":
        print(json.dumps(kb_size_info(args.dbs), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
