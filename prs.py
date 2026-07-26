#!/usr/bin/env python3
# prs.py -- Project Summarizer scanner
# Implements the prs project tree scanner with config-driven filters, metadata
# rendering, git markers, and installer-safe runtime behavior.

from __future__ import annotations

import argparse
import errno
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence


# ─── CONSTANTS ──────────────────────────────────────────────────────────────

PROGRAM_NAME = "prs"
ALIAS_COMMAND = "project-summarizer"
PRODUCT_TITLE = "Project Summarizer"
VERSION = "2026.06.20.31"
CONFIG_FILE_NAME = "config.yaml"
DEFAULT_SCAN_TIMEOUT_SECONDS = 60.0
ENTRY_TYPES = ("file", "dir", "link")
# Emoji prefix shown next to entry names when scan_emojis is enabled.
# Co-located with ENTRY_TYPES so the kind set has a single source of truth.
KIND_EMOJIS = {"dir": "📁 ", "file": "📄 ", "link": "🔗 "}
STYLING_LEVELS = ("full", "low", "minimal")
SCAN_DATA_ITEMS = ("tree", "lines", "size", "modified", "type", "git", "summary")
SCAN_DATA_DEFAULT = "tree, lines, size, modified, type, git, summary"
SIZE_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")
# Binary step between adjacent SIZE_UNITS (1024 = KiB/MiB/GiB boundaries).
# Centralised so format_size and any future size renderer agree on the unit
# boundary instead of each caller re-hardcoding 1024.
SIZE_UNIT_STEP = 1024
TERMINAL_WIDTH_FALLBACK = 100
# Hard floor on terminal columns. Below this the layout math breaks down and
# renderers prefer cropping over producing unreadable output. Used by
# terminal_columns to clamp both the COLUMNS env var and the detected size.
TERMINAL_WIDTH_MINIMUM = 16
TERMINAL_WIDTH_MAXIMUM = 500
# Row count handed to shutil.get_terminal_size when no real terminal is
# attached (matches the historical 80x24 VT100 default).
TERMINAL_ROWS_FALLBACK = 24
# Cap on horizontal rule width so banners and status boxes stay readable on
# wide terminals instead of stretching edge-to-edge.
BANNER_RULE_WIDTH_MAXIMUM = 100
METADATA_COLUMN_WIDTH = 12
METADATA_SEPARATOR = " "
# Separator between values inside a single summary row (total/largest/newest/
# types/scanned). Centralised so every summary row uses the same spacing.
SUMMARY_VALUE_SEPARATOR = "    "
# Layout-policy thresholds. Below these widths the renderers fall back to a
# more compact form so output stays readable on narrow terminals.
SUMMARY_FRAMED_MIN_WIDTH = 40
# Framed-summary two-column layout: │ <label:SUMMARY_LABEL_WIDTH> │ <value> │.
# The label width and its derivatives (cell width incl. padding, border-char
# count, value-column overhead) are centralised so summary_label_line and
# render_framed_summary agree on every dimension. Changing the label width
# only requires editing SUMMARY_LABEL_WIDTH; the cell width, border count,
# and value overhead all derive from it.
SUMMARY_LABEL_WIDTH = 20
SUMMARY_LABEL_CELL_WIDTH = SUMMARY_LABEL_WIDTH + 2
SUMMARY_BORDER_CHARS = 3
SUMMARY_VALUE_OVERHEAD = SUMMARY_LABEL_WIDTH + 7
HELP_TABLE_MIN_WIDTH = 60
HELP_OPTION_COLUMN_MAX_WIDTH = 38
HELP_DESCRIPTION_COLUMN_MIN_WIDTH = 20
# Fixed horizontal chars around the description column in help_table
# (2-space left margin + 2-space gap before the description text).
HELP_TABLE_COLUMN_OVERHEAD = 4
NAME_COLUMN_MIN_WIDTH = 8
TEXT_READ_CHUNK_SIZE = 64 * 1024
FILE_READ_MAX_ATTEMPTS = 2
CLIPBOARD_TIMEOUT_SECONDS = 2.0
GIT_TIMEOUT_CAP_SECONDS = 5.0

GIT_CONFIG_OVERRIDES = (
    "core.fsmonitor=false",
    "core.hooksPath=" + os.devnull,
    "core.pager=cat",
    "pager.status=false",
)
GIT_ENVIRONMENT_KEYS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_ATTR_SOURCE",
    "GIT_CONFIG_PARAMETERS",
)
# git ls-tree HEAD exits 128 when HEAD does not exist (e.g., a freshly-init'd
# repo with no commits). Treated as "no tree to recover metadata from" rather
# than a hard failure; any other non-zero status is surfaced as a real error.
GIT_FATAL_EXIT_CODE = 128

