import asyncio
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
import zlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .store import Store, Busy, StorageFull, dumps

store = Store(os.environ.get('ATLAS_DB', 'data/atlas.sqlite'), int(os.environ.get('ATLAS_DISK_BUDGET_MB','18000')))
WEB=Path(__file__).resolve().parent.parent / 'web'
MAX_BODY=2*1024*1024


@asynccontextmanager
async def lifespan(app):
    store.start()
    yield
    await asyncio.to_thread(store.stop)


app=FastAPI(title='Atlas · 宇宙万物观测站',version='1.0.0',lifespan=lifespan,docs_url=None,redoc_url=None)


@app.middleware('http')
async def security_headers(request, call_next):
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='same-origin'
    response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
    return response


@app.exception_handler(sqlite3.OperationalError)
async def query_error(request, exc):
    return JSONResponse({'detail':'查询超过资源或时间限制，请缩小筛选范围'},status_code=503)


@app.get('/healthz')
def health():
    if store.fatal or not store.thread or not store.thread.is_alive(): raise HTTPException(503,'writer unavailable')
    return {'status':'ok','durability':'SQLite WAL synchronous=FULL','queue':store.tasks.qsize()}


@app.post('/ingest', status_code=202)
async def ingest(request:Request):
    length=request.headers.get('content-length')
    if length and (not length.isdigit() or int(length)>MAX_BODY): raise HTTPException(413,'maximum message size is 2 MiB')
    chunks=bytearray()
    async for chunk in request.stream():
        if len(chunks)+len(chunk)>MAX_BODY: raise HTTPException(413,'maximum message size is 2 MiB')
        chunks.extend(chunk)
    if not chunks: raise HTTPException(400,'empty body')
    headers={k:v[:1000] for k,v in request.headers.items() if k.startswith('x-flightgen-') or k=='content-type'}
    try:
        future=store.submit(bytes(chunks),headers)
        return await asyncio.shield(asyncio.wrap_future(future))
    except Busy as exc: raise HTTPException(429,str(exc),headers={'Retry-After':'1'})
    except StorageFull as exc: raise HTTPException(507,str(exc))
    except sqlite3.Error: raise HTTPException(503,'durable write failed; retry')


def selected_run(run):
    if run: return run
    rows=store.read('SELECT id FROM runs ORDER BY last_seen DESC LIMIT 1')
    return rows[0]['id'] if rows else ''


@app.get('/api/stats')
def stats(): return store.stats()


@app.get('/api/types')
def types(run:Optional[str]=None, revision:Optional[int]=Query(None,ge=1)):
    run=selected_run(run)
    if revision:
        rows=store.read('SELECT h.type_id,h.parent_id,h.name,h.revision,COALESCE(c.count,0) count FROM type_history h JOIN (SELECT type_id,MAX(revision) revision FROM type_history WHERE run=? AND revision<=? GROUP BY type_id) x ON h.type_id=x.type_id AND h.revision=x.revision LEFT JOIN type_counts c ON c.run=h.run AND c.type_id=h.type_id WHERE h.run=? ORDER BY h.type_id',(run,revision,run))
    else:
        rows=store.read('SELECT t.*,COALESCE(c.count,0) count FROM types t LEFT JOIN type_counts c ON c.run=t.run AND c.type_id=t.type_id WHERE t.run=? ORDER BY t.type_id',(run,))
    return {'run':run,'types':rows,'history':store.read('SELECT type_id,revision,parent_id,name,op FROM type_history WHERE run=? ORDER BY revision DESC,type_id DESC LIMIT 80',(run,))}


def filters(run,type_id,q,anomaly,frame,attr,value,minimum,maximum):
    clauses=['o.run=?']; args=[run]
    if type_id is not None:
        clauses.append('o.type_id IN (WITH RECURSIVE children(id) AS (SELECT ? UNION SELECT t.type_id FROM types t JOIN children c ON t.parent_id=c.id WHERE t.run=?) SELECT id FROM children)')
        args.extend([type_id,run])
    if q:
        clauses.append("(o.name LIKE ? ESCAPE '\\' OR o.id LIKE ? ESCAPE '\\')")
        needle='%'+q.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        args.extend([needle,needle])
    if anomaly: clauses.append('o.anomaly=1')
    if frame: clauses.append('o.frame=?'); args.append(frame)
    if attr:
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,5}',attr): raise HTTPException(400,'invalid attribute path')
        path='$.attributes.'+attr
        if value is not None:
            clauses.append('CAST(json_extract(o.data,?) AS TEXT)=?'); args.extend([path,value])
        if minimum is not None or maximum is not None:
            clauses.append("json_type(o.data,?) IN ('integer','real')"); args.append(path)
            if minimum is not None: clauses.append('json_extract(o.data,?)>=?');args.extend([path,minimum])
            if maximum is not None: clauses.append('json_extract(o.data,?)<=?');args.extend([path,maximum])
    return ' AND '.join(clauses),args


@app.get('/api/objects')
def objects(run:Optional[str]=None,type_id:Optional[int]=Query(None,ge=1),q:str=Query('',max_length=100),anomaly:bool=False,
    frame:Optional[str]=None,cursor:Optional[int]=Query(None,ge=1),limit:int=Query(50,ge=1,le=100),
    attr:Optional[str]=None,value:Optional[str]=None,minimum:Optional[float]=None,maximum:Optional[float]=None):
    run=selected_run(run)
    where,args=filters(run,type_id,q,anomaly,frame,attr,value,minimum,maximum)
    if cursor: where+=' AND o.oid<?'; args.append(cursor)
    rows=store.read(f'SELECT o.oid,o.id,o.name,o.type_id,o.timestamp_ms,o.tree_revision,o.frame,o.unit,o.source,o.anomaly,t.name type_name FROM objects o LEFT JOIN types t ON t.run=o.run AND t.type_id=o.type_id WHERE {where} ORDER BY o.oid DESC LIMIT ?',(*args,limit+1))
    return {'run':run,'items':rows[:limit],'next_cursor':rows[limit-1]['oid'] if len(rows)>limit else None}


