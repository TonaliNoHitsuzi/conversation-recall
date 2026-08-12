#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""知识库人工终端 · 本地 web 服务。

只读所有内容；写操作仅限 meta.db（评分/issue）与灰库搬迁（evict/revive）。
永不修改任何文档正文（数据不可变原则）。
"""
import sys
import os
import re
import json
import sqlite3
import logging
import threading
import urllib.parse
import webbrowser
import argparse
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.normpath(os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, SCRIPTS)

LOG_DIR = os.path.join(HERE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
CUR_LOG = os.path.join(LOG_DIR, "server.log")
AUDIT_LOG = os.path.join(LOG_DIR, "audit.jsonl")
CRASH_KEEP = 3
CLEAN_MARKER = "=== CLEAN SHUTDOWN ==="


def _iso_now():
    return datetime.now().isoformat(timespec="seconds")


def _rotate_on_startup():
    """启动时检查上次 server.log：若非正常退出(无 CLEAN 标记)且含错误，归档为 crash-*.log，保留 CRASH_KEEP 个；否则丢弃。"""
    if not os.path.exists(CUR_LOG):
        return
    try:
        with open(CUR_LOG, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return
    is_clean = content.rstrip().endswith(CLEAN_MARKER)
    has_error = ("ERROR" in content) or ("Traceback" in content)
    if (not is_clean) and has_error and content.strip():
        crash_path = os.path.join(LOG_DIR, "crash-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".log")
        try:
            os.replace(CUR_LOG, crash_path)
        except Exception:
            return
        crashes = sorted(c for c in os.listdir(LOG_DIR) if c.startswith("crash-") and c.endswith(".log"))
        for old in crashes[:-CRASH_KEEP]:
            try:
                os.remove(os.path.join(LOG_DIR, old))
            except Exception:
                pass
    else:
        try:
            os.remove(CUR_LOG)
        except Exception:
            pass


_rotate_on_startup()
log = logging.getLogger("kb-web")
log.setLevel(logging.INFO)
_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_fh = logging.FileHandler(CUR_LOG, encoding="utf-8")
_fh.setFormatter(_fmt)
log.addHandler(_fh)
_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
log.addHandler(_sh)


def append_audit(record):
    """append-only 审计日志（仅重要修改操作：rate/issue/灰库）。每行一条 JSON，含 before 快照 + undo 指令。"""
    line = json.dumps(record, ensure_ascii=False)
    try:
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        log.warning("audit 写入失败")

import recall
import kb_core

recall.load_config()
CONFIG = recall.CONFIG
META_DB = CONFIG.get("meta_db")
GRAY_DB = (CONFIG.get("gray") or {}).get("db_path")


def _refresh_config():
    """config 变更后调用：重新加载 + 同步模块级引用。"""
    global CONFIG, META_DB, GRAY_DB
    recall.load_config()
    CONFIG = recall.CONFIG
    META_DB = CONFIG.get("meta_db")
    GRAY_DB = (CONFIG.get("gray") or {}).get("db_path")

ASSETS = os.path.join(HERE, "assets")
DEFAULT_PORT = 8719

MIME = {".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2",
        # v3 新增：raw 文件服务
        ".md": "text/markdown; charset=utf-8",
        ".txt": "text/plain; charset=utf-8",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".pdf": "application/pdf",
        ".py": "text/x-python; charset=utf-8",
        ".sh": "text/x-shellscript; charset=utf-8",
        ".bat": "text/x-batch; charset=utf-8",
        ".ps1": "text/plain; charset=utf-8",
        ".d2": "text/plain; charset=utf-8",
        ".csv": "text/csv; charset=utf-8",
        ".xml": "application/xml; charset=utf-8",
        ".yaml": "text/yaml; charset=utf-8",
        ".yml": "text/yaml; charset=utf-8",
        ".toml": "text/plain; charset=utf-8",
        ".zip": "application/zip"}


def _content_type_for(path, fallback="application/octet-stream"):
    ext = os.path.splitext(path)[1].lower()
    return MIME.get(ext, fallback)


def _domain_source_root(domain):
    """根据 domain 返回该域的 source_root 绝对路径（用于 raw 文件服务）。
    对 conversation 域返回 None（没文件系统源）。"""
    if not domain or domain in ("conversation", "gray", "gray-doc"):
        return None
    if domain == "weekly":
        for d in CONFIG.get("doc_domains", []):
            if d.get("domain") == "weekly" or d.get("key") == "weekly":
                return d.get("source_root")
        return None
    if domain.startswith("project:"):
        slug = domain[len("project:"):]
        for d in CONFIG.get("doc_domains", []):
            if d.get("domain") == domain or d.get("key") == "project:" + slug:
                return d.get("source_root")
        return None
    return None


def _safe_join_path(source_root, rel_path):
    """把相对路径拼到 source_root 下，校验不越狱。返回绝对路径或 None（失败）。"""
    if not source_root or not rel_path:
        return None
    # 防止 Windows/Unix 路径分隔混用
    rel = rel_path.replace("\\", "/").lstrip("/")
    # 阻断绝对路径和 .. 越狱
    if rel.startswith("/") or ":" in rel.split("/", 1)[0]:
        return None
    abs_root = os.path.abspath(source_root)
    abs_path = os.path.abspath(os.path.join(abs_root, rel.replace("/", os.sep)))
    if not abs_path.startswith(abs_root + os.sep) and abs_path != abs_root:
        return None
    return abs_path


def _jsend(obj):
    return json.dumps(obj, ensure_ascii=False, indent=None).encode("utf-8")


def _open_ro():
    return recall.connect_ro()


def _open_idx():
    c = recall.connect_idx()
    recall.ensure_schema(c)
    return c


def _badge(domain, unit_id):
    if not META_DB:
        return {"score": 0.0, "errata_open": 0, "is_favorite": False}
    try:
        score = kb_core.unit_rank(META_DB, domain, unit_id)
    except Exception:
        score = 0.0
    try:
        errs = kb_core.open_errata(META_DB, domain, unit_id)
    except Exception:
        errs = []
    try:
        is_fav = bool(kb_core.is_favorites_batch(META_DB, [(domain, unit_id)]))
    except Exception:
        is_fav = False
    return {"score": score, "errata_open": len(errs), "errata": [
        {"note": e[0], "ts": e[1]} for e in errs], "is_favorite": is_fav}


def _hit_genre(dcfg, doc_id):
    """取单个 hit 的 genre，失败返回空字符串。"""
    try:
        g = kb_core.get_doc_genre(dcfg["db_path"], doc_id)
        return (g or {}).get("genre", "")
    except Exception:
        return ""


def _enrich_hits_with_genre(hits, doc_domains_by_domain):
    """批量给 hits 加 genre 字段。doc_domains_by_domain: {domain_key: dcfg}。"""
    # 按 domain 分组，每组一次 batch 查询
    by_dom = {}
    for h in hits:
        by_dom.setdefault(h["domain"], []).append(h)
    for dom, hh in by_dom.items():
        dcfg = doc_domains_by_domain.get(dom)
        if not dcfg or not dcfg.get("db_path"):
            continue
        doc_ids = list({h["unit_id"] for h in hh})
        genres = kb_core.get_genres_batch(dcfg["db_path"], doc_ids)
        for h in hh:
            h["genre"] = genres.get(h["unit_id"], "")


def _conv_step(ro, idx, part_id):
    row = ro.execute("SELECT session_id FROM part WHERE id=?", (part_id,)).fetchone()
    if not row:
        return None
    sid = row[0]
    timeline = recall.load_timeline(ro, sid)
    roles = recall.load_roles(ro, sid)
    ti = next((i for i, x in enumerate(timeline) if x["id"] == part_id), None)
    if ti is None:
        return None
    trole = roles.get(timeline[ti]["mid"], "?")
    start, end = recall.find_step_bounds(timeline, ti, trole, roles)
    blocks = recall.collect_step_text(timeline, roles, start, end)
    md = "\n\n".join("**[{0}]**\n\n{1}".format(l, t) for l, t in blocks) or "(该步无正文)"
    title = (ro.execute("SELECT title FROM session WHERE id=?", (sid,)).fetchone() or ("?",))[0]
    return {"domain": "conversation", "unit_id": sid, "title": title or sid, "md": md}


def _conv_session_md(ro, sid):
    blocks = recall.fetch_session_text_blocks(ro, sid)
    md = "\n\n---\n\n".join("**[{0}]**\n\n{1}".format(l, t) for l, t in blocks)
    title = (ro.execute("SELECT title FROM session WHERE id=?", (sid,)).fetchone() or ("?",))[0]
    return {"domain": "conversation", "unit_id": sid, "title": title or sid, "md": md}


_FM_URL_RE = re.compile(r'^multimodal_url:\s*["\']?([^\s"\'\n]+)["\']?', re.M)


def _doc_file_path(db_path, doc_id):
    """查 doc_id 的源文件相对路径（doc_idx.file_path），供回读 frontmatter。"""
    try:
        con = sqlite3.connect(db_path)
        con.execute("PRAGMA query_only=ON")
        r = con.execute("SELECT file_path FROM doc_idx WHERE doc_id=? LIMIT 1", (doc_id,)).fetchone()
        con.close()
        return r[0] if r else None
    except Exception:
        return None


def _fm_url(dcfg, file_path):
    """从源文件 frontmatter 提取 multimodal_url（周报 v4 kimi 回填字段）。

    周报走 doc_idx（FTS 索引），库不存 frontmatter；源文件是真相，按 file_path 回读。
    """
    if not dcfg or not file_path:
        return None
    sr = dcfg.get("source_root")
    if not sr:
        return None
    abs_path = os.path.join(sr, file_path)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
    except Exception:
        return None
    m = _FM_URL_RE.search(head)
    return m.group(1) if m else None


def _doc_section(dcfg, section_id):
    r = kb_core.expand_section(dcfg["db_path"], section_id)
    if not r:
        return None
    section_id_, doc_id, file_path, file_title, heading, level, text = r
    g = kb_core.get_doc_genre(dcfg["db_path"], doc_id) or {}
    return {"domain": dcfg["domain"], "unit_id": doc_id, "title": file_title or doc_id,
            "heading": heading, "md": text,
            "file_path": file_path,
            "multimodal_url": _fm_url(dcfg, file_path),
            "genre": g.get("genre", ""), "tags": g.get("tags", "")}


def _doc_whole(dcfg, doc_id):
    title, blocks = kb_core.expand_document(dcfg["db_path"], doc_id)
    if not blocks:
        return None
    md = "\n\n".join("### {0}\n\n{1}".format(h, t) for h, t in blocks)
    g = kb_core.get_doc_genre(dcfg["db_path"], doc_id) or {}
    fp = _doc_file_path(dcfg["db_path"], doc_id)
    return {"domain": dcfg["domain"], "unit_id": doc_id, "title": title, "md": md,
            "file_path": fp or "",
            "multimodal_url": _fm_url(dcfg, fp),
            "genre": g.get("genre", ""), "tags": g.get("tags", "")}


def _find_doc_domain_by_id(id_):
    """用 id 在各文档域查（section 或 doc），返回 (dcfg, kind)。"""
    for d in CONFIG["doc_domains"]:
        if not d.get("enabled") or not d.get("db_path") or not kb_core.available(d["db_path"]):
            continue
        if kb_core.expand_section(d["db_path"], id_):
            return d, "section"
    return None, None


def _search(queries, domain_filter, role, limit, since=None, until=None, negatives=None, genre=None):
    out = {"hits": [], "gray_hits": [], "offline": [], "used_gray_fallback": False}
    # 浏览模式：无关键词 → 按时间倒序直接 SELECT
    if not queries:
        if recall._want_domain(domain_filter, "conversation", "conversation", "对话") and CONFIG["conversation"]["enabled"]:
            try:
                idx = _open_idx()
                hits = recall._conv_browse(idx, since, until, limit)
                idx.close()
            except Exception:
                hits = []
            for a in hits:
                out["hits"].append({"domain":"conversation","domain_label":"对话","ref_id":a["ref"],
                    "unit_id":a["sid"],"title":a["title"] or "(无标题)",
                    "sub":"{0} · role={1}".format(recall.fmt_time(a["tc"]), a["role"]),
                    "matched":0,"snippet":(a["text"][:80]+"…").replace("\n"," "),"badge":_badge("conversation",a["sid"])})
        for d in CONFIG["doc_domains"]:
            if not d.get("enabled") or not recall._want_domain(domain_filter, "doc", d["key"], d["label"]):
                continue
            if not d.get("db_path") or not kb_core.available(d["db_path"]):
                out["offline"].append(d["label"]); continue
            for h in kb_core.browse_doc_db(d["db_path"], since, until, limit, genre=genre):
                out["hits"].append({"domain":d["domain"],"domain_label":d["label"],"ref_id":h["section_id"],
                    "unit_id":h["doc_id"],"title":h["file_title"] or "(无标题)",
                    "sub":"{0} › {1}".format(recall.short_path(h["file_path"]), h["heading"][:40]),
                    "matched":0,"snippet":(h["text"][:80]+"…").replace("\n"," "),"badge":_badge(d["domain"],h["doc_id"])})
        # v3: 浏览模式也要加 genre
        dcfg_map = {d["domain"]: d for d in CONFIG["doc_domains"]}
        _enrich_hits_with_genre(out["hits"], dcfg_map)
        return out
    terms = []
    for q in queries:
        terms.extend([t for t in __import__("jieba").cut(q) if t.strip()])
    # conversation
    if recall._want_domain(domain_filter, "conversation", "conversation", "对话") and CONFIG["conversation"]["enabled"]:
        try:
            idx = _open_idx()
            hits, _ = recall._conv_search(idx, queries, role, None, limit, since=since, until=until, negatives=negatives)
            idx.close()
        except Exception:
            hits = []
        for a in hits:
            b = _badge("conversation", a["sid"])
            out["hits"].append({"domain": "conversation", "domain_label": "对话",
                                "ref_id": a["ref"], "unit_id": a["sid"], "title": a["title"] or "(无标题)",
                                "sub": "{0} · role={1}".format(recall.fmt_time(a["tc"]), a["role"]),
                                "matched": len(a["matched"]), "snippet": recall.make_snippet(a["text"], terms),
                                "badge": b})
    # doc domains
    for d in CONFIG["doc_domains"]:
        if not d.get("enabled"):
            continue
        if not recall._want_domain(domain_filter, "doc", d["key"], d["label"]):
            continue
        if not d.get("db_path") or not kb_core.available(d["db_path"]):
            out["offline"].append(d["label"])
            continue
        hits, _ = kb_core.query_doc_db(d["db_path"], queries, limit, since=since, until=until, negatives=negatives, genre=genre)
        for h in hits:
            b = _badge(d["domain"], h["doc_id"])
            out["hits"].append({"domain": d["domain"], "domain_label": d["label"],
                                "ref_id": h["section_id"], "unit_id": h["doc_id"], "title": h["file_title"] or "(无标题)",
                                "sub": "{0} › {1}".format(recall.short_path(h["file_path"]), h["heading"][:40]),
                                "matched": len(h["matched"]), "snippet": recall.make_snippet(h["text"], terms),
                                "badge": b})
    # gray fallback
    if not out["hits"] and GRAY_DB and (CONFIG.get("gray") or {}).get("enabled") and kb_core.available(GRAY_DB):
        ghits, _ = kb_core.query_doc_db(GRAY_DB, queries, limit, since=since, until=until, negatives=negatives, genre=genre)
        out["used_gray_fallback"] = True
        for h in ghits:
            out["gray_hits"].append({"domain": h["domain"], "domain_label": "灰库·" + h["domain"],
                                     "ref_id": h["section_id"], "unit_id": h["doc_id"], "title": h["file_title"],
                                     "sub": h["heading"][:50], "matched": len(h["matched"]),
                                     "snippet": recall.make_snippet(h["text"], terms), "badge": _badge(h["domain"], h["doc_id"])})
    out["hits"].sort(key=lambda h: (-h["matched"]))
    # v3: 批量加 genre
    dcfg_map = {d["domain"]: d for d in CONFIG["doc_domains"]}
    _enrich_hits_with_genre(out["hits"], dcfg_map)
    if out["gray_hits"]:
        _enrich_hits_with_genre(out["gray_hits"], dcfg_map)
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code, body=b"", ctype="application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_file(self, path):
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            body = f.read()
        self._send(200, body, ctype)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        qs = urllib.parse.parse_qs(u.query)
        try:
            if p == "/" or p == "/index.html":
                self._send_file(os.path.join(HERE, "index.html"))
            elif p.startswith("/assets/"):
                name = os.path.basename(p)
                fp = os.path.join(ASSETS, name)
                if os.path.isfile(fp):
                    self._send_file(fp)
                else:
                    self._send(404, b'{"err":"asset not found"}')
            elif p == "/api/status":
                self._send(200, _jsend(self._status()))
            elif p == "/api/search":
                q = qs.get("q", [""])[0]
                dom = qs.get("domain", ["all"])[0]
                queries = [x for x in re.split(r"[\s,，]+", q) if x]
                since = recall._parse_time(qs.get("since", [None])[0])
                until = recall._parse_time(qs.get("until", [None])[0], end=True)
                genre = qs.get("genre", [None])[0]
                import jieba as _jb
                negatives = []
                for n in qs.get("not", []):
                    negatives.extend([t for t in _jb.cut(n) if t.strip()])
                self._send(200, _jsend(_search(queries, dom, None, 50, since=since, until=until, negatives=negatives or None, genre=genre)))
            elif p == "/api/expand":
                self._send(200, _jsend(self._expand(qs.get("id", [""])[0])))
            elif p == "/api/whole":
                self._send(200, _jsend(self._whole(qs.get("id", [""])[0], qs.get("kind", ["document"])[0])))
            elif p == "/api/issues":
                self._send(200, _jsend({"issues": kb_core.list_errata(META_DB, qs.get("domain", [None])[0],
                                                                       qs.get("unit_id", [None])[0],
                                                                       qs.get("status", [None])[0])}))
            elif p == "/api/gray":
                docs = kb_core.gray_list(GRAY_DB) if GRAY_DB else []
                self._send(200, _jsend({"docs": docs}))
            elif p == "/api/genres":
                # 列出指定库的所有 genre（或全部库的 genre 并集）
                dom = qs.get("domain", ["all"])[0]
                result = {}
                for d in CONFIG["doc_domains"]:
                    if not d.get("enabled") or not d.get("db_path"):
                        continue
                    if dom != "all" and not recall._want_domain(dom, "doc", d["key"], d["label"]):
                        continue
                    if not kb_core.available(d["db_path"]):
                        continue
                    result[d["key"]] = {"label": d["label"], "genres": kb_core.list_genres(d["db_path"])}
                self._send(200, _jsend(result))
            elif p == "/api/favorites":
                dom = qs.get("domain", [None])[0]
                favs = kb_core.list_favorites(META_DB, domain_filter=dom)
                # 跨域 JOIN 元信息（标题/genre/文件路径）
                detailed = []
                for f in favs:
                    item = {"domain": f["domain"], "unit_id": f["unit_id"], "added_ts": f["added_ts"],
                            "title": "(未知)", "file_path": "", "genre": ""}
                    # 找对应 db
                    if f["domain"] == "conversation":
                        item["title"] = "(对话会话)"
                    else:
                        for d in CONFIG["doc_domains"]:
                            if d["domain"] == f["domain"] and d.get("db_path") and kb_core.available(d["db_path"]):
                                r = kb_core.expand_document(d["db_path"], f["unit_id"])
                                if r and r[0]:
                                    item["title"] = r[0]
                                g = kb_core.get_doc_genre(d["db_path"], f["unit_id"])
                                if g:
                                    item["genre"] = g.get("genre", "")
                                fp = kb_core.expand_section(d["db_path"], f["unit_id"])
                                # expand_section 需要 section_id 不是 doc_id；改用直接查
                                try:
                                    con = sqlite3.connect(d["db_path"]); con.execute("PRAGMA query_only=ON")
                                    rr = con.execute("SELECT file_path FROM doc_idx WHERE doc_id=? LIMIT 1", (f["unit_id"],)).fetchone()
                                    con.close()
                                    if rr: item["file_path"] = rr[0]
                                except Exception:
                                    pass
                                break
                    detailed.append(item)
                self._send(200, _jsend({"favorites": detailed}))
            elif p == "/api/libraries":
                # 列出全部库（含元信息）
                libs = []
                # 内置域
                conv = CONFIG.get("conversation", {})
                libs.append({"slug": "conversation", "label": "对话", "domain": "conversation",
                             "description": "opencode / AI 助手的历史对话", "tags": ["内置"],
                             "icon": "💬", "enabled": conv.get("enabled", True),
                             "builtin": True})
                w = CONFIG.get("domains", {}).get("weekly", {})
                libs.append({"slug": "weekly", "label": w.get("label", "周报"), "domain": "weekly",
                             "description": "每周工作周记 + kimi 多模态展示", "tags": ["内置"],
                             "icon": "📅", "enabled": w.get("enabled", True),
                             "builtin": True})
                # 项目库
                for p_entry in CONFIG.get("projects", []):
                    libs.append({
                        "slug": p_entry.get("slug"), "label": p_entry.get("label", ""),
                        "domain": "project:" + p_entry.get("slug", ""),
                        "description": p_entry.get("description", ""),
                        "tags": p_entry.get("tags", []),
                        "icon": p_entry.get("icon", "📁"),
                        "enabled": p_entry.get("enabled", True),
                        "builtin": False,
                        "source_root": p_entry.get("source_root", ""),
                        "db_path": p_entry.get("db_path", ""),
                    })
                # 灰库（特殊）
                if GRAY_DB:
                    libs.append({"slug": "gray", "label": "灰库", "domain": "gray",
                                 "description": "失效归档（可恢复）", "tags": ["系统"],
                                 "icon": "🗑️", "enabled": True, "builtin": True})
                # 含分组信息
                folders = kb_core.list_folders(META_DB) if META_DB else {"folders": []}
                self._send(200, _jsend({"libraries": libs, "folders": folders.get("folders", [])}))
            elif p == "/api/folders":
                self._send(200, _jsend(kb_core.list_folders(META_DB) if META_DB else {"folders": [], "unassigned": []}))
            elif p == "/api/dirlist":
                # 目录浏览器：列出指定路径下的子目录（仅目录，不含文件）
                # 路径为空时列盘符（Windows）或根（Unix）；本地单用户工具，不做路径白名单
                raw = qs.get("path", [""])[0]
                if not raw:
                    import string
                    if os.name == 'nt':
                        drives = [letter + ':/' for letter in string.ascii_uppercase if os.path.exists(letter + ':/')]
                    else:
                        drives = ['/']
                    self._send(200, _jsend({"current": "", "parent": None, "dirs": drives, "is_root": True}))
                else:
                    path = os.path.abspath(raw)
                    if not os.path.isdir(path):
                        self._send(400, _jsend({"err": "不是有效目录: " + raw}))
                    else:
                        try:
                            entries = sorted(os.listdir(path))
                        except PermissionError:
                            self._send(403, _jsend({"err": "无权限访问: " + path}))
                        except Exception as e:
                            self._send(400, _jsend({"err": str(e)}))
                        else:
                            # 仅保留目录，隐藏点开头（系统/隐藏）
                            dirs = [e for e in entries if not e.startswith('.') and os.path.isdir(os.path.join(path, e))]
                            # 计算父目录：盘符根时父为 None（不可再上）
                            norm = path.replace('\\', '/').rstrip('/')
                            parent = None
                            if '/' in norm:
                                head = norm.rsplit('/', 1)[0]
                                # 如果剩下的是 "D:" 这种盘符，仍允许返回（用户可继续往上）
                                parent = head + '/' if head else None
                            elif os.name == 'nt' and len(norm) == 2 and norm[1] == ':':
                                parent = None  # 盘符根，不可再上
                            self._send(200, _jsend({
                                "current": path.replace('\\', '/'),
                                "parent": parent,
                                "dirs": dirs,
                                "is_root": False
                            }))
            elif p == "/api/siblings":
                doc_id = qs.get("doc_id", [""])[0]
                if not doc_id:
                    self._send(400, _jsend({"err": "doc_id 必填"}))
                else:
                    # 在所有文档域里找 doc
                    dcfg, _ = _find_doc_domain_by_id(doc_id)
                    if not dcfg:
                        # 可能直接是 doc_id 不是 section_id，按 doc_id 模糊查
                        for d in CONFIG["doc_domains"]:
                            if d.get("db_path") and kb_core.available(d["db_path"]):
                                title, blocks = kb_core.expand_document(d["db_path"], doc_id)
                                if blocks:
                                    dcfg = d; break
                    if not dcfg:
                        # 不是文档域（可能是对话会话）→ 返回空 siblings，不要 404
                        # 前端 api() 在非 200 时抛错，会污染整个 expand 流程
                        self._send(200, _jsend({"same_folder": [], "referenced_images": [], "week_mate": []}))
                    else:
                        sr = dcfg.get("source_root")
                        self._send(200, _jsend(kb_core.list_siblings(dcfg["db_path"], doc_id, source_root=sr)))
            elif p == "/api/raw":
                # 文件原始字节服务（路径校验严格）
                domain = qs.get("domain", [""])[0]
                rel_path = qs.get("path", [""])[0]
                download = qs.get("download", ["0"])[0] == "1"
                self._handle_raw(domain, rel_path, download)
            else:
                self._send(404, b'{"err":"not found"}')
        except Exception:
            log.exception("do_GET %s", p)
            self._send(500, _jsend({"err": traceback.format_exc()[-800:]}))

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        p = urllib.parse.unquote(u.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            data = {}
        try:
            if p == "/api/rate":
                before = kb_core.get_rank(META_DB, data["domain"], data["unit_id"])
                r = kb_core.record_feedback(META_DB, data["domain"], data["unit_id"], data["verdict"], data.get("note", ""))
                after = kb_core.get_rank(META_DB, data["domain"], data["unit_id"])
                undo = ("set_rank score={0} useful={1} negative={2}".format(before["score"], before["useful"], before["negative"])) if before else "delete rank 行（新建的）"
                append_audit({"ts": _iso_now(), "action": "rate", "domain": data["domain"], "unit_id": data["unit_id"],
                              "verdict": data["verdict"], "note": data.get("note", ""), "before": before, "after": after,
                              "undo": undo, "actor": self.client_address[0]})
                self._send(200, _jsend({"ok": True, "rank": r}))
            elif p == "/api/issue":
                r = kb_core.add_errata(META_DB, data["domain"], data["unit_id"], data["note"])
                append_audit({"ts": _iso_now(), "action": "issue_add", "domain": data["domain"], "unit_id": data["unit_id"],
                              "note": data["note"], "issue_id": r["id"], "before": None,
                              "undo": "delete_errata id={0}".format(r["id"]), "actor": self.client_address[0]})
                self._send(200, _jsend({"ok": True, "issue": r}))
            elif p == "/api/issue_close":
                eid = int(data["id"])
                before = kb_core.get_errata(META_DB, eid)
                n = kb_core.close_errata(META_DB, eid)
                append_audit({"ts": _iso_now(), "action": "issue_close", "issue_id": eid, "before": before,
                              "undo": "reopen_errata id={0}".format(eid), "actor": self.client_address[0]})
                self._send(200, _jsend({"ok": True, "closed": n}))
            elif p == "/api/evict":
                dcfg = self._dcfg_for_domain(data["domain"])
                if not dcfg:
                    self._send(400, _jsend({"err": "域未找到/非成果域"}))
                    return
                before_stats = kb_core.domain_stats(dcfg["db_path"])
                r = kb_core.evict_to_gray(dcfg["db_path"], GRAY_DB, data["domain"], data["unit_id"],
                                          META_DB, data.get("reason", "人工终端淘汰"))
                append_audit({"ts": _iso_now(), "action": "evict", "domain": data["domain"], "unit_id": data["unit_id"],
                              "reason": data.get("reason", "人工终端淘汰"), "sections_moved": r.get("evicted", 0),
                              "before_active_stats": before_stats, "undo": "revive_from_gray（终端：灰库→恢复）",
                              "actor": self.client_address[0]})
                self._send(200, _jsend({"ok": True, "evicted": r}))
            elif p == "/api/revive":
                dcfg = self._dcfg_for_domain(data["domain"])
                if not dcfg:
                    self._send(400, _jsend({"err": "域未找到"}))
                    return
                r = kb_core.revive_from_gray(GRAY_DB, dcfg["db_path"], data["domain"], data["unit_id"], META_DB)
                append_audit({"ts": _iso_now(), "action": "revive", "domain": data["domain"], "unit_id": data["unit_id"],
                              "sections_moved": r.get("revived", 0), "undo": "evict_to_gray（重新移入灰库）",
                              "actor": self.client_address[0]})
                self._send(200, _jsend({"ok": True, "revived": r}))
            elif p == "/api/favorite":
                # 切换收藏
                r = kb_core.toggle_favorite(META_DB, data["domain"], data["unit_id"])
                append_audit({"ts": _iso_now(), "action": "favorite_toggle",
                              "domain": data["domain"], "unit_id": data["unit_id"],
                              "is_favorite": r.get("is_favorite"),
                              "undo": "favorite_toggle（再点切换）", "actor": self.client_address[0]})
                self._send(200, _jsend(r))
            elif p == "/api/library":
                # 新建库（写 config.json）
                config_path = recall.CONFIG_PATH
                # 字段校验
                slug = (data.get("slug") or "").strip()
                label = (data.get("label") or "").strip()
                source_root = (data.get("source_root") or "").strip()
                if not slug or not label or not source_root:
                    self._send(400, _jsend({"err": "slug/label/source_root 必填"}))
                    return
                if not os.path.isdir(source_root):
                    self._send(400, _jsend({"err": "source_root 不是有效目录: " + source_root}))
                    return
                entry = kb_core.register_library(
                    config_path, slug, label, source_root,
                    description=data.get("description", ""),
                    tags=data.get("tags", []),
                    icon=data.get("icon", "📁"),
                    db_path=data.get("db_path"),
                    index_globs=data.get("index_globs"),
                    store_only_globs=data.get("store_only_globs"),
                    exclude_globs=data.get("exclude_globs"),
                )
                # 触发首次同步
                cfg = {**entry, "domain": "project:" + slug}
                ch, st = kb_core.sync_doc_domain(cfg, force=True)
                append_audit({"ts": _iso_now(), "action": "library_create", "slug": slug,
                              "entry": entry, "undo": "delete_library（移除 config）",
                              "actor": self.client_address[0]})
                # 重载 server 的 CONFIG（让新库立即可用）
                _refresh_config()
                self._send(200, _jsend({"ok": True, "library": entry, "initial_sync": st}))
            elif p == "/api/library/update":
                config_path = recall.CONFIG_PATH
                slug = data.get("slug")
                if not slug:
                    self._send(400, _jsend({"err": "slug 必填"}))
                    return
                fields = {k: v for k, v in data.items() if k != "slug"}
                updated = kb_core.update_library(config_path, slug, **fields)
                if updated:
                    _refresh_config()
                    append_audit({"ts": _iso_now(), "action": "library_update", "slug": slug,
                                  "fields": fields, "actor": self.client_address[0]})
                self._send(200, _jsend({"ok": bool(updated), "library": updated}))
            elif p == "/api/library/delete":
                config_path = recall.CONFIG_PATH
                slug = data.get("slug")
                keep_db = data.get("keep_db", True)
                if not slug:
                    self._send(400, _jsend({"err": "slug 必填"}))
                    return
                r = kb_core.delete_library(config_path, slug, keep_db=keep_db)
                _refresh_config()
                append_audit({"ts": _iso_now(), "action": "library_delete", "slug": slug,
                              "removed": r.get("removed"), "keep_db": keep_db,
                              "undo": "重新 register_library（手动恢复 config）",
                              "actor": self.client_address[0]})
                self._send(200, _jsend({"ok": True, **r}))
            elif p == "/api/folder":
                # 创建分组
                r = kb_core.create_folder(META_DB, data["name"], data.get("parent_id"))
                append_audit({"ts": _iso_now(), "action": "folder_create", "folder": r,
                              "undo": "delete_folder", "actor": self.client_address[0]})
                self._send(200, _jsend(r))
            elif p == "/api/folder/member":
                r = kb_core.add_folder_member(META_DB, int(data["folder_id"]), data["slug"])
                self._send(200, _jsend(r))
            elif p == "/api/folder/member/remove":
                r = kb_core.remove_folder_member(META_DB, int(data["folder_id"]), data["slug"])
                self._send(200, _jsend(r))
            elif p == "/api/folder/delete":
                r = kb_core.delete_folder(META_DB, int(data["folder_id"]))
                append_audit({"ts": _iso_now(), "action": "folder_delete",
                              "folder_id": data["folder_id"], "undo": "（无法撤销）",
                              "actor": self.client_address[0]})
                self._send(200, _jsend(r))
            elif p == "/api/pick-folder":
                # 调用 Windows 原生 FolderBrowserDialog（PowerShell 子进程）
                import subprocess
                ps_script = (
                    "Add-Type -AssemblyName System.Windows.Forms\n"
                    "$d = New-Object System.Windows.Forms.FolderBrowserDialog\n"
                    "$d.Description = '为知识库选择源目录'\n"
                    "$d.ShowNewFolderButton = $true\n"
                    "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) {\n"
                    "    Write-Output $d.SelectedPath\n"
                    "} else {\n"
                    "    Write-Output ''\n"
                    "}\n"
                )
                try:
                    result = subprocess.run(
                        ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps_script],
                        capture_output=True, text=True, timeout=300, encoding='utf-8'
                    )
                    picked = (result.stdout or '').strip().replace('\\', '/').rstrip('/')
                    self._send(200, _jsend({"path": picked or None}))
                except subprocess.TimeoutExpired:
                    self._send(200, _jsend({"path": None, "err": "选择超时"}))
                except Exception as e:
                    self._send(500, _jsend({"err": str(e)}))
            else:
                self._send(404, b'{"err":"not found"}')
        except Exception:
            log.exception("do_POST %s", p)
            self._send(500, _jsend({"err": traceback.format_exc()[-800:]}))

    def _dcfg_for_domain(self, domain):
        for d in CONFIG["doc_domains"]:
            if d["domain"] == domain and d["domain"].startswith("project:"):
                return d
        return None

    def _handle_raw(self, domain, rel_path, download=False):
        """统一文件服务入口。
        - conversation 域：动态拼装会话 md（按 session_id），用 Content-Disposition
        - 其他域：从 source_root 拼路径，校验后服务原始字节
        """
        # conversation 特例
        if domain == "conversation":
            sid = rel_path  # 约定：path 字段传 session_id
            try:
                ro = _open_ro()
                try:
                    r = _conv_session_md(ro, sid)
                finally:
                    ro.close()
                if not r:
                    self._send(404, _jsend({"err": "会话不存在"}))
                    return
                body = (r["md"] or "(空)").encode("utf-8")
                ctype = "text/markdown; charset=utf-8"
                disp = 'attachment; filename="conversation-{0}.md"'.format(sid)
            except Exception:
                log.exception("raw conversation")
                self._send(500, _jsend({"err": "会话读取失败"}))
                return
        else:
            sr = _domain_source_root(domain)
            if not sr:
                self._send(400, _jsend({"err": "该域不支持文件服务: " + domain}))
                return
            abs_path = _safe_join_path(sr, rel_path)
            if not abs_path or not os.path.isfile(abs_path):
                self._send(404, _jsend({"err": "文件不存在或越狱: " + rel_path}))
                return
            try:
                with open(abs_path, "rb") as f:
                    body = f.read()
            except Exception:
                self._send(500, _jsend({"err": "读取失败"}))
                return
            ctype = _content_type_for(abs_path)
            base = os.path.basename(abs_path)
            # 用 utf-8 文件名（RFC 5987）
            from urllib.parse import quote
            disp = "attachment; filename*=UTF-8''" + quote(base) if download else "inline"
        # 发送
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", disp)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _expand(self, id_):
        if not id_:
            return {"err": "no id"}
        ro = _open_ro()
        try:
            cs = _conv_step(ro, _open_idx(), id_) if id_.startswith("prt_") else None
            if cs:
                cs["badge"] = _badge(cs["domain"], cs["unit_id"])
                return cs
        finally:
            ro.close()
        dcfg, kind = _find_doc_domain_by_id(id_)
        if dcfg:
            sec = _doc_section(dcfg, id_)
            sec["badge"] = _badge(sec["domain"], sec["unit_id"])
            return sec
        if GRAY_DB and kb_core.available(GRAY_DB):
            r = kb_core.expand_section(GRAY_DB, id_)
            if r:
                section_id, doc_id, file_path, file_title, heading, level, text = r
                gc = sqlite3.connect(GRAY_DB)
                gc.execute("PRAGMA query_only=ON")
                od = gc.execute("SELECT domain FROM doc_idx WHERE section_id=?", (id_,)).fetchone()
                gc.close()
                orig = od[0] if od else "gray"
                return {"domain": orig, "unit_id": doc_id, "title": file_title or doc_id,
                        "heading": heading, "md": text, "isGray": True, "badge": _badge(orig, doc_id)}
        return {"err": "未找到 id（可能在离线域）"}

    def _whole(self, id_, kind):
        ro = _open_ro()
        try:
            if kind == "session":
                r = _conv_session_md(ro, id_)
                r["badge"] = _badge(r["domain"], r["unit_id"])
                return r
        finally:
            ro.close()
        for d in CONFIG["doc_domains"]:
            if not d.get("enabled") or not d.get("db_path") or not kb_core.available(d["db_path"]):
                continue
            r = _doc_whole(d, id_)
            if r:
                r["badge"] = _badge(r["domain"], r["unit_id"])
                return r
        if GRAY_DB and kb_core.available(GRAY_DB):
            con = sqlite3.connect(GRAY_DB)
            con.execute("PRAGMA query_only=ON")
            dr = con.execute("SELECT domain, file_title FROM doc_idx WHERE doc_id=? LIMIT 1", (id_,)).fetchone()
            rows = con.execute("SELECT heading_path, text_orig FROM doc_idx WHERE doc_id=? ORDER BY rowid", (id_,)).fetchall()
            con.close()
            if dr and rows:
                md = "\n\n".join("### {0}\n\n{1}".format(h, x) for h, x in rows)
                return {"domain": dr[0], "unit_id": id_, "title": dr[1] or "灰库文档",
                        "md": md, "isGray": True, "badge": _badge(dr[0], id_)}
        return {"err": "未找到 doc_id"}

    def _status(self):
        st = {"domains": []}
        conv = CONFIG["conversation"]
        idx = _open_idx()
        cnt = recall.meta_get(idx, "indexed_count", "0")
        idx.close()
        st["domains"].append({"key": "conversation", "label": "对话", "indexed": cnt, "available": True})
        for d in CONFIG["doc_domains"]:
            avail = bool(d.get("db_path")) and kb_core.available(d["db_path"])
            entry = {"key": d["key"], "label": d["label"], "available": avail}
            if avail:
                ds = kb_core.domain_stats(d["db_path"])
                entry.update({"indexed": ds.get("indexed", 0), "stored": ds.get("stored", 0),
                              "docs": ds.get("docs", 0)})
            st["domains"].append(entry)
        dbs = [conv["db_path"]] + [d["db_path"] for d in CONFIG["doc_domains"] if d.get("db_path")]
        if GRAY_DB:
            dbs.append(GRAY_DB)
        st["size"] = kb_core.kb_size_info([p for p in dbs if p])
        return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open-browser", action="store_true")
    args = ap.parse_args()
    port = args.port
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:{0}/".format(port)
    log.info("知识库终端启动 %s | 日志 %s | 审计 %s | meta=%s gray=%s", url, CUR_LOG, AUDIT_LOG, META_DB, GRAY_DB)
    if args.open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            with open(CUR_LOG, "a", encoding="utf-8") as f:
                f.write("\n" + CLEAN_MARKER + "\n")
        except Exception:
            pass
        log.info("已正常关闭")


if __name__ == "__main__":
    main()
