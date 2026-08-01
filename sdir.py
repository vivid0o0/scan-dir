#!/usr/bin/env python3
# sdir.py -- Scan Dir runtime
# Scans a file or directory and renders deterministic project context with
# configurable filtering, metadata, Git state, and safe terminal output.
# Tags: cli, filesystem, git, rendering, configuration
# 2026-07-28

from __future__ import annotations

import argparse
import errno
import itertools
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, NoReturn, TextIO

# ─── CONSTANTS ──────────────────────────────────────────────────────────────

PROGRAM_NAME = "sdir"
ALIAS_COMMAND = "scan-dir"
PRODUCT_TITLE = "Scan Dir"
VERSION = "1.0.0"
CONFIG_FILE_NAME = "config.yaml"
CONFIG_DIRECTORY_NAME = "scan-dir"
PROJECT_CONFIG_FILE_NAMES = (".sdir.yaml", "sdir.yaml")
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
CONFIG_MAX_BYTES = 1024 * 1024
FILE_READ_MAX_ATTEMPTS = 2
CLIPBOARD_TIMEOUT_SECONDS = 2.0
GIT_TIMEOUT_CAP_SECONDS = 5.0

GIT_CONFIG_OVERRIDES = (
    "core.fsmonitor=false",
    "core.hooksPath=" + os.devnull,
    "core.pager=cat",
    "pager.status=false",
    "status.renames=copies",
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


# ─── ERRORS ─────────────────────────────────────────────────────────────────


class SdirError(Exception):
    pass


class ConfigError(SdirError):
    pass


class HelpRequested(SdirError):
    """Raised by the argument parser when it requests a clean exit (status 0).

    argparse calls ``parser.exit(0, ...)`` when a help action fires; we use
    this dedicated type so callers can render help instead of misreporting
    the exit as a config error.
    """


class ClipboardError(SdirError):
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
    config_paths: tuple[Path, ...]
    filter_mode: str
    rules: FilterRules
    ignore_hidden: bool
    ignore_empty: bool
    scan_styling: str
    scan_emojis: bool
    scan_data: frozenset[str]
    scan_timeout: float
    auto_copy: bool

    @property
    def config_path(self) -> Path | None:
        return self.config_paths[-1] if self.config_paths else None


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
    children: list[EntryNode] = field(default_factory=list)
    total_files: int = 0
    total_dirs: int = 0
    total_links: int = 0
    total_lines: int = 0
    unknown_lines: int = 0
    largest_file: tuple[str, int] | None = None
    newest_entry: tuple[str, float] | None = None
    incomplete: bool = False

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
    visited_directories: set[tuple[int, int]] = field(default_factory=set)

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


# ─── CLI CONTRACT ───────────────────────────────────────────────────────────

CliValueMode = Literal["flag", "single", "multiple"]


@dataclass(frozen=True)
class CliOptionSpec:
    """Single source of truth for parsing and argv preprocessing.

    The previous implementation repeated option names across several tables
    and ``build_parser()``, so adding or renaming a flag could silently break
    path extraction. Every parser and preprocessor lookup is now derived from
    this immutable schema.
    """

    flags: tuple[str, ...]
    dest: str
    value_mode: CliValueMode
    help: str
    group: str = "general"
    action: str | None = None
    choices: tuple[str, ...] = ()
    converter: Callable[[str], object] | None = None
    const: object | None = None
    default: object | None = None

    @property
    def canonical_flag(self) -> str:
        return next((flag for flag in self.flags if flag.startswith("--")), self.flags[0])


CLI_OPTION_SPECS: tuple[CliOptionSpec, ...] = (
    CliOptionSpec(
        ("--ignore",), "ignore", "flag", "exclude entries matching filters", group="mode", action="store_true"
    ),
    CliOptionSpec(
        ("--only",), "only", "flag", "include only entries matching filters", group="mode", action="store_true"
    ),
    CliOptionSpec(
        ("--full",),
        "full",
        "flag",
        "include all entries, including hidden and empty entries",
        group="mode",
        action="store_true",
    ),
    CliOptionSpec(
        ("-f", "--paths"),
        "paths",
        "multiple",
        "match relative paths and matched directory contents",
        group="selector",
        action="append",
    ),
    CliOptionSpec(
        ("-t", "--types"), "types", "multiple", "match entry types: file, dir, link", group="selector", action="append"
    ),
    CliOptionSpec(
        ("-e", "--extensions"),
        "extensions",
        "multiple",
        "match file extensions, such as .ts or .md",
        group="selector",
        action="append",
    ),
    CliOptionSpec(
        ("-n", "--names"),
        "names",
        "multiple",
        "match exact file or directory basenames",
        group="selector",
        action="append",
    ),
    CliOptionSpec(
        ("--ignore-hidden",),
        "ignore_hidden",
        "flag",
        "hide dot-prefixed files and directories",
        group="hidden",
        action="store_const",
        const=True,
        default=None,
    ),
    CliOptionSpec(
        ("--include-hidden",),
        "ignore_hidden",
        "flag",
        "show dot-prefixed files and directories",
        group="hidden",
        action="store_const",
        const=False,
        default=None,
    ),
    CliOptionSpec(
        ("--ignore-empty",),
        "ignore_empty",
        "flag",
        "hide empty files and directories",
        group="empty",
        action="store_const",
        const=True,
        default=None,
    ),
    CliOptionSpec(
        ("--include-empty",),
        "ignore_empty",
        "flag",
        "show empty files and directories",
        group="empty",
        action="store_const",
        const=False,
        default=None,
    ),
    CliOptionSpec(("--scan-styling",), "scan_styling", "single", "set output styling", choices=STYLING_LEVELS),
    CliOptionSpec(("--scan-emojis",), "scan_emojis", "single", "show or hide emojis", choices=("true", "false")),
    CliOptionSpec(("--scan-data",), "scan_data", "single", "comma-separated metadata items"),
    CliOptionSpec(
        ("--scan-timeout",), "scan_timeout", "single", "stop after seconds and print a partial result", converter=float
    ),
    CliOptionSpec(
        ("--auto-copy",), "auto_copy", "single", "copy plain output to the clipboard", choices=("true", "false")
    ),
    CliOptionSpec(("--config",), "config", "single", "use a specific config.yaml file"),
    CliOptionSpec(
        ("--project-config",),
        "project_config",
        "single",
        "control repository-owned configuration discovery",
        choices=("auto", "ignore", "require"),
        default="auto",
    ),
    CliOptionSpec(("--help", "-h"), "help", "flag", "show help", action="store_true"),
    CliOptionSpec(("--version",), "version", "flag", "show version", action="store_true"),
)

OPTION_SPEC_BY_FLAG = {flag: spec for spec in CLI_OPTION_SPECS for flag in spec.flags}
SHORTCUT_MODE_FLAGS = {"--ignore", "--only"}
SELECTOR_ORDER = tuple(spec.canonical_flag for spec in CLI_OPTION_SPECS if spec.group == "selector")


# ─── NORMALIZATION ──────────────────────────────────────────────────────────


def sanitize_terminal_text(value: object) -> str:
    """Return terminal-safe text without losing undecodable filename bytes.

    Paths decoded by Python's ``surrogateescape`` retain original bytes in the
    U+DC80..U+DCFF range. Render those as explicit ``\\xNN`` sequences rather
    than replacing them with U+FFFD, so diagnostics remain unambiguous and
    round-trippable for operators. Other control/surrogate characters are
    escaped, while format and bidirectional controls are named visibly.
    """
    text = os.fsdecode(value) if isinstance(value, bytes) else str(value)
    output: list[str] = []
    for char in text:
        codepoint = ord(char)
        if 0xDC80 <= codepoint <= 0xDCFF:
            output.append(f"\\x{codepoint - 0xDC00:02X}")
            continue
        category = unicodedata.category(char)
        if category in {"Cc", "Cs"}:
            if codepoint <= 0xFFFF:
                output.append(f"\\u{codepoint:04X}")
            else:
                output.append(f"\\U{codepoint:08X}")
        elif category == "Cf" or 0x202A <= codepoint <= 0x202E or 0x2066 <= codepoint <= 0x2069:
            output.append(f"<U+{codepoint:04X}>")
        else:
            output.append(char)
    return "".join(output)


def expand_user_path(value: str | os.PathLike[str], description: str) -> Path:
    try:
        return Path(value).expanduser()
    except (KeyError, RuntimeError, ValueError) as exc:
        raise ConfigError(f"unable to expand {description}: {sanitize_terminal_text(value)}") from exc


def resolve_scan_path(value: str) -> Path:
    """Resolve a user-supplied scan path to an absolute path.

    No case-insensitive fallback: case-sensitive filesystems (Linux) must
    receive the exact path the user typed. macOS and Windows filesystems
    are case-insensitive at the OS level, so they handle mismatches
    natively. Guessing silently on Linux hides typos and breaks user trust.
    """
    expanded = expand_user_path(value, "scan path")
    return Path(os.path.abspath(expanded))


ASCII_TRANSLATION = str.maketrans(
    {
        "─": "-",
        "│": "|",
        "├": "+",
        "└": "`",
        "┌": "+",
        "┐": "+",
        "┬": "+",
        "┤": "+",
        "┘": "+",
        "┴": "+",
        "→": "->",
        "…": ".",
    }
)


def ascii_terminal_text(text: str) -> str:
    translated = text.translate(ASCII_TRANSLATION)
    output: list[str] = []
    for char in unicodedata.normalize("NFKD", translated):
        if ord(char) < 128:
            output.append(char)
        elif char == "\ufffd":
            output.append("?")
        elif unicodedata.combining(char) or unicodedata.category(char) in {
            "Cf",
            "Mn",
            "Me",
            "So",
            "Sk",
        }:
            continue
        else:
            output.append("?")
    return "".join(output)


def stream_text(stream: TextIO, text: str) -> str:
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return text
    try:
        text.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return ascii_terminal_text(text)
    return text


def write_stream(stream: TextIO, text: str) -> None:
    stream.write(stream_text(stream, text))
    stream.flush()


def normalize_selector_path(value: str) -> str:
    """Normalize a relative selector without rewriting valid POSIX names."""
    if value == ".":
        return "."
    if not value:
        raise ConfigError("path filters cannot contain an empty value")
    normalized = value
    if os.sep != "/":
        normalized = normalized.replace(os.sep, "/")
    if os.altsep and os.altsep != "/":
        normalized = normalized.replace(os.altsep, "/")
    if normalized.startswith("/"):
        raise ConfigError(f"path filters must be relative: {sanitize_terminal_text(value)}")
    parts: list[str] = []
    for part in normalized.split("/"):
        if part in {"", "."}:
            continue
        if part == "..":
            raise ConfigError(f"path filters cannot traverse above the scan root: {sanitize_terminal_text(value)}")
        parts.append(part)
    if not parts:
        return "."
    return "/".join(parts)


def git_path_identity(value: str) -> str:
    return value.replace(os.sep, "/") if os.sep != "/" else value


def normalize_extension(value: str) -> str:
    extension = value.lower()
    if not extension.startswith("."):
        raise ConfigError(f"extension filters must include the leading dot: {sanitize_terminal_text(value)}")
    if extension == ".":
        raise ConfigError("extension filter cannot be only '.'")
    return extension


def normalize_entry_type(value: str) -> str:
    entry_type = value.lower()
    if entry_type not in ENTRY_TYPES:
        joined = ", ".join(ENTRY_TYPES)
        raise ConfigError(f"invalid entry type '{sanitize_terminal_text(value)}', expected one of: {joined}")
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
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
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
    normalized = value.lower()
    if normalized not in choices:
        raise ConfigError(f"{key} must be one of: {', '.join(choices)}")
    return normalized


def normalize_scan_data(value: object, key: str = "scan_data") -> frozenset[str]:
    """Parse a non-empty comma-separated set of exact metadata names."""
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string")
    items: set[str] = set()
    for raw_item in value.split(","):
        item = raw_item.strip().lower()
        if not item:
            continue
        if item not in SCAN_DATA_ITEMS:
            raise ConfigError(
                f"{key} contains unknown item '{sanitize_terminal_text(raw_item.strip())}'; "
                f"valid items: {', '.join(SCAN_DATA_ITEMS)}"
            )
        items.add(item)
    if not items:
        raise ConfigError(f"{key} must select at least one item: {', '.join(SCAN_DATA_ITEMS)}")
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
        "auto_copy": False,
    }


