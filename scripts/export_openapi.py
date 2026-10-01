"""Write the API's OpenAPI document to docs/api/openapi.json (the contract clients rely on)."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.support import fresh_environment  # noqa: E402

fresh_environment(Path(tempfile.mkdtemp(prefix="rosetta-openapi-")))
from rosetta.api.main import create_app  # noqa: E402

out = ROOT / "docs" / "api" / "openapi.json"
out.parent.mkdir(parents=True, exist_ok=True)
spec = create_app().openapi()
out.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n")
print(f"{out.relative_to(ROOT)}: {sum(len(v) for v in spec['paths'].values())} operations")
