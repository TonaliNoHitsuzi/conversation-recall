# -*- coding: utf-8 -*-
"""倒查：给定文件夹，列出与它关联的全部 opencode 对话。
数据源：opencode.db(会话表) + recall.db(全文索引)，均只读。
用法:
  python find-conversations.py "D:\\FileCenter\\20-项目\\ACGS"          # 包含该前缀/子串
  python find-conversations.py "ACGS" --like                            # 任意子串
  python find-conversations.py "..." --json                             # 机器可读
"""
import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

OC_DB = r'C:/Users/Zzy/.local/share/opencode/opencode.db'
RECALL_DB = r'D:/Zzy的Skill工具包/conversation-recall/index/recall.db'


def fmt(t):
    if t is None:
        return '?'
    if isinstance(t, (int, float)):
        try:
            if t > 10 ** 12:
                t /= 1000
            return datetime.fromtimestamp(t).strftime('%Y-%m-%d %H:%M')
        except (OSError, ValueError, OverflowError):
            return str(t)[:16]
    return str(t)[:16]


def norm(p):
    return p.replace('\\', '/').rstrip('/').lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder')
    ap.add_argument('--like', action='store_true', help='按任意子串匹配（默认为包含匹配，等价）')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args()

    needle = norm(args.folder)
    like = f'%{needle}%'
    sessions = {}

    # --- opencode.db: session table ---
    try:
        oc = sqlite3.connect(f'file:{OC_DB.replace(chr(92), "/")}?mode=ro', uri=True)
        oc.execute('PRAGMA query_only=1')
        cols = [r[1] for r in oc.execute('PRAGMA table_info(session)')]
        dcol = next((c for c in ('directory', 'cwd', 'workdir', 'path') if c in cols), None)
        if dcol:
            for sid, title, d in oc.execute(
                    f'SELECT id, title, {dcol} FROM session WHERE {dcol} LIKE ?', (like,)):
                s = sessions.setdefault(sid, {'id': sid, 'title': title, 'dirs': set(), 'src': set()})
                s['src'].add('OC')
                if d:
                    s['dirs'].add(d)
        oc.close()
    except Exception as e:
        print(f'! opencode.db: {e}', file=sys.stderr)

    # --- recall.db: text_idx ---
    try:
        rc = sqlite3.connect(f'file:{RECALL_DB.replace(chr(92), "/")}?mode=ro', uri=True)
        rc.execute('PRAGMA query_only=1')
        for sid, title, d, cnt, tmin, tmax in rc.execute(
                'SELECT session_id, session_title, directory, COUNT(*), '
                'MIN(time_created), MAX(time_created) FROM text_idx '
                'WHERE directory LIKE ? GROUP BY session_id', (like,)):
            s = sessions.setdefault(sid, {'id': sid, 'title': title, 'dirs': set(), 'src': set()})
            s['src'].add('recall')
            s['parts'] = cnt
            s['first'], s['last'] = fmt(tmin), fmt(tmax)
            if d:
                s['dirs'].add(d)
        rc.close()
    except Exception as e:
        print(f'! recall.db: {e}', file=sys.stderr)

    rows = sorted(sessions.values(),
                  key=lambda x: -(x.get('parts') or 0))
    if args.json:
        for r in rows:
            r['dirs'] = sorted(r['dirs'])
            r['src'] = sorted(r['src'])
        print(json.dumps(rows, ensure_ascii=False, indent=1))
        return

    print(f'文件夹: {args.folder}')
    print(f'关联对话: {len(rows)} 个\n')
    for r in rows:
        src = '+'.join(r['src'])
        parts = r.get('parts', '-')
        span = f"{r.get('first', '?')} ~ {r.get('last', '?')}" if 'parts' in r else '-'
        d = ' | '.join(sorted(r['dirs']))[:110]
        print(f"[{src}] ({parts} 段, {span}) {r['title']}")
        print(f"    id: {r['id']}")
        print(f"    目录: {d}\n")


if __name__ == '__main__':
    main()
