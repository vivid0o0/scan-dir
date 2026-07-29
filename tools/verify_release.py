#!/usr/bin/env python3
"""Verify release identity and embedded package integrity metadata."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = ("LICENSE", "README.md", "SKILL.md", "config.yaml", "install.sh", "prs.py")
RELEASE_FILE_MAX_BYTES = 16 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024


def fail(message: str) -> NoReturn:
    raise SystemExit(f"release verification failed: {message}")


def require_match(pattern: str, text: str, label: str) -> str:
    match = re.search(pattern, text, re.MULTILINE)
    if match is None:
        fail(f"missing {label}")
    return match.group(1)


def section(text: str, heading: str, next_heading_level: str) -> str:
    start = text.find(heading)
    if start < 0:
        fail(f"missing documentation section {heading!r}")
    body_start = start + len(heading)
    end_match = re.search(rf"^{re.escape(next_heading_level)}\s", text[body_start:], re.MULTILINE)
    end = body_start + end_match.start() if end_match else len(text)
    return text[body_start:end]


def require_documented(flags: set[str], documentation: str, label: str) -> None:
    missing = sorted(flag for flag in flags if f"`{flag}" not in documentation and f", {flag}" not in documentation)
    if missing:
        fail(f"undocumented {label} options: {', '.join(missing)}")


def file_signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def read_regular_file(path: Path, maximum: int = RELEASE_FILE_MAX_BYTES) -> bytes:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            fail(f"required release file is not regular: {path.name}")
        if before.st_size > maximum:
            fail(f"required release file exceeds {maximum} bytes: {path.name}")
        chunks: list[bytes] = []
        total = 0
        while total <= maximum:
            chunk = os.read(descriptor, min(READ_CHUNK_BYTES, maximum + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > maximum:
            fail(f"required release file exceeds {maximum} bytes: {path.name}")
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
        if file_signature(before) != file_signature(after) or file_signature(after) != file_signature(current):
            fail(f"required release file changed while being read: {path.name}")
        return b"".join(chunks)
    except SystemExit:
        raise
    except OSError as exc:
        fail(f"unable to read required release file safely: {path.name}: {exc}")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_release_snapshot(root: Path = ROOT) -> dict[str, bytes]:
    return {filename: read_regular_file(root / filename) for filename in RELEASE_FILES}


def decode_release_file(snapshot: Mapping[str, bytes], filename: str) -> str:
    try:
        payload = snapshot[filename]
    except KeyError:
        fail(f"release snapshot is missing {filename}")
    try:
        return payload.decode("utf-8")
    except UnicodeError as exc:
        fail(f"release file is not valid UTF-8: {filename}: {exc}")


def sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def verify_release(snapshot: Mapping[str, bytes] | None = None) -> str:
    release = load_release_snapshot() if snapshot is None else snapshot
    runtime_text = decode_release_file(release, "prs.py")
    installer_text = decode_release_file(release, "install.sh")
    readme_text = decode_release_file(release, "README.md")

    runtime_version = require_match(r'^VERSION = "([^"]+)"$', runtime_text, "runtime version")
    installer_version = require_match(
        r"^readonly INSTALLER_VERSION='([^']+)'$",
        installer_text,
        "installer version",
    )
    if runtime_version != installer_version:
        fail(f"runtime and installer versions differ ({runtime_version} != {installer_version})")

    package_files = (
        ("DEFAULT_RUNTIME_SHA256", "prs.py"),
        ("DEFAULT_CONFIG_SHA256", "config.yaml"),
        ("DEFAULT_SKILL_SHA256", "SKILL.md"),
    )
    for constant, filename in package_files:
        embedded = require_match(
            rf"^readonly {constant}='([0-9a-f]{{64}})'$",
            installer_text,
            constant,
        )
        try:
            payload = release[filename]
        except KeyError:
            fail(f"release snapshot is missing {filename}")
        actual = sha256(payload)
        if embedded != actual:
            fail(f"{constant} does not match {filename} ({embedded} != {actual})")

    runtime_specs = require_match(
        r"(?s)CLI_OPTION_SPECS:.*?= \((.*?)\n\)",
        runtime_text,
        "runtime CLI option schema",
    )
    runtime_flags = set(re.findall(r'"(--[a-z0-9-]+)"', runtime_specs))
    require_documented(runtime_flags, readme_text, "runtime")

    usage_body = require_match(
        r"(?s)usage\(\) \{\n\s+cat <<EOF\n(.*?)\nEOF\n\}",
        installer_text,
        "installer usage text",
    )
    usage_flags = set(re.findall(r"--[a-z0-9-]+", usage_body))
    parser_body = require_match(
        r"(?ms)^parse_args\(\) \{\n(.*?)^\}\n",
        installer_text,
        "installer argument parser",
    )
    parser_flags: set[str] = set()
    for selector in re.findall(r"(?m)^\s+([^()]+)\)\s", parser_body):
        parser_flags.update(part.strip() for part in selector.split("|") if part.strip().startswith("--"))
    undocumented_parser_flags = sorted(parser_flags - usage_flags)
    if undocumented_parser_flags:
        fail(f"installer parser options missing from --help: {', '.join(undocumented_parser_flags)}")
    if "bash install.sh --help" not in readme_text:
        fail("README does not point users to canonical installer help")

    license_text = decode_release_file(release, "LICENSE")
    if not license_text.startswith("MIT License\n") or "Copyright (c) 2026 vivid0o0" not in license_text:
        fail("LICENSE is missing or does not contain the expected MIT grant")

    return runtime_version


def main() -> None:
    version = verify_release()
    print(f"release metadata verified: {version}")


if __name__ == "__main__":
    main()
