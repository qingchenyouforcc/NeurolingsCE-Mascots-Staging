"""Validate the NeurolingsCE-Mascots registry."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry_checks import validate_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", nargs="?", default=".", help="registry root (default: current dir)"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit stable JSON output"
    )
    args = parser.parse_args()

    errors = validate_registry(Path(args.root).resolve())
    ok = not errors
    if args.json:
        print(json.dumps({"ok": ok, "errors": errors}, sort_keys=True))
    else:
        if ok:
            print("Registry OK")
        else:
            for error in errors:
                print(f"- {error}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
