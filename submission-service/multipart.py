"""Minimal streaming multipart/form-data parser (stdlib-only).

The request body is spooled to a temporary file with a hard size cap, then
parsed part-by-part so only field values and the uploaded file are retained.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path


class MultipartError(Exception):
    pass


@dataclass
class UploadedFile:
    field_name: str
    file_name: str
    content_type: str
    temp_path: Path
    size: int


@dataclass
class ParsedMultipart:
    fields: dict[str, str]
    files: dict[str, UploadedFile]


_HEADER_END_RE = re.compile(rb"\r\n\r\n")


def _spool_body(rfile, content_length: int | None, max_total: int) -> Path:
    if content_length is None:
        raise MultipartError("Content-Length is required for multipart uploads")
    if content_length > max_total:
        raise MultipartError("request body exceeds size limit")
    fd, path = tempfile.mkstemp(prefix="neurolingsce-upload-")
    written = 0
    try:
        with open(fd, "wb") as out:
            while written < content_length:
                remaining = content_length - written
                chunk = rfile.read(min(remaining, 65536))
                if not chunk:
                    raise MultipartError("request body ended unexpectedly")
                written += len(chunk)
                out.write(chunk)
        return Path(path)
    except Exception:
        Path(path).unlink(missing_ok=True)
        raise


def _parse_part(headers_block: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in headers_block.split(b"\r\n"):
        if b":" not in line:
            continue
        key, _, value = line.partition(b":")
        headers[key.strip().lower().decode("latin-1")] = value.strip().decode("latin-1")
    return headers


def parse_multipart(rfile, content_type: str, content_length: int | None,
                    max_upload_bytes: int, max_metadata_bytes: int = 262144,
                    max_total_bytes: int | None = None) -> ParsedMultipart:
    match = re.search(r"boundary=([^;]+)", content_type)
    if not match:
        raise MultipartError("multipart boundary is missing")
    boundary = match.group(1).strip().strip('"').encode("latin-1")
    if max_total_bytes is None:
        max_total_bytes = max_upload_bytes + 4 * 1024 * 1024

    spool = _spool_body(rfile, content_length, max_total_bytes)
    fields: dict[str, str] = {}
    files: dict[str, UploadedFile] = {}
    try:
        data = spool.read_bytes()
        if not data.startswith(b"--" + boundary):
            raise MultipartError("malformed multipart body")
        # Split on the boundary; discard the trailing epilogue.
        parts = data.split(b"--" + boundary)
        for part in parts[1:]:
            if part.startswith(b"--"):
                continue
            if part.startswith(b"\r\n"):
                part = part[2:]
            if part.endswith(b"\r\n"):
                part = part[:-2]
            header_end = _HEADER_END_RE.search(part)
            if header_end is None:
                raise MultipartError("multipart part is missing headers")
            headers_block = part[: header_end.start()]
            body = part[header_end.end() :]
            headers = _parse_part(headers_block)
            disposition = headers.get("content-disposition", "")
            if not disposition or "form-data" not in disposition:
                continue
            name_match = re.search(r'name="([^"]*)"', disposition)
            if not name_match:
                raise MultipartError("multipart part is missing a field name")
            field_name = name_match.group(1)
            file_match = re.search(r'filename="([^"]*)"', disposition)
            if file_match:
                if len(body) > max_upload_bytes:
                    raise MultipartError("uploaded file exceeds size limit")
                fd, temp_path = tempfile.mkstemp(prefix="neurolingsce-file-")
                with open(fd, "wb") as out:
                    out.write(body)
                files[field_name] = UploadedFile(
                    field_name=field_name,
                    file_name=file_match.group(1),
                    content_type=headers.get("content-type", "application/octet-stream"),
                    temp_path=Path(temp_path),
                    size=len(body),
                )
            else:
                if len(body) > max_metadata_bytes:
                    raise MultipartError("metadata field exceeds size limit")
                fields[field_name] = body.decode("utf-8", "replace")
        return ParsedMultipart(fields=fields, files=files)
    finally:
        spool.unlink(missing_ok=True)