def _yaml_error(source: Path, line_number: int, message: str) -> ConfigError:
    return ConfigError(f"config file {sanitize_terminal_text(source)} line {line_number}: {message}")


def _strip_yaml_comment(value: str, source: Path, line_number: int) -> str:
    """Remove an inline YAML comment and validate quote termination."""
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                if index + 1 < len(value) and value[index + 1] == "'":
                    index += 1
                else:
                    quote = None
        else:
            if char in {'"', "'"}:
                quote = char
            elif char == "#" and (index == 0 or value[index - 1].isspace()):
                return value[:index]
        index += 1
    if quote is not None or escaped:
        raise _yaml_error(source, line_number, "unterminated quoted string")
    return value


def _unescape_yaml_double_quoted(inner: str, source: Path, line_number: int) -> str:
    output: list[str] = []
    index = 0
    simple = {
        "0": "\0",
        "a": "\a",
        "b": "\b",
        "t": "\t",
        "n": "\n",
        "v": "\v",
        "f": "\f",
        "r": "\r",
        "e": "\x1b",
        " ": " ",
        '"': '"',
        "/": "/",
        "\\": "\\",
        "N": "\u0085",
        "_": "\u00a0",
        "L": "\u2028",
        "P": "\u2029",
    }
    while index < len(inner):
        char = inner[index]
        if char == '"':
            raise _yaml_error(source, line_number, "unescaped double quote in double-quoted string")
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 >= len(inner):
            raise _yaml_error(source, line_number, "trailing backslash in double-quoted string")
        marker = inner[index + 1]
        if marker in simple:
            output.append(simple[marker])
            index += 2
            continue
        lengths = {"x": 2, "u": 4, "U": 8}
        if marker in lengths:
            length = lengths[marker]
            digits = inner[index + 2 : index + 2 + length]
            if len(digits) != length or not re.fullmatch(r"[0-9A-Fa-f]+", digits):
                raise _yaml_error(source, line_number, f"invalid \\{marker} escape")
            codepoint = int(digits, 16)
            if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                raise _yaml_error(source, line_number, "invalid Unicode code point in escape")
            output.append(chr(codepoint))
            index += 2 + length
            continue
        raise _yaml_error(source, line_number, f"unsupported escape sequence \\{sanitize_terminal_text(marker)}")
    return "".join(output)


def _parse_single_quoted(inner: str, source: Path, line_number: int) -> str:
    output: list[str] = []
    index = 0
    while index < len(inner):
        if inner[index] != "'":
            output.append(inner[index])
            index += 1
            continue
        if index + 1 < len(inner) and inner[index + 1] == "'":
            output.append("'")
            index += 2
            continue
        raise _yaml_error(source, line_number, "single quotes inside a single-quoted string must be doubled")
    return "".join(output)


def _parse_yaml_scalar(raw: str, source: Path, line_number: int) -> object:
    text = raw.strip()
    if not text:
        return ""
    if text.startswith('"'):
        if len(text) < 2 or not text.endswith('"'):
            raise _yaml_error(source, line_number, "unterminated double-quoted string")
        return _unescape_yaml_double_quoted(text[1:-1], source, line_number)
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise _yaml_error(source, line_number, "unterminated single-quoted string")
        return _parse_single_quoted(text[1:-1], source, line_number)
    lowered = text.lower()
    if lowered in {"true", "yes", "on"}:
        return True
    if lowered in {"false", "no", "off"}:
        return False
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?(?:0|[1-9][0-9]*)", text):
        try:
            return int(text, 10)
        except ValueError:
            pass
    if re.fullmatch(r"[-+]?(?:(?:[0-9]+\.[0-9]*)|(?:[0-9]*\.[0-9]+)|(?:[0-9]+))(?:[eE][-+]?[0-9]+)?", text):
        try:
            number = float(text)
        except ValueError:
            pass
        else:
            if math.isfinite(number):
                return number
    return text


def _parse_yaml_inline_list(value: str, source: Path, line_number: int) -> list[object]:
    if not (value.startswith("[") and value.endswith("]")):
        raise _yaml_error(source, line_number, "malformed inline list")
    inner = value[1:-1]
    if not inner.strip():
        return []
    items: list[object] = []
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(inner):
        char = inner[index]
        if quote == '"':
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = None
        elif quote == "'":
            if char == "'":
                if index + 1 < len(inner) and inner[index + 1] == "'":
                    index += 1
                else:
                    quote = None
        else:
            if char in {'"', "'"}:
                quote = char
            elif char in "[{":
                raise _yaml_error(source, line_number, "nested inline collections are not supported")
            elif char in "]}":
                raise _yaml_error(source, line_number, "unexpected closing collection delimiter")
            elif char == ",":
                item = inner[start:index]
                if not item.strip():
                    raise _yaml_error(source, line_number, "inline lists cannot contain empty items")
                items.append(_parse_yaml_scalar(item, source, line_number))
                start = index + 1
        index += 1
    if quote is not None or escaped:
        raise _yaml_error(source, line_number, "unterminated quoted string in inline list")
    final = inner[start:]
    if not final.strip():
        raise _yaml_error(source, line_number, "inline lists cannot end with a comma")
    items.append(_parse_yaml_scalar(final, source, line_number))
    return items


