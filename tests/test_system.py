import hashlib
import json
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from observatory import app as module
from observatory.store import Store, StorageFull, Busy, dumps


@pytest.fixture
def client(tmp_path,monkeypatch):
    storage=Store(tmp_path/'test.sqlite')
    monkeypatch.setattr(module,'store',storage)
    with TestClient(module.app) as client:
        yield client,storage


def wait_projection(storage):
    end=time.monotonic()+10
    while time.monotonic()<end:
        if storage.fatal:raise AssertionError(storage.fatal)
        if storage.read("SELECT COUNT(*) n FROM inbox WHERE status='pending'")[0]['n']==0:return
        time.sleep(.02)
    raise AssertionError('projection did not drain')


def node(revision=1,name='星体',parent=0,tid=1,op='create'):
    return {'kind':'types','test_id':'test','revision':revision,'changes':[{'op':op,'node':{'type_id':tid,'parent_id':parent,'name':name}}]}


def obj(i=0,**overrides):
    result={'id':f'obj-{i}','sequence':i,'type_id':1,'name':f'恒星 🌏 #{i}','timestamp_ms':1792627200000,
        'position':{'frame_id':'galaxy-0','frame_kind':'galactic','unit':'ly','x':1,'y':2,'z':0},
        'attributes':{'source':'华东站','tags':['零',False],'null':None,'active':False,'nested':{'quoted':'"中文"\nC:\\data'},'mass':i}}
    result.update(overrides)
    return result


def batch(objects,revision=1,batch_id=0):
    return {'kind':'objects','test_id':'test','tree_revision':revision,'batch_id':batch_id,'objects':objects}


def post(client,msg):
    raw=dumps(msg).encode()
    response=client.post('/ingest',content=raw,headers={'X-Flightgen-Test-Id':'test','Content-Type':'application/json'})
    assert response.status_code==202,response.text
    return raw,response.json()


def test_durable_ack_exact_bytes_and_unicode(client):
    c,s=client
    post(c,node());raw,receipt=post(c,batch([obj()]))
    # The ACK implies the complete original message is already readable on a second connection.
    row=s.read('SELECT * FROM inbox WHERE id=?',(receipt['receipt_id'],))[0]
    assert zlib.decompress(row['body'])==raw
    assert receipt['sha256']==hashlib.sha256(raw).hexdigest()
    wait_projection(s)
    detail=c.get('/api/objects/obj-0?run=test').json()
    assert detail['data']==obj()
    assert c.get(f"/api/receipts/{receipt['receipt_id']}?raw=true").content==raw
    assert c.get('/healthz').status_code==200


def test_concurrent_redelivery_is_idempotent(client):
    c,s=client;post(c,node())
    data=batch([obj(i) for i in range(25)])
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:post(c,data)[1],range(20)))
    assert len({r['receipt_id'] for r in results})==1
    assert sum(not r['duplicate'] for r in results)==1
    wait_projection(s)
    stats=c.get('/api/stats').json()
    assert stats['objects']==25
    assert stats['counters']['duplicates']==19
    # Logical object dedupe also survives reserialization / another batch envelope.
    post(c,batch([obj(0)],batch_id=10));wait_projection(s)
    assert c.get('/api/stats').json()['counters']['object_duplicates']==1
    # Key ordering is not a change in object content.
    reordered=dict(reversed(list(obj(0).items())))
    reordered['attributes']=dict(reversed(list(reordered['attributes'].items())))
    response=c.post('/ingest',json=batch([reordered],batch_id=11))
    assert response.status_code==202
    wait_projection(s)
    assert c.get('/api/stats').json()['counters']['object_duplicates']==2
    assert c.get('/api/stats').json()['counters']['conflicts']==0


def test_revision_history_out_of_order_and_descendant_queries(client):
    c,s=client
    post(c,node());post(c,node(tid=2,parent=1,name='子类'))
    post(c,node(revision=3,name='恒星（新名）',op='rename'))
    post(c,node(revision=2,name='恒星（旧名）',op='rename'))
    post(c,batch([obj(type_id=2)],revision=1))
    wait_projection(s)
    assert c.get('/api/types?run=test').json()['types'][0]['name']=='恒星（新名）'
    assert c.get('/api/types?run=test&revision=2').json()['types'][0]['name']=='恒星（旧名）'
    assert len(c.get('/api/objects?run=test&type_id=1').json()['items'])==1
    detail=c.get('/api/objects/obj-0?run=test').json()
    assert detail['observed_types'][0]['name']=='星体'


