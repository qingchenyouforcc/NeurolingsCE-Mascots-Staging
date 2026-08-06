"""Mascot package safety checks.

Authoritative path: delegates to the public C++ validator
(NeurolingsCE-cli --mascot validate <file> --json) when ``VALIDATOR_CLI`` is
configured. The embedded checks are a bootstrap fallback that mirrors the
same limits (SecurityLimits in the NeurolingsCE app repository) and must not
diverge from them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import zipfile
from pathlib import Path

MAX_PACKAGE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 16 * 1024 * 1024
MAX_ENTRY_COUNT = 4096

FORBIDDEN_EXTENSIONS = {
    ".exe", ".dll", ".com", ".bat", ".cmd", ".ps1", ".sh", ".js", ".vbs",
    ".lnk", ".scr", ".pif", ".msi", ".msp", ".hta", ".jar",
}
NESTED_ARCHIVE_EXTENSIONS = {
    ".zip", ".mascot", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz",
    ".cab", ".iso", ".apk", ".war", ".ear",
}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".aac", ".opus"}
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._ /-]+$")


def _normalize(path: str) -> str | None:
    path = path.replace("\\", "/")
    parts = [part for part in path.split("/") if part]
    cleaned = []
    for part in parts:
        if part in (".", "..") or ":" in part:
            return None
        cleaned.append(part)
    return "/".join(cleaned)


def _is_allowed_entry(raw: str, normalized: str) -> bool:
    lower = normalized.lower()
    if lower in ("info.json", "bubble_context.txt", "actions.xml", "behaviors.xml"):
        return True
    if lower.startswith("img/") and lower.endswith(".png"):
        return True
    if lower.startswith("sound/"):
        return any(lower.endswith(ext) for ext in AUDIO_EXTENSIONS)
    return False


def _is_forbidden(lower: str) -> bool:
    return any(lower.endswith(ext) for ext in FORBIDDEN_EXTENSIONS | NESTED_ARCHIVE_EXTENSIONS)


def _png_dimensions(header: bytes) -> tuple[int, int] | None:
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    width = int.from_bytes(header[16:20], "big")
    height = int.from_bytes(header[20:24], "big")
    return width, height


def embedded_validate(path: Path) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not path.is_file():
        return False, ["Mascot package does not exist"]
    size = path.stat().st_size
    if size > MAX_PACKAGE_BYTES:
        return False, [f"Mascot package exceeds the maximum size of {MAX_PACKAGE_BYTES} bytes"]
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        return False, [f"Package is not a valid ZIP archive ({exc})"]
    infos = archive.infolist()
    if len(infos) > MAX_ENTRY_COUNT:
        errors.append(f"Archive contains too many entries ({len(infos)}, maximum {MAX_ENTRY_COUNT})")
    has_info = has_actions = has_behaviors = has_image = False
    total = 0
    for info in infos:
        raw = info.filename
        normalized = _normalize(raw)
        if normalized is None:
            errors.append(f"Unsupported or unsafe package entry: {raw!r}")
            continue
        if raw.endswith("/"):
            continue
        lower = normalized.lower()
        if _is_forbidden(lower):
            errors.append(f"Package contains a forbidden payload entry: {normalized}")
            continue
        if not _is_allowed_entry(raw, normalized):
            errors.append(f"Unsupported or unsafe package entry: {normalized}")
            continue
        total += info.file_size
        if info.file_size > MAX_SINGLE_FILE_BYTES:
            errors.append(f"Package entry {normalized} exceeds size limits")
        if total > MAX_EXTRACTED_BYTES:
            errors.append("Package extracted data is too large")
        has_info = has_info or lower == "info.json"
        has_actions = has_actions or lower == "actions.xml"
        has_behaviors = has_behaviors or lower == "behaviors.xml"
        if lower.startswith("img/") and lower.endswith(".png"):
            has_image = True
            header = archive.read(info)
            dims = _png_dimensions(header[:24])
            if dims is None:
                errors.append(f"Image {normalized} is not a valid PNG")
            elif dims[0] == 0 or dims[1] == 0 or dims[0] * dims[1] > 4096 * 4096:
                errors.append(f"Image {normalized} exceeds the maximum pixel count of 16777216")
    if not has_info:
        errors.append("Package must contain info.json")
    if not has_actions:
        errors.append("Package must contain actions.xml")
    if not has_behaviors:
        errors.append("Package must contain behaviors.xml")
    if not has_image:
        errors.append("Package must contain img/*.png")
    return not errors, errors


def validate_mascot(path: Path, validator_cli: str | None = None) -> tuple[bool, list[str]]:
    cli = validator_cli or os.environ.get("VALIDATOR_CLI", "")
    if cli:
        try:
            result = subprocess.run(
                [cli, "--json", "--mascot", "validate", str(path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, [f"public validator could not run ({exc})"]
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            return False, ["public validator returned malformed JSON"]
        return bool(report.get("ok")), list(report.get("errors", []))
    return embedded_validate(path)
