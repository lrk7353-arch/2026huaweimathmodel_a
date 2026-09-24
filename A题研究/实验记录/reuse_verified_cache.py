"""Reuse immutable successful evaluator results across run directories.

Only exact existing cache keys are copied; evaluator.py still computes the key
from current inputs and verifies the compressed output before a hit. No scores
or official outputs are edited. Original failed attempts remain untouched.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"solver"))
from common import atomic_json, read_json, digest


def import_cache(source,destination):
    source,destination=Path(source).resolve(),Path(destination).resolve()
    imported=[]
    for path in sorted((source/"cache").glob("*/success.json")):
        record=read_json(path)
        if record.get("status")!="success":continue
        result=Path(record["result_path"])
        if not result.is_file() or digest(result)!=record.get("result_sha256"):
            raise ValueError(f"Missing/corrupt success result: {path}")
        key=record["cache_key"]
        if key!=path.parent.name:raise ValueError("cache directory/key mismatch")
        encoded=json.dumps(record["hashes"],ensure_ascii=False,allow_nan=False,separators=(",",":")).encode()
        if hashlib.sha256(encoded).hexdigest()!=key:raise ValueError("hash manifest/key mismatch")
        target=destination/"cache"/key/"success.json"
        if target.exists():continue
        atomic_json(target,record)
        imported.append({"cache_key":key,"source":str(path),"target":str(target),"result":str(result)})
    log=destination/"cache_imports"/(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")+".json")
    atomic_json(log,{"source":str(source),"destination":str(destination),"imported":imported})
    return imported


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("source",type=Path);p.add_argument("destination",type=Path)
    a=p.parse_args();print("Verified cache entries imported:",len(import_cache(a.source,a.destination)))
