"""Kill a container after a real ACK, restart, replay, and verify every byte."""
import concurrent.futures
import hashlib
import json
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE='http://127.0.0.1:8002'
CONTAINER='exam-atlas-arm64'
run='crash-'+str(int(time.time()))
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
payloads=[]
for batch in range(24):
    objects=[{'id':f'{run}-{batch*100+i}','sequence':batch*100+i,'type_id':1,'name':f'恢复测试 {batch*100+i}',
        'timestamp_ms':1792627200000,'position':None,'attributes':{'value':i,'null':None,'flag':False,'nested':{'text':'原文🌏\nC:\\data'}}} for i in range(100)]
    payloads.append(json.dumps({'kind':'objects','test_id':run,'batch_id':batch,'tree_revision':1,'objects':objects},ensure_ascii=False).encode())

def send(i):
    req=urllib.request.Request(BASE+'/ingest',data=payloads[i],headers={'X-Flightgen-Test-Id':run,'Content-Type':'application/json'})
    with opener.open(req,timeout=15) as r:return i,json.load(r)

acknowledged={};failed=[];killed=False
with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
    futures={pool.submit(send,i):i for i in range(len(payloads))}
    for future in concurrent.futures.as_completed(futures):
        try:
            i,receipt=future.result();acknowledged[i]=receipt
            if not killed:
                subprocess.run(['docker','kill','--signal','KILL',CONTAINER],check=True,capture_output=True)
                killed=True
        except Exception:failed.append(futures[future])
assert killed and acknowledged
before_restart=len(acknowledged)
subprocess.run(['docker','start',CONTAINER],check=True,capture_output=True)
for _ in range(100):
    try:
        with opener.open(BASE+'/healthz',timeout=2) as r:
            if r.status==200:break
    except Exception:time.sleep(.2)
else:raise AssertionError('container did not recover')

# Replay ALL messages, including those whose response reached the caller before KILL.
replayed={}
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
    for i,receipt in pool.map(send,range(len(payloads))):replayed[i]=receipt
for i,receipt in acknowledged.items():
    assert replayed[i]['receipt_id']==receipt['receipt_id']
    assert replayed[i]['duplicate'] is True
for _ in range(300):
    with opener.open(BASE+'/api/stats',timeout=5) as r:stats=json.load(r)
    if not stats['statuses'].get('pending',0):break
    time.sleep(.2)
else:raise AssertionError('recovery backlog did not drain')
record=next(r for r in stats['runs'] if r['id']==run)
assert record['objects']==2400,record
for i,receipt in replayed.items():
    with opener.open(BASE+f"/api/receipts/{receipt['receipt_id']}?raw=true",timeout=5) as r:raw=r.read()
    assert raw==payloads[i]
    assert hashlib.sha256(raw).hexdigest()==receipt['sha256']
result={'test_id':run,'signal':'SIGKILL','acknowledged_before_kill':before_restart,'requests_interrupted':len(failed),
    'replayed_messages':len(replayed),'objects_after_recovery':record['objects'],'expected_objects':2400,
    'acknowledged_receipts_preserved':True,'all_raw_bytes_verified':True,'duplicate_replay_no_double_count':True,'passed':True}
Path('docs/results').mkdir(parents=True,exist_ok=True)
Path('docs/results/crash-recovery.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