def test_poison_messages_partial_batch_and_id_conflicts(client):
    c,s=client
    assert c.post('/ingest',content=b'{broken',headers={'X-Flightgen-Test-Id':'test'}).status_code==202
    post(c,batch([obj(),obj(1,type_id='bad'),obj(2,attributes=[])]))
    post(c,batch([obj(name='conflict')],batch_id=2))
    wait_projection(s)
    stats=c.get('/api/stats').json()
    assert stats['objects']==1
    assert stats['statuses']['quarantined']==1
    assert stats['statuses']['partial']==1
    assert stats['counters']['invalid_objects']==2
    assert stats['counters']['conflicts']==1
    assert c.get('/api/objects/obj-0?run=test').json()['data']['name']==obj()['name']
    assert len(c.get('/api/quality?run=test').json()['issues'])==3


def test_typed_numeric_filters_cursor_and_schema_drift(client):
    c,s=client;post(c,node())
    objects=[obj(i) for i in range(6)];objects[2]['attributes']['mass']='200'
    post(c,batch(objects));wait_projection(s)
    first=c.get('/api/objects?run=test&limit=2').json()
    second=c.get('/api/objects',params={'run':'test','limit':2,'cursor':first['next_cursor']}).json()
    assert {x['id'] for x in first['items']}.isdisjoint({x['id'] for x in second['items']})
    numeric=c.get('/api/objects?run=test&attr=mass&minimum=2').json()['items']
    assert {o['id'] for o in numeric}=={'obj-3','obj-4','obj-5'}
    fields=c.get('/api/fields?run=test').json()
    assert {f['kind'] for f in fields if f['path']=='mass'}=={'number','string'}
    assert c.get('/api/objects?run=test&attr=x%22).attack').status_code==400


def test_coordinate_frames_units_and_missing_positions(client):
    c,s=client
    a=obj();b=obj(1);b['position']['unit']='km';d=obj(3);d['position']['frame_id']='other'
    post(c,batch([a,b,obj(2,position=None),d]));wait_projection(s)
    points=c.get('/api/map?run=test&frame=galaxy-0').json()
    assert len(points['points'])==1
    assert len(c.get('/api/frames?run=test').json())==3
    assert c.get('/api/stats').json()['objects']==4


def test_cross_run_isolation_and_header_mismatch(client):
    c,s=client;post(c,batch([obj()]))
    message=batch([obj()]);message['test_id']='second'
    assert c.post('/ingest',json=message).status_code==202
    c.post('/ingest',json=node(),headers={'X-Flightgen-Test-Id':'wrong'})
    wait_projection(s)
    assert c.get('/api/stats').json()['objects']==2
    assert len(c.get('/api/objects?run=second').json()['items'])==1
    assert s.read("SELECT COUNT(*) n FROM inbox WHERE status='quarantined'")[0]['n']==1


def test_pending_inbox_recovered_after_restart(tmp_path):
    path=tmp_path/'restart.sqlite';s=Store(path)
    raw=dumps(batch([obj()])).encode()
    with s.connect() as db:
        db.execute('INSERT INTO inbox(digest,run,received,body,headers,bytes) VALUES(?,?,?,?,?,?)',(hashlib.sha256(raw).hexdigest(),'test',time.time(),zlib.compress(raw), '{}',len(raw)))
    recovered=Store(path);recovered.start()
    try:
        wait_projection(recovered)
        assert recovered.read('SELECT COUNT(*) n FROM objects')[0]['n']==1
    finally:recovered.stop()
    again=Store(path);again.start()
    try:
        wait_projection(again)
        assert again.read('SELECT COUNT(*) n FROM objects')[0]['n']==1
    finally:again.stop()


def test_disk_budget_returns_no_success_ack(tmp_path):
    s=Store(tmp_path/'full.sqlite',disk_budget_mb=1)
    with pytest.raises(StorageFull):s.submit(b'{}',{})


def test_admission_queue_is_bounded(tmp_path):
    s=Store(tmp_path/'queue.sqlite')
    for _ in range(128):s.submit(b'{}',{})
    with pytest.raises(Busy):s.submit(b'{}',{})


def test_http_bounds_and_static_assets(client):
    c,_=client
    assert c.post('/ingest',content=b'').status_code==400
    assert c.post('/ingest',content=b'x'*(2*1024*1024+1)).status_code==413
    assert c.get('/api/objects?limit=100000').status_code==422
    assert c.get('/api/generator/evil').status_code==404
    assert c.get('/').status_code==200
    assert c.get('/assets/app.js').status_code==200
