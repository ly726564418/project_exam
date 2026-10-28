"""Online consistent SQLite backup; the source WAL is included by SQLite."""
import argparse
import sqlite3
from pathlib import Path

parser=argparse.ArgumentParser()
parser.add_argument('--db',default='data/atlas.sqlite')
parser.add_argument('--output',required=True)
args=parser.parse_args()
source=Path(args.db).resolve();target=Path(args.output).resolve()
if source==target or target.exists():raise SystemExit('Backup target must be a new file distinct from the source')
target.parent.mkdir(parents=True,exist_ok=True)
with sqlite3.connect(source.as_uri()+'?mode=ro',uri=True) as src,sqlite3.connect(str(target)) as dst:
    src.backup(dst,pages=256,sleep=.05)
    result=dst.execute('PRAGMA quick_check').fetchone()[0]
    if result!='ok':raise SystemExit('Backup verification failed: '+result)
print(str(target))
