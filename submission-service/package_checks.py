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
import tempfile
import threading
import zipfile
from io import BytesIO
from pathlib import Path

MAX_PACKAGE_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_BYTES = 100 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 16 * 1024 * 1024
MAX_ENTRY_COUNT = 4096
MAX_VALIDATOR_OUTPUT_BYTES = 1024 * 1024
MAX_VALIDATOR_ERROR_LENGTH = 500

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


def _sanitize_error(value) -> str:
    text = str(value)
    text = "".join(
        char for char in text if char.isprintable() or char in ("\t", "\n")
    )
    text = text.replace("\t", " ").replace("\n", " ")
    return text[:MAX_VALIDATOR_ERROR_LENGTH]


def _read_output_limited(process, max_bytes: int,
                         result: dict) -> None:
    """Read stdout/stderr with a hard cap; kill the process if it overflows."""
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                process.kill()
                result["overflow"] = True
                break
            chunks.append(chunk)
    except Exception:  # noqa: BLE001 - process may disappear during teardown
        pass
    result["stdout"] = b"".join(chunks)


def run_external_validator(path: Path, cli: str,
                           timeout_seconds: int = 120) -> tuple[bool, list[str]]:
    """Run the public C++ validator with fail-closed semantics.

    Any timeout, crash, non-zero exit, malformed JSON, oversized output or
    missing ok=true is a rejection. This function never falls back to the
    embedded Python checks.
    """
    try:
        process = subprocess.Popen(
            [cli, "--json", "--mascot", "validate", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except OSError as exc:
        return False, [f"public validator could not be started ({exc})"]
    result: dict = {"stdout": b"", "overflow": False}
    reader = threading.Thread(
        target=_read_output_limited,
        args=(process, MAX_VALIDATOR_OUTPUT_BYTES, result),
        daemon=True,
    )
    reader.start()
    try:
        returncode = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        reader.join(timeout=5)
        process.stdout.close()
        return False, ["public validator timed out"]
    reader.join(timeout=5)
    process.stdout.close()
    stdout = result["stdout"]
    overflow = result["overflow"]
    if overflow:
        return False, ["public validator output exceeded the size limit"]
    if returncode != 0:
        tail = _sanitize_error(stdout[-200:])
        return False, [f"public validator exited with code {returncode}: {tail}"]
    try:
        report = json.loads(stdout.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, ["public validator returned malformed JSON"]
    if not isinstance(report, dict):
        return False, ["public validator returned a non-object JSON report"]
    ok = report.get("ok")
    if not isinstance(ok, bool) or not ok:
        errors = report.get("errors", [])
        if isinstance(errors, list):
            sanitized = [_sanitize_error(item) for item in errors]
        else:
            sanitized = ["public validator reported failure"]
        return False, sanitized
    return True, []


def validator_self_check(cli: str, timeout_seconds: int = 120) -> tuple[bool, str]:
    """Run the validator against a built-in minimal valid mascot."""
    buffer = BytesIO()
    png = bytes([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
        0x89, 0x00, 0x00, 0x00, 0x0D, 0x49, 0x44, 0x41,
        0x54, 0x78, 0x9C, 0x63, 0xF8, 0xCF, 0xC0, 0xF0,
        0x1F, 0x00, 0x05, 0x00, 0x01, 0xFF, 0x89, 0x99,
        0x3D, 0x1D, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45,
        0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ])
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "info.json",
            '{"name":"SelfCheck","version":"0.0.1","description":"self check",'
            '"author":"neurolingsce"}',
        )
        archive.writestr("actions.xml", "<Mascot><ActionList /></Mascot>")
        archive.writestr("behaviors.xml", "<Mascot><BehaviorList /></Mascot>")
        archive.writestr("img/selfcheck.png", png)
    with tempfile.TemporaryDirectory(prefix="neurolingsce-selfcheck-") as tmp:
        path = Path(tmp) / "selfcheck.mascot"
        path.write_bytes(buffer.getvalue())
        ok, errors = run_external_validator(path, cli, timeout_seconds)
        if ok:
            return True, ""
        return False, "; ".join(errors)


def validate_mascot(path: Path, validator_cli: str | None = None,
                    submission_env: str = "development",
                    validator_timeout_seconds: int = 120) -> tuple[bool, list[str]]:
    cli = validator_cli or os.environ.get("VALIDATOR_CLI", "")
    if cli:
        return run_external_validator(
            path, cli, validator_timeout_seconds
        )
    if submission_env == "production":
        # Should be unreachable because startup rejects missing VALIDATOR_CLI,
        # but fail closed anyway: never fall back to embedded checks.
        return False, ["production mode requires the public validator"]
    return embedded_validate(path)
