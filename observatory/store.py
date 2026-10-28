"""Durable inbox + restartable projection. SQLite has exactly one writer."""
import concurrent.futures
import hashlib
import json
import logging
import math
import os
import queue
import sqlite3
import threading
import time
import zlib
from collections import Counter, deque
from pathlib import Path

log = logging.getLogger(__name__)

DDL = """
CREATE TABLE IF NOT EXISTS inbox(
 id INTEGER PRIMARY KEY, digest TEXT NOT NULL UNIQUE, run TEXT NOT NULL,
 kind TEXT, received REAL NOT NULL, body BLOB NOT NULL, headers TEXT NOT NULL,
 bytes INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending', error TEXT,
 objects INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(status,id);
CREATE INDEX IF NOT EXISTS inbox_run ON inbox(run,id);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, first_seen REAL, last_seen REAL,
 objects INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS types(
 run TEXT NOT NULL, type_id INTEGER NOT NULL, parent_id INTEGER NOT NULL,
 name TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(run,type_id));
CREATE TABLE IF NOT EXISTS type_history(
 run TEXT NOT NULL, type_id INTEGER NOT NULL, revision INTEGER NOT NULL,
 parent_id INTEGER NOT NULL, name TEXT NOT NULL, op TEXT NOT NULL,
 inbox_id INTEGER NOT NULL, PRIMARY KEY(run,type_id,revision));
CREATE TABLE IF NOT EXISTS objects(
 oid INTEGER PRIMARY KEY, run TEXT NOT NULL, id TEXT NOT NULL, sequence INTEGER NOT NULL,
 type_id INTEGER NOT NULL, name TEXT NOT NULL, timestamp_ms INTEGER NOT NULL,
 tree_revision INTEGER NOT NULL, frame TEXT, unit TEXT, x REAL, y REAL, z REAL,
 source TEXT, anomaly INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL,
 inbox_id INTEGER NOT NULL, UNIQUE(run,id));
CREATE INDEX IF NOT EXISTS objects_type ON objects(run,type_id,oid);
CREATE INDEX IF NOT EXISTS objects_frame ON objects(run,frame,oid);
CREATE INDEX IF NOT EXISTS objects_anomaly ON objects(run,anomaly,oid);
CREATE TABLE IF NOT EXISTS type_counts(run TEXT,type_id INTEGER,count INTEGER NOT NULL,PRIMARY KEY(run,type_id));
CREATE TABLE IF NOT EXISTS frames(run TEXT,frame TEXT,unit TEXT,count INTEGER NOT NULL,PRIMARY KEY(run,frame,unit));
CREATE TABLE IF NOT EXISTS fields(
 run TEXT NOT NULL, type_id INTEGER NOT NULL, path TEXT NOT NULL, kind TEXT NOT NULL,
 count INTEGER NOT NULL, example TEXT, PRIMARY KEY(run,type_id,path,kind));
CREATE TABLE IF NOT EXISTS issues(
 id INTEGER PRIMARY KEY, inbox_id INTEGER, run TEXT, object_id TEXT, reason TEXT);
CREATE TABLE IF NOT EXISTS counters(key TEXT PRIMARY KEY,value INTEGER NOT NULL);
INSERT OR IGNORE INTO counters VALUES('requests',0),('duplicates',0),('bytes',0),
 ('invalid_objects',0),('object_duplicates',0),('conflicts',0);
PRAGMA user_version=1;
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False, sort_keys=True)


def integer(value, minimum=0):
    return isinstance(value, int) and not isinstance(value, bool) and minimum <= value <= 2**63 - 1


def field_kind(value):
    if value is None: return 'null'
    if isinstance(value, bool): return 'boolean'
    if isinstance(value, (float, int)): return 'number'
    if isinstance(value, str): return 'string'
    if isinstance(value, list): return 'array'
    return 'object'


def flatten(value, prefix='', depth=0):
    for key, item in value.items():
        path = f'{prefix}.{key}' if prefix else key
        yield path, field_kind(item), item
        if isinstance(item, dict) and depth < 2:
            yield from flatten(item, path, depth + 1)


class StorageFull(Exception): pass
class Busy(Exception): pass


class Store:
    def __init__(self, path, disk_budget_mb=18000):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.disk_budget = disk_budget_mb * 1024 * 1024
        self.tasks = queue.Queue(maxsize=128)
        self.stopping = threading.Event()
        self.thread = None
        self.latencies = deque(maxlen=1000)
        self.series = deque(maxlen=90)
        self.fatal = None
        with self.connect() as db:
            db.executescript(DDL)

    def connect(self, readonly=False):
        if readonly:
            db = sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
            db.execute('PRAGMA query_only=ON')
        else:
            db = sqlite3.connect(self.path, timeout=10)
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA cache_size=-32768')
            db.execute('PRAGMA journal_size_limit=67108864')
            # Hard ceiling for main DB; leave budget headroom for WAL and metadata.
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            db.execute('PRAGMA max_page_count='+str(max(256,(self.disk_budget-128*1024*1024)//page_size)))
        db.row_factory = sqlite3.Row
        return db

    def read(self, sql, args=()):
        with self.connect(True) as db:
            # Interactive scans cannot monopolize a CPU indefinitely.
            deadline = time.monotonic() + 2
            db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
            return [dict(row) for row in db.execute(sql, args)]

    def disk_bytes(self):
        return sum(p.stat().st_size for p in [Path(self.path), Path(self.path + '-wal'), Path(self.path + '-shm')] if p.exists())

    def start(self):
        self.thread = threading.Thread(target=self._writer, name='atlas-writer', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopping.set()
        if self.thread: self.thread.join(timeout=25)

    def submit(self, body, headers):
        if self.fatal or self.stopping.is_set(): raise Busy('writer unavailable')
        if self.disk_bytes() + len(body) * 5 + 16 * 1024 * 1024 > self.disk_budget:
            raise StorageFull('persistent storage budget reached')
        future = concurrent.futures.Future()
        try: self.tasks.put_nowait((body, headers, future, time.perf_counter()))
        except queue.Full: raise Busy('ingest queue full; retry later')
        return future

    @staticmethod
    def increment(db, key, count=1):
        db.execute('UPDATE counters SET value=value+? WHERE key=?', (count, key))

    def _writer(self):
        db = self.connect()
        last_tick = 0
        try:
            while not self.stopping.is_set() or not self.tasks.empty():
                batch = []
                try:
                    batch.append(self.tasks.get(timeout=0.015))
                    until = time.perf_counter() + 0.006
                    while len(batch) < 32 and time.perf_counter() < until:
                        try: batch.append(self.tasks.get_nowait())
                        except queue.Empty: break
                except queue.Empty: pass
                if batch:
                    results = []
                    try:
                        db.execute('BEGIN IMMEDIATE')
                        for body, headers, future, started in batch:
                            digest = hashlib.sha256(body).hexdigest()
                            run = headers.get('x-flightgen-test-id', 'unknown')[:200]
                            cur = db.execute('INSERT OR IGNORE INTO inbox(digest,run,kind,received,body,headers,bytes) VALUES(?,?,?,?,?,?,?)',
                                (digest,run,headers.get('x-flightgen-kind'),time.time(),zlib.compress(body, 1),dumps(headers),len(body)))
                            duplicate = cur.rowcount == 0
                            row_id = cur.lastrowid if not duplicate else db.execute('SELECT id FROM inbox WHERE digest=?',(digest,)).fetchone()[0]
                            self.increment(db, 'requests')
                            self.increment(db, 'duplicates' if duplicate else 'bytes', 1 if duplicate else len(body))
                            results.append({'receipt_id':row_id,'sha256':digest,'duplicate':duplicate,'durable':True})
                        db.commit()
                    except Exception as exc:
                        db.rollback()
                        for _, _, future, _ in batch:
                            if not future.done(): future.set_exception(exc)
                    else:
                        for task, result in zip(batch, results):
                            self.latencies.append((time.perf_counter()-task[3])*1000)
                            if not task[2].done(): task[2].set_result(result)
                # Inbox is the persistent work queue: restart requires no replay from sender.
                pending = db.execute("SELECT id,body,headers FROM inbox WHERE status='pending' ORDER BY id LIMIT 8").fetchall()
                projection_started=time.perf_counter()
                for row in pending:
                    try:
                        db.execute('BEGIN IMMEDIATE')
                        db.execute('SAVEPOINT projection')
                        self._project(db, row)
                        db.execute('RELEASE projection')
                        db.commit()
                    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
                        db.execute('ROLLBACK TO projection')
                        db.execute("UPDATE inbox SET status='quarantined',error=? WHERE id=?",(str(exc)[:1000],row['id']))
                        db.commit()
                    except Exception:
                        db.rollback()
                        raise
                    if not self.tasks.empty() and time.perf_counter()-projection_started > .05:
                        break
                now = time.time()
                if now - last_tick >= 1:
                    counts = dict(db.execute('SELECT key,value FROM counters').fetchall())
                    total = db.execute('SELECT COALESCE(SUM(objects),0) FROM runs').fetchone()[0]
                    self.series.append({'time':now,'objects':total,'requests':counts['requests'],'queue':self.tasks.qsize()})
                    last_tick = now
        except Exception as exc:
            log.exception('writer stopped; inbox remains durable')
            self.fatal = str(exc)
            while not self.tasks.empty():
                task = self.tasks.get_nowait()
                if not task[2].done(): task[2].set_exception(exc)
        finally:
            db.close()

    def _project(self, db, row):
        body = zlib.decompress(row['body'])
        msg = json.loads(body, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite JSON number')))
        if not isinstance(msg, dict): raise ValueError('message must be a JSON object')
        run = msg.get('test_id')
        if not isinstance(run,str) or not run or len(run)>200: raise ValueError('missing or invalid test_id')
        headers = json.loads(row['headers'])
        if headers.get('x-flightgen-test-id',run) != run: raise ValueError('header/body test_id mismatch')
        kind = msg.get('kind')
        db.execute('UPDATE inbox SET run=?,kind=? WHERE id=?',(run,kind,row['id']))
        now = time.time()
        db.execute('INSERT INTO runs(id,first_seen,last_seen) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen',(run,now,now))
        if kind == 'types':
            revision, changes = msg.get('revision'), msg.get('changes')
            if not integer(revision,1) or not isinstance(changes,list): raise ValueError('invalid type revision/changes')
            for change in changes:
                if not isinstance(change,dict): raise ValueError('invalid type change')
                node, op = change.get('node'), change.get('op')
                if op not in ('create','rename','move') or not isinstance(node,dict): raise ValueError('unsupported type operation')
                tid, parent, name = node.get('type_id'), node.get('parent_id'), node.get('name')
                if not integer(tid,1) or not integer(parent) or tid==parent or not isinstance(name,str) or not name: raise ValueError('invalid type node')
                previous = db.execute('SELECT parent_id,name,op FROM type_history WHERE run=? AND type_id=? AND revision=?',(run,tid,revision)).fetchone()
                if previous and tuple(previous)!=(parent,name,op): raise ValueError('conflicting type revision')
                db.execute('INSERT OR IGNORE INTO type_history VALUES(?,?,?,?,?,?,?)',(run,tid,revision,parent,name,op,row['id']))
                db.execute('INSERT INTO types VALUES(?,?,?,?,?) ON CONFLICT(run,type_id) DO UPDATE SET parent_id=excluded.parent_id,name=excluded.name,revision=excluded.revision WHERE excluded.revision>types.revision',(run,tid,parent,name,revision))
            db.execute('UPDATE runs SET revision=MAX(revision,?) WHERE id=?',(revision,run))
            db.execute("UPDATE inbox SET status='projected' WHERE id=?",(row['id'],))
        elif kind == 'objects':
            revision, batch_id, objects = msg.get('tree_revision'), msg.get('batch_id'), msg.get('objects')
            if not integer(revision,1) or not integer(batch_id) or not isinstance(objects,list): raise ValueError('invalid object batch envelope')
            inserted, bad = 0, 0
            fields, examples, type_counts, frame_counts = Counter(), {}, Counter(), Counter()
            for obj in objects:
                error = self.validate_object(obj)
                if error:
                    bad += 1
                    db.execute('INSERT INTO issues(inbox_id,run,object_id,reason) VALUES(?,?,?,?)',(row['id'],run,str(obj.get('id',''))[:200] if isinstance(obj,dict) else '',error))
                    continue
                data = dumps(obj)
                existing = db.execute('SELECT data FROM objects WHERE run=? AND id=?',(run,obj['id'])).fetchone()
                if existing:
                    if existing[0] == data: self.increment(db,'object_duplicates')
                    else:
                        self.increment(db,'conflicts')
                        db.execute('INSERT INTO issues(inbox_id,run,object_id,reason) VALUES(?,?,?,?)',(row['id'],run,obj['id'],'conflicting object id; first observation retained'))
                    continue
                pos, attrs = obj.get('position') or {}, obj['attributes']
                anomaly = attrs.get('anomalous') is True or attrs.get('hazardous') is True or attrs.get('state') in ('failed','error','offline')
                db.execute('INSERT INTO objects(run,id,sequence,type_id,name,timestamp_ms,tree_revision,frame,unit,x,y,z,source,anomaly,data,inbox_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (run,obj['id'],obj['sequence'],obj['type_id'],obj['name'],obj['timestamp_ms'],revision,pos.get('frame_id'),pos.get('unit'),pos.get('x'),pos.get('y'),pos.get('z'),str(attrs.get('source',''))[:200],int(anomaly),data,row['id']))
                inserted += 1
                type_counts[(run,obj['type_id'])] += 1
                if pos: frame_counts[(run,pos['frame_id'],pos['unit'])] += 1
                for path, value_kind, example in flatten(attrs):
                    key=(run,obj['type_id'],path,value_kind)
                    fields[key]+=1
                    if key not in examples: examples[key]=dumps(example)[:180]
            db.executemany('INSERT INTO fields VALUES(?,?,?,?,?,?) ON CONFLICT(run,type_id,path,kind) DO UPDATE SET count=count+excluded.count',
                [(*key,count,examples[key]) for key,count in fields.items()])
            db.executemany('INSERT INTO type_counts VALUES(?,?,?) ON CONFLICT(run,type_id) DO UPDATE SET count=count+excluded.count',[(*key,count) for key,count in type_counts.items()])
            db.executemany('INSERT INTO frames VALUES(?,?,?,?) ON CONFLICT(run,frame,unit) DO UPDATE SET count=count+excluded.count',[(*key,count) for key,count in frame_counts.items()])
            db.execute('UPDATE runs SET objects=objects+? WHERE id=?',(inserted,run))
            self.increment(db,'invalid_objects',bad)
            db.execute('UPDATE inbox SET status=?,objects=?,error=? WHERE id=?',('partial' if bad else 'projected',inserted,f'{bad} invalid objects' if bad else None,row['id']))
        else: raise ValueError(f'unknown message kind: {str(kind)[:100]}')

    @staticmethod
    def validate_object(obj):
        if not isinstance(obj,dict): return 'object must be a JSON object'
        for key in ('id','name'):
            if not isinstance(obj.get(key),str) or not obj[key] or len(obj[key])>1000: return f'invalid {key}'
        for key, minimum in [('sequence',0),('type_id',1),('timestamp_ms',0)]:
            if not integer(obj.get(key),minimum): return f'invalid {key}'
        if not isinstance(obj.get('attributes'),dict): return 'attributes must be an object'
        pos = obj.get('position')
        if pos is not None:
            if not isinstance(pos,dict): return 'position must be object or null'
            if not all(isinstance(pos.get(k),str) and pos[k] for k in ('frame_id','frame_kind','unit')): return 'invalid coordinate frame'
            if not all(isinstance(pos.get(k),(int,float)) and not isinstance(pos[k],bool) and math.isfinite(pos[k]) and abs(pos[k])<1e100 for k in ('x','y','z')): return 'invalid coordinates'
        return None

    def stats(self):
        counters = {r['key']:r['value'] for r in self.read('SELECT * FROM counters')}
        runs = self.read('SELECT * FROM runs ORDER BY last_seen DESC')
        statuses = {r['status']:r['n'] for r in self.read('SELECT status,COUNT(*) n FROM inbox GROUP BY status')}
        lat = sorted(self.latencies)
        series=list(self.series)
        rate = (series[-1]['objects']-series[-2]['objects']) / max(.001,series[-1]['time']-series[-2]['time']) if len(series)>1 else 0
        return {'counters':counters,'runs':runs,'statuses':statuses,'objects':sum(r['objects'] for r in runs),
            'types':self.read('SELECT COUNT(*) n FROM types')[0]['n'],'disk_bytes':self.disk_bytes(),
            'disk_budget_bytes':self.disk_budget,'queue':self.tasks.qsize(),'writer_error':self.fatal,
            'ack_p50_ms':lat[len(lat)//2] if lat else 0,'ack_p95_ms':lat[min(len(lat)-1,int(len(lat)*.95))] if lat else 0,
            'objects_per_second':round(rate,1),'series':series}
