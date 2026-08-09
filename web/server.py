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

ASSETS = os.path.join(HERE, "assets")
DEFAULT_PORT = 8719

MIME = {".js": "application/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".json": "application/json; charset=utf-8",
        ".svg": "image/svg+xml",
        ".woff2": "font/woff2"}


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
        return {"score": 0.0, "errata_open": 0}
    try:
        score = kb_core.unit_rank(META_DB, domain, unit_id)
    except Exception:
        score = 0.0
    try:
        errs = kb_core.open_errata(META_DB, domain, unit_id)
    except Exception:
        errs = []
    return {"score": score, "errata_open": len(errs), "errata": [
        {"note": e[0], "ts": e[1]} for e in errs]}


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
    return {"domain": dcfg["domain"], "unit_id": doc_id, "title": file_title or doc_id,
            "heading": heading, "md": text, "multimodal_url": _fm_url(dcfg, file_path)}


def _doc_whole(dcfg, doc_id):
    title, blocks = kb_core.expand_document(dcfg["db_path"], doc_id)
    if not blocks:
        return None
    md = "\n\n".join("### {0}\n\n{1}".format(h, t) for h, t in blocks)
    return {"domain": dcfg["domain"], "unit_id": doc_id, "title": title, "md": md,
            "multimodal_url": _fm_url(dcfg, _doc_file_path(dcfg["db_path"], doc_id))}


def _find_doc_domain_by_id(id_):
    """用 id 在各文档域查（section 或 doc），返回 (dcfg, kind)。"""
    for d in CONFIG["doc_domains"]:
        if not d.get("enabled") or not d.get("db_path") or not kb_core.available(d["db_path"]):
            continue
        if kb_core.expand_section(d["db_path"], id_):
            return d, "section"
    return None, None


def _search(queries, domain_filter, role, limit, since=None, until=None, negatives=None):
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
            for h in kb_core.browse_doc_db(d["db_path"], since, until, limit):
                out["hits"].append({"domain":d["domain"],"domain_label":d["label"],"ref_id":h["section_id"],
                    "unit_id":h["doc_id"],"title":h["file_title"] or "(无标题)",
                    "sub":"{0} › {1}".format(recall.short_path(h["file_path"]), h["heading"][:40]),
                    "matched":0,"snippet":(h["text"][:80]+"…").replace("\n"," "),"badge":_badge(d["domain"],h["doc_id"])})
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
        hits, _ = kb_core.query_doc_db(d["db_path"], queries, limit, since=since, until=until, negatives=negatives)
        for h in hits:
            b = _badge(d["domain"], h["doc_id"])
            out["hits"].append({"domain": d["domain"], "domain_label": d["label"],
                                "ref_id": h["section_id"], "unit_id": h["doc_id"], "title": h["file_title"] or "(无标题)",
                                "sub": "{0} › {1}".format(recall.short_path(h["file_path"]), h["heading"][:40]),
                                "matched": len(h["matched"]), "snippet": recall.make_snippet(h["text"], terms),
                                "badge": b})
    # gray fallback
    if not out["hits"] and GRAY_DB and (CONFIG.get("gray") or {}).get("enabled") and kb_core.available(GRAY_DB):
        ghits, _ = kb_core.query_doc_db(GRAY_DB, queries, limit, since=since, until=until, negatives=negatives)
        out["used_gray_fallback"] = True
        for h in ghits:
            out["gray_hits"].append({"domain": h["domain"], "domain_label": "灰库·" + h["domain"],
                                     "ref_id": h["section_id"], "unit_id": h["doc_id"], "title": h["file_title"],
                                     "sub": h["heading"][:50], "matched": len(h["matched"]),
                                     "snippet": recall.make_snippet(h["text"], terms), "badge": _badge(h["domain"], h["doc_id"])})
    out["hits"].sort(key=lambda h: (-h["matched"]))
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
                import jieba as _jb
                negatives = []
                for n in qs.get("not", []):
                    negatives.extend([t for t in _jb.cut(n) if t.strip()])
                self._send(200, _jsend(_search(queries, dom, None, 50, since=since, until=until, negatives=negatives or None)))
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
                entry.update({"indexed": ds.get("indexed", 0), "stored": ds.get("stored", 0)})
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
