#!/usr/bin/env bash
# End-to-end demo over HTTP against a running instance (default :8000).
# Runs the interleaved-write / pause-FUA / power-loss scenario from the README.
set -euo pipefail
BASE="${BASE:-http://127.0.0.1:8000}"
PY="${PY:-python3}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
jget() { "$PY" "$HERE/tests/jget.py" "$1"; }
padhex() { "$PY" -c "import sys;print(sys.argv[1].encode().ljust(4096,b'.').hex())" "$1"; }
post() { curl -fsS -X POST "$BASE$1" -H 'Content-Type: application/json' -d "${2:-}"; }
wait_until() { # argument: boolean expression on f (the flush object)
  for _ in $(seq 1 100); do
    ok="$(curl -fsS "$BASE/api/state" | jget "$1")"
    [ "$ok" = "True" ] && return 0
    sleep 0.05
  done
  echo "timed out waiting for: $1" >&2; exit 1
}

echo "== reset =="
post /api/reset >/dev/null

echo "== 1. normal writes (cache only) =="
post /api/write "{\"block\":0,\"encoding\":\"hex\",\"data\":\"$(padhex A-v1)\"}" >/dev/null
post /api/write '{"block":1,"data":"B-v1","pad":true}' >/dev/null
post /api/write '{"block":0,"data":"A-v2-NEWEST","pad":true}' >/dev/null
curl -fsS "$BASE/api/read?block=0" \
  | jget "d['result']['source']+' seq'+str(d['result']['seq'])+' '+d['result']['preview'][:16]"

echo "== 2. FLUSH snapshot vs post-accept write =="
post /api/flush '{"selected":[0,1]}' >/dev/null
post /api/write '{"block":1,"data":"B-v2-AFTER-ACCEPT","pad":true}' >/dev/null
wait_until "f['status']=='done'"
curl -fsS "$BASE/api/state" \
  | jget "'flush='+d['flush']['status']+' results='+str(d['flush']['results'])"
curl -fsS "$BASE/api/read?block=1" \
  | jget "d['result']['source']+' '+d['result']['preview'][:22]"

echo "== 3. pause, FUA, resume must not overwrite newer platter value =="
for b in 2 3 4; do post /api/write "{\"block\":$b,\"data\":\"B$b-snap\",\"pad\":true}" >/dev/null; done
post /api/flush '{"selected":[2,3,4]}' >/dev/null
wait_until "f['cursor']==1 and f['status']=='running'"
post /api/flush/pause >/dev/null
post /api/write '{"block":4,"data":"B4-newer-while-paused","pad":true}' >/dev/null
post /api/write '{"block":3,"data":"B3-FUA-ON-PLATTER","pad":true,"fua":true}' \
  | jget "'FUA persisted='+str(d['result']['persisted'])"
post /api/flush/resume >/dev/null
wait_until "f['status']=='done'"
curl -fsS "$BASE/api/state" | jget "str(d['flush']['results'])"

echo "== 4. power loss mid-round =="
post /api/write '{"block":5,"data":"B5-never-persisted","pad":true}' >/dev/null
post /api/flush '{"selected":[4,5]}' >/dev/null
wait_until "f['cursor']==1 and f['status']=='running'"
post /api/power-loss | jget "'flush after loss='+d['state']['flush']['status']"
curl -fsS "$BASE/api/read?block=5" \
  | jget "'block5 -> '+d['result']['source']+' '+d['result']['preview'][:16]"
curl -fsS "$BASE/api/read?block=3" \
  | jget "'block3 survived -> '+d['result']['preview'][:20]"
echo "ALL DEMO STEPS OK"