@app.get('/api/objects/{object_id:path}')
def object_detail(object_id:str,run:Optional[str]=None):
    run=selected_run(run)
    rows=store.read('SELECT * FROM objects WHERE run=? AND id=?',(run,object_id))
    if not rows: raise HTTPException(404,'object not found')
    row=rows[0]; row['data']=json.loads(row['data'])
    row['observed_types']=types(run,row['tree_revision'])['types']
    row['receipt']=store.read('SELECT id,digest,received,bytes,status FROM inbox WHERE id=?',(row['inbox_id'],))[0]
    return row


@app.get('/api/frames')
def frames(run:Optional[str]=None):
    return store.read('SELECT frame,unit,count FROM frames WHERE run=? ORDER BY count DESC',(selected_run(run),))


@app.get('/api/map')
def star_map(run:Optional[str]=None,frame:Optional[str]=None,type_id:Optional[int]=None,limit:int=Query(2500,ge=1,le=5000)):
    run=selected_run(run)
    if not frame:
        options=frames(run)
        if not options:return {'points':[],'frame':None,'unit':None,'sampled':True}
        frame=options[0]['frame']
    # Never project incompatible coordinate frames/units onto the same map.
    options=store.read('SELECT unit,count FROM frames WHERE run=? AND frame=? ORDER BY count DESC LIMIT 1',(run,frame))
    unit=options[0]['unit'] if options else None
    where,args=filters(run,type_id,'',False,frame,None,None,None,None)
    rows=store.read(f'SELECT o.id,o.name,o.type_id,o.x,o.y,o.z,o.anomaly FROM objects o WHERE {where} AND o.unit=? ORDER BY o.oid DESC LIMIT ?',(*args,unit,limit))
    return {'points':rows,'frame':frame,'unit':unit,'sampled':True,'population':options[0]['count'] if options else 0}


@app.get('/api/fields')
def fields(run:Optional[str]=None,type_id:Optional[int]=None):
    run=selected_run(run)
    return store.read('SELECT path,kind,SUM(count) count,MIN(example) example FROM fields WHERE run=?'+(' AND type_id=?' if type_id else '')+' GROUP BY path,kind ORDER BY path,kind LIMIT 600',(run,type_id) if type_id else (run,))


@app.get('/api/quality')
def quality(run:Optional[str]=None):
    run=selected_run(run)
    return {'messages':store.read("SELECT id,digest,kind,received,bytes,status,error,objects FROM inbox WHERE run=? AND status IN ('partial','quarantined') ORDER BY id DESC LIMIT 80",(run,)),
        'issues':store.read('SELECT * FROM issues WHERE run=? ORDER BY id DESC LIMIT 100',(run,)),
        'recent':store.read('SELECT id,kind,received,bytes,status,objects,error FROM inbox WHERE run=? ORDER BY id DESC LIMIT 30',(run,))}


@app.get('/api/receipts/{receipt_id}')
def receipt(receipt_id:int,raw:bool=False):
    rows=store.read('SELECT * FROM inbox WHERE id=?',(receipt_id,))
    if not rows: raise HTTPException(404,'receipt not found')
    row=rows[0]
    body=zlib.decompress(row.pop('body'))
    if raw:return Response(body,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="message-{receipt_id}.json"'})
    row['headers']=json.loads(row['headers']);row['body']=body.decode('utf-8',errors='replace')
    return row


@app.post('/api/demo')
async def demo():
    from .demo import messages
    run='demo-'+time.strftime('%Y%m%d-%H%M%S')
    for msg in messages(run):
        try:await asyncio.wrap_future(store.submit(dumps(msg).encode(),{'x-flightgen-test-id':run,'x-flightgen-kind':msg['kind']}))
        except (Busy,StorageFull) as exc:raise HTTPException(503,str(exc))
    return {'run':run,'demo':True}


@app.get('/api/generator/{action}',operation_id='generator_read')
@app.post('/api/generator/{action}',operation_id='generator_control')
async def generator(action:str,request:Request):
    if action not in ('status','start','stop','rate','receipt'): raise HTTPException(404,'unsupported generator action')
    if request.method != ('GET' if action in ('status','receipt') else 'POST'): raise HTTPException(405,'incorrect method')
    base=os.environ.get('ATLAS_GENERATOR_URL','http://127.0.0.1:9001').rstrip('/')
    body=await request.body() if request.method=='POST' else None
    if body and len(body)>16384:raise HTTPException(413,'control payload too large')
    def call():
        req=urllib.request.Request(base+'/'+action,data=body,method=request.method,headers={'Content-Type':'application/json'})
        # Local container traffic must not pass through system proxies.
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req,timeout=8) as result:return Response(result.read(),status_code=result.status,media_type='application/json')
        except urllib.error.HTTPError as exc:return Response(exc.read(),status_code=exc.code,media_type='application/json')
        except (urllib.error.URLError,TimeoutError,OSError):raise HTTPException(503,'无法连接发生器。请按 README 启动端口 9001 的发生器容器。')
    return await asyncio.to_thread(call)


@app.get('/')
def index():return FileResponse(WEB/'index.html')


@app.get('/docs',include_in_schema=False)
def api_docs():return FileResponse(WEB/'api.html')


app.mount('/assets',StaticFiles(directory=WEB),name='assets')