# Clean, high-contrast ANSI palette. We use standard (not bright) colors
# for most surfaces to avoid the neon look. Bold + a single accent hue per
# semantic role keeps contrast strong without visual noise.
ANSI_RESET = "\033[0m"
ANSI_BOLD = "\033[1m"
ANSI_DIM = "\033[2m"
ANSI_RED = "\033[31m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_CYAN = "\033[36m"
ANSI_WHITE = "\033[37m"
ANSI_BRIGHT_RED = "\033[91m"
ANSI_BRIGHT_GREEN = "\033[92m"
ANSI_BRIGHT_YELLOW = "\033[93m"
ANSI_BRIGHT_BLUE = "\033[94m"
ANSI_BRIGHT_MAGENTA = "\033[95m"
ANSI_BRIGHT_CYAN = "\033[96m"


# ─── ERRORS ─────────────────────────────────────────────────────────────────

class PrsError(Exception):
    pass


class ConfigError(PrsError):
    pass


class HelpRequested(PrsError):
    """Raised by the argument parser when it requests a clean exit (status 0).

    argparse calls ``parser.exit(0, ...)`` when a help action fires; we use
    this dedicated type so callers can render help instead of misreporting
    the exit as a config error.
    """
    pass


class ClipboardError(PrsError):
    pass


class ClipboardUnavailableError(ClipboardError):
    """No clipboard backend is available in this environment.

    Distinct from ClipboardError so the entry point can warn and continue
    (exit 0) instead of failing the scan. A missing backend is an
    environment condition (headless server, container, CI), not a runtime
    error; the scan output is already printed to stdout and is fully usable.
    """


class ClipboardFailureError(ClipboardError):
    """A backend was present but the copy operation failed.

    This is a real error worth surfacing with a non-zero exit: the user
    explicitly enabled auto-copy, a backend was detected, and it broke.
    """


# ─── DATA MODELS ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FilterRules:
    paths: tuple[str, ...] = ()
    types: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    names: tuple[str, ...] = ()

    @property
    def has_rules(self) -> bool:
        return bool(self.paths or self.types or self.extensions or self.names)


@dataclass(frozen=True)
class RuntimeConfig:
    root_path: Path
    config_path: Path | None
    filter_mode: str
    rules: FilterRules
    ignore_hidden: bool
    ignore_empty: bool
    scan_styling: str
    scan_emojis: bool
    scan_data: frozenset[str]
    scan_timeout: float
    auto_copy: bool


@dataclass
class ScanWarning:
    rel_path: str
    message: str


@dataclass(frozen=True)
class DeletedGitEntry:
    rel_path: str
    kind: str
    size: int


@dataclass
class EntryNode:
    path: Path
    rel_path: str
    name: str
    kind: str
    size: int
    mtime: float
    lines: int | None = None
    target: str | None = None
    children: list["EntryNode"] = field(default_factory=list)
    total_files: int = 0
    total_dirs: int = 0
    total_links: int = 0
    total_lines: int = 0
    unknown_lines: int = 0
    largest_file: tuple[str, int] | None = None
    newest_entry: tuple[str, float] | None = None

    @property
    def summary_path(self) -> str:
        return self.name if self.rel_path == "." else self.rel_path


@dataclass
class ScanResult:
    root: EntryNode
    elapsed_ms: int
    timed_out: bool
    warnings: list[ScanWarning]
    git_markers: dict[str, str]
    deleted_git_entries: list[DeletedGitEntry]


@dataclass
class ScanState:
    config: RuntimeConfig
    started_at: float
    deadline: float
    physical_root: Path
    git_deadline: float | None = None
    timed_out: bool = False
    warnings: list[ScanWarning] = field(default_factory=list)

    def timeout_reached(self) -> bool:
        if time.monotonic() >= self.deadline:
            self.timed_out = True
            return True
        return False

    def warn(self, rel_path: str, message: str) -> None:
        self.warnings.append(ScanWarning(rel_path=rel_path, message=message))

    def remaining(self, cap: float | None = None) -> float:
        remaining = max(0.0, self.deadline - time.monotonic())
        return min(remaining, cap) if cap is not None else remaining


# ─── NORMALIZATION ──────────────────────────────────────────────────────────

def sanitize_terminal_text(value: object) -> str:
    text = os.fsdecode(value) if isinstance(value, bytes) else str(value)
    if any(0xDC80 <= ord(char) <= 0xDCFF for char in text):
        try:
            raw_text = text.encode(sys.getfilesystemencoding(), "surrogateescape")
        except UnicodeEncodeError:
            raw_text = text.encode("utf-8", "surrogateescape")
        text = raw_text.decode("utf-8", "replace")
    output: list[str] = []
    for char in text:
        codepoint = ord(char)
        category = unicodedata.category(char)
        if char == "\t":
            output.append(" ")
        elif char in {"\n", "\r"} or category in {"Cc", "Cs"}:
            output.append(f"\\u{codepoint:04X}")
        elif category == "Cf" or 0x202A <= codepoint <= 0x202E or 0x2066 <= codepoint <= 0x2069:
            output.append(f"<U+{codepoint:04X}>")
        else:
            output.append(char)
    return "".join(output)


def expand_user_path(value: str | os.PathLike[str], description: str) -> Path:
    try:
        return Path(value).expanduser()
    except (KeyError, RuntimeError, ValueError) as exc:
        raise ConfigError(
            f"unable to expand {description}: {sanitize_terminal_text(value)}"
        ) from exc


def resolve_scan_path(value: str) -> Path:
    """Resolve a user-supplied scan path to an absolute path.

    No case-insensitive fallback: case-sensitive filesystems (Linux) must
    receive the exact path the user typed. macOS and Windows filesystems
    are case-insensitive at the OS level, so they handle mismatches
    natively. Guessing silently on Linux hides typos and breaks user trust.
    """
    expanded = expand_user_path(value, "scan path")
    return Path(os.path.abspath(expanded))


ASCII_TRANSLATION = str.maketrans({
    "─": "-", "│": "|", "├": "+", "└": "`", "┌": "+", "┐": "+",
    "┬": "+", "┤": "+", "┘": "+", "┴": "+", "→": "->", "…": ".",
})


def ascii_terminal_text(text: str) -> str:
    translated = text.translate(ASCII_TRANSLATION)
    output: list[str] = []
    for char in unicodedata.normalize("NFKD", translated):
        if ord(char) < 128:
            output.append(char)
        elif char == "\ufffd":
            output.append("?")
        elif unicodedata.combining(char):
            continue
        elif unicodedata.category(char) in {"Cf", "Mn", "Me", "So", "Sk"}:
            continue
        else:
            output.append("?")
    return "".join(output)


def stream_text(stream: object, text: str) -> str:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return text
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ascii_terminal_text(text)
    return text


def write_stream(stream: object, text: str) -> None:
    stream.write(stream_text(stream, text))
    stream.flush()

def normalize_selector_path(value: str) -> str:
    if value == ".":
        return "."
    normalized = value
    if os.sep != "/":
        normalized = normalized.replace(os.sep, "/")
    if os.altsep and os.altsep != "/":
        normalized = normalized.replace(os.altsep, "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    normalized = normalized.strip("/")
    return normalized


def git_path_identity(value: str) -> str:
    return value.replace(os.sep, "/") if os.sep != "/" else value


def normalize_extension(value: str) -> str:
    extension = value.strip().lower()
    if not extension.startswith("."):
        raise ConfigError(f"extension filters must include the leading dot: {value}")
    if extension == ".":
        raise ConfigError("extension filter cannot be only '.'")
    return extension


def normalize_entry_type(value: str) -> str:
    entry_type = value.strip().lower()
    if entry_type not in ENTRY_TYPES:
        joined = ", ".join(ENTRY_TYPES)
        raise ConfigError(f"invalid entry type '{value}', expected one of: {joined}")
    return entry_type


def normalize_bool(value: object, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ConfigError(f"{key} must be true or false")


def normalize_float(value: object, key: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{key} must be a number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{key} must be a number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ConfigError(f"{key} must be greater than 0")
    return number


def normalize_choice(value: object, key: str, choices: Sequence[str]) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be one of: {', '.join(choices)}")
    normalized = value.strip().lower()
    if normalized not in choices:
        raise ConfigError(f"{key} must be one of: {', '.join(choices)}")
    return normalized


def normalize_scan_data(value: object, key: str = "scan_data") -> frozenset[str]:
    """Parse a scan_data string into a frozenset of item names.

    Accepts a comma-separated string like 'Tree, lines count, size, last
    modified, type, git marker, summary' and returns a frozenset of
    canonical item names from SCAN_DATA_ITEMS. Each comma-separated entry
    must contain at least one whitespace-separated word that exactly
    matches (case-insensitively) a canonical item name. This allows
    human-friendly variants like 'lines count' (matches 'lines'), 'last
    modified' (matches 'modified'), 'git marker' (matches 'git'), while
    rejecting plurals, typos, and unknown items with a ConfigError.
    """
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    items: set[str] = set()
    for raw_item in value.split(","):
        item = raw_item.strip().lower().rstrip(".")
        if not item:
            continue
        matched = False
        for canonical in SCAN_DATA_ITEMS:
            if canonical in item.split():
                items.add(canonical)
                matched = True
                break
        if not matched:
            raise ConfigError(f"{key} contains unknown item '{raw_item.strip()}'; valid items: {', '.join(SCAN_DATA_ITEMS)}")
    return frozenset(items)


def normalize_list(value: object, key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError(f"{key} must be a list")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ConfigError(f"{key} must contain only strings")
        result.append(item)
    return tuple(result)


def normalize_rules(payload: dict[str, object]) -> FilterRules:
    raw_paths = normalize_list(payload.get("paths"), "paths")
    raw_types = normalize_list(payload.get("types"), "types")
    raw_extensions = normalize_list(payload.get("extensions"), "extensions")
    raw_names = normalize_list(payload.get("names"), "names")
    if any(item == "" for item in raw_paths):
        raise ConfigError("path filters cannot contain an empty value")
    if any(item == "" for item in raw_names):
        raise ConfigError("name filters cannot contain an empty value")
    return FilterRules(
        paths=tuple(normalize_selector_path(item) for item in raw_paths),
        types=tuple(normalize_entry_type(item) for item in raw_types),
        extensions=tuple(normalize_extension(item) for item in raw_extensions),
        names=raw_names,
    )


# ─── CONFIG LOADING ─────────────────────────────────────────────────────────

def default_payload() -> dict[str, object]:
    return {
        "paths": [],
        "types": [],
        "extensions": [],
        "names": [],
        "ignore_hidden": False,
        "ignore_empty": False,
        "scan_styling": "full",
        "scan_emojis": True,
        "scan_data": SCAN_DATA_DEFAULT,
        "scan_timeout": DEFAULT_SCAN_TIMEOUT_SECONDS,
        "auto_copy": True,
    }


def _strip_yaml_comment(value: str) -> str:
    """Remove a trailing inline YAML comment while honoring quotes."""
    in_single = False
    in_double = False
    for index, char in enumerate(value):
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "#" and not in_single and not in_double:
            if index == 0 or value[index - 1].isspace():
                return value[:index]
    return value


def _unescape_yaml_double_quoted(inner: str) -> str:
    r"""Unescape a YAML double-quoted string body.

    Handles the escape sequences defined by the YAML spec (\n, \t, \r, \",
    \\, \/, \uXXXX, \xXX) while leaving all other characters (including
    multi-byte UTF-8) untouched. Using str.encode/decode with
    "unicode_escape" mangles non-ASCII content, so the unescape is performed
    one character at a time on the unicode string.
    """
    output: list[str] = []
    index = 0
    length = len(inner)
    hex_digits = "0123456789abcdefABCDEF"
    while index < length:
        char = inner[index]
        if char != "\\" or index == length - 1:
            output.append(char)
            index += 1
            continue
        next_char = inner[index + 1]
        simple = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "/": "/"}
        if next_char in simple:
            output.append(simple[next_char])
            index += 2
            continue
        if next_char == "u":
            escape = inner[index + 2 : index + 6]
            if len(escape) == 4 and all(ch in hex_digits for ch in escape):
                output.append(chr(int(escape, 16)))
                index += 6
                continue
        if next_char == "x":
            escape = inner[index + 2 : index + 4]
            if len(escape) == 2 and all(ch in hex_digits for ch in escape):
                output.append(chr(int(escape, 16)))
                index += 4
                continue
        # Unknown or malformed escape: preserve the backslash literally so
        # the value remains observable rather than silently rewritten.
        output.append(char)
        index += 1
    return "".join(output)


def _parse_yaml_scalar(raw: str) -> object:
    """Convert a YAML scalar literal into the matching Python value."""
    text = raw.strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        if len(text) >= 2:
            quote = text[0]
            inner = text[1:-1]
            if quote == '"':
                return _unescape_yaml_double_quoted(inner)
            return inner.replace("''", "'")
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~", "none"}:
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        number = float(text)
        if math.isfinite(number):
            return number
    except ValueError:
        pass
    return text


def _parse_yaml_inline_list(value: str) -> list[object]:
    """Parse a `[a, b, c]` flow sequence that has already been stripped of brackets."""
    items: list[object] = []
    buffer = ""
    in_single = False
    in_double = False
    for char in value:
        if char == "'" and not in_double:
            in_single = not in_single
            buffer += char
        elif char == '"' and not in_single:
            in_double = not in_double
            buffer += char
        elif char == "," and not in_single and not in_double:
            items.append(_parse_yaml_scalar(buffer))
            buffer = ""
        else:
            buffer += char
    if buffer.strip():
        items.append(_parse_yaml_scalar(buffer))
    return items


def parse_config_yaml(text: str, source: Path) -> dict[str, object]:
    """Parse the YAML subset supported by config.yaml without external deps.

    Supports top-level mappings, scalar literals (bool/int/float/string/null),
    inline flow lists (`[]` and `[a, b]`), block lists (`- item`), `#` comments
    (full-line and trailing), and single- or double-quoted strings. Duplicate
    keys are rejected. The grammar is intentionally restricted to the config
    schema documented in README.md so the runtime remains self-contained.
    """
    lines = text.splitlines()
    mapping: dict[str, object] = {}
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        index += 1
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key_part = _strip_yaml_comment(raw_line).rstrip()
        if not key_part.strip():
            continue
        if key_part.lstrip().startswith("- "):
            raise ConfigError(f"config file {source} is not a mapping: top-level list entry")
        if ":" not in key_part:
            raise ConfigError(f"config file {source}: invalid line: {sanitize_terminal_text(stripped)}")
        key, _, value = key_part.partition(":")
        key = key.strip()
        if not key:
            raise ConfigError(f"config file {source}: empty mapping key on line: {sanitize_terminal_text(stripped)}")
        if key in mapping:
            raise ConfigError(f"duplicate YAML key '{key}'")
        value = value.strip()
        if not value:
            if index < len(lines):
                lookahead = lines[index]
                lookahead_stripped = lookahead.strip()
                if lookahead_stripped.startswith("- ") or lookahead_stripped == "-":
                    items: list[object] = []
                    while index < len(lines):
                        item_line = lines[index]
                        item_stripped = item_line.strip()
                        if not item_stripped or item_stripped.startswith("#"):
                            index += 1
                            continue
                        if not (item_stripped.startswith("- ") or item_stripped == "-"):
                            break
                        item_body = item_stripped[1:].strip()
                        if item_body:
                            items.append(_parse_yaml_scalar(item_body))
                        index += 1
                    mapping[key] = items
                    continue
            mapping[key] = None
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            mapping[key] = _parse_yaml_inline_list(inner) if inner else []
            continue
        if value.startswith("{") and value.endswith("}"):
            raise ConfigError(f"config file {source}: inline mappings are not supported (key '{key}')")
        mapping[key] = _parse_yaml_scalar(value)
    return mapping


def load_yaml_payload(path: Path) -> dict[str, object]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            text = handle.read()
    except UnicodeError as exc:
        raise ConfigError(
            f"config file is not valid UTF-8: {sanitize_terminal_text(path)}"
        ) from exc
    except OSError as exc:
        raise ConfigError(f"unable to read config file: {sanitize_terminal_text(path)}") from exc

    try:
        loaded = parse_config_yaml(text, path)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"unable to parse config file: {sanitize_terminal_text(path)}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ConfigError(f"config file must contain a YAML mapping: {path}")
    return dict(loaded)


def find_config_path(explicit_path: str | None, root_path: Path) -> Path | None:
    if explicit_path is not None:
        path = expand_user_path(explicit_path, "config path")
        if not path.exists():
            raise ConfigError(f"config file does not exist: {path}")
        if not path.is_file():
            raise ConfigError(f"config path is not a file: {path}")
        return path
    # Project-local config: next to the scan root (or its parent when the
    # root is a file). `is_dir()` follows symlinks so a symlinked directory's
    # config is picked up correctly.
    if root_path.is_dir():
        candidate = root_path / CONFIG_FILE_NAME
    else:
        candidate = root_path.parent / CONFIG_FILE_NAME
    if candidate.is_file():
        return candidate
    # User-level config: XDG_CONFIG_HOME or ~/.config. Persists across
    # installs and is user-owned (never overwritten by the installer).
    try:
        xdg_base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    except RuntimeError:
        xdg_base = ""
    if xdg_base:
        xdg_config = Path(xdg_base) / "project-summarizer" / CONFIG_FILE_NAME
        if xdg_config.is_file():
            return xdg_config
    # Global installed config: next to the prs.py runtime. Supports layered
    # defaults (project-local > user > installed > built-in defaults).
    app_dir_config = Path(__file__).resolve().parent / CONFIG_FILE_NAME
    if app_dir_config.is_file():
        return app_dir_config
    return None


def config_from_payload(payload: dict[str, object]) -> dict[str, object]:
    # Accept both kebab-case (as documented in README.md and shipped in
    # config.yaml) and snake_case (as mentioned in SKILL.md) by normalizing
    # hyphens to underscores before lookup. Two originals that collapse onto
    # the same canonical key (e.g., "scan-styling" and "scan_styling" in the
    # same file) is a configuration error rather than a silent last-wins.
    merged = default_payload()
    canonical_to_original: dict[str, str] = {}
    for key, value in payload.items():
        normalized = key.replace("-", "_")
        if normalized not in merged:
            raise ConfigError(f"unknown config key: {key}")
        if normalized in canonical_to_original:
            raise ConfigError(
                f"config key collision: both '{canonical_to_original[normalized]}' and '{key}' map to '{normalized}'"
            )
        canonical_to_original[normalized] = key
        merged[normalized] = value
    return merged


def is_help_token(value: str) -> bool:
    return value in {"help", "-h", "--help"}


def is_version_token(value: str) -> bool:
    return value in {"version", "--version"}


def is_status_token(value: str) -> bool:
    return value == "status"


def render_status(color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    cwd_config = find_config_path(None, Path.cwd())
    config_text = sanitize_terminal_text(cwd_config) if cwd_config is not None else "not found (checked: project dir, user config, app dir)"
    # APP_DIR is the directory containing the prs executable that the user
    # actually invoked, falling back to the source file location when the
    # interpreter was started without an argv[0] (e.g., embedded runs).
    if sys.argv and sys.argv[0]:
        app_dir = Path(sys.argv[0]).resolve().parent
    else:
        app_dir = Path(__file__).resolve().parent
    rows = (
        ("product", PRODUCT_TITLE),
        ("command", PROGRAM_NAME),
        ("alias", ALIAS_COMMAND),
        ("version", VERSION),
        ("app dir", str(app_dir)),
        ("python", sys.executable),
        ("config", config_text),
    )
    label_width = max(len(label) for label, _ in rows)
    rule = "─" * min(terminal_columns(), BANNER_RULE_WIDTH_MAXIMUM)
    lines = [
        style(f"{PRODUCT_TITLE} status", ANSI_CYAN, ANSI_BOLD, enabled=color),
        style(rule, ANSI_DIM, ANSI_CYAN, enabled=color),
    ]
    for label, value in rows:
        lines.append(
            f"  {style(pad_cells(label, label_width), ANSI_BLUE, ANSI_BOLD, enabled=color)}  "
            f"{style(sanitize_terminal_text(value), ANSI_WHITE, enabled=color)}"
        )
    return "\n".join(lines) + "\n"


def render_version(color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    return f"{style(PROGRAM_NAME, ANSI_CYAN, ANSI_BOLD, enabled=color)} {style(VERSION, ANSI_GREEN, ANSI_BOLD, enabled=color)}\n"


class PrsArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ConfigError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:
        # argparse calls exit(0) when a help action fires; raise a dedicated
        # type so callers can render help instead of misreporting the exit
        # as a config error. status!=0 always originates from error().
        if status == 0:
            raise HelpRequested()
        raise ConfigError((message or "argument parsing failed").strip())


# ─── HELP RENDERING ─────────────────────────────────────────────────────────

def help_usage_line(color: bool) -> str:
    tokens = (
        ("prs", (ANSI_CYAN, ANSI_BOLD)),
        ("[path]", (ANSI_DIM,)),
        ("[mode]", (ANSI_DIM,)),
        ("[selectors]", (ANSI_DIM,)),
        ("[rendering]", (ANSI_DIM,)),
        ("[runtime]", (ANSI_DIM,)),
    )
    width = terminal_columns()
    lines: list[str] = []
    current: list[tuple[str, tuple[str, ...]]] = []
    current_width = 2
    for token, codes in tokens:
        added_width = cell_width(token) + (1 if current else 0)
        if current and current_width + added_width > width:
            lines.append("  " + " ".join(style(text, *token_codes, enabled=color) for text, token_codes in current))
            current = []
            current_width = 2
            added_width = cell_width(token)
        current.append((token, codes))
        current_width += added_width
    if current:
        lines.append("  " + " ".join(style(text, *token_codes, enabled=color) for text, token_codes in current))
    return "\n".join(lines)


def help_wrapped_lines(
    text: str, color: bool, *codes: str, indent: int = 0,
) -> list[str]:
    width = terminal_columns()
    longest_word = max((cell_width(word) for word in text.split()), default=0)
    readable_indent = max(0, width - longest_word) if longest_word <= width else 0
    prefix = " " * min(indent, readable_indent, max(0, width - 1))
    content_width = max(1, width - cell_width(prefix))
    chunks: list[str] = []
    current = ""
    for word in text.split():
        candidate = word if not current else f"{current} {word}"
        if cell_width(candidate) <= content_width:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if cell_width(word) <= content_width:
            current = word
        else:
            word_chunks = wrap_cells(word, content_width)
            chunks.extend(word_chunks[:-1])
            current = word_chunks[-1]
    if current or not chunks:
        chunks.append(current)
    return [prefix + style(chunk, *codes, enabled=color) for chunk in chunks]


def help_table(title: str, rows: Sequence[tuple[str, str]], color: bool) -> list[str]:
    width = terminal_columns()
    lines = ["", style(title, ANSI_CYAN, ANSI_BOLD, enabled=color)]
    if width < HELP_TABLE_MIN_WIDTH:
        for option, description in rows:
            lines.extend(help_wrapped_lines(option, color, ANSI_BLUE, ANSI_BOLD, indent=2))
            lines.extend(help_wrapped_lines(description, color, ANSI_WHITE, indent=4))
        return lines
    option_width = min(HELP_OPTION_COLUMN_MAX_WIDTH, max(cell_width(option) for option, _ in rows) + 2)
    desc_width = max(HELP_DESCRIPTION_COLUMN_MIN_WIDTH, width - option_width - HELP_TABLE_COLUMN_OVERHEAD)
    for option, description in rows:
        description_lines = wrap_cells(description, desc_width)
        option_text = style(option, ANSI_BLUE, ANSI_BOLD, enabled=color)
        first_desc = style(description_lines[0], ANSI_WHITE, enabled=color) if description_lines else ""
        lines.append(f"  {option_text}{' ' * max(0, option_width - cell_width(option))}{first_desc}")
        for chunk in description_lines[1:]:
            lines.append(" " * (option_width + 2) + style(chunk, ANSI_WHITE, enabled=color))
    return lines


def render_help(color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    width = terminal_columns()
    rule = "─" * min(width, BANNER_RULE_WIDTH_MAXIMUM)
    lines: list[str] = []
    lines.append(style(f"{PRODUCT_TITLE} ({PROGRAM_NAME})", ANSI_CYAN, ANSI_BOLD, enabled=color))
    lines.append(style("Fast project context for terminals and AI agents.", ANSI_WHITE, enabled=color))
    lines.append(style(rule, ANSI_DIM, ANSI_CYAN, enabled=color))
    lines.extend(("", style("Usage", ANSI_CYAN, ANSI_BOLD, enabled=color), help_usage_line(color)))
    lines.extend(("", style("Commands", ANSI_CYAN, ANSI_BOLD, enabled=color)))
    command_rows = (
        ("prs", "scan the current directory"),
        ("prs <path>", "scan a file or directory"),
        ("prs help", "show full help"),
        ("prs version", "show version"),
        ("prs status", "show runtime status"),
    )
    command_width = max(cell_width(command) for command, _ in command_rows) + 2
    for command, description in command_rows:
        lines.append(
            f"  {style(pad_cells(command, command_width), ANSI_BLUE, ANSI_BOLD, enabled=color)} "
            f"{style(description, ANSI_WHITE, enabled=color)}"
        )
    lines.extend(("", style("Examples", ANSI_CYAN, ANSI_BOLD, enabled=color)))
    for example in (
        "prs",
        "prs ~/code/app",
        "prs --only .md",
        'prs ~/code/app --only -e .ts .tsx --scan-data "tree, lines, size"',
        "prs ~/code/app --ignore-hidden --ignore-empty --scan-styling full",
    ):
        lines.append("  " + style(example, ANSI_GREEN, enabled=color))
    lines.append(style(rule, ANSI_DIM, ANSI_CYAN, enabled=color))
    lines.extend(help_table("Filter modes", (
        ("--ignore", "Exclude entries matching selectors. Default mode when filters exist."),
        ("--only", "Include only entries matching selectors. Shorthand accepted, e.g. --only .md."),
        ("--full", "Include every entry, including hidden and empty entries."),
    ), color))
    lines.extend(help_table("Selectors", (
        ("-f, --paths <paths...>", "Match relative paths and everything inside matched directories."),
        ("-t, --types <types...>", "Match entry types: file, dir, link."),
        ("-e, --extensions <ext...>", "Match extensions such as .ts, .json, or .md."),
        ("-n, --names <names...>", "Match exact file or directory basenames."),
    ), color))
    lines.extend(help_table("Visibility", (
        ("--ignore-hidden", "Hide dot-prefixed files and directories."),
        ("--ignore-empty", "Hide empty files and directories."),
    ), color))
    lines.extend(help_table("Rendering", (
        ("--scan-styling <full|low|minimal>", "Set layout style: full, low, or minimal. Color follows terminal support."),
        ("--scan-emojis <true|false>", "Show or hide file-type emojis in entry names."),
        ("--scan-data <\"item, item, ...\">", "Comma-separated items: tree, lines, size, modified, type, git, summary."),
    ), color))
    lines.extend(help_table("Runtime", (
        ("--scan-timeout <seconds>", "Stop after the given time and print the partial result."),
        ("--auto-copy <true|false>", "Copy plain scan output to the clipboard after rendering."),
    ), color))
    lines.extend(help_table("Configuration", (("--config <path>", "Use a specific config.yaml file."),), color))
    lines.append("")
    lines.extend(help_wrapped_lines("Command-like path names can be scanned with ./help, ./status, or ./version.", color, ANSI_DIM))
    lines.extend(help_wrapped_lines("Selector values beginning with '-' use attached syntax, for example --names=-draft.", color, ANSI_DIM))
    return "\n".join(lines) + "\n"


def render_usage_error(message: str, color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    message = sanitize_terminal_text(message)
    lines = help_wrapped_lines(f"{PROGRAM_NAME}: {message}", color, ANSI_RED, ANSI_BOLD)
    lines.extend(("", style("Usage", ANSI_CYAN, ANSI_BOLD, enabled=color), help_usage_line(color)))
    lines.extend(help_wrapped_lines("prs help", color, ANSI_CYAN, indent=2))
    return "\n".join(lines) + "\n"


# ─── CLI PARSING ────────────────────────────────────────────────────────────

# Argv-preprocessing tables. These duplicate flag names from build_parser
# because argv must be reordered/expanded BEFORE the parser is constructed
# (the parser would otherwise misinterpret a path token as a selector
# value). Keep in sync with build_parser when adding flags.
MODE_FLAGS = {"--ignore", "--only"}
SELECTOR_FLAGS = {
    "-f": "--paths", "--paths": "--paths",
    "-t": "--types", "--types": "--types",
    "-e": "--extensions", "--extensions": "--extensions",
    "-n": "--names", "--names": "--names",
}
# Selector long forms in canonical output order (used by expand_selector_shortcuts
# to emit grouped shortcuts in a stable order).
SELECTOR_ORDER = ("--paths", "--types", "--extensions", "--names")
VALUE_FLAGS = {
    "--scan-styling", "--scan-emojis", "--scan-data", "--scan-timeout",
    "--auto-copy", "--config",
}
FLAG_OPTIONS = {
    *MODE_FLAGS, "--full", "--ignore-hidden", "--ignore-empty",
    "--help", "-h", "--version",
}


def build_parser() -> argparse.ArgumentParser:
    parser = PrsArgumentParser(
        prog=PROGRAM_NAME,
        description="Project Summarizer scans a project directory and prints a compact information-rich tree.",
        allow_abbrev=False,
        add_help=False,
    )
    parser.add_argument("path", nargs="?", default=".", help="directory or file to scan")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--ignore", action="store_true", help="exclude entries matching filters")
    mode_group.add_argument("--only", action="store_true", help="include only entries matching filters")
    mode_group.add_argument("--full", action="store_true", help="include all entries, including hidden and empty entries")

    parser.add_argument("-f", "--paths", nargs="+", action="append", help="match relative paths and matched directory contents")
    parser.add_argument("-t", "--types", nargs="+", action="append", help="match entry types: file, dir, link")
    parser.add_argument("-e", "--extensions", nargs="+", action="append", help="match file extensions, such as .ts or .md")
    parser.add_argument("-n", "--names", nargs="+", action="append", help="match exact file or directory basenames")

    parser.add_argument("--ignore-hidden", action="store_true", default=None, help="ignore dot-prefixed files and directories")
    parser.add_argument("--ignore-empty", action="store_true", default=None, help="ignore empty files and directories")
    parser.add_argument("--scan-styling", choices=STYLING_LEVELS, help="set output scan-styling")
    parser.add_argument("--scan-emojis", choices=("true", "false"), help="show or hide emojis")
    parser.add_argument("--scan-data", help="comma-separated items: tree, lines, size, modified, type, git, summary")
    parser.add_argument("--scan-timeout", type=float, help="stop scanning after seconds and print partial result")
    parser.add_argument("--auto-copy", choices=("true", "false"), help="copy scan output to clipboard")
    parser.add_argument("--config", help="use a specific config.yaml file")
    parser.add_argument("--help", "-h", action="store_true", help="show help")
    parser.add_argument("--version", action="store_true", help="show version")
    return parser


def selector_flag_for_shortcut(value: str) -> str:
    normalized = value
    if not normalized:
        return "--names"
    if normalized in ENTRY_TYPES:
        return "--types"
    if normalized == ".":
        return "--paths"
    if normalized.startswith(".") and "/" not in normalized and "\\" not in normalized:
        return "--extensions"
    if "/" in normalized or "\\" in normalized:
        return "--paths"
    return "--names"


def scan_path_exists(value: str) -> bool:
    """Return True if the expanded path exists (without following symlinks).

    Propagates ConfigError from expand_user_path (e.g., unresolvable HOME)
    so the user sees the underlying configuration problem instead of having
    the path silently treated as non-existent.
    """
    return os.path.lexists(expand_user_path(value, "scan path"))


def is_unambiguous_scan_path(value: str) -> bool:
    """Heuristic: a token is unambiguously a scan path if it is absolute,
    contains a path separator, or names an existing directory.

    Existing files (and symlinks-to-files) are intentionally NOT treated as
    unambiguous paths: a bare basename like ``package.json`` is more likely
    a ``--names`` selector value than a scan path. Directories are
    unambiguous because ``prs <dir>`` is the canonical scan form and
    directories are rarely valid selector values.
    """
    expanded = expand_user_path(value, "scan path")
    return expanded.is_absolute() or "/" in value or "\\" in value or expanded.is_dir()


def extract_scan_path_argument(argv: Sequence[str]) -> list[str]:
    """Move an unambiguous scan path ahead of variable-length selectors."""
    tokens = list(argv)
    path_indexes: list[int] = []
    inferred_path_indexes: list[int] = []
    ambiguous: list[tuple[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            trailing = tokens[index + 1 :]
            if len(trailing) != 1:
                raise ConfigError("'--' must be followed by exactly one scan path")
            if path_indexes:
                values = ", ".join(
                    [sanitize_terminal_text(tokens[item]) for item in path_indexes]
                    + [sanitize_terminal_text(trailing[0])]
                )
                raise ConfigError(f"multiple scan paths were provided: {values}")
            return tokens
        if token in VALUE_FLAGS:
            index += 2
            continue
        if token in FLAG_OPTIONS:
            if token in MODE_FLAGS:
                run_start = index + 1
                run_end = run_start
                while run_end < len(tokens) and not tokens[run_end].startswith("-"):
                    run_end += 1
                run = tokens[run_start:run_end]
                if len(run) > 1 and (scan_path_exists(run[-1]) or is_unambiguous_scan_path(run[-1])):
                    if is_unambiguous_scan_path(run[-1]):
                        inferred_path_indexes.append(run_end - 1)
                    else:
                        ambiguous.append((token, run[-1]))
                index = run_end
                continue
            index += 1
            continue
        canonical_selector = SELECTOR_FLAGS.get(token)
        if canonical_selector is not None:
            run_start = index + 1
            run_end = run_start
            while run_end < len(tokens) and not tokens[run_end].startswith("-"):
                run_end += 1
            run = tokens[run_start:run_end]
            if len(run) > 1 and (scan_path_exists(run[-1]) or is_unambiguous_scan_path(run[-1])):
                if canonical_selector in {"--paths", "--names"}:
                    ambiguous.append((token, run[-1]))
                else:
                    inferred_path_indexes.append(run_end - 1)
            index = run_end
            continue
        if token.startswith("-"):
            index += 1
            continue
        path_indexes.append(index)
        index += 1

    unique_indexes = list(dict.fromkeys(path_indexes or inferred_path_indexes))
    if len(unique_indexes) > 1:
        values = ", ".join(sanitize_terminal_text(tokens[item]) for item in unique_indexes)
        raise ConfigError(f"multiple scan paths were provided: {values}")
    if not unique_indexes and ambiguous:
        flag, value = ambiguous[0]
        raise ConfigError(
            f"ambiguous scan path '{sanitize_terminal_text(value)}' after {flag}; "
            "place the scan path before the selector or after '--'"
        )
    if unique_indexes:
        path_index = unique_indexes[0]
        path = tokens.pop(path_index)
        tokens.insert(0, path)
    return tokens


def expand_selector_shortcuts(argv: Sequence[str]) -> list[str]:
    if not any(token in MODE_FLAGS for token in argv):
        return list(argv)
    output: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in MODE_FLAGS:
            output.append(token)
            index += 1
            grouped: dict[str, list[str]] = {}
            while index < len(argv) and not argv[index].startswith("-"):
                shortcut = argv[index]
                grouped.setdefault(selector_flag_for_shortcut(shortcut), []).append(shortcut)
                index += 1
            for flag in SELECTOR_ORDER:
                if flag in grouped:
                    output.extend([flag, *grouped[flag]])
            continue
        output.append(token)
        index += 1
    return output

def cli_rules(args: argparse.Namespace) -> FilterRules:
    def flatten(groups: list[list[str]] | None) -> list[str]:
        return [item for group in groups or [] for item in group]

    payload: dict[str, object] = {
        "paths": flatten(args.paths),
        "types": flatten(args.types),
        "extensions": flatten(args.extensions),
        "names": flatten(args.names),
    }
    return normalize_rules(payload)


def resolve_runtime_config(argv: Sequence[str]) -> RuntimeConfig:
    parser = build_parser()
    expanded_argv = expand_selector_shortcuts(extract_scan_path_argument(argv))
    args = parser.parse_args(expanded_argv)
    # args.help / args.version are unreachable here: run() dispatches the
    # help/version tokens before resolve_runtime_config is called, and the
    # parser's own --help/--version actions are store_true (no exit). If
    # argparse ever does call parser.exit() for help, HelpRequested propagates.

    root_path = resolve_scan_path(args.path)
    if not os.path.lexists(root_path):
        raise ConfigError(f"scan path does not exist: {root_path}")

    config_path = find_config_path(args.config, root_path)
    file_payload = load_yaml_payload(config_path) if config_path is not None else {}
    config_payload = config_from_payload(file_payload)
    config_rules = normalize_rules(config_payload)
    explicit_rules = cli_rules(args)

    rules = FilterRules(
        paths=explicit_rules.paths if args.paths is not None else config_rules.paths,
        types=explicit_rules.types if args.types is not None else config_rules.types,
        extensions=explicit_rules.extensions if args.extensions is not None else config_rules.extensions,
        names=explicit_rules.names if args.names is not None else config_rules.names,
    )

    if args.full:
        filter_mode = "full"
        rules = FilterRules()
    elif args.only:
        # --only uses explicit CLI selectors only; config defaults (e.g., the
        # ignored-name list) must not be reinterpreted as include selectors.
        filter_mode = "only"
        rules = explicit_rules
        if not rules.has_rules:
            raise ConfigError("--only requires at least one filter selector")
    else:
        filter_mode = "ignore"

    ignore_hidden = normalize_bool(config_payload["ignore_hidden"], "ignore_hidden")
    ignore_empty = normalize_bool(config_payload["ignore_empty"], "ignore_empty")
    scan_styling = normalize_choice(config_payload["scan_styling"], "scan_styling", STYLING_LEVELS)
    scan_emojis = normalize_bool(config_payload["scan_emojis"], "scan_emojis")
    scan_data = normalize_scan_data(config_payload["scan_data"])
    scan_timeout = normalize_float(config_payload["scan_timeout"], "scan_timeout")
    auto_copy = normalize_bool(config_payload["auto_copy"], "auto_copy")

    if args.ignore_hidden:
        ignore_hidden = True
    if args.ignore_empty:
        ignore_empty = True
    if args.scan_styling is not None:
        scan_styling = args.scan_styling
    if args.scan_emojis is not None:
        scan_emojis = args.scan_emojis == "true"
    if args.scan_data is not None:
        scan_data = normalize_scan_data(args.scan_data)
    if args.scan_timeout is not None:
        scan_timeout = normalize_float(args.scan_timeout, "scan_timeout")
    if args.auto_copy is not None:
        auto_copy = args.auto_copy == "true"

    if filter_mode == "full":
        ignore_hidden = False
        ignore_empty = False

    return RuntimeConfig(
        root_path=root_path,
        config_path=config_path,
        filter_mode=filter_mode,
        rules=rules,
        ignore_hidden=ignore_hidden,
        ignore_empty=ignore_empty,
        scan_styling=scan_styling,
        scan_emojis=scan_emojis,
        scan_data=scan_data,
        scan_timeout=scan_timeout,
        auto_copy=auto_copy,
    )


# ─── FILTERING ──────────────────────────────────────────────────────────────

def is_hidden_rel_path(rel_path: str) -> bool:
    if rel_path in {"", "."}:
        return False
    return any(part.startswith(".") for part in rel_path.split("/") if part)


def path_selector_matches(selector: str, rel_path: str) -> bool:
    if selector == ".":
        return True
    if not selector:
        return False
    return rel_path == selector or rel_path.startswith(f"{selector}/")


def rules_match(node: EntryNode, rules: FilterRules) -> bool:
    suffix = node.path.suffix.lower() if node.kind == "file" else ""
    return any(
        (
            any(path_selector_matches(selector, node.rel_path) for selector in rules.paths),
            node.kind in rules.types,
            bool(suffix and suffix in rules.extensions),
            node.name in rules.names,
        )
    )


def should_keep_node(node: EntryNode, config: RuntimeConfig, is_root: bool) -> bool:
    if is_root and node.kind == "dir":
        return True
    if config.ignore_hidden and is_hidden_rel_path(node.rel_path):
        return False
    if config.filter_mode == "ignore" and rules_match(node, config.rules):
        return False
    if config.filter_mode == "only":
        if node.kind == "dir" and (rules_match(node, config.rules) or node.children):
            return True
        if not rules_match(node, config.rules):
            return False
    if config.ignore_empty:
        if node.kind == "file" and node.size == 0:
            return False
        if node.kind == "dir" and not node.children:
            return False
    return True


# ─── FILE METADATA ──────────────────────────────────────────────────────────

def count_file_lines_fd(file_fd: int, state: ScanState) -> int | None:
    """Count newlines in an open file descriptor.

    Returns None if the file is binary (contains a NUL byte) or if the scan
    deadline is reached mid-read. The binary check scans every chunk rather
    than only the first one so files with binary content past the start are
    still detected instead of being miscounted as text.
    """
    total = 0
    saw_bytes = False
    last_byte = b""
    while True:
        chunk = os.read(file_fd, TEXT_READ_CHUNK_SIZE)
        if not chunk:
            break
        if b"\0" in chunk:
            return None
        saw_bytes = True
        total += chunk.count(b"\n")
        last_byte = chunk[-1:]
        if state.timeout_reached():
            return None
    if saw_bytes and last_byte != b"\n":
        total += 1
    return total


def file_snapshot_identity(path_stat: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        path_stat.st_dev,
        path_stat.st_ino,
        stat.S_IFMT(path_stat.st_mode),
        path_stat.st_size,
        path_stat.st_mtime_ns,
        path_stat.st_ctime_ns,
    )


def stat_kind_from_mode(mode: int) -> str:
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


def link_target(name: str, parent_fd: int | None = None) -> str:
    return os.readlink(name, dir_fd=parent_fd)


# ─── SCANNING ───────────────────────────────────────────────────────────────

def prefilter_entry(path: Path, rel_path: str, name: str, kind: str, config: RuntimeConfig) -> bool:
    if config.ignore_hidden and is_hidden_rel_path(rel_path):
        return False
    probe = EntryNode(path=path, rel_path=rel_path, name=name, kind=kind, size=0, mtime=0)
    if config.filter_mode == "ignore" and rules_match(probe, config.rules):
        return False
    if config.filter_mode == "only" and kind == "dir":
        path_rules = config.rules.paths
        path_relevant = any(
            path_selector_matches(selector, rel_path) or selector.startswith(f"{rel_path}/")
            for selector in path_rules
        )
        if path_rules and not path_relevant and not config.rules.types and not config.rules.extensions and not config.rules.names:
            return False
    return True


def prefilter_before_stat(rel_path: str, name: str, config: RuntimeConfig) -> bool:
    if config.ignore_hidden and is_hidden_rel_path(rel_path):
        return False
    if config.filter_mode == "ignore":
        if name in config.rules.names or any(path_selector_matches(selector, rel_path) for selector in config.rules.paths):
            return False
    if config.filter_mode == "only" and config.rules.paths and not config.rules.types and not config.rules.extensions and not config.rules.names:
        return any(
            path_selector_matches(selector, rel_path) or selector.startswith(f"{rel_path}/")
            for selector in config.rules.paths
        )
    return True


def initialize_aggregate(node: EntryNode) -> None:
    if node.kind == "file":
        node.total_files = 1
        node.total_lines = node.lines or 0
        node.unknown_lines = int(node.lines is None)
        node.largest_file = (node.summary_path, node.size)
        node.newest_entry = (node.summary_path, node.mtime)
        return
    if node.kind == "link":
        node.total_links = 1
        node.newest_entry = (node.summary_path, node.mtime)
        return
    node.total_dirs = 1
    node.total_files = sum(child.total_files for child in node.children)
    node.total_dirs += sum(child.total_dirs for child in node.children)
    node.total_links = sum(child.total_links for child in node.children)
    node.total_lines = sum(child.total_lines for child in node.children)
    node.unknown_lines = sum(child.unknown_lines for child in node.children)
    largest = [child.largest_file for child in node.children if child.largest_file is not None]
    node.largest_file = max(largest, key=lambda item: item[1], default=None)
    newest_leaves = [
        child.newest_entry for child in node.children
        if child.newest_entry is not None and (child.total_files or child.total_links)
    ]
    newest_any = [child.newest_entry for child in node.children if child.newest_entry is not None]
    node.newest_entry = max(newest_leaves or newest_any, key=lambda item: item[1], default=(node.summary_path, node.mtime))


def create_leaf(
    path: Path, rel_path: str, path_stat: os.stat_result, kind: str,
    state: ScanState, is_root: bool, parent_fd: int | None = None,
    entry_name: str | None = None,
) -> EntryNode | None:
    name = path.name or str(path)
    if kind == "special":
        state.warn(rel_path, "unsupported special filesystem entry skipped")
        return None
    if not is_root and not prefilter_entry(path, rel_path, name, kind, state.config):
        return None
    if kind == "link":
        try:
            target = link_target(entry_name or os.fspath(path), parent_fd)
        except OSError as exc:
            target = None
            state.warn(rel_path, f"unable to read link target: {exc}")
        try:
            observed_stat = os.stat(
                entry_name or os.fspath(path), dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if (observed_stat.st_dev, observed_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                state.warn(rel_path, "entry changed while it was being scanned")
                return None
        except OSError as exc:
            state.warn(rel_path, f"unable to verify link metadata: {exc}")
            return None
        node = EntryNode(path, rel_path, name, kind, 0, observed_stat.st_mtime, target=target)
    else:
        access_name = entry_name or os.fspath(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        stable_result: tuple[os.stat_result, int | None] | None = None
        instability = "file changed while its content was being inspected"
        attempts = 0
        while attempts < FILE_READ_MAX_ATTEMPTS and not state.timeout_reached():
            attempts += 1
            file_fd: int | None = None
            try:
                file_fd = os.open(access_name, flags, dir_fd=parent_fd)
                opened_stat = os.fstat(file_fd)
                if (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                    instability = "directory entry was replaced while the file was being scanned"
                    continue
                if not stat.S_ISREG(opened_stat.st_mode):
                    instability = "entry type changed while the file was being scanned"
                    continue
                try:
                    lines = count_file_lines_fd(file_fd, state)
                except OSError as exc:
                    state.warn(rel_path, f"unable to count lines: {exc}")
                    return None
                observed_stat = os.fstat(file_fd)
                try:
                    current_path_stat = os.stat(access_name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError as exc:
                    instability = f"unable to verify the directory entry after reading: {exc}"
                    continue
                if (current_path_stat.st_dev, current_path_stat.st_ino) != (
                    observed_stat.st_dev, observed_stat.st_ino
                ):
                    instability = "directory entry was replaced while the file was being read"
                    continue
                if file_snapshot_identity(opened_stat) != file_snapshot_identity(observed_stat):
                    instability = "file metadata changed while its content was being read"
                    continue
                stable_result = observed_stat, lines
                break
            except OSError as exc:
                instability = f"unable to inspect file safely: {exc}"
            finally:
                if file_fd is not None:
                    os.close(file_fd)
        if stable_result is None:
            if attempts == 0 and state.timed_out:
                return None
            state.warn(rel_path, f"{instability}; skipped after {attempts} attempt(s)")
            return None
        observed_stat, lines = stable_result
        if observed_stat.st_size == 0 and state.config.ignore_empty:
            return None
        node = EntryNode(
            path, rel_path, name, kind, observed_stat.st_size,
            observed_stat.st_mtime, lines=lines,
        )
    initialize_aggregate(node)
    return node if should_keep_node(node, state.config, is_root) else None


@dataclass
class DirectoryFrame:
    path: Path
    rel_path: str
    path_stat: os.stat_result
    is_root: bool
    iterator: Iterator[os.DirEntry[str]]
    fd: int
    children: list[EntryNode] = field(default_factory=list)


def directory_frame(
    path: Path, rel_path: str, path_stat: os.stat_result, state: ScanState,
    is_root: bool, fd: int,
) -> DirectoryFrame:
    try:
        iterator = os.scandir(fd)
    except OSError as exc:
        if exc.errno == errno.EMFILE:
            state.warn(rel_path, "file descriptor limit reached; subtree skipped")
        else:
            state.warn(rel_path, f"unable to read directory: {exc}")
        iterator = iter(())
    return DirectoryFrame(path, rel_path, path_stat, is_root, iterator, fd)


def close_directory_frame(frame: DirectoryFrame) -> None:
    close = getattr(frame.iterator, "close", None)
    if close is not None:
        close()
    os.close(frame.fd)


def scan_path(
    path: Path, rel_path: str, state: ScanState, is_root: bool = False,
    physical_path: Path | None = None,
) -> EntryNode | None:
    access_path = physical_path or path
    try:
        root_stat = access_path.lstat()
    except OSError as exc:
        state.warn(rel_path, f"unable to stat entry: {exc}")
        return None
    root_kind = stat_kind_from_mode(root_stat.st_mode)
    # Follow a symlinked scan root so its contents are scanned rather than
    # reporting the link as a leaf. Broken symlinks stay classified as "link"
    # so the caller still surfaces the leaf entry. The resolved target is
    # used to open the directory because O_NOFOLLOW would reject the link
    # path itself.
    if root_kind == "link" and is_root:
        try:
            target_stat = access_path.stat()
        except OSError:
            pass
        else:
            if stat.S_ISDIR(target_stat.st_mode):
                root_stat = target_stat
                root_kind = "dir"
                try:
                    access_path = access_path.resolve()
                except OSError:
                    pass
    if root_kind != "dir":
        return create_leaf(access_path, rel_path, root_stat, root_kind, state, is_root)

    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    root_fd: int | None = None
    try:
        root_fd = os.open(access_path, directory_flags)
        opened_root_stat = os.fstat(root_fd)
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        state.warn(rel_path, f"unable to open directory: {exc}")
        return None
    if (opened_root_stat.st_dev, opened_root_stat.st_ino) != (root_stat.st_dev, root_stat.st_ino):
        os.close(root_fd)
        state.warn(rel_path, "scan root changed while it was being opened")
        return None
    stack = [directory_frame(path, rel_path, opened_root_stat, state, is_root, root_fd)]
    completed: EntryNode | None = None
    while stack:
        frame = stack[-1]
        # Try to advance the iterator. Three outcomes:
        #   - timeout already reached: close the frame
        #   - next() raised (StopIteration or OSError): close the frame
        #   - next() returned an entry: process it below
        entry: os.DirEntry[str] | None = None
        if not state.timeout_reached():
            try:
                entry = next(frame.iterator)
            except StopIteration:
                entry = None
            except OSError as exc:
                state.warn(frame.rel_path, f"unable to continue reading directory: {exc}")
                entry = None
        if entry is None:
            close_directory_frame(frame)
            frame.children.sort(key=sort_key)
            size = sum(child.size for child in frame.children)
            mtime = max((child.mtime for child in frame.children), default=frame.path_stat.st_mtime)
            node = EntryNode(frame.path, frame.rel_path, frame.path.name or str(frame.path), "dir", size, mtime, children=frame.children)
            initialize_aggregate(node)
            kept = node if should_keep_node(node, state.config, frame.is_root) else None
            stack.pop()
            if stack and kept is not None:
                stack[-1].children.append(kept)
            else:
                completed = kept
            continue
        child_path = frame.path / entry.name
        child_rel = f"{frame.rel_path}/{entry.name}" if frame.rel_path != "." else entry.name
        if not prefilter_before_stat(child_rel, entry.name, state.config):
            continue
        try:
            child_stat = os.stat(entry.name, dir_fd=frame.fd, follow_symlinks=False)
        except OSError as exc:
            state.warn(child_rel, f"unable to stat entry: {exc}")
            continue
        child_kind = stat_kind_from_mode(child_stat.st_mode)
        if not prefilter_entry(child_path, child_rel, entry.name, child_kind, state.config):
            continue
        if child_kind == "dir":
            child_fd: int | None = None
            try:
                child_fd = os.open(entry.name, directory_flags, dir_fd=frame.fd)
                opened_child_stat = os.fstat(child_fd)
                if (opened_child_stat.st_dev, opened_child_stat.st_ino) != (child_stat.st_dev, child_stat.st_ino):
                    os.close(child_fd)
                    child_fd = None
                    state.warn(child_rel, "directory changed while it was being opened")
                    continue
            except OSError as exc:
                if child_fd is not None:
                    os.close(child_fd)
                state.warn(child_rel, f"unable to open directory safely: {exc}")
                continue
            if child_fd is None:
                continue
            stack.append(directory_frame(child_path, child_rel, opened_child_stat, state, False, child_fd))
        else:
            child = create_leaf(
                child_path, child_rel, child_stat, child_kind, state, False,
                parent_fd=frame.fd, entry_name=entry.name,
            )
            if child is not None:
                frame.children.append(child)
    return completed


SORT_KIND_RANK = {"dir": 0, "file": 1, "link": 2}


def sort_key(node: EntryNode) -> tuple[int, str, str]:
    return SORT_KIND_RANK[node.kind], node.name.casefold(), node.name


def scan(config: RuntimeConfig) -> ScanResult:
    started_at = time.monotonic()
    physical_root = physical_git_path(config.root_path)
    state = ScanState(
        config=config, started_at=started_at,
        deadline=started_at + config.scan_timeout,
        physical_root=physical_root,
    )
    root = scan_path(config.root_path, ".", state, is_root=True, physical_path=physical_root)
    if root is None:
        raise PrsError("scan root was excluded by the active filters or is not a supported filesystem entry")
    git_markers: dict[str, str] = {}
    deleted_git_entries: list[DeletedGitEntry] = []
    if "git" in config.scan_data:
        git_markers, deleted_git_entries, git_warning = load_git_markers(state.physical_root, state)
        if git_warning is not None:
            state.warn(".", git_warning)
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return ScanResult(
        root=root, elapsed_ms=elapsed_ms, timed_out=state.timed_out,
        warnings=state.warnings, git_markers=git_markers,
        deleted_git_entries=deleted_git_entries,
    )


# ─── GIT STATUS ─────────────────────────────────────────────────────────────

def run_git(
    args: Sequence[str], cwd: Path, state: ScanState, input_data: bytes | None = None,
) -> tuple[subprocess.CompletedProcess[bytes] | None, str | None]:
    if state.git_deadline is None:
        state.git_deadline = min(state.deadline, time.monotonic() + GIT_TIMEOUT_CAP_SECONDS)
    remaining = max(0.0, min(state.deadline, state.git_deadline) - time.monotonic())
    if remaining <= 0:
        if state.remaining() <= 0:
            state.timed_out = True
            return None, "scan deadline reached before git status completed"
        return None, f"Git operations exceeded their shared {GIT_TIMEOUT_CAP_SECONDS:g}s safety budget"
    scan_remaining = state.remaining()
    safety_limited = remaining < scan_remaining
    timeout = remaining
    environment = os.environ.copy()
    for key in GIT_ENVIRONMENT_KEYS:
        environment.pop(key, None)
    for key in tuple(environment):
        if key == "GIT_CONFIG_COUNT" or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(key, None)
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    command = ["git"]
    for override in GIT_CONFIG_OVERRIDES:
        command.extend(("-c", override))
    command.extend(args)
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            input=input_data,
            check=False,
            timeout=timeout,
        ), None
    except subprocess.TimeoutExpired:
        if safety_limited:
            return None, f"Git operations exceeded their shared {GIT_TIMEOUT_CAP_SECONDS:g}s safety budget"
        state.timed_out = True
        return None, "git command exceeded the remaining scan deadline"
    except OSError as exc:
        return None, f"unable to execute git: {exc}"


def git_root_for(path: Path, state: ScanState) -> tuple[Path | None, str | None]:
    cwd = path if path.is_dir() and not path.is_symlink() else path.parent
    result, error = run_git(["rev-parse", "--show-toplevel"], cwd, state)
    if result is None:
        return None, error
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or "Git could not discover a working tree"
        return None, f"Git repository discovery failed: {sanitize_terminal_text(detail)}"
    root = os.fsdecode(result.stdout.rstrip(b"\r\n"))
    if not root:
        return None, "Git repository discovery returned an empty working-tree path"
    return Path(os.path.realpath(root)), None


def marker_from_status(status: str) -> str:
    """Pick a single display marker from a porcelain v1 XY status code.

    Porcelain v1 packs index status (X) and worktree status (Y) into two
    characters, so a single path can carry multiple flags (e.g., ``MD``
    = modified in index, deleted in worktree). The precedence order is
    ``?`` > ``D`` > ``R`` > ``A`` > ``M``: untracked wins because it has
    no index counterpart; deletion is most actionable; renames and adds
    are structural; modified is the catch-all for any remaining change.
    """
    if "?" in status:
        return "[?]"
    if "D" in status:
        return "[D]"
    if "R" in status:
        return "[R]"
    if "A" in status:
        return "[A]"
    if status.strip():
        return "[M]"
    return ""


def parse_porcelain_z(payload: bytes) -> dict[str, str]:
    markers: dict[str, str] = {}
    records = payload.split(b"\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 3:
            continue
        status = record[:2].decode("ascii", "replace")
        path = os.fsdecode(record[3:])
        marker = marker_from_status(status)
        if path and marker:
            markers[git_path_identity(path)] = marker
        if "R" in status or "C" in status:
            index += 1
    return markers


def parse_index_deleted(payload: bytes) -> list[tuple[str, str, str]]:
    deleted: list[tuple[str, str, str]] = []
    for record in payload.split(b"\0"):
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) < 3:
            continue
        deleted.append((git_path_identity(os.fsdecode(raw_path)), fields[0].decode("ascii", "replace"), fields[1].decode("ascii", "replace")))
    return deleted


def parse_tree_entries(payload: bytes) -> dict[str, tuple[str, str]]:
    entries: dict[str, tuple[str, str]] = {}
    for record in payload.split(b"\0"):
        metadata, separator, raw_path = record.partition(b"\t")
        fields = metadata.split()
        if not separator or len(fields) < 3:
            continue
        path = git_path_identity(os.fsdecode(raw_path))
        entries[path] = (
            fields[0].decode("ascii", "replace"),
            fields[2].decode("ascii", "replace"),
        )
    return entries


def executable_git_config_warning(repo_root: Path, state: ScanState) -> str | None:
    result, error = run_git(
        [
            "config", "--includes", "--null", "--show-scope", "--show-origin",
            "--name-only", "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process)|diff\.external|diff\..*\.(command|textconv)|core\.(fsmonitor|hookspath))$",
        ],
        repo_root, state,
    )
    if result is None:
        return error or "unable to verify Git configuration safety"
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        detail = os.fsdecode(result.stderr).strip() or "git config inspection failed"
        return f"unable to verify Git configuration safety: {sanitize_terminal_text(detail)}"
    fields = result.stdout.split(b"\0")
    if fields and fields[-1] == b"":
        fields.pop()
    if len(fields) % 3:
        return "unable to verify Git configuration safety: git config returned malformed output"
    keys = sorted({
        sanitize_terminal_text(os.fsdecode(fields[index + 2]))
        for index in range(0, len(fields), 3)
        if os.fsdecode(fields[index]).lower() in {"local", "worktree"}
    })
    if not keys:
        return None
    return "Git markers disabled because executable repository configuration is active: " + ", ".join(keys)


def deleted_git_metadata(
    repo_root: Path, state: ScanState, deleted_paths: set[str],
) -> tuple[list[DeletedGitEntry], str | None]:
    indexed, error = run_git(["ls-files", "--deleted", "--stage", "-z"], repo_root, state)
    if indexed is None:
        return [], error or "unable to inspect deleted Git index entries"
    if indexed.returncode != 0:
        detail = os.fsdecode(indexed.stderr).strip() or "git ls-files failed"
        return [], f"deleted Git index entries unavailable: {detail}"
    by_path = {path: (path, mode, oid) for path, mode, oid in parse_index_deleted(indexed.stdout)}
    missing_paths = deleted_paths.difference(by_path)
    if missing_paths:
        tree, tree_error = run_git(
            ["ls-tree", "-r", "-z", "--full-tree", "HEAD"], repo_root, state,
        )
        if tree is None:
            return [], tree_error or "unable to inspect deleted Git paths"
        # git ls-tree HEAD exits GIT_FATAL_EXIT_CODE when HEAD does not exist
        # (e.g., a freshly-init'd repo with no commits). In that case there is
        # no tree to recover metadata from, so treat it as an empty result
        # rather than a hard failure. Any other non-zero status is a real
        # error and is surfaced.
        if tree.returncode not in {0, GIT_FATAL_EXIT_CODE}:
            detail = os.fsdecode(tree.stderr).strip() or "git ls-tree failed"
            return [], f"deleted Git paths unavailable: {sanitize_terminal_text(detail)}"
        head_entries = parse_tree_entries(tree.stdout) if tree.returncode == 0 else {}
        for path in missing_paths:
            metadata = head_entries.get(path)
            if metadata is not None:
                mode, oid = metadata
                by_path[path] = (path, mode, oid)
    raw_entries = list(by_path.values())
    if not raw_entries:
        return [], None
    object_ids = list(dict.fromkeys(oid for _, _, oid in raw_entries))
    query = ("\n".join(object_ids) + "\n").encode("ascii")
    sizes_result, size_error = run_git(
        ["cat-file", "--batch-check=%(objectname) %(objectsize)"], repo_root, state, query,
    )
    if sizes_result is None:
        return [], size_error or "unable to inspect deleted Git object sizes"
    if sizes_result.returncode != 0:
        detail = os.fsdecode(sizes_result.stderr).strip() or "git cat-file failed"
        return [], f"deleted Git object sizes unavailable: {detail}"
    sizes: dict[str, int] = {}
    for line in sizes_result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit():
            sizes[fields[0].decode("ascii", "replace")] = int(fields[1])
    entries: list[DeletedGitEntry] = []
    for path, mode, oid in raw_entries:
        if oid not in sizes:
            return [], f"deleted Git object metadata was incomplete for {sanitize_terminal_text(path)}"
        kind = "link" if mode == "120000" else "dir" if mode == "160000" else "file"
        entries.append(DeletedGitEntry(path, kind, sizes[oid]))
    return entries, None


def has_git_metadata(path: Path) -> bool:
    """Return True if a .git entry exists anywhere in path's ancestor chain.

    If lstat fails with an error other than FileNotFoundError (e.g.,
    permission denied), we conservatively return True so the caller surfaces
    the git error rather than silently swallowing a potential configuration
    issue.
    """
    current = path if path.is_dir() and not path.is_symlink() else path.parent
    for candidate in (current, *current.parents):
        try:
            os.lstat(candidate / ".git")
        except FileNotFoundError:
            continue
        except OSError:
            return True
        else:
            return True
    return False


def physical_git_path(path: Path) -> Path:
    """Resolve ancestor symlinks while retaining the final entry's own identity."""
    return Path(os.path.realpath(path.parent)) / path.name


def load_git_markers(
    scan_root: Path, state: ScanState,
) -> tuple[dict[str, str], list[DeletedGitEntry], str | None]:
    git_scan_root = scan_root
    # Follow a symlinked scan root so git markers reflect the directory's
    # contents rather than just the link itself. A broken or file symlink is
    # left as-is so the existing file-branch handles it.
    if git_scan_root.is_symlink():
        try:
            resolved = git_scan_root.resolve()
        except OSError:
            pass
        else:
            if resolved.is_dir():
                git_scan_root = resolved
    repo_root, root_error = git_root_for(git_scan_root, state)
    if repo_root is None:
        warning = root_error if has_git_metadata(git_scan_root) else None
        return {}, [], warning
    config_warning = executable_git_config_warning(repo_root, state)
    if config_warning is not None:
        return {}, [], config_warning
    result, run_error = run_git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=dirty"],
        repo_root, state,
    )
    if result is None:
        return {}, [], run_error or "git markers could not be rendered"
    if result.returncode != 0:
        message = os.fsdecode(result.stderr).strip() or "git status failed"
        return {}, [], f"git markers unavailable: {message}"
    repo_markers = parse_porcelain_z(result.stdout)
    if any(marker == "[D]" for marker in repo_markers.values()):
        deleted_entries, deleted_warning = deleted_git_metadata(
            repo_root, state,
            {path for path, marker in repo_markers.items() if marker == "[D]"},
        )
    else:
        deleted_entries, deleted_warning = [], None
    if not git_scan_root.is_dir() or git_scan_root.is_symlink():
        file_rel = git_path_identity(os.path.relpath(git_scan_root, repo_root))
        marker = repo_markers.get(file_rel)
        return ({".": marker} if marker else {}), [], deleted_warning
    scan_base = git_scan_root
    scan_base_rel = git_path_identity(os.path.relpath(scan_base, repo_root))
    if scan_base_rel == ".":
        return repo_markers, deleted_entries, deleted_warning
    trimmed: dict[str, str] = {}
    trimmed_deleted: list[DeletedGitEntry] = []
    for repo_rel, marker in repo_markers.items():
        if repo_rel == scan_base_rel:
            trimmed["."] = marker
        elif repo_rel.startswith(f"{scan_base_rel}/"):
            trimmed[repo_rel[len(scan_base_rel) + 1 :]] = marker
    for entry in deleted_entries:
        if entry.rel_path.startswith(f"{scan_base_rel}/"):
            trimmed_deleted.append(DeletedGitEntry(entry.rel_path[len(scan_base_rel) + 1 :], entry.kind, entry.size))
    return trimmed, trimmed_deleted, deleted_warning


# ─── TERMINAL AND DISPLAY WIDTH ───────────────────────────────────────────

def terminal_columns() -> int:
    raw_columns = os.environ.get("COLUMNS", "")
    if raw_columns.isdigit() and int(raw_columns) > 0:
        return min(TERMINAL_WIDTH_MAXIMUM, max(TERMINAL_WIDTH_MINIMUM, int(raw_columns)))
    # shutil.get_terminal_size is guaranteed to return a usable value: it
    # falls back to (TERMINAL_WIDTH_FALLBACK, TERMINAL_ROWS_FALLBACK) when the
    # terminal size cannot be queried, so no exception handling is needed here.
    return min(
        TERMINAL_WIDTH_MAXIMUM,
        max(TERMINAL_WIDTH_MINIMUM, shutil.get_terminal_size((TERMINAL_WIDTH_FALLBACK, TERMINAL_ROWS_FALLBACK)).columns),
    )


def char_cell_width(char: str) -> int:
    """Terminal cell width of a single character.

    Combining marks, format controls, control chars, and surrogates
    contribute 0. Fullwidth and wide chars (CJK) contribute 2. All
    others contribute 1. Centralised so cell_width and truncate_cells
    agree on every category.
    """
    category = unicodedata.category(char)
    if unicodedata.combining(char) or category in {"Mn", "Me", "Cf", "Cc", "Cs"}:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1


def cell_width(text: str) -> int:
    return sum(char_cell_width(char) for char in text)


def truncate_cells(text: str, width: int) -> str:
    if cell_width(text) <= width:
        return text
    if width <= 1:
        return "…"
    output = ""
    used = 0
    limit = width - 1
    for char in text:
        char_width = char_cell_width(char)
        if used + char_width > limit:
            break
        output += char
        used += char_width
    return output + "…"


def pad_cells(text: str, width: int) -> str:
    truncated = truncate_cells(text, width)
    return truncated + " " * max(0, width - cell_width(truncated))


def wrap_cells(text: str, width: int) -> list[str]:
    if width <= 0:
        return [""]
    lines: list[str] = []
    remaining = text
    while cell_width(remaining) > width:
        candidate = truncate_cells(remaining, width)
        chunk = candidate[:-1] if candidate.endswith("…") else candidate
        split_at = chunk.rfind(" ")
        if split_at > 0:
            chunk = chunk[:split_at]
        if not chunk:
            chunk = remaining[0]
        lines.append(chunk.rstrip())
        remaining = remaining[len(chunk):].lstrip()
    lines.append(remaining)
    return lines


def terminal_color_enabled() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR") == "1":
        return True
    term = os.environ.get("TERM", "")
    return sys.stdout.isatty() and term != "dumb"


def style(text: str, *codes: str, enabled: bool = True) -> str:
    if not enabled or not codes:
        return text
    return "".join(codes) + text + ANSI_RESET


# SGR (Select Graphic Rendition) escape sequence: ESC [ <params> m. The
# render layer only ever emits SGR codes, so this is the only sequence
# strip_ansi needs to recognise to recover the plain-text output.
_ANSI_SGR_PATTERN = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """Remove ANSI SGR escape sequences from text.

    Used to derive the plain (clipboard-bound) render from the coloured
    (terminal-bound) render in a single pass, instead of walking the tree
    a second time with color=False.
    """
    return _ANSI_SGR_PATTERN.sub("", text)


def style_git_marker(marker: str, enabled: bool) -> str:
    # Standard colors (not bright) for clean contrast. Bold makes them pop
    # without the neon look of bright variants.
    palette = {
        "[M]": ANSI_YELLOW,
        "[A]": ANSI_GREEN,
        "[D]": ANSI_RED,
        "[R]": ANSI_MAGENTA,
        "[?]": ANSI_BLUE,
    }
    return style(marker, palette.get(marker, ANSI_YELLOW), ANSI_BOLD, enabled=enabled)


def row_style_for_node(node: EntryNode, enabled: bool) -> tuple[str, ...]:
    if not enabled:
        return ()
    # Directories are bold cyan (strong, readable), links are bold magenta
    # (distinct from dirs), files are default (no color wash — keeps the
    # tree visually quiet so structure stands out).
    if node.kind == "dir":
        return (ANSI_CYAN, ANSI_BOLD)
    if node.kind == "link":
        return (ANSI_MAGENTA, ANSI_BOLD)
    return ()


def styled_columns(columns: Sequence[str], enabled: bool) -> str:
    # Metadata columns are secondary information; render them in dim so the
    # tree structure (the primary information) stays visually dominant.
    if not columns:
        return ""
    rendered: list[str] = []
    for column in columns:
        padded = pad_cells(column, METADATA_COLUMN_WIDTH)
        rendered.append(style(padded, ANSI_DIM, enabled=enabled))
    return "".join(rendered).rstrip()


def styled_header_columns(headers: Sequence[str], enabled: bool) -> str:
    return "".join(
        style(pad_cells(header, METADATA_COLUMN_WIDTH), ANSI_CYAN, ANSI_BOLD, enabled=enabled)
        for header in headers
    ).rstrip()


def render_padded_row(
    label: str, columns: Sequence[str], node: EntryNode | None, width: int,
    color: bool, header: bool = False, marker: str = "",
) -> str:
    """Render a single tree row: label, optional git marker, padding, columns.

    When ``marker`` is non-empty, room is reserved for ``" <marker>"`` after
    a truncated label and the marker is styled via ``style_git_marker`` so
    it can pick up its own color independently of the label. When columns
    is empty no separator is emitted, so the row ends cleanly at the label
    padding instead of trailing whitespace.
    """
    if marker:
        marker_gap = 1 + cell_width(marker)
        name_width = max(1, width - marker_gap)
        truncated_name = truncate_cells(label, name_width)
        if node is None:
            styled_label = truncated_name
        else:
            styled_label = style(truncated_name, *row_style_for_node(node, color), enabled=color)
        styled_label = styled_label + " " + style_git_marker(marker, color)
        plain_label = truncated_name + f" {marker}"
    else:
        plain_label = truncate_cells(label, width)
        if header:
            styled_label = style(plain_label, ANSI_CYAN, ANSI_BOLD, enabled=color)
        elif node is None:
            styled_label = plain_label
        else:
            styled_label = style(plain_label, *row_style_for_node(node, color), enabled=color)
    plain_padding = " " * max(0, width - cell_width(plain_label))
    left = styled_label + plain_padding
    if not columns:
        return left
    if header:
        return left + METADATA_SEPARATOR + styled_header_columns(columns, color)
    return left + METADATA_SEPARATOR + styled_columns(columns, color)


# ─── FORMATTING UTILITIES ───────────────────────────────────────────────────

def display_name(node: EntryNode, emojis: bool) -> str:
    prefix = KIND_EMOJIS[node.kind] if emojis else ""
    name = sanitize_terminal_text(node.name)
    if node.kind == "dir":
        return f"{prefix}{name}/"
    if node.kind == "link" and node.target:
        return f"{prefix}{name} -> {sanitize_terminal_text(node.target)}"
    return f"{prefix}{name}"


def format_size(size: int) -> str:
    value = float(size)
    unit_index = 0
    while value >= SIZE_UNIT_STEP and unit_index < len(SIZE_UNITS) - 1:
        value /= SIZE_UNIT_STEP
        unit_index += 1
    unit = SIZE_UNITS[unit_index]
    if unit == "B":
        return f"{int(value)} B"
    return f"{value:.1f} {unit}"


def format_age(timestamp: float, now: float | None = None) -> str:
    current = time.time() if now is None else now
    seconds = max(0, int(current - timestamp))
    # Approximate intervals. Year is derived from month (12 * 30-day months
    # = 360 days) so the year boundary aligns with 12 months instead of
    # straddling the 365-day calendar boundary: a 364-day file would
    # otherwise show "12mo" instead of "1y".
    month = 30 * 24 * 60 * 60
    intervals = (
        (12 * month, "y"),
        (month, "mo"),
        (7 * 24 * 60 * 60, "w"),
        (24 * 60 * 60, "d"),
        (60 * 60, "h"),
        (60, "m"),
    )
    for interval, suffix in intervals:
        if seconds >= interval:
            return f"{seconds // interval}{suffix} ago"
    return "now"


def format_lines(lines: int | None) -> str:
    return "?L" if lines is None else f"{lines:,}L"


def format_entries(node: EntryNode) -> str:
    # The "entries" column reports entry-type semantics: a type label for
    # leaves (file/link) and a child-count breakdown for directories. Line
    # counts live in the dedicated "lines" column to avoid duplication.
    if node.kind == "file":
        return "file"
    if node.kind == "link":
        return "link"
    file_count = node.total_files
    dir_count = max(0, node.total_dirs - 1)
    link_count = node.total_links
    parts: list[str] = []
    if file_count:
        parts.append(f"{file_count} file" + ("s" if file_count != 1 else ""))
    if dir_count:
        parts.append(f"{dir_count} dir" + ("s" if dir_count != 1 else ""))
    if link_count:
        parts.append(f"{link_count} link" + ("s" if link_count != 1 else ""))
    verbose = ", ".join(parts) if parts else "empty"
    if cell_width(verbose) <= METADATA_COLUMN_WIDTH:
        return verbose
    # Compact fallback ("26f 14d 3l") when the verbose form would be truncated
    # mid-word by the fixed-width metadata column.
    compact: list[str] = []
    if file_count:
        compact.append(f"{file_count}f")
    if dir_count:
        compact.append(f"{dir_count}d")
    if link_count:
        compact.append(f"{link_count}l")
    return " ".join(compact) if compact else "empty"


def visible_node_label(node: EntryNode, prefix: str, connector: str, emojis: bool) -> str:
    return f"{prefix}{connector}{display_name(node, emojis)}"


# ─── TREE RENDERING ─────────────────────────────────────────────────────────

def metadata_columns(node: EntryNode, scan_data: frozenset[str]) -> list[str]:
    columns: list[str] = []
    if "type" in scan_data:
        columns.append(format_entries(node))
    if "lines" in scan_data:
        # Links carry no line count; show "-" consistent with the size column
        # rather than duplicating the "link" label already in the entries column.
        if node.kind == "link":
            columns.append("-")
        else:
            columns.append(format_lines(node.total_lines if node.kind == "dir" else node.lines))
    if "size" in scan_data:
        columns.append("-" if node.kind == "link" else format_size(node.size))
    if "modified" in scan_data:
        columns.append(format_age(node.mtime))
    return columns


def header_columns(scan_data: frozenset[str]) -> list[str]:
    headers: list[str] = []
    if "type" in scan_data:
        headers.append("entries")
    if "lines" in scan_data:
        headers.append("lines")
    if "size" in scan_data:
        headers.append("size")
    if "modified" in scan_data:
        headers.append("modified")
    return headers


def active_header_columns(scan_data: frozenset[str]) -> list[str]:
    headers = header_columns(scan_data)
    available = terminal_columns() - NAME_COLUMN_MIN_WIDTH - cell_width(METADATA_SEPARATOR)
    maximum = max(0, available // METADATA_COLUMN_WIDTH)
    return headers[:maximum]


def name_column_width(scan_data: frozenset[str]) -> int:
    metadata_width = METADATA_COLUMN_WIDTH * len(active_header_columns(scan_data))
    separator_width = cell_width(METADATA_SEPARATOR) if metadata_width else 0
    return max(NAME_COLUMN_MIN_WIDTH, terminal_columns() - metadata_width - separator_width)


def render_flat_entries(root: EntryNode, result: ScanResult, config: RuntimeConfig, color: bool = False) -> list[str]:
    lines: list[str] = []
    headers = active_header_columns(config.scan_data)
    width = name_column_width(config.scan_data)
    root_label = display_name(root, config.scan_emojis)
    root_marker = result.git_markers.get(".", "") if "git" in config.scan_data else ""
    if headers and root.kind == "dir":
        lines.append(render_padded_row(root_label, headers, root, width, color, header=True, marker=root_marker))
    elif headers:
        root_columns = metadata_columns(root, config.scan_data)[:len(headers)]
        lines.append(render_padded_row(root_label, root_columns, root, width, color, marker=root_marker))
    else:
        label = f"{root_label} {root_marker}" if root_marker else root_label
        lines.append(style(truncate_cells(label, width), *row_style_for_node(root, color), enabled=color))
    stack: list[EntryNode] = []
    if root.kind == "dir":
        stack.extend(reversed(root.children))
    while stack:
        node = stack.pop()
        plain_name = display_name(node, config.scan_emojis)
        marker = result.git_markers.get(node.rel_path, "") if "git" in config.scan_data else ""
        columns = metadata_columns(node, config.scan_data)[:len(headers)]
        lines.append(render_padded_row(plain_name, columns, node, width, color, marker=marker))
        if node.kind == "dir" and node.children:
            stack.extend(reversed(node.children))
    return lines


def render_tree_lines(root: EntryNode, result: ScanResult, config: RuntimeConfig, color: bool = False) -> list[str]:
    if "tree" not in config.scan_data:
        return render_flat_entries(root, result, config, color)
    width = name_column_width(config.scan_data)
    lines: list[str] = []
    headers = active_header_columns(config.scan_data)
    root_label = display_name(root, config.scan_emojis)
    root_marker = result.git_markers.get(".", "") if "git" in config.scan_data else ""
    if headers and root.kind == "dir":
        lines.append(render_padded_row(root_label, headers, root, width, color, header=True, marker=root_marker))
    elif headers:
        root_columns = metadata_columns(root, config.scan_data)[:len(headers)]
        lines.append(render_padded_row(root_label, root_columns, root, width, color, marker=root_marker))
    else:
        label = f"{root_label} {root_marker}" if root_marker else root_label
        lines.append(style(truncate_cells(label, width), *row_style_for_node(root, color), enabled=color))

    pending: list[tuple[EntryNode, str, bool]] = []
    if root.kind == "dir":
        pending.extend((child, "", index == len(root.children) - 1) for index, child in reversed(list(enumerate(root.children))))
    while pending:
        node, prefix, is_last = pending.pop()
        connector = "└── " if is_last else "├── "
        plain_name = visible_node_label(node, prefix, connector, config.scan_emojis)
        marker = result.git_markers.get(node.rel_path, "") if "git" in config.scan_data else ""
        columns = metadata_columns(node, config.scan_data)[:len(headers)]
        lines.append(render_padded_row(plain_name, columns, node, width, color, marker=marker))
        if node.kind == "dir" and node.children:
            child_prefix = prefix + ("    " if is_last else "│   ")
            pending.extend(
                (child, child_prefix, child_index == len(node.children) - 1)
                for child_index, child in reversed(list(enumerate(node.children)))
            )
    return lines


def deleted_git_entry_visible(entry: DeletedGitEntry, config: RuntimeConfig) -> bool:
    if config.ignore_hidden and is_hidden_rel_path(entry.rel_path):
        return False
    path = Path(entry.rel_path)
    node = EntryNode(
        path=path, rel_path=entry.rel_path, name=path.name,
        kind=entry.kind, size=entry.size, mtime=0,
    )
    if config.filter_mode == "ignore" and rules_match(node, config.rules):
        return False
    if config.filter_mode == "only" and not rules_match(node, config.rules):
        return False
    if config.ignore_empty and entry.kind == "file" and entry.size == 0:
        return False
    return True


def render_deleted_git_paths(result: ScanResult, config: RuntimeConfig) -> list[str]:
    if "git" not in config.scan_data:
        return []
    present: set[str] = set()
    stack = [result.root]
    while stack:
        node = stack.pop()
        present.add(node.rel_path)
        stack.extend(node.children)
    deleted = sorted(
        entry.rel_path for entry in result.deleted_git_entries
        if entry.rel_path not in present and deleted_git_entry_visible(entry, config)
    )
    if not deleted:
        return []
    width = terminal_columns()
    return [truncate_cells(f"deleted {sanitize_terminal_text(path)} [D]", width) for path in deleted]

# ─── SUMMARY RENDERING ──────────────────────────────────────────────────────

def type_counts(root: EntryNode) -> dict[str, int]:
    counts: dict[str, int] = {}
    stack = [root]
    while stack:
        node = stack.pop()
        if node.kind == "file":
            key = node.path.suffix.lower().lstrip(".") or "none"
            counts[key] = counts.get(key, 0) + 1
        stack.extend(node.children)
    return counts


def format_type_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "none"
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return SUMMARY_VALUE_SEPARATOR.join(f"{sanitize_terminal_text(key)}: {value}" for key, value in ordered)


def summary_box_line(text: str, width: int, border: str = "unicode", color: bool = False) -> str:
    body_width = max(1, width - 4)
    body = pad_cells(text, body_width)
    if border == "ascii":
        return f"| {body} |"
    return style("│", ANSI_DIM, ANSI_CYAN, enabled=color) + f" {body} " + style("│", ANSI_DIM, ANSI_CYAN, enabled=color)


def _format_timestamp(now: datetime) -> str:
    """Format the scan timestamp as a 12-hour clock with date for the summary footer."""
    hour = now.hour % 12 or 12
    return f"{hour}:{now:%M %p}"


def _scanned_footer(elapsed_ms: int, now: datetime, width: int) -> str:
    """Build a terminal-aware scan footer that distributes three columns evenly."""
    elapsed = f"scanned in {elapsed_ms}ms"
    date = f"{now:%Y-%m-%d}"
    clock = _format_timestamp(now)
    gap = SUMMARY_VALUE_SEPARATOR
    # Three columns (elapsed, date, clock) separated by two gaps. The
    # remaining width is split evenly between the two gaps so the columns
    # distribute across the available space.
    available = max(1, width - sum(cell_width(part) for part in (elapsed, date, clock)) - 2 * cell_width(gap))
    pad = " " * (available // 2)
    return f"{elapsed}{gap}{pad}{date}{gap}{pad}{clock}"


def summary_values(result: ScanResult, footer_width: int | None = None) -> dict[str, str]:
    root = result.root
    files = root.total_files
    dirs = max(0, root.total_dirs - 1 if root.kind == "dir" else root.total_dirs)
    links = root.total_links
    file_word = "file" if files == 1 else "files"
    dir_word = "dir" if dirs == 1 else "dirs"
    link_word = "link" if links == 1 else "links"
    largest = root.largest_file
    newest_path, newest_time = root.newest_entry or (root.summary_path, root.mtime)
    now = datetime.now()
    # The footer must fit inside the framed-summary box body (width - 4) when
    # rendered in framed mode; in minimal mode it uses the full terminal width.
    # Callers pass the appropriate width; the default keeps the legacy full-width
    # behavior for any caller that doesn't specify.
    effective_footer_width = terminal_columns() if footer_width is None else footer_width
    # "+? lines" indicates that some files (binary or unreadable) could not be
    # counted, so the real total is strictly higher than the number shown.
    lines_label = f"{root.total_lines:,}+? lines" if root.unknown_lines else f"{root.total_lines:,} lines"
    return {
        "total": SUMMARY_VALUE_SEPARATOR.join((
            f"{files:,} {file_word}",
            f"{dirs:,} {dir_word}",
            f"{links:,} {link_word}",
            lines_label,
            format_size(root.size),
        )),
        "largest": "none" if largest is None else f"{sanitize_terminal_text(largest[0])}{SUMMARY_VALUE_SEPARATOR}{format_size(largest[1])}",
        "newest": f"{sanitize_terminal_text(newest_path)}{SUMMARY_VALUE_SEPARATOR}{format_age(newest_time)}",
        "types": format_type_counts(type_counts(root)),
        "scanned": _scanned_footer(result.elapsed_ms, now, effective_footer_width),
    }


def render_minimal_summary(result: ScanResult) -> list[str]:
    values = summary_values(result)
    lines = [
        values["total"],
        f"largest {values['largest']}",
        f"newest  {values['newest']}",
        f"types   {values['types']}",
    ]
    if result.timed_out:
        lines.append("timeout reached; partial result shown")
    if result.warnings:
        count = len(result.warnings)
        noun = "issue" if count == 1 else "issues"
        lines.append(f"warnings {count} {noun}; scan output preserved")
    lines.append(values["scanned"])
    return lines


def summary_label_line(label: str, value: str, width: int, color: bool, border: str = "unicode") -> str:
    # Two-column layout matching the mid/joined separators in render_framed_summary:
    # │ <label:SUMMARY_LABEL_WIDTH> │ <value> │
    # The inner │ at index 23 aligns with the ┬/┴ in the separator rows.
    body_width = max(1, width - 4)
    value_width = max(1, body_width - SUMMARY_LABEL_WIDTH - 3)  # space + │ + space + value
    label_field = f"{label:<{SUMMARY_LABEL_WIDTH}}"
    value_field = pad_cells(value, value_width)
    if border == "ascii":
        return f"| {label_field} | {value_field} |"
    styled_label = style(label_field, ANSI_CYAN, ANSI_BOLD, enabled=color) if label else label_field
    inner_sep = style("│", ANSI_DIM, ANSI_CYAN, enabled=color)
    outer = style("│", ANSI_DIM, ANSI_CYAN, enabled=color)
    return f"{outer} {styled_label} {inner_sep} {value_field} {outer}"


def render_framed_summary(result: ScanResult, border: str, color: bool = False) -> list[str]:
    width = terminal_columns()
    # The footer lives inside the framed box body, which is width - 4 (two
    # border chars + two padding spaces). Passing the body width ensures the
    # footer's 3-column layout fits on one line instead of wrapping.
    values = summary_values(result, max(1, width - 4))
    # The label cell (label + 1-space padding each side) and the three border
    # chars (outer ├, middle ┬/┴, outer ┤) are fixed; the value column gets
    # the remaining width. Both numbers derive from SUMMARY_LABEL_WIDTH so
    # changing the label width only requires editing one constant.
    label_dashes = SUMMARY_LABEL_CELL_WIDTH
    value_dashes = max(1, width - SUMMARY_LABEL_CELL_WIDTH - SUMMARY_BORDER_CHARS)
    if border == "ascii":
        separator = "+" + "-" * label_dashes + "+" + "-" * value_dashes + "+"
        top = "+" + "-" * (width - 2) + "+"
        mid = separator
        joined = separator
        bottom = "+" + "-" * (width - 2) + "+"
        line = lambda text: summary_box_line(text, width, "ascii", color)
    else:
        top = style("┌" + "─" * (width - 2) + "┐", ANSI_DIM, ANSI_CYAN, enabled=color)
        mid = style("├" + "─" * label_dashes + "┬" + "─" * value_dashes + "┤", ANSI_DIM, ANSI_CYAN, enabled=color)
        joined = style("├" + "─" * label_dashes + "┴" + "─" * value_dashes + "┤", ANSI_DIM, ANSI_CYAN, enabled=color)
        bottom = style("└" + "─" * (width - 2) + "┘", ANSI_DIM, ANSI_CYAN, enabled=color)
        line = lambda text: summary_box_line(text, width, "unicode", color)
    lines = [top]
    lines.extend(line(chunk) for chunk in wrap_cells(values["total"], max(1, width - 4)))
    lines.append(mid)
    for label_name in ("largest", "newest"):
        chunks = wrap_cells(values[label_name], max(1, width - SUMMARY_VALUE_OVERHEAD))
        lines.append(summary_label_line(label_name, chunks[0], width, color, border))
        lines.extend(summary_label_line("", chunk, width, color, border) for chunk in chunks[1:])
    type_chunks = wrap_cells(values["types"], max(1, width - SUMMARY_VALUE_OVERHEAD))
    lines.append(summary_label_line("types", type_chunks[0], width, color, border))
    lines.extend(summary_label_line("", chunk, width, color, border) for chunk in type_chunks[1:])
    if result.timed_out:
        lines.append(joined)
        lines.append(line("timeout reached; partial result shown"))
    if result.warnings:
        if not result.timed_out:
            lines.append(joined)
        count = len(result.warnings)
        noun = "issue" if count == 1 else "issues"
        lines.append(summary_label_line("warnings", f"{count} {noun}; scan output preserved", width, color, border))
    lines.append(joined)
    lines.extend(line(chunk) for chunk in wrap_cells(values["scanned"], max(1, width - 4)))
    lines.append(bottom)
    return lines


def render_summary(result: ScanResult, styling: str, color: bool = False) -> list[str]:
    if styling == "minimal":
        return render_minimal_summary(result)
    if styling == "full" and terminal_columns() >= SUMMARY_FRAMED_MIN_WIDTH:
        return render_framed_summary(result, "unicode", color)
    values = summary_values(result)
    lines = [values["total"], f"largest  {values['largest']}", f"newest   {values['newest']}", f"types    {values['types']}"]
    if result.timed_out:
        lines.append("timeout reached; partial result shown")
    if result.warnings:
        count = len(result.warnings)
        noun = "issue" if count == 1 else "issues"
        lines.append(f"warnings {count} {noun}; scan output preserved")
    lines.append(values["scanned"])
    wrapped: list[str] = []
    for line_text in lines:
        wrapped.extend(wrap_cells(line_text, terminal_columns()))
    return wrapped


def render_warnings(warnings: Sequence[ScanWarning]) -> list[str]:
    if not warnings:
        return []
    lines = ["", "warnings:"]
    for warning in warnings:
        # Sanitization happens at the rendering boundary, not in the data
        # model, so ScanWarning stays a plain record of what the scanner saw.
        text = f"- {sanitize_terminal_text(warning.rel_path)}: {sanitize_terminal_text(warning.message)}"
        lines.extend(wrap_cells(text, terminal_columns()))
    return lines


# ─── STYLING ────────────────────────────────────────────────────────────────

# ASCII translations for minimal scan-styling. The tree is rendered with the
# Unicode box characters and then translated as a single pass at the end so
# the rendering code can stay shape-agnostic.
MINIMAL_ASCII_TRANSLATIONS = (("├── ", "+-- "), ("└── ", "`-- "), ("│   ", "|   "))


def apply_minimal_ascii(lines: list[str]) -> list[str]:
    for unicode_form, ascii_form in MINIMAL_ASCII_TRANSLATIONS:
        lines = [line.replace(unicode_form, ascii_form) for line in lines]
    return lines


def render(result: ScanResult, config: RuntimeConfig, color: bool | None = None) -> str:
    # Color follows terminal support and applies to every styling level for
    # the tree and the framed summary. The low and minimal summary blocks
    # stay plain so their token cost stays low for AI agents and pipes.
    color = terminal_color_enabled() if color is None else color
    lines: list[str] = []
    tree_lines = render_tree_lines(result.root, result, config, color=color)
    if tree_lines:
        lines.extend(tree_lines)
        lines.extend(render_deleted_git_paths(result, config))
        lines.append("")
    if "summary" in config.scan_data:
        lines.extend(render_summary(result, config.scan_styling, color=color))
    lines.extend(render_warnings(result.warnings))
    if config.scan_styling == "minimal":
        lines = apply_minimal_ascii(lines)
    return "\n".join(lines) + "\n"

# ─── CLIPBOARD ──────────────────────────────────────────────────────────────

def clipboard_backend_candidates() -> list[tuple[str, ...]]:
    candidates: list[tuple[str, ...]] = []
    if sys.platform == "darwin":
        candidates.append(("pbcopy",))
    elif os.name == "nt" or os.environ.get("WSL_INTEROP"):
        candidates.append(("clip.exe",))
    else:
        if os.environ.get("WAYLAND_DISPLAY"):
            candidates.append(("wl-copy",))
        if os.environ.get("DISPLAY"):
            candidates.extend((("xclip", "-selection", "clipboard"), ("xsel", "--clipboard", "--input")))
    return candidates


def run_clipboard_command(command: Sequence[str], text: str) -> tuple[bool, str | None, bool]:
    """Run a single clipboard backend.

    Returns ``(executable_found, error, environment_issue)``.

    - ``executable_found`` is False when ``shutil.which`` cannot resolve the
      binary (so the caller can try the next candidate without confusing it
      with a real failure).
    - ``error`` is None on success or a human-readable failure description.
    - ``environment_issue`` is True when the failure is an environment
      condition (timeout, connection refused, no display socket) rather than
      a real backend error. The caller treats these as "unavailable" (warn
      and continue) instead of "failed" (exit non-zero), because the backend
      binary is present and runnable but the desktop session isn't actually
      reachable — the scan output on stdout is still fully usable.
    """
    executable = shutil.which(command[0])
    if executable is None:
        return False, None, False
    try:
        result = subprocess.run(
            [executable, *command[1:]], input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=False,
            timeout=CLIPBOARD_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        # Timeout means the backend is trying to reach a desktop session
        # that isn't responding (stale WAYLAND_DISPLAY, dead X server, SSH
        # session with inherited env vars). This is an environment issue,
        # not a backend bug.
        return True, f"timed out after {CLIPBOARD_TIMEOUT_SECONDS:g}s", True
    except OSError as exc:
        # ENOENT on the resolved executable, EACCES, etc. Treat as
        # environment issue: the binary is present per shutil.which but
        # couldn't actually run.
        return True, sanitize_terminal_text(exc), True
    if result.returncode == 0:
        return True, None, False
    detail = os.fsdecode(result.stderr).strip()
    message = sanitize_terminal_text(detail or f"exited with status {result.returncode}")
    # Common patterns when the desktop session isn't actually reachable:
    # xclip: "Can't open display: :0", "could not open display"
    # xsel:   "Can't open display (null):", "Error: Can't open display: :0"
    # wl-copy: "Failed to connect to a Wayland session", "wayland-0 not found"
    # These are environment issues (no live session), not backend bugs.
    lower = message.lower()
    environment_patterns = (
        "can't open display",
        "cannot open display",
        "could not open display",
        "failed to connect to a wayland",
        "wayland session",
        "not connected to a wayland",
        "no such file or directory",  # stale socket path
        "connection refused",
        "no protocol specified",
        "authorization required",
    )
    is_environment = any(pattern in lower for pattern in environment_patterns)
    return True, message, is_environment


def copy_to_clipboard(text: str) -> None:
    candidates = clipboard_backend_candidates()
    if not candidates:
        raise ClipboardUnavailableError("no desktop clipboard session detected")
    real_failures: list[str] = []
    environment_failures: list[str] = []
    any_executable_found = False
    for command in candidates:
        executable_found, error, is_environment = run_clipboard_command(command, text)
        if executable_found:
            any_executable_found = True
        if error is None:
            if executable_found:
                return
            continue
        entry = f"{command[0]}: {error}"
        if is_environment:
            environment_failures.append(entry)
        else:
            real_failures.append(entry)
    # Real backend errors (not environment issues) are worth surfacing with a
    # non-zero exit: the user explicitly enabled auto-copy, a backend was
    # detected, and it broke in an unexpected way.
    if real_failures:
        raise ClipboardFailureError("clipboard backend failed (" + "; ".join(real_failures) + ")")
    # Every candidate that ran failed due to environment issues (timeout,
    # can't open display, connection refused, stale socket). The desktop
    # session isn't actually reachable even though the env vars suggested it
    # was. Treat as unavailable: warn and continue, exit 0.
    if environment_failures:
        raise ClipboardUnavailableError(
            "desktop session not reachable (" + "; ".join(environment_failures) + ")"
        )
    if any_executable_found:
        raise ClipboardFailureError("clipboard backend ran but copy did not complete")
    raise ClipboardUnavailableError("clipboard backend unavailable for this desktop session")


# ─── ENTRY POINT ────────────────────────────────────────────────────────────

def run(argv: Sequence[str]) -> int:
    try:
        if argv and is_help_token(argv[0]):
            write_stream(sys.stdout, render_help())
            return 0
        if argv and is_version_token(argv[0]):
            write_stream(sys.stdout, render_version())
            return 0
        if argv and is_status_token(argv[0]):
            write_stream(sys.stdout, render_status())
            return 0
        if any(token in {"-h", "--help"} for token in argv):
            write_stream(sys.stdout, render_help())
            return 0
        if any(token == "--version" for token in argv):
            write_stream(sys.stdout, render_version())
            return 0
        config = resolve_runtime_config(argv)
        result = scan(config)
        output = render(result, config)
        write_stream(sys.stdout, output)
        if config.auto_copy:
            try:
                # Derive the plain render from the coloured one in a single
                # pass instead of walking the tree a second time.
                copy_to_clipboard(strip_ansi(output))
            except ClipboardUnavailableError as exc:
                # No backend in this environment (headless server, container,
                # CI). The scan output is already on stdout and fully usable;
                # auto-copy is a convenience, not a core function. Warn and
                # continue so prs works out of the box everywhere.
                write_stream(sys.stderr, f"{PROGRAM_NAME}: auto-copy skipped: {exc}\n")
            except ClipboardFailureError as exc:
                # A backend was detected but the copy operation broke. This is
                # a real error worth surfacing: the user explicitly enabled
                # auto-copy and it failed unexpectedly.
                write_stream(sys.stderr, f"{PROGRAM_NAME}: auto-copy failed: {exc}\n")
                return 1
        return 0
    except HelpRequested:
        # Defensive: argparse only calls parser.exit(0) for help actions,
        # and our --help is store_true with add_help=False, so this branch
        # is unreachable in normal flow. If argparse ever does fire it,
        # render help instead of misreporting it as a config error.
        write_stream(sys.stdout, render_help())
        return 0
    except ConfigError as exc:
        write_stream(sys.stderr, render_usage_error(str(exc)))
        return 2
    except PrsError as exc:
        write_stream(sys.stderr, render_usage_error(str(exc)))
        return 1
    except BrokenPipeError:
        # Redirect stdout to devnull so any remaining writes don't retrigger
        # the broken pipe. Surface cleanup failures to stderr instead of
        # swallowing them silently.
        try:
            stdout_fd = sys.stdout.fileno()
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, stdout_fd)
            os.close(devnull_fd)
        except OSError as exc:
            write_stream(sys.stderr, f"{PROGRAM_NAME}: broken-pipe cleanup failed: {exc}\n")
        return 0
    except OSError as exc:
        write_stream(sys.stderr, render_usage_error(f"operating system error: {sanitize_terminal_text(exc)}"))
        return 1
    except KeyboardInterrupt:
        write_stream(sys.stderr, f"{PROGRAM_NAME}: interrupted\n")
        return 130

def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
