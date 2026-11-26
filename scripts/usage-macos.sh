#!/bin/bash
# Local-only usage summary for the macOS control center. Never uploads anything.
set -u
DEST="$HOME/Library/Application Support/openhop"
PY="$DEST/venv/bin/python"
[ -x "$PY" ] || { echo '{}'; exit 0; }
exec "$PY" - "$DEST/src" <<'PY'
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import usage_stats
s = usage_stats.summary()
state = {}
try:
    state = json.loads((Path.home() / "Library" / "Application Support" / "openhop" / "guard" / "state.json").read_text())
except Exception:
    pass
print(json.dumps({
    "tokens": s["tokens"],
    "messages": s["messages"],
    "chats": s["chats"],
    "rotationsToday": s["events"].get("hop", 0),
    "rotationsAll": int(state.get("hops", 0) or 0),
    "goodIPs": s["events"].get("good", 0),
    "usedUp": s["events"].get("spent", 0),
    "limits": s["events"].get("limit", 0),
    "continues": s["events"].get("cont", 0),
}))
PY
