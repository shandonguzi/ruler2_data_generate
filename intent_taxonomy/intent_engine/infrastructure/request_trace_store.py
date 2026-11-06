"""Rebuildable, incremental index for request logs; JSONL remains the source of truth."""
import hashlib
import json
import sqlite3
import threading
from pathlib import Path

from intent_engine.infrastructure.request_trace_tree import cluster_context, cluster_tree

_LOCK = threading.RLock()


def _values(record):
    values = set()

    def collect(value):
        if isinstance(value, str):
            values.add(value)
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    payload = record.get('input', {})
    collect(payload.get('input', []))
    for message in payload.get('messages', []):
        if message.get('role') == 'user':
            content = message.get('content', '')
            try:
                collect(json.loads(content))
            except (ValueError, TypeError):
                collect(content)
    return values


def _digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _sync(db, path):
    stat = path.stat()
    identity = [stat.st_dev, stat.st_ino]
    state_row = db.execute('SELECT value FROM state').fetchone()
    state = json.loads(state_row[0]) if state_row else {}
    if state.get('signature') == [*identity, stat.st_size, stat.st_mtime_ns]:
        return
    position = state.get('position', 0)
    # A replaced/truncated/rewritten file invalidates byte offsets and cached trees.
    if state.get('identity') != identity or stat.st_size <= state.get('size', 0):
        db.execute('DELETE FROM records')
        db.execute('DELETE FROM terms')
        position = 0
    db.execute('DELETE FROM records WHERE position >= ?', (position,))
    db.execute('DELETE FROM terms WHERE position >= ?', (position,))
    db.execute('DELETE FROM trees')
    with path.open('rb') as stream:
        stream.seek(position)
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line:
                break
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                if not line.endswith(b'\n'):
                    break  # Retry a partially written last line on the next update.
                position = stream.tell()
                continue
            if not isinstance(record, dict):
                position = stream.tell()
                continue
            context = cluster_context(record)
            started = record.get('started_at')
            started = started if isinstance(started, (int, float)) else 0
            db.execute('INSERT INTO records VALUES (?, ?, ?, ?, ?, ?)', (
                start, len(line), record.get('stage_id', '__unassigned__'), started,
                context['cluster_id'] if context else None,
                json.dumps(context) if context else None))
            db.executemany('INSERT INTO terms VALUES (?, ?)', ((start, _digest(v)) for v in _values(record)))
            # A complete JSON object without a newline is readable, but re-index it
            # if its line is extended later (historical logs may omit the newline).
            position = stream.tell() if line.endswith(b'\n') else start
            if not line.endswith(b'\n'):
                break
    db.execute('DELETE FROM state')
    db.execute('INSERT INTO state VALUES (?)', (json.dumps({
        'identity': identity, 'size': stat.st_size, 'position': position,
        'signature': [*identity, stat.st_size, stat.st_mtime_ns]}),))


def _connect(path):
    index = Path(str(path) + '.sqlite3')
    if index.is_symlink():
        raise ValueError('请求索引不能是符号链接')
    db = sqlite3.connect(index, timeout=30)
    db.executescript('''
        CREATE TABLE IF NOT EXISTS state (value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS records (
            position INTEGER PRIMARY KEY, length INTEGER, stage TEXT,
            started REAL, cluster INTEGER, context TEXT);
        CREATE INDEX IF NOT EXISTS records_recent ON records(started DESC, position DESC);
        CREATE INDEX IF NOT EXISTS records_stage ON records(stage, started DESC, position DESC);
        CREATE INDEX IF NOT EXISTS records_cluster ON records(stage, cluster);
        CREATE TABLE IF NOT EXISTS terms (position INTEGER, value TEXT, PRIMARY KEY(position, value));
        CREATE INDEX IF NOT EXISTS terms_value ON terms(value, position);
        CREATE TABLE IF NOT EXISTS trees (key TEXT PRIMARY KEY, value TEXT);
    ''')
    return db


def prepare_request_index(path):
    """Called after appending a trace so payload parsing happens before inspection."""
    path = Path(path)
    with _LOCK:
        try:
            db = _connect(path)
            try:
                with db:
                    _sync(db, path)
            finally:
                db.close()
        except (sqlite3.Error, OSError):
            # Original JSONL is already durable; a later read can catch up.
            import logging
            logging.getLogger(__name__).exception('Unable to update request index: %s', path)


def read_indexed_requests(path, *, stage='', offset=0, terms=(), view='list', cluster=None):
    path = Path(path)
    if not path.exists():
        return cluster_tree([], offset) if view == 'tree' else {'total': 0, 'records': []}
    with _LOCK:
        db = _connect(path)
        try:
            with db:
                _sync(db, path)
                clauses, args = [], []
                if stage:
                    clauses.append('stage = ?')
                    args.append(stage)
                if cluster is not None:
                    clauses.append('cluster = ?')
                    args.append(cluster)
                if terms:
                    clauses.append('position IN (SELECT position FROM terms WHERE value IN (' + ','.join('?' for _ in terms) + '))')
                    args.extend(_digest(term) for term in terms)
                where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
                start = max(0, offset)
                if view == 'tree':
                    key = json.dumps([stage, cluster, sorted(set(terms))])
                    cached = db.execute('SELECT value FROM trees WHERE key = ?', (key,)).fetchone()
                    if cached:
                        result = json.loads(cached[0])
                    else:
                        metadata = [{'_cluster_context': json.loads(ctx) if ctx else None, 'started_at': when}
                                    for ctx, when in db.execute('SELECT context, started FROM records' + where + ' ORDER BY position', args)]
                        result = cluster_tree(metadata, precomputed=True, page_size=None)
                        db.execute('INSERT OR REPLACE INTO trees VALUES (?, ?)', (key, json.dumps(result)))
                    result['nodes'] = result['nodes'][start:start + 10]
                    return result
                total = db.execute('SELECT COUNT(*) FROM records' + where, args).fetchone()[0]
                rows = db.execute('SELECT position, length FROM records' + where +
                                  ' ORDER BY started DESC, position DESC LIMIT 10 OFFSET ?', [*args, start]).fetchall()
                records = []
                with path.open('rb') as stream:
                    for position, length in rows:
                        stream.seek(position)
                        records.append(json.loads(stream.read(length)))
                return {'total': total, 'records': records}
        finally:
            db.close()