def parse_config_yaml(text: str, source: Path) -> dict[str, object]:
    """Parse the documented top-level YAML subset without external packages."""
    lines = text.splitlines()
    mapping: dict[str, object] = {}
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line_number = index + 1
        index += 1
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise _yaml_error(source, line_number, "tabs are not allowed for indentation")
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if raw_line[:1].isspace():
            raise _yaml_error(source, line_number, "unexpected indentation at top level")
        key_line = _strip_yaml_comment(raw_line, source, line_number).rstrip()
        if not key_line:
            continue
        if key_line in {"---", "..."} or key_line.startswith("%YAML"):
            raise _yaml_error(source, line_number, "YAML directives and document markers are not supported")
        if ":" not in key_line:
            raise _yaml_error(source, line_number, f"invalid mapping entry: {sanitize_terminal_text(stripped)}")
        key, _, raw_value = key_line.partition(":")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
            raise _yaml_error(source, line_number, f"invalid mapping key: {sanitize_terminal_text(key)}")
        if key in mapping:
            raise _yaml_error(source, line_number, f"duplicate YAML key '{key}'")
        value = raw_value.strip()
        if value:
            if value.startswith("[") or value.endswith("]"):
                mapping[key] = _parse_yaml_inline_list(value, source, line_number)
            elif value.startswith("{") or value.endswith("}"):
                raise _yaml_error(source, line_number, "inline mappings are not supported")
            else:
                mapping[key] = _parse_yaml_scalar(value, source, line_number)
            continue

        items: list[object] = []
        list_indent: int | None = None
        while index < len(lines):
            item_raw = lines[index]
            item_line_number = index + 1
            if "\t" in item_raw[: len(item_raw) - len(item_raw.lstrip())]:
                raise _yaml_error(source, item_line_number, "tabs are not allowed for indentation")
            item_stripped = item_raw.strip()
            if not item_stripped or item_stripped.startswith("#"):
                index += 1
                continue
            indent = len(item_raw) - len(item_raw.lstrip(" "))
            if indent == 0:
                break
            if list_indent is None:
                list_indent = indent
                if list_indent < 2:
                    raise _yaml_error(
                        source, item_line_number, "block list items must be indented by at least two spaces"
                    )
            if indent != list_indent:
                raise _yaml_error(source, item_line_number, "inconsistent block-list indentation")
            body = item_raw[indent:]
            if not (body == "-" or body.startswith("- ")):
                raise _yaml_error(source, item_line_number, "only block-list items are allowed below a key")
            item_text = _strip_yaml_comment(body[1:].lstrip(), source, item_line_number).rstrip()
            if not item_text:
                raise _yaml_error(source, item_line_number, "block lists cannot contain empty items")
            items.append(_parse_yaml_scalar(item_text, source, item_line_number))
            index += 1
        mapping[key] = items if list_indent is not None else None
    return mapping


