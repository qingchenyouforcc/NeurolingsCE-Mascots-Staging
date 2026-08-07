"""Generate generated/index-v1.json deterministically from the registry.

Same input produces byte-identical output: entries are sorted by id and
json.dumps uses sorted keys with stable separators.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from registry_checks import iter_manifest_paths, load_index_entry, load_manifest  # noqa: E402
from validate_registry import main as validate_main  # noqa: E402


DEFAULT_REGISTRY = "qingchenyouforcc/NeurolingsCE-Mascots"


def _published_download_url(registry: str, manifest: dict) -> str:
    """Derive the canonical published asset URL from the release tag.

    The manifest's package.url may have been captured while the release was
    still a draft (GitHub returns an ``untagged-<sha>`` download path that
    404s after publishing). The tag-based URL is canonical and works for
    both draft-slash tags and normal tags.
    """
    release = manifest.get("release") or {}
    package = manifest.get("package") or {}
    tag = release.get("tag")
    file_name = package.get("fileName")
    if isinstance(tag, str) and isinstance(file_name, str) and file_name:
        return (
            f"https://github.com/{registry}/releases/download/"
            f"{tag}/{file_name}"
        )
    return package.get("url", "")


def build_index(root: Path, generated_at: str, registry: str,
                published_tags: set[str] | None = None) -> dict:
    """Build the index deterministically.

    When ``published_tags`` is provided, only manifests whose release tag is in
    that set are included (published state is derived from the GitHub Releases
    API, never persisted back into main). When it is None (local/offline runs),
    the legacy behavior applies: manifests with status != "draft" are included.
    """
    mascots = []
    for _directory_name, manifest_path in iter_manifest_paths(root):
        manifest, manifest_errors = load_manifest(manifest_path)
        if manifest_errors:
            raise ValueError(f"{manifest_path}: {manifest_errors[0]}")
        if published_tags is not None:
            tag = (manifest.get("release") or {}).get("tag")
            if not isinstance(tag, str) or tag not in published_tags:
                continue
        elif manifest.get("status") == "draft":
            continue
        entry = load_index_entry(manifest)
        entry["download"]["url"] = _published_download_url(registry, manifest)
        mascots.append(entry)
    mascots.sort(key=lambda item: item["id"])
    return {
        "schemaVersion": 1,
        "generatedAt": generated_at,
        "registry": registry,
        "mascots": mascots,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="registry root")
    parser.add_argument(
        "--output",
        default="generated/index-v1.json",
        help="output path (default: generated/index-v1.json)",
    )
    parser.add_argument(
        "--registry", default=DEFAULT_REGISTRY, help="registry slug for the index"
    )
    parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO-8601 timestamp; default is current UTC time",
    )
    parser.add_argument(
        "--published-tags-file",
        default=None,
        help="JSON file with the list of published release tags",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if validate_main_with_args([str(root)]):
        print("Registry validation failed; refusing to generate an index", file=sys.stderr)
        return 1
    generated_at = args.generated_at or datetime.now(timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    published_tags: set[str] | None = None
    if args.published_tags_file:
        tags_path = Path(args.published_tags_file)
        try:
            raw_tags = json.loads(tags_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Could not read published tags file: {exc}", file=sys.stderr)
            return 1
        if not isinstance(raw_tags, list) or not all(
            isinstance(tag, str) for tag in raw_tags
        ):
            print("published tags file must contain a JSON array of strings",
                  file=sys.stderr)
            return 1
        published_tags = set(raw_tags)
    index = build_index(root, generated_at, args.registry, published_tags)
    output = Path(args.output)
    if not output.is_absolute():
        output = root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    output.write_text(text, encoding="utf-8")
    print(f"Wrote {output}")
    return 0


def validate_main_with_args(argv: list[str]) -> int:
    """Run the registry validator in-process (no subprocess)."""
    old_argv = sys.argv
    try:
        sys.argv = ["validate_registry.py", *argv]
        return validate_main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    sys.exit(main())
