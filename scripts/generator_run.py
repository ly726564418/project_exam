"""Run the supplied generator, save its receipt, and measure HTTP query latency."""
import argparse
import json
import statistics
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--rate',type=int,default=1000)
parser.add_argument('--seconds',type=int,default=20)
parser.add_argument('--batch',type=int,default=100)
parser.add_argument('--workers',type=int,default=4)
parser.add_argument('--id',default='benchmark-'+str(int(time.time())))
parser.add_argument('--system',default='http://127.0.0.1:8000')
parser.add_argument('--generator',default='http://127.0.0.1:9001')
parser.add_argument('--target',default='http://host.docker.internal:8000/ingest')
parser.add_argument('--output',default='docs/results')
parser.add_argument('--container',default='')
parser.add_argument('--drain-seconds',type=int,default=600)
args=parser.parse_args()
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))

def request(url,data=None):
    req=urllib.request.Request(url,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'})
    try:
        with opener.open(req,timeout=15) as r:return json.load(r)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f'{url}: HTTP {exc.code}: '+exc.read().decode(errors='replace')) from exc

ready=False
for _ in range(60):
    try:
        request(args.system+'/healthz');ready=True;break
    except Exception:time.sleep(.5)
if not ready:raise RuntimeError('system did not become healthy')
before=request(args.system+'/api/stats')
payload={'workload':'objects','target_url':args.target,'test_id':args.id,'seed':42,'batch_size':args.batch,'workers':args.workers,'objects_per_second':args.rate,'rate_profile':'bursty','ramp_seconds':2 if args.rate else 0,'duration_seconds':args.seconds,'max_batches':50000,'retries':2,'objects':{'scenario':'evolving','tree_every_batches':10}}
request(args.generator+'/start',payload)
latencies=[];errors=[];resources=[];deadline=time.monotonic()+args.seconds+60;last_resource=0
while time.monotonic()<deadline:
    started=time.perf_counter()
    try:request(args.system+'/api/objects?run='+args.id+'&limit=50');latencies.append((time.perf_counter()-started)*1000)
    except Exception as exc:errors.append(str(exc))
    status=request(args.generator+'/status')
    if args.container and time.monotonic()-last_resource>4:
        sample=subprocess.run(['docker','stats',args.container,'exam-object-source','--no-stream','--format','{{json .}}'],capture_output=True,text=True,timeout=15)
        if sample.returncode==0:resources.extend(json.loads(line) for line in sample.stdout.splitlines() if line.strip())
        last_resource=time.monotonic()
    if status.get('state')=='finished':break
    time.sleep(.4)
else:raise RuntimeError('generator did not finish')
receipt=request(args.generator+'/receipt')
drain_started=time.monotonic()
while time.monotonic()-drain_started<args.drain_seconds:
    after=request(args.system+'/api/stats')
    if after['writer_error']:raise RuntimeError(after['writer_error'])
    if not after['statuses'].get('pending',0):break
    time.sleep(.2)
else:raise RuntimeError('projection backlog did not drain within the configured deadline')
run=next((r for r in after['runs'] if r['id']==args.id),None)
latencies.sort()
summary={'test_id':args.id,'target_rate':args.rate,'duration_seconds':args.seconds,'batch_size':args.batch,'workers':args.workers,
    'generator':receipt['stats'],'materialized_run':run,'system_counters_before':before['counters'],'system_counters_after':after['counters'],
    'statuses':after['statuses'],'disk_bytes':after['disk_bytes'],'disk_growth_bytes':after['disk_bytes']-before['disk_bytes'],
    'query_samples':len(latencies),'query_p50_ms':statistics.median(latencies) if latencies else None,
    'query_p95_ms':latencies[min(len(latencies)-1,int(len(latencies)*.95))] if latencies else None,'query_errors':errors,
    'projection_drain_seconds':time.monotonic()-drain_started,'ack_p95_ms':after['ack_p95_ms'],'resource_samples':resources}
path=Path(args.output);path.mkdir(parents=True,exist_ok=True)
(path/(args.id+'-receipt.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
(path/(args.id+'-summary.json')).write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