def load_yaml_payload(path: Path) -> dict[str, object]:
    file_fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        file_fd = os.open(path, flags)
        opened_stat = os.fstat(file_fd)
        if not stat.S_ISREG(opened_stat.st_mode):
            raise ConfigError(f"config file is not a regular file: {sanitize_terminal_text(path)}")
        if opened_stat.st_size > CONFIG_MAX_BYTES:
            raise ConfigError(f"config file exceeds {CONFIG_MAX_BYTES} bytes: {sanitize_terminal_text(path)}")
        chunks: list[bytes] = []
        total = 0
        while total <= CONFIG_MAX_BYTES:
            chunk = os.read(file_fd, min(TEXT_READ_CHUNK_SIZE, CONFIG_MAX_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > CONFIG_MAX_BYTES:
            raise ConfigError(f"config file exceeds {CONFIG_MAX_BYTES} bytes: {sanitize_terminal_text(path)}")
        after_stat = os.fstat(file_fd)
        current_stat = os.stat(path, follow_symlinks=False)
        before_signature = (
            opened_stat.st_dev,
            opened_stat.st_ino,
            opened_stat.st_mode,
            opened_stat.st_size,
            opened_stat.st_mtime_ns,
            opened_stat.st_ctime_ns,
        )
        after_signature = (
            after_stat.st_dev,
            after_stat.st_ino,
            after_stat.st_mode,
            after_stat.st_size,
            after_stat.st_mtime_ns,
            after_stat.st_ctime_ns,
        )
        current_signature = (
            current_stat.st_dev,
            current_stat.st_ino,
            current_stat.st_mode,
            current_stat.st_size,
            current_stat.st_mtime_ns,
            current_stat.st_ctime_ns,
        )
        if before_signature != after_signature or after_signature != current_signature:
            raise ConfigError(f"config file changed while being read: {sanitize_terminal_text(path)}")
        raw = b"".join(chunks)
    except ConfigError:
        raise
    except OSError as exc:
        raise ConfigError(
            f"unable to read config file safely: {sanitize_terminal_text(path)}: {sanitize_terminal_text(exc)}"
        ) from exc
    finally:
        if file_fd is not None:
            os.close(file_fd)
    try:
        decoded = raw.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ConfigError(f"config file is not valid UTF-8: {sanitize_terminal_text(path)}") from exc
    return parse_config_yaml(decoded, path)


def user_config_path() -> Path | None:
    explicit_dir = os.environ.get("SDIR_CONFIG_DIR")
    if explicit_dir:
        candidate = expand_user_path(explicit_dir, "SDIR_CONFIG_DIR")
        if not candidate.is_absolute():
            raise ConfigError(f"SDIR_CONFIG_DIR must be absolute: {sanitize_terminal_text(explicit_dir)}")
        return candidate / CONFIG_FILE_NAME

    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_home:
        xdg_path = expand_user_path(xdg_home, "XDG_CONFIG_HOME")
        if xdg_path.is_absolute():
            return xdg_path / CONFIG_DIRECTORY_NAME / CONFIG_FILE_NAME

    try:
        home = Path.home()
    except RuntimeError:
        return None
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / CONFIG_DIRECTORY_NAME / "config" / CONFIG_FILE_NAME
    return home / ".config" / CONFIG_DIRECTORY_NAME / CONFIG_FILE_NAME


def _config_candidate(path: Path, label: str, required: bool = False) -> Path | None:
    if not os.path.lexists(path):
        if required:
            raise ConfigError(f"{label} does not exist: {sanitize_terminal_text(path)}")
        return None
    if not path.is_file():
        raise ConfigError(f"{label} is not a regular file: {sanitize_terminal_text(path)}")
    return path


def _append_unique_config(paths: list[Path], candidate: Path | None) -> None:
    if candidate is None:
        return
    identity = os.path.realpath(candidate)
    if any(os.path.realpath(existing) == identity for existing in paths):
        return
    paths.append(candidate)


def project_config_path(root_path: Path) -> Path | None:
    directory = root_path if root_path.is_dir() else root_path.parent
    existing = [
        candidate
        for name in PROJECT_CONFIG_FILE_NAMES
        if (candidate := _config_candidate(directory / name, "project config")) is not None
    ]
    if len(existing) > 1:
        names = ", ".join(path.name for path in existing)
        raise ConfigError(f"multiple project configuration files found in {sanitize_terminal_text(directory)}: {names}")
    return existing[0] if existing else None


def selected_project_config_path(root_path: Path, mode: str) -> Path | None:
    if mode == "ignore":
        return None
    project = project_config_path(root_path)
    if mode == "require" and project is None:
        directory = root_path if root_path.is_dir() else root_path.parent
        expected = " or ".join(PROJECT_CONFIG_FILE_NAMES)
        raise ConfigError(
            f"project configuration is required but {expected} was not found in {sanitize_terminal_text(directory)}"
        )
    return project


def config_paths_with_project(
    explicit_path: str | None,
    project_path: Path | None,
) -> tuple[Path, ...]:
    """Return configuration layers from lowest to highest precedence."""
    paths: list[Path] = []
    managed = _config_candidate(Path(__file__).resolve().parent / CONFIG_FILE_NAME, "managed config")
    _append_unique_config(paths, managed)
    user_path = user_config_path()
    if user_path is not None:
        _append_unique_config(paths, _config_candidate(user_path, "user config"))
    _append_unique_config(paths, project_path)
    if explicit_path is not None:
        explicit = expand_user_path(explicit_path, "config path")
        _append_unique_config(paths, _config_candidate(explicit, "explicit config", required=True))
    return tuple(paths)


def find_config_paths(
    explicit_path: str | None,
    root_path: Path,
    project_config_mode: str = "auto",
) -> tuple[Path, ...]:
    project_path = selected_project_config_path(root_path, project_config_mode)
    return config_paths_with_project(explicit_path, project_path)


def canonicalize_config_payload(payload: dict[str, object], source: Path) -> dict[str, object]:
    allowed = default_payload()
    result: dict[str, object] = {}
    canonical_to_original: dict[str, str] = {}
    for key, value in payload.items():
        normalized = key.replace("-", "_")
        if normalized not in allowed:
            raise ConfigError(f"unknown config key in {sanitize_terminal_text(source)}: {sanitize_terminal_text(key)}")
        if normalized in canonical_to_original:
            raise ConfigError(
                f"config key collision in {sanitize_terminal_text(source)}: both "
                f"'{canonical_to_original[normalized]}' and '{key}' map to '{normalized}'"
            )
        canonical_to_original[normalized] = key
        result[normalized] = value
    return result


def merged_config_payload(
    paths: Sequence[Path],
    restricted_auto_copy_path: Path | None = None,
) -> dict[str, object]:
    merged = default_payload()
    restricted_identity = os.path.realpath(restricted_auto_copy_path) if restricted_auto_copy_path is not None else None
    for path in paths:
        payload = canonicalize_config_payload(load_yaml_payload(path), path)
        if restricted_identity is not None and os.path.realpath(path) == restricted_identity:
            payload.pop("auto_copy", None)
        merged.update(payload)
    return merged


def is_help_token(value: str) -> bool:
    return value == "help"


def is_version_token(value: str) -> bool:
    return value == "version"


def is_status_token(value: str) -> bool:
    return value == "status"


def option_before_separator(argv: Sequence[str], options: set[str]) -> bool:
    """Return whether an exact flag appears before the path separator.

    Attached forms such as ``--help=value`` are deliberately not accepted for
    no-value flags; they must reach normal parsing so a usage error is emitted.
    """
    for token in argv:
        if token == "--":
            return False
        if token in options:
            return True
    return False


def render_status(color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    cwd_configs = find_config_paths(None, Path.cwd())
    config_text = " -> ".join(sanitize_terminal_text(path) for path in cwd_configs)
    if not config_text:
        config_text = "built-in defaults only"
    # APP_DIR is the directory containing the sdir executable that the user
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
    width = terminal_columns()
    label_width = max(len(label) for label, _ in rows)
    rule = "─" * min(width, BANNER_RULE_WIDTH_MAXIMUM)
    lines = help_wrapped_lines(
        f"{PRODUCT_TITLE} status",
        color,
        ANSI_CYAN,
        ANSI_BOLD,
    )
    lines.append(style(rule, ANSI_DIM, ANSI_CYAN, enabled=color))
    for label, value in rows:
        value = sanitize_terminal_text(value)
        prefix_text = f"  {pad_cells(label, label_width)}  "
        prefix_width = cell_width(prefix_text)
        if prefix_width >= width:
            lines.extend(help_wrapped_lines(f"{label}: {value}", color, ANSI_WHITE))
            continue
        value_chunks = wrap_cells(value, width - prefix_width)
        styled_prefix = "  " + style(pad_cells(label, label_width), ANSI_BLUE, ANSI_BOLD, enabled=color) + "  "
        lines.append(styled_prefix + style(value_chunks[0], ANSI_WHITE, enabled=color))
        continuation = " " * prefix_width
        lines.extend(continuation + style(chunk, ANSI_WHITE, enabled=color) for chunk in value_chunks[1:])
    return "\n".join(lines) + "\n"


def render_version(color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    return f"{style(PROGRAM_NAME, ANSI_CYAN, ANSI_BOLD, enabled=color)} {style(VERSION, ANSI_GREEN, ANSI_BOLD, enabled=color)}\n"


class SdirArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ConfigError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        # argparse calls exit(0) when a help action fires; raise a dedicated
        # type so callers can render help instead of misreporting the exit
        # as a config error. status!=0 always originates from error().
        if status == 0:
            raise HelpRequested()
        raise ConfigError((message or "argument parsing failed").strip())


# ─── HELP RENDERING ─────────────────────────────────────────────────────────


def help_usage_line(color: bool) -> str:
    tokens = (
        ("sdir", (ANSI_CYAN, ANSI_BOLD)),
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
    text: str,
    color: bool,
    *codes: str,
    indent: int = 0,
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
    lines = help_wrapped_lines(
        f"{PRODUCT_TITLE} ({PROGRAM_NAME})",
        color,
        ANSI_CYAN,
        ANSI_BOLD,
    )
    lines.extend(
        help_wrapped_lines(
            "Fast project context for terminals and AI agents.",
            color,
            ANSI_WHITE,
        )
    )
    lines.extend(
        help_wrapped_lines(
            f"Equivalent command alias: {ALIAS_COMMAND}",
            color,
            ANSI_DIM,
        )
    )
    lines.append(style(rule, ANSI_DIM, ANSI_CYAN, enabled=color))
    lines.extend(("", style("Usage", ANSI_CYAN, ANSI_BOLD, enabled=color), help_usage_line(color)))
    command_rows = (
        ("sdir", "scan the current directory"),
        ("sdir <path>", "scan a file or directory"),
        ("sdir help", "show full help"),
        ("sdir version", "show version"),
        ("sdir status", "show runtime status"),
    )
    lines.extend(help_table("Commands", command_rows, color))
    lines.extend(("", style("Examples", ANSI_CYAN, ANSI_BOLD, enabled=color)))
    for example in (
        "sdir",
        "sdir ~/code/app",
        "sdir --only .md",
        'sdir ~/code/app --only -e .ts .tsx --scan-data "tree, lines, size"',
        "sdir ~/code/app --ignore-hidden --ignore-empty --scan-styling full",
        "sdir --only .md -- ~/code/app",
    ):
        lines.extend(help_wrapped_lines(example, color, ANSI_GREEN, indent=2))
    lines.append(style(rule, ANSI_DIM, ANSI_CYAN, enabled=color))
    lines.extend(
        help_table(
            "Filter modes",
            (
                ("--ignore", "Exclude entries matching selectors. Default mode when filters exist."),
                ("--only", "Include only entries matching selectors. Shorthand accepted, e.g. --only .md."),
                ("--full", "Include every entry, including hidden and empty entries."),
            ),
            color,
        )
    )
    lines.extend(
        help_table(
            "Selectors",
            (
                ("-f, --paths <paths...>", "Match relative paths and everything inside matched directories."),
                ("-t, --types <types...>", "Match entry types: file, dir, link."),
                ("-e, --extensions <ext...>", "Match extensions such as .ts, .json, or .md."),
                ("-n, --names <names...>", "Match exact file or directory basenames."),
            ),
            color,
        )
    )
    lines.extend(
        help_table(
            "Visibility",
            (
                ("--ignore-hidden", "Hide dot-prefixed files and directories."),
                ("--include-hidden", "Show dot-prefixed files and directories, overriding config."),
                ("--ignore-empty", "Hide empty files and directories."),
                ("--include-empty", "Show empty files and directories, overriding config."),
            ),
            color,
        )
    )
    lines.extend(
        help_table(
            "Rendering",
            (
                (
                    "--scan-styling <full|low|minimal>",
                    "Set layout style: full, low, or minimal. Color follows terminal support.",
                ),
                ("--scan-emojis <true|false>", "Show or hide file-type emojis in entry names."),
                (
                    '--scan-data <"item, item, ...">',
                    "Comma-separated items: tree, lines, size, modified, type, git, summary. "
                    "Omitting tree uses flat paths; summary adds the summary block.",
                ),
            ),
            color,
        )
    )
    lines.extend(
        help_table(
            "Runtime",
            (
                ("--scan-timeout <seconds>", "Use a best-effort scan budget and print a partial result when exceeded."),
                ("--auto-copy <true|false>", "Copy plain scan output to the clipboard after rendering."),
                ("-h, --help", "Show this help when used before '--'."),
                ("--version", "Show the runtime version when used before '--'."),
            ),
            color,
        )
    )
    lines.extend(
        help_table(
            "Configuration",
            (
                ("--config <path>", "Use a specific config.yaml file."),
                (
                    "--project-config <auto|ignore|require>",
                    "Control repository-owned .sdir.yaml or sdir.yaml discovery.",
                ),
            ),
            color,
        )
    )
    lines.append("")
    lines.extend(
        help_wrapped_lines(
            "Command-like path names can be scanned with ./help, ./status, or ./version.", color, ANSI_DIM
        )
    )
    lines.extend(
        help_wrapped_lines(
            "Place the scan path before selectors, or after '--' when selectors come first.", color, ANSI_DIM
        )
    )
    lines.extend(
        help_wrapped_lines(
            "Selector values beginning with '-' use attached syntax, for example --names=-draft.", color, ANSI_DIM
        )
    )
    lines.extend(
        help_wrapped_lines(
            "--full rejects selectors and hide flags instead of silently discarding them.", color, ANSI_DIM
        )
    )
    return "\n".join(lines) + "\n"


def render_usage_error(message: str, color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    message = sanitize_terminal_text(message)
    lines = help_wrapped_lines(f"{PROGRAM_NAME}: {message}", color, ANSI_RED, ANSI_BOLD)
    lines.extend(("", style("Usage", ANSI_CYAN, ANSI_BOLD, enabled=color), help_usage_line(color)))
    lines.extend(help_wrapped_lines("sdir help", color, ANSI_CYAN, indent=2))
    return "\n".join(lines) + "\n"


def render_runtime_error(message: str, color: bool | None = None) -> str:
    color = terminal_color_enabled() if color is None else color
    message = sanitize_terminal_text(message)
    lines = help_wrapped_lines(f"{PROGRAM_NAME}: {message}", color, ANSI_RED, ANSI_BOLD)
    return "\n".join(lines) + "\n"


# ─── CLI PARSING ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = SdirArgumentParser(
        prog=PROGRAM_NAME,
        description="Scan Dir scans a project directory and prints a compact information-rich tree.",
        allow_abbrev=False,
        add_help=False,
    )
    parser.add_argument("path", nargs="?", default=".", help="directory or file to scan")

    groups: dict[str, argparse._ActionsContainer] = {
        "general": parser,
        "selector": parser,
        "mode": parser.add_mutually_exclusive_group(),
        "hidden": parser.add_mutually_exclusive_group(),
        "empty": parser.add_mutually_exclusive_group(),
    }
    for spec in CLI_OPTION_SPECS:
        target = groups[spec.group]
        # argparse exposes a heterogeneous keyword surface; Any is confined
        # to this adapter after values are validated by CLIOptionSpec.
        kwargs: dict[str, Any] = {"dest": spec.dest, "help": spec.help}
        if spec.value_mode == "multiple":
            kwargs.update(nargs="+", action=spec.action or "append")
        elif spec.value_mode == "single":
            if spec.choices:
                kwargs["choices"] = spec.choices
            if spec.converter is not None:
                kwargs["type"] = spec.converter
        else:
            kwargs["action"] = spec.action or "store_true"
            if spec.action == "store_const":
                kwargs["const"] = spec.const
                kwargs["default"] = spec.default
        target.add_argument(*spec.flags, **kwargs)
    return parser


def selector_flag_for_shortcut(value: str) -> str:
    normalized = value
    if not normalized:
        return "--names"
    if normalized in ENTRY_TYPES:
        return "--types"
    if normalized == ".":
        return "--paths"
    if normalized.startswith(".") and "/" not in normalized:
        return "--extensions"
    if "/" in normalized:
        return "--paths"
    return "--names"


def split_attached_option(token: str) -> tuple[str, str | None]:
    """Split an attached option value without interpreting arbitrary tokens."""
    if "=" not in token or not token.startswith("-"):
        return token, None
    flag, value = token.split("=", 1)
    return flag, value


def grouped_shortcut_tokens(values: Sequence[str]) -> list[str]:
    """Convert mode shorthand values into canonical selector arguments."""
    grouped: dict[str, list[str]] = {}
    for value in values:
        grouped.setdefault(selector_flag_for_shortcut(value), []).append(value)
    output: list[str] = []
    for flag in SELECTOR_ORDER:
        if flag in grouped:
            output.extend((flag, *grouped[flag]))
    return output


def canonicalize_cli_argv(argv: Sequence[str]) -> list[str]:
    """Return deterministic argv for argparse without filesystem heuristics.

    The scan path is the sole bare positional token outside option values. A
    path following selectors must be placed after ``--``. Selector options
    consume one or more values until the next option; values beginning with a
    dash use attached syntax such as ``--names=-draft``.
    """
    option_tokens: list[str] = []
    scan_path: str | None = None
    tokens = list(argv)
    index = 0

    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            trailing = tokens[index + 1 :]
            if len(trailing) != 1:
                raise ConfigError("'--' must be followed by exactly one scan path")
            if scan_path is not None:
                raise ConfigError(
                    "multiple scan paths were provided: "
                    f"{sanitize_terminal_text(scan_path)}, {sanitize_terminal_text(trailing[0])}"
                )
            scan_path = trailing[0]
            break

        flag, attached_value = split_attached_option(token)
        spec = OPTION_SPEC_BY_FLAG.get(flag)
        if spec is None:
            if token.startswith("-"):
                option_tokens.append(token)
            else:
                if scan_path is not None:
                    raise ConfigError(
                        "multiple scan paths were provided: "
                        f"{sanitize_terminal_text(scan_path)}, {sanitize_terminal_text(token)}"
                    )
                scan_path = token
            index += 1
            continue

        if spec.value_mode == "flag":
            if attached_value is not None:
                raise ConfigError(f"{flag} does not accept a value")
            option_tokens.append(flag)
            index += 1
            if flag in SHORTCUT_MODE_FLAGS:
                values: list[str] = []
                while index < len(tokens) and not tokens[index].startswith("-"):
                    values.append(tokens[index])
                    index += 1
                option_tokens.extend(grouped_shortcut_tokens(values))
            continue

        if spec.value_mode == "single":
            if attached_value is not None:
                option_tokens.append(f"{flag}={attached_value}")
                index += 1
                continue
            index += 1
            if index >= len(tokens):
                raise ConfigError(f"{flag} requires a value")
            next_flag, _ = split_attached_option(tokens[index])
            if tokens[index] == "--" or next_flag in OPTION_SPEC_BY_FLAG:
                raise ConfigError(f"{flag} requires a value")
            option_tokens.extend((flag, tokens[index]))
            index += 1
            continue

        if attached_value is not None:
            option_tokens.append(f"{flag}={attached_value}")
            index += 1
            continue
        selector_values: list[str] = []
        index += 1
        while index < len(tokens) and not tokens[index].startswith("-"):
            selector_values.append(tokens[index])
            index += 1
        if not selector_values:
            raise ConfigError(f"{flag} requires at least one value")
        option_tokens.extend((flag, *selector_values))

    return [scan_path or ".", *option_tokens]


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
    canonical_argv = canonicalize_cli_argv(argv)
    args = parser.parse_args(canonical_argv)
    # args.help / args.version are unreachable here: run() dispatches the
    # help/version tokens before resolve_runtime_config is called, and the
    # parser's own --help/--version actions are store_true (no exit). If
    # argparse ever does call parser.exit() for help, HelpRequested propagates.

    root_path = resolve_scan_path(args.path)
    if not os.path.lexists(root_path):
        raise ConfigError(f"scan path does not exist: {root_path}")

    project_config_mode = args.project_config or "auto"
    project_path = selected_project_config_path(root_path, project_config_mode)
    config_paths = config_paths_with_project(args.config, project_path)
    explicit_identity = (
        os.path.realpath(expand_user_path(args.config, "config path")) if args.config is not None else None
    )
    restrict_project_auto_copy = (
        project_path
        if project_path is not None
        and (explicit_identity is None or os.path.realpath(project_path) != explicit_identity)
        else None
    )
    config_payload = merged_config_payload(config_paths, restrict_project_auto_copy)
    config_rules = normalize_rules(config_payload)
    explicit_rules = cli_rules(args)

    if args.full and explicit_rules.has_rules:
        raise ConfigError("--full cannot be combined with filter selectors")
    if args.full and (args.ignore_hidden is True or args.ignore_empty is True):
        raise ConfigError("--full cannot be combined with --ignore-hidden or --ignore-empty")

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

    if args.ignore_hidden is not None:
        ignore_hidden = args.ignore_hidden
    if args.ignore_empty is not None:
        ignore_empty = args.ignore_empty
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
        config_paths=config_paths,
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
    if is_root:
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
        if node.kind == "dir" and not node.children and not node.incomplete:
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
        if state.timeout_reached():
            return None
        chunk = os.read(file_fd, TEXT_READ_CHUNK_SIZE)
        if state.timeout_reached():
            return None
        if not chunk:
            break
        if b"\0" in chunk:
            return None
        saw_bytes = True
        total += chunk.count(b"\n")
        last_byte = chunk[-1:]
    if saw_bytes and last_byte != b"\n":
        total += 1
    return total


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


def prefilter_entry(rel_path: str, name: str, kind: str, config: RuntimeConfig) -> bool:
    if config.ignore_hidden and is_hidden_rel_path(rel_path):
        return False
    rules = config.rules
    if config.filter_mode == "ignore":
        if name in rules.names or any(path_selector_matches(selector, rel_path) for selector in rules.paths):
            return False
        if kind in rules.types:
            return False
        if kind == "file":
            ext = Path(name).suffix.lower()
            if ext and ext in rules.extensions:
                return False
    if config.filter_mode == "only" and kind == "dir":
        path_rules = rules.paths
        path_relevant = any(
            path_selector_matches(selector, rel_path) or selector.startswith(f"{rel_path}/") for selector in path_rules
        )
        if path_rules and not path_relevant and not rules.types and not rules.extensions and not rules.names:
            return False
    return True


def prefilter_before_stat(rel_path: str, name: str, config: RuntimeConfig) -> bool:
    if config.ignore_hidden and is_hidden_rel_path(rel_path):
        return False
    if config.filter_mode == "ignore" and (
        name in config.rules.names or any(path_selector_matches(selector, rel_path) for selector in config.rules.paths)
    ):
        return False
    if (
        config.filter_mode == "only"
        and config.rules.paths
        and not config.rules.types
        and not config.rules.extensions
        and not config.rules.names
    ):
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
    node.total_files = 0
    node.total_links = 0
    node.total_lines = 0
    node.unknown_lines = 0
    largest: tuple[str, int] | None = None
    newest_any: tuple[str, float] = (node.summary_path, node.mtime)
    for child in node.children:
        node.total_files += child.total_files
        node.total_dirs += child.total_dirs
        node.total_links += child.total_links
        node.total_lines += child.total_lines
        node.unknown_lines += child.unknown_lines
        if child.largest_file is not None and (largest is None or child.largest_file[1] > largest[1]):
            largest = child.largest_file
        if child.newest_entry is not None and child.newest_entry[1] > newest_any[1]:
            newest_any = child.newest_entry
    node.largest_file = largest
    node.newest_entry = newest_any


def create_leaf(
    path: Path,
    rel_path: str,
    path_stat: os.stat_result,
    kind: str,
    state: ScanState,
    is_root: bool,
    parent_fd: int | None = None,
    entry_name: str | None = None,
) -> EntryNode | None:
    name = path.name or str(path)
    if kind == "special":
        state.warn(rel_path, "unsupported special filesystem entry skipped")
        return None
    if not is_root and not prefilter_entry(rel_path, name, kind, state.config):
        return None
    if kind == "link":
        try:
            target = link_target(entry_name or os.fspath(path), parent_fd)
        except OSError as exc:
            target = None
            state.warn(rel_path, f"unable to read link target: {exc}")
        try:
            observed_stat = os.stat(
                entry_name or os.fspath(path),
                dir_fd=parent_fd,
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
        inspect_content = "lines" in state.config.scan_data or "summary" in state.config.scan_data
        if not inspect_content:
            try:
                observed_stat = os.stat(
                    access_name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                state.warn(rel_path, f"unable to verify file metadata: {sanitize_terminal_text(exc)}")
                return None
            if (observed_stat.st_dev, observed_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
                state.warn(rel_path, "directory entry was replaced while the file was being scanned")
                return None
            if not stat.S_ISREG(observed_stat.st_mode):
                state.warn(rel_path, "entry type changed while the file was being scanned")
                return None
            if observed_stat.st_size == 0 and state.config.ignore_empty and not is_root:
                return None
            node = EntryNode(
                path,
                rel_path,
                name,
                kind,
                observed_stat.st_size,
                observed_stat.st_mtime,
                lines=None,
            )
            initialize_aggregate(node)
            return node if should_keep_node(node, state.config, is_root) else None

        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        stable_result: tuple[os.stat_result, int | None] | None = None
        instability = "file changed while its content was being inspected"
        last_error: OSError | None = None
        attempts = 0
        while attempts < FILE_READ_MAX_ATTEMPTS and not state.timeout_reached():
            attempts += 1
            last_error = None
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
                    lines = None
                    state.warn(rel_path, f"unable to count lines: {sanitize_terminal_text(exc)}")
                try:
                    post_read_stat = os.fstat(file_fd)
                    current_path_stat = os.stat(access_name, dir_fd=parent_fd, follow_symlinks=False)
                except OSError as exc:
                    instability = f"unable to verify file metadata after reading: {exc}"
                    continue
                if (current_path_stat.st_dev, current_path_stat.st_ino) != (opened_stat.st_dev, opened_stat.st_ino):
                    instability = "directory entry was replaced while the file was being read"
                    continue
                before_signature = (
                    opened_stat.st_size,
                    opened_stat.st_mtime_ns,
                    opened_stat.st_ctime_ns,
                )
                after_signature = (
                    post_read_stat.st_size,
                    post_read_stat.st_mtime_ns,
                    post_read_stat.st_ctime_ns,
                )
                if after_signature != before_signature:
                    instability = "file content changed while it was being read"
                    continue
                stable_result = post_read_stat, lines
                break
            except OSError as exc:
                last_error = exc
                instability = f"unable to inspect file safely: {exc}"
            finally:
                if file_fd is not None:
                    os.close(file_fd)
        if stable_result is None:
            if attempts == 0 and state.timed_out:
                return None
            if last_error is not None and last_error.errno in {
                errno.EACCES,
                errno.EPERM,
                errno.EMFILE,
                errno.ENFILE,
            }:
                state.warn(rel_path, f"unable to read file content: {sanitize_terminal_text(last_error)}")
                node = EntryNode(
                    path,
                    rel_path,
                    name,
                    kind,
                    path_stat.st_size,
                    path_stat.st_mtime,
                    lines=None,
                    incomplete=True,
                )
                initialize_aggregate(node)
                return node if should_keep_node(node, state.config, is_root) else None
            state.warn(rel_path, f"{instability}; skipped after {attempts} attempt(s)")
            return None
        observed_stat, lines = stable_result
        if observed_stat.st_size == 0 and state.config.ignore_empty and not is_root:
            return None
        node = EntryNode(
            path,
            rel_path,
            name,
            kind,
            observed_stat.st_size,
            observed_stat.st_mtime,
            lines=lines,
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
    incomplete: bool = False


def directory_frame(
    path: Path,
    rel_path: str,
    path_stat: os.stat_result,
    state: ScanState,
    is_root: bool,
    fd: int,
) -> DirectoryFrame:
    incomplete = False
    iterator: Iterator[os.DirEntry[str]]
    try:
        iterator = os.scandir(fd)
    except OSError as exc:
        if exc.errno == errno.EMFILE:
            state.warn(rel_path, "file descriptor limit reached; subtree skipped")
        else:
            state.warn(rel_path, f"unable to read directory: {exc}")
        iterator = iter(())
        incomplete = True
    return DirectoryFrame(path, rel_path, path_stat, is_root, iterator, fd, incomplete=incomplete)


def incomplete_directory_node(
    path: Path,
    rel_path: str,
    path_stat: os.stat_result,
    config: RuntimeConfig,
    is_root: bool,
) -> EntryNode | None:
    node = EntryNode(
        path,
        rel_path,
        path.name or str(path),
        "dir",
        0,
        path_stat.st_mtime,
        incomplete=True,
    )
    initialize_aggregate(node)
    return node if should_keep_node(node, config, is_root) else None


def close_directory_frame(frame: DirectoryFrame) -> None:
    close = getattr(frame.iterator, "close", None)
    try:
        if close is not None:
            close()
    finally:
        os.close(frame.fd)


def scan_path(
    path: Path,
    rel_path: str,
    state: ScanState,
    is_root: bool = False,
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

    directory_flags = (
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    root_fd: int | None = None
    try:
        root_fd = os.open(access_path, directory_flags)
        opened_root_stat = os.fstat(root_fd)
    except OSError as exc:
        if root_fd is not None:
            os.close(root_fd)
        state.warn(rel_path, f"unable to open directory: {exc}")
        return incomplete_directory_node(path, rel_path, root_stat, state.config, is_root)
    if (opened_root_stat.st_dev, opened_root_stat.st_ino) != (root_stat.st_dev, root_stat.st_ino):
        os.close(root_fd)
        state.warn(rel_path, "scan root changed while it was being opened")
        return None
    root_identity = (opened_root_stat.st_dev, opened_root_stat.st_ino)
    state.visited_directories.add(root_identity)
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
                if state.timeout_reached():
                    frame.incomplete = True
            except OSError as exc:
                state.warn(frame.rel_path, f"unable to continue reading directory: {exc}")
                frame.incomplete = True
                entry = None
            else:
                if state.timeout_reached():
                    frame.incomplete = True
                    entry = None
        else:
            frame.incomplete = True
        if entry is None:
            close_directory_frame(frame)
            frame.children.sort(key=sort_key)
            size = sum(child.size for child in frame.children)
            mtime = max((child.mtime for child in frame.children), default=frame.path_stat.st_mtime)
            node = EntryNode(
                frame.path,
                frame.rel_path,
                frame.path.name or str(frame.path),
                "dir",
                size,
                mtime,
                children=frame.children,
                incomplete=frame.incomplete,
            )
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
        if not prefilter_entry(child_rel, entry.name, child_kind, state.config):
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
                placeholder = incomplete_directory_node(
                    child_path,
                    child_rel,
                    child_stat,
                    state.config,
                    False,
                )
                if placeholder is not None:
                    frame.children.append(placeholder)
                continue
            if child_fd is None:
                continue
            child_identity = (opened_child_stat.st_dev, opened_child_stat.st_ino)
            if child_identity in state.visited_directories:
                os.close(child_fd)
                state.warn(child_rel, "directory identity was already scanned; subtree not followed")
                placeholder = incomplete_directory_node(
                    child_path,
                    child_rel,
                    opened_child_stat,
                    state.config,
                    False,
                )
                if placeholder is not None:
                    frame.children.append(placeholder)
                continue
            state.visited_directories.add(child_identity)
            stack.append(directory_frame(child_path, child_rel, opened_child_stat, state, False, child_fd))
        else:
            child = create_leaf(
                child_path,
                child_rel,
                child_stat,
                child_kind,
                state,
                False,
                parent_fd=frame.fd,
                entry_name=entry.name,
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
        config=config,
        started_at=started_at,
        deadline=started_at + config.scan_timeout,
        physical_root=physical_root,
    )
    root = scan_path(config.root_path, ".", state, is_root=True, physical_path=physical_root)
    if root is None:
        raise SdirError("scan root was excluded by the active filters or is not a supported filesystem entry")
    git_markers: dict[str, str] = {}
    deleted_git_entries: list[DeletedGitEntry] = []
    if "git" in config.scan_data:
        git_markers, deleted_git_entries, git_warning = load_git_markers(state.physical_root, state)
        if git_warning is not None:
            state.warn(".", git_warning)
    state.timeout_reached()
    elapsed_ms = int((time.monotonic() - started_at) * 1000)
    return ScanResult(
        root=root,
        elapsed_ms=elapsed_ms,
        timed_out=state.timed_out,
        warnings=state.warnings,
        git_markers=git_markers,
        deleted_git_entries=deleted_git_entries,
    )


# ─── GIT STATUS ─────────────────────────────────────────────────────────────


def run_git(
    args: Sequence[str],
    cwd: Path,
    state: ScanState,
    input_data: bytes | None = None,
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
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    command = ["git"]
    for override in GIT_CONFIG_OVERRIDES:
        command.extend(("-c", override))
    command.extend(args)
    try:
        return subprocess.run(
            command,
            cwd=str(cwd),
            env=environment,
            capture_output=True,
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
    if status == "!!":
        return "[!]"
    if status in {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}:
        return "[U]"
    if status == "??":
        return "[?]"
    if "D" in status:
        return "[D]"
    if "R" in status:
        return "[R]"
    if "C" in status:
        return "[C]"
    if "A" in status:
        return "[A]"
    if any(marker in status for marker in ("M", "T")):
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
            # Porcelain appends '/' to ignored directory records. Scanner
            # relative paths never do, so normalize that presentation suffix.
            normalized_path = git_path_identity(path).removesuffix("/")
            markers[normalized_path] = marker
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
        deleted.append(
            (
                git_path_identity(os.fsdecode(raw_path)),
                fields[0].decode("ascii", "replace"),
                fields[1].decode("ascii", "replace"),
            )
        )
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
            "config",
            "--includes",
            "--null",
            "--show-scope",
            "--show-origin",
            "--name-only",
            "--get-regexp",
            r"^(filter\..*\.(clean|smudge|process)|diff\.external|diff\..*\.(command|textconv)|core\.(fsmonitor|hookspath))$",
        ],
        repo_root,
        state,
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
    keys = sorted(
        {
            sanitize_terminal_text(os.fsdecode(fields[index + 2]))
            for index in range(0, len(fields), 3)
            if os.fsdecode(fields[index]).lower() in {"local", "worktree"}
        }
    )
    if not keys:
        return None
    return "Git markers disabled because executable repository configuration is active: " + ", ".join(keys)


def deleted_git_metadata(
    repo_root: Path,
    state: ScanState,
    deleted_paths: set[str],
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
            ["ls-tree", "-r", "-z", "--full-tree", "HEAD"],
            repo_root,
            state,
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
        ["cat-file", "--batch-check=%(objectname) %(objectsize)"],
        repo_root,
        state,
        query,
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
    for candidate in itertools.chain([current], current.parents):
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
    scan_root: Path,
    state: ScanState,
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
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignored=matching",
            "--ignore-submodules=dirty",
        ],
        repo_root,
        state,
    )
    if result is None:
        return {}, [], run_error or "git markers could not be rendered"
    if result.returncode != 0:
        message = os.fsdecode(result.stderr).strip() or "git status failed"
        return {}, [], f"git markers unavailable: {message}"
    repo_markers = parse_porcelain_z(result.stdout)
    if any(marker == "[D]" for marker in repo_markers.values()):
        deleted_entries, deleted_warning = deleted_git_metadata(
            repo_root,
            state,
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
    """Return a bounded terminal width without trusting hostile environment data."""
    raw_columns = os.environ.get("COLUMNS", "")
    if re.fullmatch(r"[0-9]{1,6}", raw_columns):
        columns = int(raw_columns, 10)
        if columns > 0:
            return min(TERMINAL_WIDTH_MAXIMUM, max(TERMINAL_WIDTH_MINIMUM, columns))
    try:
        columns = os.get_terminal_size(sys.stdout.fileno()).columns
    except (AttributeError, OSError, ValueError):
        columns = TERMINAL_WIDTH_FALLBACK
    return min(TERMINAL_WIDTH_MAXIMUM, max(TERMINAL_WIDTH_MINIMUM, columns))


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
        chunk = candidate.removesuffix("…")
        split_at = chunk.rfind(" ")
        if split_at > 0:
            chunk = chunk[:split_at]
        if not chunk:
            chunk = remaining[0]
        lines.append(chunk.rstrip())
        remaining = remaining[len(chunk) :].lstrip()
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
        "[C]": ANSI_CYAN,
        "[U]": ANSI_RED,
        "[?]": ANSI_BLUE,
        "[!]": ANSI_DIM,
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
        style(pad_cells(header, METADATA_COLUMN_WIDTH), ANSI_CYAN, ANSI_BOLD, enabled=enabled) for header in headers
    ).rstrip()


def render_padded_row(
    label: str,
    columns: Sequence[str],
    node: EntryNode | None,
    width: int,
    color: bool,
    header: bool = False,
    marker: str = "",
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


def display_relative_path(node: EntryNode, emojis: bool) -> str:
    """Render an unambiguous path for non-tree output.

    A flat listing must not collapse ``src/a.py`` and ``tests/a.py`` into two
    indistinguishable ``a.py`` rows. Tree output uses basenames because the
    connectors carry hierarchy; flat output uses the complete relative path.
    """

    prefix = KIND_EMOJIS[node.kind] if emojis else ""
    relative = sanitize_terminal_text(node.rel_path)
    if node.kind == "dir":
        return f"{prefix}{relative}/"
    if node.kind == "link" and node.target:
        return f"{prefix}{relative} -> {sanitize_terminal_text(node.target)}"
    return f"{prefix}{relative}"


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


def inline_metadata_enabled(scan_data: frozenset[str]) -> bool:
    headers = header_columns(scan_data)
    if not headers:
        return False
    required = NAME_COLUMN_MIN_WIDTH + cell_width(METADATA_SEPARATOR) + METADATA_COLUMN_WIDTH * len(headers)
    return terminal_columns() >= required


def active_header_columns(scan_data: frozenset[str]) -> list[str]:
    headers = header_columns(scan_data)
    return headers if inline_metadata_enabled(scan_data) else []


def name_column_width(scan_data: frozenset[str]) -> int:
    metadata_width = METADATA_COLUMN_WIDTH * len(active_header_columns(scan_data))
    separator_width = cell_width(METADATA_SEPARATOR) if metadata_width else 0
    return max(NAME_COLUMN_MIN_WIDTH, terminal_columns() - metadata_width - separator_width)


def render_label_row(
    label: str,
    node: EntryNode,
    color: bool,
    marker: str,
) -> str:
    return render_padded_row(
        label,
        (),
        node,
        terminal_columns(),
        color,
        marker=marker,
    ).rstrip()


def render_metadata_detail_lines(
    node: EntryNode,
    scan_data: frozenset[str],
    color: bool,
) -> list[str]:
    lines: list[str] = []
    for header, value in zip(
        header_columns(scan_data),
        metadata_columns(node, scan_data),
        strict=True,
    ):
        lines.extend(
            help_wrapped_lines(
                f"{header}: {value}",
                color,
                ANSI_DIM,
                indent=2,
            )
        )
    return lines


def render_node_rows(
    label: str,
    node: EntryNode,
    config: RuntimeConfig,
    color: bool,
    marker: str,
    *,
    header: bool = False,
) -> list[str]:
    headers = header_columns(config.scan_data)
    if not headers:
        return [render_label_row(label, node, color, marker)]
    if inline_metadata_enabled(config.scan_data):
        width = name_column_width(config.scan_data)
        columns = headers if header else metadata_columns(node, config.scan_data)
        return [
            render_padded_row(
                label,
                columns,
                node,
                width,
                color,
                header=header,
                marker=marker,
            )
        ]
    return [
        render_label_row(label, node, color, marker),
        *render_metadata_detail_lines(node, config.scan_data, color),
    ]


def _render_root_row(
    root: EntryNode,
    config: RuntimeConfig,
    color: bool,
    marker: str,
    lines: list[str],
) -> None:
    root_label = display_name(root, config.scan_emojis)
    lines.extend(
        render_node_rows(
            root_label,
            root,
            config,
            color,
            marker,
            header=root.kind == "dir" and inline_metadata_enabled(config.scan_data),
        )
    )


def render_flat_entries(
    root: EntryNode,
    result: ScanResult,
    config: RuntimeConfig,
    color: bool = False,
) -> list[str]:
    lines: list[str] = []
    marker = result.git_markers.get(".", "") if "git" in config.scan_data else ""
    _render_root_row(root, config, color, marker, lines)
    stack: list[EntryNode] = []
    if root.kind == "dir":
        stack.extend(reversed(root.children))
    while stack:
        node = stack.pop()
        plain_name = display_relative_path(node, config.scan_emojis)
        marker = result.git_markers.get(node.rel_path, "") if "git" in config.scan_data else ""
        lines.extend(render_node_rows(plain_name, node, config, color, marker))
        if node.kind == "dir" and node.children:
            stack.extend(reversed(node.children))
    return lines


def render_tree_lines(
    root: EntryNode,
    result: ScanResult,
    config: RuntimeConfig,
    color: bool = False,
) -> list[str]:
    if "tree" not in config.scan_data:
        return render_flat_entries(root, result, config, color)
    lines: list[str] = []
    marker = result.git_markers.get(".", "") if "git" in config.scan_data else ""
    _render_root_row(root, config, color, marker, lines)

    pending: list[tuple[EntryNode, str, bool]] = []
    if root.kind == "dir":
        pending.extend(
            (child, "", index == len(root.children) - 1) for index, child in reversed(list(enumerate(root.children)))
        )
    while pending:
        node, prefix, is_last = pending.pop()
        connector = "└── " if is_last else "├── "
        plain_name = visible_node_label(node, prefix, connector, config.scan_emojis)
        marker = result.git_markers.get(node.rel_path, "") if "git" in config.scan_data else ""
        lines.extend(render_node_rows(plain_name, node, config, color, marker))
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
        path=path,
        rel_path=entry.rel_path,
        name=path.name,
        kind=entry.kind,
        size=entry.size,
        mtime=0,
    )
    if config.filter_mode == "ignore":
        ancestor_names = path.parts[:-1]
        ignored_ancestor = any(name in config.rules.names for name in ancestor_names) or (
            bool(ancestor_names) and "dir" in config.rules.types
        )
        if ignored_ancestor or rules_match(node, config.rules):
            return False
    if config.filter_mode == "only" and not rules_match(node, config.rules):
        return False
    return not (config.ignore_empty and entry.kind == "file" and entry.size == 0)


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
        entry.rel_path
        for entry in result.deleted_git_entries
        if entry.rel_path not in present and deleted_git_entry_visible(entry, config)
    )
    if not deleted:
        return []
    width = terminal_columns()
    return [
        render_padded_row(
            f"deleted {sanitize_terminal_text(path)}",
            (),
            None,
            width,
            False,
            marker="[D]",
        ).rstrip()
        for path in deleted
    ]


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
    now = datetime.now().astimezone()
    # The footer must fit inside the framed-summary box body (width - 4) when
    # rendered in framed mode; in minimal mode it uses the full terminal width.
    # Callers pass the appropriate width; the default keeps the legacy full-width
    # behavior for any caller that doesn't specify.
    effective_footer_width = terminal_columns() if footer_width is None else footer_width
    # "+? lines" indicates that some files (binary or unreadable) could not be
    # counted, so the real total is strictly higher than the number shown.
    lines_label = f"{root.total_lines:,}+? lines" if root.unknown_lines else f"{root.total_lines:,} lines"
    return {
        "total": SUMMARY_VALUE_SEPARATOR.join(
            (
                f"{files:,} {file_word}",
                f"{dirs:,} {dir_word}",
                f"{links:,} {link_word}",
                lines_label,
                format_size(root.size),
            )
        ),
        "largest": "none"
        if largest is None
        else f"{sanitize_terminal_text(largest[0])}{SUMMARY_VALUE_SEPARATOR}{format_size(largest[1])}",
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
    width = terminal_columns()
    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_cells(line, width))
    return wrapped


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
        box_border = "ascii"
    else:
        top = style("┌" + "─" * (width - 2) + "┐", ANSI_DIM, ANSI_CYAN, enabled=color)
        mid = style("├" + "─" * label_dashes + "┬" + "─" * value_dashes + "┤", ANSI_DIM, ANSI_CYAN, enabled=color)
        joined = style("├" + "─" * label_dashes + "┴" + "─" * value_dashes + "┤", ANSI_DIM, ANSI_CYAN, enabled=color)
        bottom = style("└" + "─" * (width - 2) + "┘", ANSI_DIM, ANSI_CYAN, enabled=color)
        box_border = "unicode"

    def line(text: str) -> str:
        return summary_box_line(text, width, box_border, color)

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
    lines = [
        values["total"],
        f"largest  {values['largest']}",
        f"newest   {values['newest']}",
        f"types    {values['types']}",
    ]
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
            [executable, *command[1:]],
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
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
        raise ClipboardUnavailableError("desktop session not reachable (" + "; ".join(environment_failures) + ")")
    if any_executable_found:
        raise ClipboardFailureError("clipboard backend ran but copy did not complete")
    raise ClipboardUnavailableError("clipboard backend unavailable for this desktop session")


# ─── ENTRY POINT ────────────────────────────────────────────────────────────


def run(argv: Sequence[str]) -> int:
    try:
        if argv and is_help_token(argv[0]):
            if len(argv) != 1:
                raise ConfigError("help does not accept arguments")
            write_stream(sys.stdout, render_help())
            return 0
        if argv and is_version_token(argv[0]):
            if len(argv) != 1:
                raise ConfigError("version does not accept arguments")
            write_stream(sys.stdout, render_version())
            return 0
        if argv and is_status_token(argv[0]):
            if len(argv) != 1:
                raise ConfigError("status does not accept arguments")
            write_stream(sys.stdout, render_status())
            return 0
        if option_before_separator(argv, {"-h", "--help"}):
            write_stream(sys.stdout, render_help())
            return 0
        if option_before_separator(argv, {"--version"}):
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
                # continue so sdir works out of the box everywhere.
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
    except SdirError as exc:
        write_stream(sys.stderr, render_runtime_error(str(exc)))
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
        write_stream(
            sys.stderr,
            render_runtime_error(f"operating system error: {sanitize_terminal_text(exc)}"),
        )
        return 1
    except KeyboardInterrupt:
        write_stream(sys.stderr, f"{PROGRAM_NAME}: interrupted\n")
        return 130


def main() -> None:
    raise SystemExit(run(sys.argv[1:]))


if __name__ == "__main__":
    main()
