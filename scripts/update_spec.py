"""Refresh the bundled Ramp OpenAPI spec: python scripts/update_spec.py"""

import json
import pathlib
import urllib.request

URL = "https://docs.ramp.com/openapi/developer-api.json"
DEST = pathlib.Path(__file__).resolve().parent.parent / "src" / "ramp_mcp" / "openapi.json"

with urllib.request.urlopen(URL, timeout=60) as resp:
    spec = json.load(resp)
DEST.write_text(json.dumps(spec, separators=(",", ":")), encoding="utf-8")
print(f"Wrote {DEST} ({len(spec.get('paths', {}))} paths)")
