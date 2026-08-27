#!/usr/bin/env python3
"""Generate or check the structural JSON Schema for detached bundle locks."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "bundles" / "schema.json"
sys.path.insert(0, str(ROOT))

from contextos.bundle_schema import bundle_schema_document  # noqa: E402
from contextos.component_schema import ComponentManifestError, write_generated_file  # noqa: E402


def schema_text() -> str:
    return json.dumps(bundle_schema_document(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("generate", "check"))
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            write_generated_file(SCHEMA_PATH, schema_text(), root=ROOT)
        elif not SCHEMA_PATH.exists() or SCHEMA_PATH.read_text(encoding="utf-8") != schema_text():
            raise ComponentManifestError(
                "bundles/schema.json is stale; run scripts/bundle-locks.py generate"
            )
    except (ComponentManifestError, OSError, UnicodeError) as exc:
        print(f"bundle-locks: {exc}", file=sys.stderr)
        return 1
    print(f"Bundle lock schema {args.command} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
