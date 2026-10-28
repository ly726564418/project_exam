"""Reconcile ACK bitmap, durable raw bytes and materialized object data."""
import argparse
import base64
import hashlib
import json
import sqlite3
import sys
import zlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from observatory.store import Store, dumps

parser=argparse.ArgumentParser()
parser.add_argument('--db',default='data/atlas.sqlite')
parser.add_argument('--receipt',required=True)
args=parser.parse_args()
with open(args.receipt,encoding='utf-8') as f:receipt=json.load(f)
run=receipt['config']['test_id'];batch_size=receipt['config']['batch_size']
db=sqlite3.connect('file:'+args.db+'?mode=ro',uri=True)
seen=set();invalid=0;raw_count=0;digest_errors=0;object_errors=0
for digest,compressed,status in db.execute('SELECT digest,body,status FROM inbox WHERE run=? ORDER BY id',(run,)):
    raw=zlib.decompress(compressed);raw_count+=1
    if hashlib.sha256(raw).hexdigest()!=digest:digest_errors+=1
    try:message=json.loads(raw)
    except (ValueError,UnicodeError):continue
    if message.get('kind')!='objects':continue
    for obj in message.get('objects',[]):
        if Store.validate_object(obj):invalid+=1;continue
        if obj['id'] in seen:continue
        seen.add(obj['id'])
        row=db.execute('SELECT data FROM objects WHERE run=? AND id=?',(run,obj['id'])).fetchone()
        if not row or dumps(json.loads(row[0]))!=dumps(obj):object_errors+=1
bitmap=base64.b64decode(receipt['ack_bitmap'])
ack_batches=[i*8+bit for i,byte in enumerate(bitmap) for bit in range(8) if byte & (1<<bit)]
missing=[]
for batch_id in ack_batches:
    for offset in range(batch_size):
        object_id=f'{run}-{batch_id*batch_size+offset}'
        if object_id not in seen:missing.append(object_id)
stored=db.execute('SELECT COUNT(*) FROM objects WHERE run=?',(run,)).fetchone()[0]
pending=db.execute("SELECT COUNT(*) FROM inbox WHERE run=? AND status='pending'",(run,)).fetchone()[0]
result={'test_id':run,'verified_raw_messages':raw_count,'raw_digest_errors':digest_errors,
    'acknowledged_normal_batches':len(ack_batches),'acknowledged_normal_objects':receipt['stats']['acknowledged_objects'],
    'missing_acknowledged_objects':len(missing),'missing_examples':missing[:10],
    'valid_unique_objects_in_raw':len(seen),'stored_objects':stored,'invalid_objects_in_raw':invalid,
    'object_content_errors':object_errors,'pending_messages':pending}
result['passed']=not(digest_errors or object_errors or missing or pending) and len(seen)==stored
print(json.dumps(result,ensure_ascii=False,indent=2))
raise SystemExit(0 if result['passed'] else 1)
