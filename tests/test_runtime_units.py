# test_runtime_units.py -- Scan Dir runtime unit tests
# Exercises validation, parsing, Git adapters, rendering helpers, and clipboard error paths.
# Tags: tests, runtime, parsing, git, rendering, clipboard
# 2026-07-28

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import sdir


class AsciiStream(io.StringIO):
    @property
    def encoding(self) -> str:
        return "ascii"


def runtime_config(root: Path) -> sdir.RuntimeConfig:
    return sdir.RuntimeConfig(
        root_path=root,
        config_paths=(),
        filter_mode="ignore",
        rules=sdir.FilterRules(),
        ignore_hidden=False,
        ignore_empty=False,
        scan_styling="minimal",
        scan_emojis=False,
        scan_data=frozenset({"tree", "summary"}),
        scan_timeout=60.0,
        auto_copy=False,
    )


def scan_state(root: Path, *, deadline_offset: float = 60.0) -> sdir.ScanState:
    now = time.monotonic()
    return sdir.ScanState(
        config=runtime_config(root),
        started_at=now,
        deadline=now + deadline_offset,
        physical_root=root,
    )


def node(kind: str = "file", *, rel_path: str = "item.txt", name: str = "item.txt") -> sdir.EntryNode:
    return sdir.EntryNode(
        path=Path("/") / rel_path,
        rel_path=rel_path,
        name=name,
        kind=kind,
        size=1024,
        mtime=100.0,
    )


def completed(returncode: int = 0, stdout: bytes = b"", stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


def test_terminal_sanitization_and_stream_fallback() -> None:
    assert sdir.sanitize_terminal_text(b"bad-\xff") == "bad-\\xFF"
    assert sdir.sanitize_terminal_text("line\n\u202ename") == "line\\u000A<U+202E>name"
    assert sdir.ascii_terminal_text("café ─ 😀") == "cafe - "
    assert sdir.stream_text(AsciiStream(), "café ─") == "cafe -"
    assert sdir.stream_text(io.StringIO(), "café") == "café"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(".", "."), ("./src//pkg", "src/pkg"), ("src/./pkg", "src/pkg")],
)
def test_selector_path_normalization(value: str, expected: str) -> None:
    assert sdir.normalize_selector_path(value) == expected


@pytest.mark.parametrize("value", ["", "/absolute", "../escape", "src/../../escape"])
def test_selector_path_rejects_invalid_values(value: str) -> None:
    with pytest.raises(sdir.ConfigError):
        sdir.normalize_selector_path(value)


def test_scalar_normalizers() -> None:
    assert sdir.normalize_extension(".PY") == ".py"
    assert sdir.normalize_entry_type("FILE") == "file"
    assert sdir.normalize_bool(" yes ", "flag") is True
    assert sdir.normalize_bool("OFF", "flag") is False
    assert sdir.normalize_float("1.5", "timeout") == 1.5
    assert sdir.normalize_choice("LOW", "style", sdir.STYLING_LEVELS) == "low"
    assert sdir.normalize_scan_data(" tree, size, tree ") == frozenset({"tree", "size"})
    # README: `--scan-data max` / `scan-data: "max"` selects every data item.
    assert sdir.normalize_scan_data("max") == frozenset(sdir.SCAN_DATA_ITEMS)
    assert sdir.normalize_scan_data("max, size") == frozenset(sdir.SCAN_DATA_ITEMS)
    assert sdir.normalize_scan_data("MAX") == frozenset(sdir.SCAN_DATA_ITEMS)
    assert sdir.normalize_list(None, "names") == ()
    assert sdir.normalize_list(["a", "b"], "names") == ("a", "b")

    invalid_calls = (
        lambda: sdir.normalize_extension("py"),
        lambda: sdir.normalize_extension("."),
        lambda: sdir.normalize_entry_type("socket"),
        lambda: sdir.normalize_bool("maybe", "flag"),
        lambda: sdir.normalize_float(True, "timeout"),
        lambda: sdir.normalize_float(float("inf"), "timeout"),
        lambda: sdir.normalize_choice(1, "style", sdir.STYLING_LEVELS),
        lambda: sdir.normalize_scan_data("unknown"),
        lambda: sdir.normalize_scan_data(" , "),
        lambda: sdir.normalize_list("a", "names"),
        lambda: sdir.normalize_list([1], "names"),
        lambda: sdir.normalize_rules({"names": [""]}),
    )
    for call in invalid_calls:
        with pytest.raises(sdir.ConfigError):
            call()


def test_set_config_persists_default_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # README: `sdir --set-config <path>` changes the persistent default config
    # path. It must resolve to an absolute path and survive a re-read.
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SDIR_STATE_DIR", str(state_dir))

    target = (tmp_path / "custom.yaml").resolve()
    assert sdir.set_default_config_path(str(target)) == target

    marker = state_dir / sdir.DEFAULT_CONFIG_PATH_MARKER
    assert marker.exists()
    assert marker.read_text(encoding="utf-8").strip() == str(target)
    assert sdir.persisted_default_config_path() == target

    # The persisted default is honoured by the user-config resolver.
    monkeypatch.delenv("SDIR_CONFIG_DIR", raising=False)
    assert sdir.user_config_path() == target


def test_set_config_resolves_relative_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SDIR_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    expected = tmp_path / "relative.yaml"
    assert sdir.set_default_config_path("relative.yaml") == expected
    assert sdir.persisted_default_config_path() == expected


def test_set_config_arity_error_does_not_claim_absolute_path(capsys: pytest.CaptureFixture[str]) -> None:
    assert sdir.run(["--set-config"]) == 2
    error = capsys.readouterr().err
    assert "requires exactly one config path" in error
    assert "absolute" not in error


def test_persisted_default_ignores_malformed_marker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setenv("SDIR_STATE_DIR", str(state_dir))
    marker = state_dir / sdir.DEFAULT_CONFIG_PATH_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("relative.yaml\n", encoding="utf-8")
    assert sdir.persisted_default_config_path() is None


def test_yaml_parser_extended_subset_and_file_loading(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    source.write_bytes(
        b"\xef\xbb\xbfnames:\n  - 'it''s'\n  - \"line\\nvalue\"\nignore-hidden: on\nscan-timeout: 2e1\nauto-copy: null\n"
    )
    payload = sdir.load_yaml_payload(source)
    assert payload == {
        "names": ["it's", "line\nvalue"],
        "ignore-hidden": True,
        "scan-timeout": 20.0,
        "auto-copy": None,
    }

    invalid_documents = (
        "\tbad: value\n",
        "---\n",
        "bad key: value\n",
        "names: [a,,b]\n",
        "names: [a,]\n",
        "names: [[a]]\n",
        "names: {a: b}\n",
        "names:\n - a\n",
        "names:\n  value\n",
        "names:\n  -\n",
        'name: "unterminated\n',
        'name: "\\q"\n',
        "name: '''\n",
    )
    for document in invalid_documents:
        with pytest.raises(sdir.ConfigError):
            sdir.parse_config_yaml(document, source)

    source.write_bytes(b"\xff")
    with pytest.raises(sdir.ConfigError, match="not valid UTF-8"):
        sdir.load_yaml_payload(source)


def test_config_canonicalization_and_discovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "config.yaml"
    assert sdir.canonicalize_config_payload({"ignore-hidden": True}, source) == {"ignore_hidden": True}
    with pytest.raises(sdir.ConfigError, match="unknown config key"):
        sdir.canonicalize_config_payload({"unknown": True}, source)
    with pytest.raises(sdir.ConfigError, match="key collision"):
        sdir.canonicalize_config_payload({"ignore-hidden": True, "ignore_hidden": False}, source)

    monkeypatch.setenv("SDIR_CONFIG_DIR", "relative")
    with pytest.raises(sdir.ConfigError, match="must be absolute"):
        sdir.user_config_path()
    user_dir = tmp_path / "user"
    monkeypatch.setenv("SDIR_CONFIG_DIR", str(user_dir))
    assert sdir.user_config_path() == user_dir / "config.yaml"

    # README.md defines the default user config path uniformly for the
    # Linux/macOS product. Darwin must not silently redirect it into
    # ~/Library/Application Support when no explicit override is configured.
    home = tmp_path / "darwin-home"
    home.mkdir()
    monkeypatch.delenv("SDIR_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "platform", "darwin")
    assert sdir.user_config_path() == home / ".config" / "scan-dir" / "config.yaml"

    missing = tmp_path / "missing.yaml"
    assert sdir._config_candidate(missing, "optional") is None
    with pytest.raises(sdir.ConfigError, match="does not exist"):
        sdir._config_candidate(missing, "required", required=True)
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(sdir.ConfigError, match="not a regular file"):
        sdir._config_candidate(directory, "directory")

    first = tmp_path / "first.yaml"
    first.write_text("names: []\n", encoding="utf-8")
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(first)
    paths: list[Path] = []
    sdir._append_unique_config(paths, first)
    sdir._append_unique_config(paths, alias)
    assert paths == [first]


def test_cli_canonicalization_and_parser_errors() -> None:
    assert sdir.selector_flag_for_shortcut("file") == "--types"
    assert sdir.selector_flag_for_shortcut(".py") == "--extensions"
    assert sdir.selector_flag_for_shortcut("src/app") == "--paths"
    assert sdir.selector_flag_for_shortcut("README.md") == "--names"
    assert sdir.split_attached_option("--names=-draft") == ("--names", "-draft")
    assert sdir.grouped_shortcut_tokens(["README.md", ".py", "file", "src/app"]) == [
        "--paths",
        "src/app",
        "--types",
        "file",
        "--extensions",
        ".py",
        "--names",
        "README.md",
    ]
    assert sdir.canonicalize_cli_argv(["--only", ".py", "--", "src"]) == [
        "src",
        "--only",
        "--extensions",
        ".py",
    ]
    assert sdir.canonicalize_cli_argv(["path", "--scan-timeout=1"]) == ["path", "--scan-timeout=1"]

    invalid_argv = (
        ["--"],
        ["--", "a", "b"],
        ["a", "b"],
        ["a", "--", "b"],
        ["--help=value"],
        ["--scan-timeout"],
        ["--scan-timeout", "--full"],
        ["--names"],
    )
    for argv in invalid_argv:
        with pytest.raises(sdir.ConfigError):
            sdir.canonicalize_cli_argv(argv)


def test_filtering_and_entry_formatting() -> None:
    file_node = node()
    dir_node = node("dir", rel_path="src", name="src")
    dir_node.total_files = 26
    dir_node.total_dirs = 15
    dir_node.total_links = 3
    link_node = node("link", rel_path="link", name="link")
    link_node.target = "target"

    rules = sdir.FilterRules(paths=("src",), extensions=(".py",), names=("README.md",))
    assert rules.has_rules
    assert sdir.path_selector_matches("src", "src/pkg/a.py")
    assert not sdir.path_selector_matches("src", "source/a.py")
    assert sdir.is_hidden_rel_path("src/.secret/file")
    assert sdir.display_name(dir_node, False) == "src/"
    assert sdir.display_name(link_node, False) == "link -> target"
    assert sdir.display_relative_path(link_node, False) == "link -> target"
    assert sdir.format_size(0) == "0 B"
    assert sdir.format_size(1024) == "1.0 KB"
    assert sdir.format_age(0, now=60) == "1m ago"
    assert sdir.format_age(100, now=50) == "now"
    assert sdir.format_lines(None) == "?L"
    assert sdir.format_lines(1234) == "1,234L"
    assert sdir.format_entries(file_node) == "file"
    assert sdir.format_entries(link_node) == "link"
    assert sdir.format_entries(dir_node) == "26 files, 14 dirs, 3 links"


def test_terminal_width_and_styling_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "1")
    assert sdir.terminal_columns() == sdir.TERMINAL_WIDTH_MINIMUM
    monkeypatch.setenv("COLUMNS", "700")
    assert sdir.terminal_columns() == sdir.TERMINAL_WIDTH_MAXIMUM
    monkeypatch.setenv("COLUMNS", "9" * 5000)
    assert sdir.terminal_columns() == sdir.TERMINAL_WIDTH_MAXIMUM
    bounded_row = sdir.render_padded_row("x", ("file",), None, sdir.terminal_columns(), False)
    assert sdir.cell_width(bounded_row) <= (
        sdir.TERMINAL_WIDTH_MAXIMUM + sdir.cell_width(sdir.METADATA_SEPARATOR) + sdir.METADATA_COLUMN_WIDTH
    )
    monkeypatch.setenv("COLUMNS", "9" * 5000 + "x")
    monkeypatch.setattr(os, "get_terminal_size", lambda _fd: os.terminal_size((77, 24)))
    assert sdir.terminal_columns() == 77

    assert sdir.char_cell_width("\u0301") == 0
    assert sdir.char_cell_width("界") == 2
    assert sdir.cell_width("a界") == 3
    assert sdir.pad_cells("a", 3) == "a  "
    assert sdir.pad_cells("abcdef", 4) == "abcdef"
    assert sdir.wrap_cells("x", 1) == ["x"]
    assert sdir.wrap_cells("abcdef", 2) == ["ab", "cd", "ef"]
    assert sdir.wrap_cells("alpha beta", 6) == ["alpha", " beta"]
    preserved = "  界…  value  "
    assert "".join(sdir.wrap_cells(preserved, 4)) == preserved
    assert sdir.wrap_cells("x", 0) == ["x"]
    assert sdir.styled_columns(("metadata-value-that-exceeds-width", "next"), False) == (
        "metadata-value-that-exceeds-width next"
    )
    styled = sdir.style("x", sdir.ANSI_BOLD, enabled=True)
    assert sdir.strip_ansi(styled) == "x"
    assert sdir.style_git_marker("[M]", False) == "[M]"
    assert sdir.row_style_for_node(node("dir"), True) == (sdir.ANSI_CYAN, sdir.ANSI_BOLD)
    assert sdir.row_style_for_node(node("link"), True) == (sdir.ANSI_MAGENTA, sdir.ANSI_BOLD)
    assert sdir.row_style_for_node(node(), True) == ()


def test_help_explains_scan_data_layout() -> None:
    help_text = " ".join(sdir.render_help(color=False).split())
    assert "Omitting tree uses flat paths; summary adds the summary block." in help_text


def test_help_separates_long_options_and_stays_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "100")
    wide_help = sdir.render_help(color=False)
    assert "--project-config <auto|ignore|require>Control" not in wide_help
    assert "--project-config <auto|ignore|require> Control repository-owned" in wide_help

    monkeypatch.setenv("COLUMNS", str(sdir.HELP_TABLE_MIN_WIDTH))
    narrow_help = sdir.render_help(color=False)
    assert max(sdir.cell_width(line) for line in narrow_help.splitlines()) <= sdir.HELP_TABLE_MIN_WIDTH


def test_git_status_parsers() -> None:
    assert sdir.marker_from_status("!!") == "[!]"
    assert sdir.marker_from_status("UU") == "[U]"
    assert sdir.marker_from_status("??") == "[?]"
    assert sdir.marker_from_status(" D") == "[D]"
    assert sdir.marker_from_status("R ") == "[R]"
    assert sdir.marker_from_status("C ") == "[C]"
    assert sdir.marker_from_status("A ") == "[A]"
    assert sdir.marker_from_status(" T") == "[M]"
    assert sdir.marker_from_status("  ") == ""

    payload = b" M tracked.txt\0?? new.txt\0!! ignored/\0R  renamed.txt\0old.txt\0"
    assert sdir.parse_porcelain_z(payload) == {
        "tracked.txt": "[M]",
        "new.txt": "[?]",
        "ignored": "[!]",
        "renamed.txt": "[R]",
    }
    assert sdir.parse_index_deleted(b"100644 abcdef 0\told.txt\0bad\0") == [("old.txt", "100644", "abcdef")]
    assert sdir.parse_tree_entries(b"100644 blob abcdef\tfile.txt\0bad\0") == {"file.txt": ("100644", "abcdef")}


def test_git_execution_adapter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = scan_state(tmp_path)
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        captured["command"] = command
        captured.update(kwargs)
        return completed(stdout=b"ok\n")

    monkeypatch.setenv("GIT_DIR", "hostile")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result, error = sdir.run_git(["status"], tmp_path, state)
    assert error is None and result is not None and result.stdout == b"ok\n"
    assert captured["command"][:1] == ["git"]
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert "GIT_DIR" not in environment
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    timeout = captured["timeout"]
    assert isinstance(timeout, float)
    assert timeout > 20, "Git must receive the README scan budget, not an undocumented 5s cap"

    monkeypatch.setattr(
        subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("git", 1))
    )
    _, timeout_error = sdir.run_git(["status"], tmp_path, scan_state(tmp_path))
    assert timeout_error is not None
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing")))
    _, os_error = sdir.run_git(["status"], tmp_path, scan_state(tmp_path))
    assert os_error == "unable to execute git: missing"

    expired = scan_state(tmp_path, deadline_offset=-1)
    result, error = sdir.run_git(["status"], tmp_path, expired)
    assert result is None and expired.timed_out and "deadline" in str(error)


def test_executable_git_config_detection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = scan_state(tmp_path)
    monkeypatch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (completed(returncode=1), None))
    assert sdir.executable_git_config_warning(tmp_path, state) is None
    monkeypatch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (completed(returncode=2, stderr=b"bad"), None))
    assert "bad" in str(sdir.executable_git_config_warning(tmp_path, state))
    malformed = completed(stdout=b"local\0file:.git/config\0")
    monkeypatch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (malformed, None))
    assert "malformed" in str(sdir.executable_git_config_warning(tmp_path, state))
    configured = completed(
        stdout=b"local\0file:.git/config\0filter.demo.clean\0global\0file:~/.gitconfig\0diff.external\0"
    )
    monkeypatch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (configured, None))
    warning = sdir.executable_git_config_warning(tmp_path, state)
    assert warning is not None and "filter.demo.clean" in warning and "diff.external" not in warning


def test_clipboard_backend_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    assert sdir.clipboard_backend_candidates() == [
        ("wl-copy",),
        ("xclip", "-selection", "clipboard"),
        ("xsel", "--clipboard", "--input"),
    ]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert sdir.clipboard_backend_candidates() == [("pbcopy",)]


def test_clipboard_command_and_aggregation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdir.shutil, "which", lambda _name: None)
    assert sdir.run_clipboard_command(("missing",), "text") == (False, None, False)

    monkeypatch.setattr(sdir.shutil, "which", lambda _name: "/bin/tool")
    monkeypatch.setattr(sdir.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stderr=b""))
    assert sdir.run_clipboard_command(("tool",), "text") == (True, None, False)
    monkeypatch.setattr(
        sdir.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=b"Can't open display: :0"),
    )
    assert sdir.run_clipboard_command(("tool",), "text") == (True, "Can't open display: :0", True)
    monkeypatch.setattr(
        sdir.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=7, stderr=b"backend exploded"),
    )
    assert sdir.run_clipboard_command(("tool",), "text") == (True, "backend exploded", False)
    monkeypatch.setattr(sdir.subprocess, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("denied")))
    assert sdir.run_clipboard_command(("tool",), "text") == (True, "denied", True)

    monkeypatch.setattr(sdir, "clipboard_backend_candidates", lambda: [])
    with pytest.raises(sdir.ClipboardUnavailableError):
        sdir.copy_to_clipboard("text")
    monkeypatch.setattr(sdir, "clipboard_backend_candidates", lambda: [("one",), ("two",)])
    monkeypatch.setattr(sdir, "run_clipboard_command", lambda command, _text: (command[0] == "two", None, False))
    sdir.copy_to_clipboard("text")
    monkeypatch.setattr(sdir, "run_clipboard_command", lambda _command, _text: (True, "broken", False))
    with pytest.raises(sdir.ClipboardFailureError, match="broken"):
        sdir.copy_to_clipboard("text")
    monkeypatch.setattr(sdir, "run_clipboard_command", lambda _command, _text: (True, "display", True))
    with pytest.raises(sdir.ClipboardUnavailableError, match="desktop session"):
        sdir.copy_to_clipboard("text")
    monkeypatch.setattr(sdir, "run_clipboard_command", lambda _command, _text: (False, None, False))
    with pytest.raises(sdir.ClipboardUnavailableError, match="unavailable"):
        sdir.copy_to_clipboard("text")


def test_entrypoint_error_boundaries(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sdir, "resolve_runtime_config", lambda _argv: (_ for _ in ()).throw(OSError("disk")))
    assert sdir.run(["."]) == 1
    runtime_error = capsys.readouterr().err
    assert "operating system error" in runtime_error
    assert "Usage" not in runtime_error

    monkeypatch.setattr(sdir, "resolve_runtime_config", lambda _argv: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert sdir.run(["."]) == 130
    assert "interrupted" in capsys.readouterr().err


def test_tree_only_scan_does_not_read_file_contents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "large.bin").write_bytes(b"x" * 1024)
    config = runtime_config(tmp_path)
    config = sdir.RuntimeConfig(
        **{**config.__dict__, "scan_data": frozenset({"tree"})},
    )
    monkeypatch.setattr(os, "read", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("content read")))
    result = sdir.scan(config)
    assert result.root.total_files == 1
    assert result.root.children[0].lines is None


def test_newest_entry_includes_directory_mtime() -> None:
    directory = node("dir", rel_path="recent-dir", name="recent-dir")
    directory.mtime = 200.0
    child = node(rel_path="recent-dir/old.txt", name="old.txt")
    child.mtime = 100.0
    sdir.initialize_aggregate(child)
    directory.children = [child]
    sdir.initialize_aggregate(directory)
    assert directory.newest_entry == ("recent-dir", 200.0)


def test_file_content_race_retries_until_stable_within_scan_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "racing.txt"
    path.write_text("start\n", encoding="utf-8")
    initial_stat = path.stat()
    config = runtime_config(tmp_path)
    config = sdir.RuntimeConfig(**{**config.__dict__, "scan_data": frozenset({"tree", "lines"})})
    now = time.monotonic()
    state = sdir.ScanState(
        config=config,
        started_at=now,
        deadline=now + config.scan_timeout,
        physical_root=tmp_path,
    )
    calls = 0

    def racing_count(_descriptor: int, _state: sdir.ScanState) -> int:
        nonlocal calls
        calls += 1
        if calls <= 3:
            with path.open("ab", buffering=0) as handle:
                handle.write(b"changed\n")
                os.fsync(handle.fileno())
        return calls

    monkeypatch.setattr(sdir, "count_file_lines_fd", racing_count)
    result = sdir.create_leaf(path, "racing.txt", initial_stat, "file", state, True)
    assert result is not None
    assert calls == 4
    assert result.lines == 4


def test_line_counter_marks_blocking_read_that_crosses_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = scan_state(tmp_path, deadline_offset=0.001)

    def slow_eof(_fd: int, _size: int) -> bytes:
        time.sleep(0.01)
        return b""

    monkeypatch.setattr(os, "read", slow_eof)
    assert sdir.count_file_lines_fd(1, state) is None
    assert state.timed_out


def test_explicit_root_file_bypasses_descendant_filters(tmp_path: Path) -> None:
    root = tmp_path / "dist"
    root.write_bytes(b"")
    config = sdir.RuntimeConfig(
        root_path=root,
        config_paths=(),
        filter_mode="ignore",
        rules=sdir.FilterRules(names=("dist",), types=("file",)),
        ignore_hidden=True,
        ignore_empty=True,
        scan_styling="minimal",
        scan_emojis=False,
        scan_data=frozenset({"tree"}),
        scan_timeout=60.0,
        auto_copy=False,
    )
    result = sdir.scan(config)
    assert result.root.kind == "file"
    assert result.root.name == "dist"
    assert result.root.size == 0


def test_directory_iterator_crossing_deadline_marks_partial_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class SlowEmptyIterator:
        def __iter__(self) -> SlowEmptyIterator:
            return self

        def __next__(self) -> os.DirEntry[str]:
            time.sleep(0.02)
            raise StopIteration

        def close(self) -> None:
            return None

    original_directory_frame = sdir.directory_frame

    def slow_directory_frame(
        path: Path,
        rel_path: str,
        path_stat: os.stat_result,
        state: sdir.ScanState,
        is_root: bool,
        fd: int,
    ) -> sdir.DirectoryFrame:
        frame = original_directory_frame(path, rel_path, path_stat, state, is_root, fd)
        close = getattr(frame.iterator, "close", None)
        if close is not None:
            close()
        frame.iterator = SlowEmptyIterator()
        return frame

    monkeypatch.setattr(sdir, "directory_frame", slow_directory_frame)
    config = runtime_config(tmp_path)
    config = sdir.RuntimeConfig(**{**config.__dict__, "scan_timeout": 0.005})
    result = sdir.scan(config)
    assert result.timed_out
    assert result.root.incomplete
    assert result.elapsed_ms >= 5


def test_config_reads_complete_large_file_and_reject_symlinks(tmp_path: Path) -> None:
    large = tmp_path / "large.yaml"
    large.write_text(("# padding\n" * 150_000) + "names: [tail]\n", encoding="utf-8")
    assert sdir.load_yaml_payload(large)["names"] == ["tail"]

    real = tmp_path / "real.yaml"
    real.write_text("names: []\n", encoding="utf-8")
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(real)
    with pytest.raises(sdir.ConfigError, match="safely"):
        sdir.load_yaml_payload(alias)


def test_config_read_rejects_in_place_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "racing.yaml"
    size = sdir.TEXT_READ_CHUNK_SIZE * 2
    prefix = b"scan-timeout: 1\n"
    original = prefix + b"# old\n" * ((size - len(prefix)) // 6)
    original = original[:size] + b" " * max(0, size - len(original))
    replacement = bytearray(original)
    replacement[sdir.TEXT_READ_CHUNK_SIZE :] = b"#" * (len(replacement) - sdir.TEXT_READ_CHUNK_SIZE)
    source.write_bytes(original)
    original_read = os.read
    mutated = False

    def racing_read(descriptor: int, amount: int) -> bytes:
        nonlocal mutated
        chunk = original_read(descriptor, amount)
        if chunk and not mutated:
            mutated = True
            with source.open("r+b", buffering=0) as handle:
                handle.seek(sdir.TEXT_READ_CHUNK_SIZE)
                handle.write(replacement[sdir.TEXT_READ_CHUNK_SIZE :])
                os.fsync(handle.fileno())
        return chunk

    monkeypatch.setattr(sdir.os, "read", racing_read)
    with pytest.raises(sdir.ConfigError, match="changed while being read"):
        sdir.load_yaml_payload(source)
    assert mutated


def test_coverage_normalization_and_config_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = runtime_config(tmp_path)
    assert config.config_path is None
    layered = sdir.RuntimeConfig(**{**config.__dict__, "config_paths": (tmp_path / "a", tmp_path / "b")})
    assert layered.config_path == tmp_path / "b"

    original_expanduser = Path.expanduser

    def broken_expanduser(self: Path) -> Path:
        raise KeyError("missing home")

    monkeypatch.setattr(Path, "expanduser", broken_expanduser)
    with pytest.raises(sdir.ConfigError, match="unable to expand scan path"):
        sdir.expand_user_path("~missing/value", "scan path")
    monkeypatch.setattr(Path, "expanduser", original_expanduser)

    assert sdir.ascii_terminal_text("é � 你") == "e ? ?"
    assert sdir.normalize_selector_path("a/./b") == "a/b"
    assert sdir.normalize_selector_path("./") == "."
    with pytest.raises(sdir.ConfigError, match="must be relative"):
        sdir.normalize_selector_path("/absolute")
    with pytest.raises(sdir.ConfigError, match="cannot traverse"):
        sdir.normalize_selector_path("a/../b")
    assert sdir.normalize_bool(" off ", "flag") is False
    with pytest.raises(sdir.ConfigError, match="true or false"):
        sdir.normalize_bool(1, "flag")
    with pytest.raises(sdir.ConfigError, match="must be a number"):
        sdir.normalize_float([], "seconds")
    with pytest.raises(sdir.ConfigError, match="must be a number"):
        sdir.normalize_float("not-a-number", "seconds")
    with pytest.raises(sdir.ConfigError, match="greater than 0"):
        sdir.normalize_float(float("inf"), "seconds")
    with pytest.raises(sdir.ConfigError, match="must be one of"):
        sdir.normalize_choice(1, "choice", ("a", "b"))
    with pytest.raises(sdir.ConfigError, match="must be one of"):
        sdir.normalize_choice("c", "choice", ("a", "b"))
    with pytest.raises(sdir.ConfigError, match="must be a string"):
        sdir.normalize_scan_data([])
    with pytest.raises(sdir.ConfigError, match="unknown item"):
        sdir.normalize_scan_data("tree, nope")
    with pytest.raises(sdir.ConfigError, match="select at least one"):
        sdir.normalize_scan_data(" , ")


def test_coverage_yaml_parser_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "config.yaml"
    assert sdir._unescape_yaml_double_quoted(r"plain\n\x41\u0042\U00000043", source, 1) == "plain\nABC"
    for inner, pattern in (
        ('bad"quote', "unescaped double quote"),
        ("trailing\\", "trailing backslash"),
        (r"\xGG", "invalid"),
        (r"\U00110000", "invalid Unicode code point"),
        (r"\uD800", "invalid Unicode code point"),
        (r"\q", "unsupported escape sequence"),
    ):
        with pytest.raises(sdir.ConfigError, match=pattern):
            sdir._unescape_yaml_double_quoted(inner, source, 1)

    assert sdir._parse_single_quoted("a''b", source, 1) == "a'b"
    with pytest.raises(sdir.ConfigError, match="must be doubled"):
        sdir._parse_single_quoted("a'b", source, 1)

    assert sdir._parse_yaml_scalar("", source, 1) == ""
    assert sdir._parse_yaml_scalar("true", source, 1) is True
    assert sdir._parse_yaml_scalar("off", source, 1) is False
    assert sdir._parse_yaml_scalar("~", source, 1) is None
    assert sdir._parse_yaml_scalar("+7", source, 1) == 7
    assert sdir._parse_yaml_scalar("1e9999", source, 1) == "1e9999"
    large_integer = "9" * 5000
    assert sdir._parse_yaml_scalar(large_integer, source, 1) == 10**5000 - 1
    assert sdir._parse_yaml_scalar("-" + large_integer, source, 1) == -(10**5000 - 1)
    with monkeypatch.context() as patch:
        patch.setattr(
            sdir, "float", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("float")), raising=False
        )
        assert sdir._parse_yaml_scalar("1.5", source, 1) == "1.5"
    with pytest.raises(sdir.ConfigError, match="unterminated double-quoted"):
        sdir._parse_yaml_scalar('"unterminated', source, 1)
    with pytest.raises(sdir.ConfigError, match="unterminated single-quoted"):
        sdir._parse_yaml_scalar("'unterminated", source, 1)

    assert sdir._parse_yaml_inline_list("[]", source, 1) == []
    assert sdir._parse_yaml_inline_list(r"""["a\"b", 'c''d']""", source, 1) == ['a"b', "c'd"]
    for value, pattern in (
        ("not-a-list", "malformed inline list"),
        ("[[nested]]", "nested inline collections"),
        ("[a}]", "unexpected closing collection delimiter"),
        ("[a,,b]", "empty items"),
        ('["unterminated]', "unterminated quoted string"),
        ("[a,]", "cannot end with a comma"),
    ):
        with pytest.raises(sdir.ConfigError, match=pattern):
            sdir._parse_yaml_inline_list(value, source, 1)

    invalid_documents = (
        ("\tkey: value\n", "tabs are not allowed"),
        ("  key: value\n", "unexpected indentation"),
        ("---\n", "document markers"),
        ("%YAML 1.2\n", "directives"),
        ("missing-colon\n", "invalid mapping entry"),
        ("1bad: value\n", "invalid mapping key"),
        ("key: one\nkey: two\n", "duplicate YAML key"),
        ("key: {a: b}\n", "inline mappings"),
        ("key:\n - item\n", "at least two spaces"),
        ("key:\n  - one\n   - two\n", "inconsistent block-list indentation"),
        ("key:\n  child: value\n", "only block-list items"),
        ("key:\n  - # comment\n", "cannot contain empty items"),
    )
    for text, pattern in invalid_documents:
        with pytest.raises(sdir.ConfigError, match=pattern):
            sdir.parse_config_yaml(text, source)
    assert sdir.parse_config_yaml("key:\nnext: value\n", source) == {"key": None, "next": "value"}
    assert sdir.parse_config_yaml("key:\n\n  # comment\n  - one\nnext: two\n", source) == {
        "key": ["one"],
        "next": "two",
    }

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(sdir.ConfigError, match="not a regular file"):
        sdir.load_yaml_payload(directory)
    missing = tmp_path / "missing.yaml"
    with pytest.raises(sdir.ConfigError, match="unable to read config file safely"):
        sdir.load_yaml_payload(missing)
    invalid_utf8 = tmp_path / "invalid.yaml"
    invalid_utf8.write_bytes(b"key: \xff\n")
    with pytest.raises(sdir.ConfigError, match="not valid UTF-8"):
        sdir.load_yaml_payload(invalid_utf8)


def test_coverage_user_config_resolution_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SDIR_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("SDIR_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    monkeypatch.setenv("SDIR_STATE_DIR", "relative/state")
    with pytest.raises(sdir.ConfigError, match="SDIR_STATE_DIR must be absolute"):
        sdir.user_state_dir()
    monkeypatch.setenv("SDIR_STATE_DIR", str(tmp_path / "state"))
    assert sdir.user_state_dir() == tmp_path / "state"
    monkeypatch.delenv("SDIR_STATE_DIR")

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert sdir.user_state_dir() == tmp_path / "xdg-state" / sdir.CONFIG_DIRECTORY_NAME
    monkeypatch.setenv("XDG_STATE_HOME", "relative")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert sdir.user_state_dir() == tmp_path / ".local" / "state" / sdir.CONFIG_DIRECTORY_NAME
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no home"))))
    assert sdir.user_state_dir() is None

    monkeypatch.setattr(sdir, "default_config_marker_path", lambda: None)
    assert sdir.persisted_default_config_path() is None
    with pytest.raises(sdir.ConfigError, match="no state directory"):
        sdir.set_default_config_path("config.yaml")

    marker = tmp_path / "marker"
    monkeypatch.setattr(sdir, "default_config_marker_path", lambda: marker)
    assert sdir.persisted_default_config_path() is None
    marker.write_text("\n")
    assert sdir.persisted_default_config_path() is None
    marker.write_text("relative/config.yaml\n")
    assert sdir.persisted_default_config_path() is None
    marker.write_text(f"{tmp_path / 'persisted.yaml'}\n")
    assert sdir.persisted_default_config_path() == tmp_path / "persisted.yaml"

    monkeypatch.setattr(sdir, "persisted_default_config_path", lambda: tmp_path / "persisted.yaml")
    assert sdir.user_config_path() == tmp_path / "persisted.yaml"
    monkeypatch.setattr(sdir, "persisted_default_config_path", lambda: None)
    monkeypatch.setenv("SDIR_CONFIG_DIR", "relative/config")
    with pytest.raises(sdir.ConfigError, match="SDIR_CONFIG_DIR must be absolute"):
        sdir.user_config_path()
    monkeypatch.setenv("SDIR_CONFIG_DIR", str(tmp_path / "config-dir"))
    assert sdir.user_config_path() == tmp_path / "config-dir" / sdir.CONFIG_FILE_NAME
    monkeypatch.delenv("SDIR_CONFIG_DIR")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    assert sdir.user_config_path() == tmp_path / "xdg-config" / sdir.CONFIG_DIRECTORY_NAME / sdir.CONFIG_FILE_NAME
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no home"))))
    assert sdir.user_config_path() is None


def test_coverage_config_layer_status_and_help_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdir, "user_config_path", lambda: None)
    assert isinstance(sdir.config_paths_with_project(None, None), tuple)

    monkeypatch.setattr(sdir, "find_config_paths", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(sdir, "terminal_columns", lambda: 8)
    monkeypatch.setattr(sdir.sys, "argv", [])
    status = sdir.render_status(color=False)
    status_lines = status.splitlines()
    config_index = status_lines.index("config:")
    assert "".join(status_lines[config_index:]) == "config: built-in defaults only"
    assert "product:" in status

    parser = sdir.SdirArgumentParser(prog="sdir", add_help=False)
    with pytest.raises(sdir.HelpRequested):
        parser.exit(0)
    with pytest.raises(sdir.ConfigError, match="argument parsing failed"):
        parser.exit(2)
    with pytest.raises(sdir.ConfigError, match="custom"):
        parser.exit(2, " custom \n")

    monkeypatch.setattr(sdir, "terminal_columns", lambda: 12)
    assert "\n" in sdir.help_usage_line(False)
    assert sdir.help_wrapped_lines("supercalifragilistic", False, indent=20)
    assert sdir.help_wrapped_lines("a b", False, indent=100)


def test_coverage_cli_and_filter_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sdir.selector_flag_for_shortcut("") == "--names"
    assert sdir.selector_flag_for_shortcut(".") == "--paths"
    assert sdir.canonicalize_cli_argv(["--unknown"]) == [".", "--unknown"]
    assert sdir.canonicalize_cli_argv(["--names=-draft"]) == [".", "--names=-draft"]

    with pytest.raises(sdir.ConfigError, match="does not exist"):
        sdir.resolve_runtime_config([str(tmp_path / "missing")])
    with pytest.raises(sdir.ConfigError, match="cannot be combined with --ignore-hidden"):
        sdir.resolve_runtime_config([str(tmp_path), "--full", "--ignore-hidden"])
    with pytest.raises(sdir.ConfigError, match="requires at least one filter selector"):
        sdir.resolve_runtime_config([str(tmp_path), "--only"])

    assert not sdir.is_hidden_rel_path(".")
    assert sdir.is_hidden_rel_path("a/.hidden/b")
    assert sdir.path_selector_matches(".", "anything")
    assert not sdir.path_selector_matches("", "anything")
    assert sdir.path_selector_matches("a", "a/b")

    hidden = node(rel_path=".hidden")
    config = sdir.RuntimeConfig(**{**runtime_config(tmp_path).__dict__, "ignore_hidden": True})
    assert not sdir.should_keep_node(hidden, config, False)

    ignored = node(rel_path="ignored", name="ignored")
    config = sdir.RuntimeConfig(
        **{
            **runtime_config(tmp_path).__dict__,
            "rules": sdir.FilterRules(names=("ignored",)),
        }
    )
    assert not sdir.should_keep_node(ignored, config, False)

    only_dir = node("dir", rel_path="dir", name="dir")
    only_dir.children.append(node(rel_path="dir/kept", name="kept"))
    config = sdir.RuntimeConfig(
        **{
            **runtime_config(tmp_path).__dict__,
            "filter_mode": "only",
            "rules": sdir.FilterRules(names=("no-match",)),
        }
    )
    assert sdir.should_keep_node(only_dir, config, False)
    assert not sdir.should_keep_node(node(rel_path="other", name="other"), config, False)

    empty_file = node()
    empty_file.size = 0
    empty_dir = node("dir", rel_path="empty", name="empty")
    config = sdir.RuntimeConfig(**{**runtime_config(tmp_path).__dict__, "ignore_empty": True})
    assert not sdir.should_keep_node(empty_file, config, False)
    assert not sdir.should_keep_node(empty_dir, config, False)
    empty_dir.incomplete = True
    assert sdir.should_keep_node(empty_dir, config, False)

    state = scan_state(tmp_path)
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"one\ntwo")
        os.close(write_fd)
        write_fd = -1
        assert sdir.count_file_lines_fd(read_fd, state) == 2
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        os.close(read_fd)

    hidden_config = sdir.RuntimeConfig(**{**runtime_config(tmp_path).__dict__, "ignore_hidden": True})
    assert not sdir.prefilter_entry(".x", ".x", "file", hidden_config)
    ignore_config = sdir.RuntimeConfig(
        **{
            **runtime_config(tmp_path).__dict__,
            "rules": sdir.FilterRules(types=("file",), extensions=(".txt",)),
        }
    )
    assert not sdir.prefilter_entry("a.txt", "a.txt", "file", ignore_config)
    assert not sdir.prefilter_entry("a", "a", "file", ignore_config)
    only_paths = sdir.RuntimeConfig(
        **{
            **runtime_config(tmp_path).__dict__,
            "filter_mode": "only",
            "rules": sdir.FilterRules(paths=("wanted/deep",)),
        }
    )
    assert not sdir.prefilter_entry("other", "other", "dir", only_paths)
    assert sdir.prefilter_before_stat("wanted", "wanted", only_paths)
    assert not sdir.prefilter_before_stat("other", "other", only_paths)

    aggregate = node("dir", rel_path="root", name="root")
    child = node(rel_path="root/file", name="file")
    child.lines = 1
    sdir.initialize_aggregate(child)
    aggregate.children = [child]
    aggregate.mtime = child.mtime + 1
    sdir.initialize_aggregate(aggregate)
    assert aggregate.newest_entry == ("root", aggregate.mtime)


def test_coverage_rendering_and_clipboard_platform_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "0")
    monkeypatch.setattr(os, "get_terminal_size", lambda _fd: os.terminal_size((91, 24)))
    assert sdir.terminal_columns() == 91

    def unavailable_terminal_size(_fd: int) -> os.terminal_size:
        raise OSError("not a terminal")

    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.setattr(os, "get_terminal_size", unavailable_terminal_size)
    assert sdir.terminal_columns() == sdir.TERMINAL_WIDTH_FALLBACK
    assert sdir.wrap_cells("界", 1) == ["界"]
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert sdir.terminal_color_enabled()
    assert sdir.styled_columns([], False) == ""
    assert sdir.render_padded_row("plain", (), None, 10, False) == "plain"

    aggregate = node("dir", rel_path="root", name="root")
    aggregate.total_files = 123456
    aggregate.total_dirs = 23456
    aggregate.total_links = 3456
    assert sdir.format_entries(aggregate) == "123456 files, 23455 dirs, 3456 links"
    aggregate.total_files = 0
    aggregate.total_dirs = 1
    aggregate.total_links = 0
    assert sdir.format_entries(aggregate) == "empty"

    file_root = node(rel_path="file.txt", name="file.txt")
    sdir.initialize_aggregate(file_root)
    result = sdir.ScanResult(file_root, 1, False, [], {}, [])
    config = runtime_config(tmp_path)
    assert sdir.render_flat_entries(file_root, result, config)

    deleted = sdir.DeletedGitEntry(".hidden/deleted.txt", "file", 10)
    hidden_config = sdir.RuntimeConfig(**{**config.__dict__, "ignore_hidden": True})
    assert not sdir.deleted_git_entry_visible(deleted, hidden_config)
    ignore_config = sdir.RuntimeConfig(
        **{
            **config.__dict__,
            "rules": sdir.FilterRules(paths=("deleted.txt",)),
        }
    )
    assert not sdir.deleted_git_entry_visible(sdir.DeletedGitEntry("deleted.txt", "file", 1), ignore_config)
    only_config = sdir.RuntimeConfig(
        **{
            **config.__dict__,
            "filter_mode": "only",
            "rules": sdir.FilterRules(names=("wanted.txt",)),
        }
    )
    assert not sdir.deleted_git_entry_visible(sdir.DeletedGitEntry("other.txt", "file", 1), only_config)

    assert sdir.summary_box_line("x", 20, "ascii").startswith("| x")
    assert sdir.summary_label_line("label", "value", 40, False, "ascii").startswith("| label")

    root = node("dir", rel_path=".", name="root")
    child = node(rel_path="a.txt", name="a.txt")
    child.lines = 1
    sdir.initialize_aggregate(child)
    root.children = [child]
    sdir.initialize_aggregate(root)
    warning_result = sdir.ScanResult(root, 5, True, [sdir.ScanWarning(".", "issue")], {}, [])
    monkeypatch.setattr(sdir, "terminal_columns", lambda: 80)
    framed = sdir.render_framed_summary(warning_result, "ascii", False)
    assert any("timeout" in line for line in framed)
    assert any("warnings" in line for line in framed)
    low = sdir.render_summary(warning_result, "low", False)
    assert any("timeout" in line for line in low)
    assert any("warnings" in line for line in low)

    monkeypatch.setattr(sdir.sys, "platform", "darwin")
    monkeypatch.setattr(sdir.os, "name", "posix")
    assert sdir.clipboard_backend_candidates() == [("pbcopy",)]
    monkeypatch.setattr(sdir.sys, "platform", "linux")
    monkeypatch.setattr(sdir.os, "name", "nt")
    assert sdir.clipboard_backend_candidates() == [("clip.exe",)]
    monkeypatch.setattr(sdir.os, "name", "posix")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    candidates = sdir.clipboard_backend_candidates()
    assert ("wl-copy",) in candidates and ("xclip", "-selection", "clipboard") in candidates


def test_coverage_run_and_main_error_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    outputs: list[tuple[object, str]] = []
    monkeypatch.setattr(sdir, "write_stream", lambda stream, text: outputs.append((stream, text)))
    for argv, text in (
        (["help", "x"], "help does not accept"),
        (["version", "x"], "version does not accept"),
        (["status", "x"], "status does not accept"),
    ):
        outputs.clear()
        assert sdir.run(argv) == 2
        assert any(text in value for _stream, value in outputs)
    outputs.clear()
    assert sdir.run(["--set-config"]) == 2
    assert any("exactly one" in value for _stream, value in outputs)

    outputs.clear()
    monkeypatch.setattr(sdir, "resolve_runtime_config", lambda _argv: (_ for _ in ()).throw(sdir.HelpRequested()))
    assert sdir.run([]) == 0
    assert outputs

    config = sdir.RuntimeConfig(**{**runtime_config(tmp_path).__dict__, "auto_copy": True})
    root = node(rel_path="root", name="root")
    sdir.initialize_aggregate(root)
    result = sdir.ScanResult(root, 1, False, [], {}, [])
    monkeypatch.setattr(sdir, "resolve_runtime_config", lambda _argv: config)
    monkeypatch.setattr(sdir, "scan", lambda _config: result)
    monkeypatch.setattr(sdir, "render", lambda _result, _config: "rendered\n")
    monkeypatch.setattr(
        sdir, "copy_to_clipboard", lambda _text: (_ for _ in ()).throw(sdir.ClipboardUnavailableError("none"))
    )
    outputs.clear()
    assert sdir.run([]) == 1
    assert any("auto-copy failed" in value for _stream, value in outputs)
    monkeypatch.setattr(
        sdir, "copy_to_clipboard", lambda _text: (_ for _ in ()).throw(sdir.ClipboardFailureError("bad"))
    )
    assert sdir.run([]) == 1

    monkeypatch.setattr(sdir, "scan", lambda _config: (_ for _ in ()).throw(sdir.SdirError("scan failed")))
    assert sdir.run([]) == 1
    monkeypatch.setattr(sdir, "resolve_runtime_config", lambda _argv: (_ for _ in ()).throw(OSError("os failed")))
    assert sdir.run([]) == 1
    monkeypatch.setattr(sdir, "resolve_runtime_config", lambda _argv: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert sdir.run([]) == 130


def test_coverage_broken_pipe_cleanup_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_write(_stream: object, _text: str) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(sdir, "write_stream", broken_write)
    monkeypatch.setattr(sdir.os, "open", lambda *_args, **_kwargs: 99)
    monkeypatch.setattr(sdir.os, "dup2", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sdir.os, "close", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sdir.sys.stdout, "fileno", lambda: 1)
    assert sdir.run(["version"]) == 0

    calls = 0
    messages: list[str] = []

    def broken_then_record(_stream: object, text: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise BrokenPipeError
        messages.append(text)

    monkeypatch.setattr(sdir, "write_stream", broken_then_record)
    monkeypatch.setattr(sdir.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup")))
    assert sdir.run(["version"]) == 0
    assert any("broken-pipe cleanup failed" in message for message in messages)


def test_coverage_module_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setattr(sys, "argv", ["sdir.py", "version"])
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path(Path(sdir.__file__), run_name="__main__")
    assert exc_info.value.code == 0


def test_coverage_create_leaf_simple_error_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    regular = tmp_path / "regular.txt"
    regular.write_text("one\ntwo\n")
    regular_stat = regular.lstat()

    state = scan_state(tmp_path)
    assert sdir.create_leaf(regular, "special", regular_stat, "special", state, False) is None
    assert any("special" in warning.message for warning in state.warnings)

    filtered = sdir.ScanState(
        config=sdir.RuntimeConfig(
            **{
                **runtime_config(tmp_path).__dict__,
                "rules": sdir.FilterRules(names=("regular.txt",)),
            }
        ),
        started_at=state.started_at,
        deadline=state.deadline,
        physical_root=tmp_path,
    )
    assert sdir.create_leaf(regular, "regular.txt", regular_stat, "file", filtered, False) is None

    link = tmp_path / "link"
    link.symlink_to(regular.name)
    link_stat = link.lstat()
    original_link_target = sdir.link_target
    monkeypatch.setattr(sdir, "link_target", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("readlink")))
    link_state = scan_state(tmp_path)
    linked = sdir.create_leaf(link, "link", link_stat, "link", link_state, False)
    assert linked is not None and linked.target is None
    assert any("unable to read link target" in warning.message for warning in link_state.warnings)
    monkeypatch.setattr(sdir, "link_target", original_link_target)

    original_stat = os.stat
    mismatch = SimpleNamespace(
        **{name: getattr(link_stat, name) for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime")}
    )
    mismatch.st_ino += 1
    monkeypatch.setattr(sdir.os, "stat", lambda *_args, **_kwargs: mismatch)
    mismatch_state = scan_state(tmp_path)
    assert sdir.create_leaf(link, "link", link_stat, "link", mismatch_state, False) is None
    assert any("changed" in warning.message for warning in mismatch_state.warnings)
    monkeypatch.setattr(sdir.os, "stat", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("stat")))
    stat_error_state = scan_state(tmp_path)
    assert sdir.create_leaf(link, "link", link_stat, "link", stat_error_state, False) is None
    assert any("verify link metadata" in warning.message for warning in stat_error_state.warnings)
    monkeypatch.setattr(sdir.os, "stat", original_stat)

    tree_config = sdir.RuntimeConfig(**{**runtime_config(tmp_path).__dict__, "scan_data": frozenset({"tree"})})
    tree_state = sdir.ScanState(tree_config, state.started_at, state.deadline, tmp_path)
    monkeypatch.setattr(sdir.os, "stat", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("verify")))
    assert sdir.create_leaf(regular, "regular.txt", regular_stat, "file", tree_state, False) is None
    monkeypatch.setattr(sdir.os, "stat", lambda *_args, **_kwargs: mismatch)
    assert sdir.create_leaf(regular, "regular.txt", regular_stat, "file", tree_state, False) is None

    type_changed = SimpleNamespace(
        st_dev=regular_stat.st_dev,
        st_ino=regular_stat.st_ino,
        st_mode=(tmp_path.stat().st_mode),
        st_size=0,
        st_mtime=regular_stat.st_mtime,
    )
    monkeypatch.setattr(sdir.os, "stat", lambda *_args, **_kwargs: type_changed)
    assert sdir.create_leaf(regular, "regular.txt", regular_stat, "file", tree_state, False) is None

    empty_stat = SimpleNamespace(
        st_dev=regular_stat.st_dev,
        st_ino=regular_stat.st_ino,
        st_mode=regular_stat.st_mode,
        st_size=0,
        st_mtime=regular_stat.st_mtime,
    )
    empty_config = sdir.RuntimeConfig(**{**tree_config.__dict__, "ignore_empty": True})
    empty_state = sdir.ScanState(empty_config, state.started_at, state.deadline, tmp_path)
    monkeypatch.setattr(sdir.os, "stat", lambda *_args, **_kwargs: empty_stat)
    assert sdir.create_leaf(regular, "regular.txt", regular_stat, "file", empty_state, False) is None
    monkeypatch.setattr(sdir.os, "stat", original_stat)
    leaf = sdir.create_leaf(regular, "regular.txt", regular_stat, "file", tree_state, False)
    assert leaf is not None and leaf.lines is None


def test_coverage_remaining_small_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sdir.os, "sep", "\\")
    monkeypatch.setattr(sdir.os, "altsep", ":")
    assert sdir.normalize_selector_path(r"a\b:c") == "a/b/c"

    with pytest.raises(sdir.ConfigError, match="tabs are not allowed"):
        sdir.parse_config_yaml("key:\n\t- item\n", tmp_path / "config.yaml")

    config_file = tmp_path / "read-error.yaml"
    config_file.write_text("key: value\n")
    original_fstat = sdir.os.fstat
    monkeypatch.setattr(sdir.os, "fstat", lambda _fd: (_ for _ in ()).throw(OSError("fstat failed")))
    with pytest.raises(sdir.ConfigError, match="unable to read config file safely"):
        sdir.load_yaml_payload(config_file)
    monkeypatch.setattr(sdir.os, "fstat", original_fstat)

    monkeypatch.setattr(sdir, "terminal_columns", lambda: 1)
    assert sdir.help_wrapped_lines("界", False) == ["界"]

    timed_state = scan_state(tmp_path, deadline_offset=-1.0)
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    try:
        assert sdir.count_file_lines_fd(read_fd, timed_state) is None
    finally:
        os.close(read_fd)

    root = node("dir", rel_path="root", name="root")
    root.mtime = 1.0
    child = node(rel_path="root/new", name="new")
    child.mtime = 2.0
    sdir.initialize_aggregate(child)
    root.children = [child]
    sdir.initialize_aggregate(root)
    assert root.newest_entry == child.newest_entry

    config = sdir.RuntimeConfig(
        **{
            **runtime_config(tmp_path).__dict__,
            "rules": sdir.FilterRules(names=("ignored",)),
        }
    )
    assert not sdir.deleted_git_entry_visible(sdir.DeletedGitEntry("ignored/deleted.txt", "file", 10), config)

    monkeypatch.setattr(sdir.sys, "platform", "linux")
    monkeypatch.setattr(sdir.os, "name", "posix")
    monkeypatch.delenv("WSL_INTEROP", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    assert sdir.clipboard_backend_candidates() == []


def test_coverage_set_config_run_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    resolved = tmp_path / "config.yaml"
    monkeypatch.setattr(sdir, "set_default_config_path", lambda _value: resolved)
    monkeypatch.setattr(sdir, "write_stream", lambda _stream, text: messages.append(text))
    assert sdir.run(["--set-config", "config.yaml"]) == 0
    assert str(resolved) in messages[0]


def test_coverage_create_leaf_content_error_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target.txt"
    target.write_text("one\ntwo\n")
    path_stat = target.lstat()

    timed = scan_state(tmp_path, deadline_offset=-1.0)
    assert sdir.create_leaf(target, "target.txt", path_stat, "file", timed, False) is None

    with monkeypatch.context() as patch:
        patch.setattr(
            sdir.os,
            "open",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError(13, "denied")),
        )
        state = scan_state(tmp_path)
        leaf = sdir.create_leaf(target, "target.txt", path_stat, "file", state, False)
        assert leaf is not None and leaf.incomplete and leaf.lines is None
        assert any("unable to read file content" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:
        checks = iter((False, True))
        state = scan_state(tmp_path)
        patch.setattr(state, "timeout_reached", lambda: next(checks))
        patch.setattr(sdir.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("gone")))
        assert sdir.create_leaf(target, "target.txt", path_stat, "file", state, False) is None
        assert any("unable to inspect file safely" in warning.message for warning in state.warnings)

    original_fstat = sdir.os.fstat
    with monkeypatch.context() as patch:
        checks = iter((False, True))
        state = scan_state(tmp_path)
        patch.setattr(state, "timeout_reached", lambda: next(checks))

        def mismatched_fstat(fd: int) -> object:
            observed = original_fstat(fd)
            return SimpleNamespace(
                st_dev=observed.st_dev,
                st_ino=observed.st_ino + 1,
                st_mode=observed.st_mode,
                st_size=observed.st_size,
                st_mtime=observed.st_mtime,
                st_mtime_ns=observed.st_mtime_ns,
                st_ctime_ns=observed.st_ctime_ns,
            )

        patch.setattr(sdir.os, "fstat", mismatched_fstat)
        assert sdir.create_leaf(target, "target.txt", path_stat, "file", state, False) is None
        assert any("replaced while the file was being scanned" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:
        checks = iter((False, True))
        state = scan_state(tmp_path)
        patch.setattr(state, "timeout_reached", lambda: next(checks))

        def directory_fstat(fd: int) -> object:
            observed = original_fstat(fd)
            return SimpleNamespace(
                st_dev=observed.st_dev,
                st_ino=observed.st_ino,
                st_mode=tmp_path.stat().st_mode,
                st_size=observed.st_size,
                st_mtime=observed.st_mtime,
                st_mtime_ns=observed.st_mtime_ns,
                st_ctime_ns=observed.st_ctime_ns,
            )

        patch.setattr(sdir.os, "fstat", directory_fstat)
        assert sdir.create_leaf(target, "target.txt", path_stat, "file", state, False) is None
        assert any("entry type changed" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:
        state = scan_state(tmp_path)
        patch.setattr(
            sdir,
            "count_file_lines_fd",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("read failed")),
        )
        leaf = sdir.create_leaf(target, "target.txt", path_stat, "file", state, False)
        assert leaf is not None and leaf.lines is None
        assert any("unable to count lines" in warning.message for warning in state.warnings)

    original_stat = sdir.os.stat
    with monkeypatch.context() as patch:
        checks = iter((False, True))
        state = scan_state(tmp_path)
        patch.setattr(state, "timeout_reached", lambda: next(checks))
        patch.setattr(sdir, "count_file_lines_fd", lambda *_args, **_kwargs: 2)
        fstat_calls = 0

        def post_read_fstat(fd: int) -> os.stat_result:
            nonlocal fstat_calls
            fstat_calls += 1
            if fstat_calls == 2:
                raise OSError("post-read")
            return original_fstat(fd)

        patch.setattr(sdir.os, "fstat", post_read_fstat)
        assert sdir.create_leaf(target, "target.txt", path_stat, "file", state, False) is None
        assert any("unable to verify file metadata after reading" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:
        checks = iter((False, True))
        state = scan_state(tmp_path)
        patch.setattr(state, "timeout_reached", lambda: next(checks))
        patch.setattr(sdir, "count_file_lines_fd", lambda *_args, **_kwargs: 2)

        def replaced_stat(*args: object, **kwargs: object) -> object:
            observed = original_stat(*args, **kwargs)
            return SimpleNamespace(
                st_dev=observed.st_dev,
                st_ino=observed.st_ino + 1,
                st_mode=observed.st_mode,
                st_size=observed.st_size,
                st_mtime=observed.st_mtime,
                st_mtime_ns=observed.st_mtime_ns,
                st_ctime_ns=observed.st_ctime_ns,
            )

        patch.setattr(sdir.os, "stat", replaced_stat)
        assert sdir.create_leaf(target, "target.txt", path_stat, "file", state, False) is None
        assert any("replaced while the file was being read" in warning.message for warning in state.warnings)


def test_coverage_directory_frame_and_scan_root_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root_stat = tmp_path.stat()
    state = scan_state(tmp_path)

    with monkeypatch.context() as patch:
        patch.setattr(
            sdir.os,
            "scandir",
            lambda _fd: (_ for _ in ()).throw(OSError(24, "too many files")),
        )
        frame = sdir.directory_frame(tmp_path, ".", root_stat, state, True, 10)
        assert frame.incomplete
        assert any("file descriptor limit" in warning.message for warning in state.warnings)
    state.warnings.clear()
    with monkeypatch.context() as patch:
        patch.setattr(sdir.os, "scandir", lambda _fd: (_ for _ in ()).throw(OSError(5, "io")))
        frame = sdir.directory_frame(tmp_path, ".", root_stat, state, True, 10)
        assert frame.incomplete
        assert any("unable to read directory" in warning.message for warning in state.warnings)

    config = sdir.RuntimeConfig(
        **{
            **runtime_config(tmp_path).__dict__,
            "filter_mode": "only",
            "rules": sdir.FilterRules(names=("wanted",)),
        }
    )
    assert sdir.incomplete_directory_node(tmp_path / "other", "other", root_stat, config, False) is None

    closed: list[int] = []
    bare_frame = sdir.DirectoryFrame(tmp_path, ".", root_stat, True, iter(()), 77)
    with monkeypatch.context() as patch:
        patch.setattr(sdir.os, "close", lambda fd: closed.append(fd))
        sdir.close_directory_frame(bare_frame)
    assert closed == [77]

    missing = tmp_path / "missing"
    missing_state = scan_state(tmp_path)
    assert sdir.scan_path(missing, ".", missing_state, True) is None
    assert any("unable to stat entry" in warning.message for warning in missing_state.warnings)

    broken = tmp_path / "broken"
    broken.symlink_to("missing-target")
    broken_state = scan_state(tmp_path)
    broken_node = sdir.scan_path(broken, ".", broken_state, True)
    assert broken_node is not None and broken_node.kind == "link"

    directory = tmp_path / "directory"
    directory.mkdir()
    original_open = sdir.os.open
    with monkeypatch.context() as patch:
        patch.setattr(sdir.os, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("open root")))
        open_state = scan_state(directory)
        result = sdir.scan_path(directory, ".", open_state, True)
        assert result is not None and result.incomplete
        assert any("unable to open directory" in warning.message for warning in open_state.warnings)

    original_fstat = sdir.os.fstat
    with monkeypatch.context() as patch:
        opened: list[int] = []

        def open_root(*args: object, **kwargs: object) -> int:
            fd = original_open(*args, **kwargs)
            opened.append(fd)
            return fd

        patch.setattr(sdir.os, "open", open_root)
        patch.setattr(sdir.os, "fstat", lambda _fd: (_ for _ in ()).throw(OSError("fstat root")))
        fstat_state = scan_state(directory)
        result = sdir.scan_path(directory, ".", fstat_state, True)
        assert result is not None and result.incomplete
        assert opened

    with monkeypatch.context() as patch:

        def mismatched_root_fstat(fd: int) -> object:
            observed = original_fstat(fd)
            return SimpleNamespace(
                st_dev=observed.st_dev,
                st_ino=observed.st_ino + 1,
                st_mode=observed.st_mode,
                st_size=observed.st_size,
                st_mtime=observed.st_mtime,
            )

        patch.setattr(sdir.os, "fstat", mismatched_root_fstat)
        changed_state = scan_state(directory)
        assert sdir.scan_path(directory, ".", changed_state, True) is None
        assert any("scan root changed" in warning.message for warning in changed_state.warnings)

    with monkeypatch.context() as patch:
        original_resolve = Path.resolve

        def broken_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            if self.name == "root-link":
                raise OSError("resolve")
            return original_resolve(self, *args, **kwargs)

        root_link = tmp_path / "root-link"
        root_link.symlink_to(directory, target_is_directory=True)
        patch.setattr(Path, "resolve", broken_resolve)
        resolve_state = scan_state(tmp_path)
        result = sdir.scan_path(root_link, ".", resolve_state, True)
        assert result is not None and result.incomplete


def test_coverage_scan_iterator_timeout_and_error_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "root"
    directory.mkdir()

    class ErrorIterator:
        def __iter__(self) -> ErrorIterator:
            return self

        def __next__(self) -> os.DirEntry[str]:
            raise OSError("iteration failed")

        def close(self) -> None:
            return None

    with monkeypatch.context() as patch:
        patch.setattr(sdir.os, "scandir", lambda _fd: ErrorIterator())
        state = scan_state(directory)
        result = sdir.scan_path(directory, ".", state, True)
        assert result is not None and result.incomplete
        assert any("unable to continue reading directory" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:
        patch.setattr(sdir.os, "scandir", lambda _fd: iter(()))
        checks = iter((False, True))
        state = scan_state(directory)
        patch.setattr(state, "timeout_reached", lambda: next(checks))
        result = sdir.scan_path(directory, ".", state, True)
        assert result is not None and result.incomplete

    child = directory / "child.txt"
    child.write_text("content")
    entries = list(os.scandir(directory))
    with monkeypatch.context() as patch:
        patch.setattr(sdir.os, "scandir", lambda _fd: iter(entries))
        checks = iter((False, True))
        state = scan_state(directory)
        patch.setattr(state, "timeout_reached", lambda: next(checks))
        result = sdir.scan_path(directory, ".", state, True)
        assert result is not None and result.incomplete and not result.children


def test_coverage_scan_child_error_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    child_dir = root / "child"
    child_dir.mkdir()
    child_identity = (child_dir.stat().st_dev, child_dir.stat().st_ino)
    original_stat = sdir.os.stat
    original_open = sdir.os.open
    original_fstat = sdir.os.fstat

    with monkeypatch.context() as patch:

        def child_stat_error(path: object, *args: object, **kwargs: object) -> object:
            if kwargs.get("dir_fd") is not None:
                raise OSError("child stat")
            return original_stat(path, *args, **kwargs)

        patch.setattr(sdir.os, "stat", child_stat_error)
        state = scan_state(root)
        result = sdir.scan_path(root, ".", state, True)
        assert result is not None and not result.children
        assert any("unable to stat entry" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:

        def child_open_error(path: object, flags: int, *args: object, **kwargs: object) -> int:
            if kwargs.get("dir_fd") is not None:
                raise OSError("child open")
            return original_open(path, flags, *args, **kwargs)

        patch.setattr(sdir.os, "open", child_open_error)
        state = scan_state(root)
        result = sdir.scan_path(root, ".", state, True)
        assert result is not None and result.children and result.children[0].incomplete
        assert any("unable to open directory safely" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:
        config = sdir.RuntimeConfig(
            **{
                **runtime_config(root).__dict__,
                "filter_mode": "only",
                "rules": sdir.FilterRules(names=("wanted",)),
            }
        )
        state = sdir.ScanState(config, time.monotonic(), time.monotonic() + 60, root)

        def child_open_error_filtered(path: object, flags: int, *args: object, **kwargs: object) -> int:
            if kwargs.get("dir_fd") is not None:
                raise OSError("child open")
            return original_open(path, flags, *args, **kwargs)

        patch.setattr(sdir.os, "open", child_open_error_filtered)
        result = sdir.scan_path(root, ".", state, True)
        assert result is not None and not result.children

    with monkeypatch.context() as patch:

        def mismatched_child_fstat(fd: int) -> object:
            observed = original_fstat(fd)
            if (observed.st_dev, observed.st_ino) == child_identity:
                return SimpleNamespace(
                    st_dev=observed.st_dev,
                    st_ino=observed.st_ino + 1,
                    st_mode=observed.st_mode,
                    st_size=observed.st_size,
                    st_mtime=observed.st_mtime,
                )
            return observed

        patch.setattr(sdir.os, "fstat", mismatched_child_fstat)
        state = scan_state(root)
        result = sdir.scan_path(root, ".", state, True)
        assert result is not None and not result.children
        assert any("directory changed while it was being opened" in warning.message for warning in state.warnings)

    with monkeypatch.context() as patch:

        def failing_child_fstat(fd: int) -> os.stat_result:
            observed = original_fstat(fd)
            if (observed.st_dev, observed.st_ino) == child_identity:
                raise OSError("child fstat")
            return observed

        patch.setattr(sdir.os, "fstat", failing_child_fstat)
        state = scan_state(root)
        result = sdir.scan_path(root, ".", state, True)
        assert result is not None and result.children and result.children[0].incomplete
        assert any("unable to open directory safely" in warning.message for warning in state.warnings)

    state = scan_state(root)
    state.visited_directories.add(child_identity)
    result = sdir.scan_path(root, ".", state, True)
    assert result is not None and result.children and result.children[0].incomplete
    assert any("already scanned" in warning.message for warning in state.warnings)

    fifo = root / "pipe"
    os.mkfifo(fifo)
    state = scan_state(root)
    result = sdir.scan_path(root, ".", state, True)
    assert result is not None
    assert any("unsupported special" in warning.message for warning in state.warnings)


def test_coverage_final_non_git_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sdir.parse_config_yaml("names:\n  - one\n", tmp_path / "config.yaml") == {"names": ["one"]}

    target = tmp_path / "target.txt"
    target.write_text("data", encoding="utf-8")
    root_link = tmp_path / "root-link"
    root_link.symlink_to(target)
    link_node = sdir.scan_path(root_link, ".", scan_state(tmp_path), True)
    assert link_node is not None and link_node.kind == "link"

    root = tmp_path / "root"
    root.mkdir()
    child = root / "child"
    child.mkdir()
    child_stat = child.stat()
    config = sdir.RuntimeConfig(
        **{
            **runtime_config(root).__dict__,
            "filter_mode": "only",
            "rules": sdir.FilterRules(names=("wanted",)),
        }
    )
    state = sdir.ScanState(config, time.monotonic(), time.monotonic() + 60, root)
    state.visited_directories.add((child_stat.st_dev, child_stat.st_ino))
    result = sdir.scan_path(root, ".", state, True)
    assert result is not None and not result.children

    fake_root = node("dir", rel_path=".", name="root")
    with monkeypatch.context() as patch:
        patch.setattr(sdir, "scan_path", lambda *_args, **_kwargs: None)
        with pytest.raises(sdir.SdirError, match="scan root was excluded"):
            sdir.scan(runtime_config(tmp_path))
    with monkeypatch.context() as patch:
        config_with_git = sdir.RuntimeConfig(
            **{**runtime_config(tmp_path).__dict__, "scan_data": frozenset({"tree", "git"})}
        )
        patch.setattr(sdir, "scan_path", lambda *_args, **_kwargs: fake_root)
        patch.setattr(sdir, "load_git_markers", lambda *_args: ({}, [], "git warning"))
        scan_result = sdir.scan(config_with_git)
        assert any(warning.message == "git warning" for warning in scan_result.warnings)

    verbose_files_absent = node("dir", rel_path=".", name="root")
    verbose_files_absent.total_files = 0
    verbose_files_absent.total_dirs = 23457
    verbose_files_absent.total_links = 3456
    assert sdir.format_entries(verbose_files_absent) == "23456 dirs, 3456 links"
    verbose_dirs_absent = node("dir", rel_path=".", name="root")
    verbose_dirs_absent.total_files = 123456
    verbose_dirs_absent.total_dirs = 1
    verbose_dirs_absent.total_links = 3456
    assert sdir.format_entries(verbose_dirs_absent) == "123456 files, 3456 links"

    ignored_config = sdir.RuntimeConfig(
        **{
            **runtime_config(tmp_path).__dict__,
            "filter_mode": "ignore",
            "rules": sdir.FilterRules(names=("other",)),
        }
    )
    assert sdir.deleted_git_entry_visible(sdir.DeletedGitEntry("kept.txt", "file", 1), ignored_config)

    warning_result = sdir.ScanResult(
        root=fake_root,
        elapsed_ms=1,
        timed_out=False,
        warnings=[sdir.ScanWarning(".", "warning")],
        git_markers={},
        deleted_git_entries=[],
    )
    assert any("warnings" in line for line in sdir.render_framed_summary(warning_result, "ascii"))


def test_coverage_git_helper_error_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = scan_state(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (None, "discovery error"))
        assert sdir.git_root_for(tmp_path, state) == (None, "discovery error")
    with monkeypatch.context() as patch:
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (completed(stdout=b"\n"), None))
        assert sdir.git_root_for(tmp_path, state) == (
            None,
            "Git repository discovery returned an empty working-tree path",
        )

    assert sdir.parse_porcelain_z(b"   plain\0") == {}

    with monkeypatch.context() as patch:
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (None, None))
        assert sdir.executable_git_config_warning(tmp_path, state) == "unable to verify Git configuration safety"
    with monkeypatch.context() as patch:
        patch.setattr(
            sdir,
            "run_git",
            lambda *_args, **_kwargs: (completed(stdout=b"local\0file:.git/config\0"), None),
        )
        assert "malformed output" in (sdir.executable_git_config_warning(tmp_path, state) or "")

    with monkeypatch.context() as patch:
        patch.setattr(
            sdir,
            "run_git",
            lambda *_args, **_kwargs: (completed(stdout=b"global\0file:.gitconfig\0core.fsmonitor"), None),
        )
        assert sdir.executable_git_config_warning(tmp_path, state) is None

    original_lstat = sdir.os.lstat
    with monkeypatch.context() as patch:

        def guarded_lstat(path: os.PathLike[str] | str, *args: object, **kwargs: object) -> os.stat_result:
            if Path(path).name == ".git":
                raise PermissionError("blocked")
            return original_lstat(path, *args, **kwargs)

        patch.setattr(sdir.os, "lstat", guarded_lstat)
        assert sdir.has_git_metadata(tmp_path)
    (tmp_path / ".git").mkdir()
    assert sdir.has_git_metadata(tmp_path)


def test_coverage_deleted_git_metadata_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = scan_state(tmp_path)

    with monkeypatch.context() as patch:
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (None, None))
        assert sdir.deleted_git_metadata(tmp_path, state, {"gone.txt"}) == (
            [],
            "unable to inspect deleted Git index entries",
        )

    with monkeypatch.context() as patch:
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (completed(2), None))
        entries, warning = sdir.deleted_git_metadata(tmp_path, state, {"gone.txt"})
        assert entries == [] and warning == "deleted Git index entries unavailable: git ls-files failed"

    with monkeypatch.context() as patch:
        responses = iter(((completed(), None), (None, None)))
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: next(responses))
        assert sdir.deleted_git_metadata(tmp_path, state, {"gone.txt"}) == (
            [],
            "unable to inspect deleted Git paths",
        )

    with monkeypatch.context() as patch:
        responses = iter(((completed(), None), (completed(2), None)))
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: next(responses))
        entries, warning = sdir.deleted_git_metadata(tmp_path, state, {"gone.txt"})
        assert entries == [] and warning == "deleted Git paths unavailable: git ls-tree failed"

    with monkeypatch.context() as patch:
        responses = iter(((completed(), None), (completed(sdir.GIT_FATAL_EXIT_CODE), None)))
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: next(responses))
        assert sdir.deleted_git_metadata(tmp_path, state, {"gone.txt"}) == ([], None)

    tree_record = b"100644 blob oid-tree\tfrom-head.txt\0"
    with monkeypatch.context() as patch:
        responses = iter(((completed(), None), (completed(stdout=tree_record), None), (None, None)))
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: next(responses))
        assert sdir.deleted_git_metadata(tmp_path, state, {"from-head.txt"}) == (
            [],
            "unable to inspect deleted Git object sizes",
        )

    index_record = b"100644 oid-index 0\tindexed.txt\0"
    with monkeypatch.context() as patch:
        responses = iter(((completed(stdout=index_record), None), (completed(2), None)))
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: next(responses))
        entries, warning = sdir.deleted_git_metadata(tmp_path, state, {"indexed.txt"})
        assert entries == [] and warning == "deleted Git object sizes unavailable: git cat-file failed"

    with monkeypatch.context() as patch:
        responses = iter(((completed(stdout=index_record), None), (completed(stdout=b"malformed\n"), None)))
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: next(responses))
        entries, warning = sdir.deleted_git_metadata(tmp_path, state, {"indexed.txt"})
        assert entries == [] and warning == "deleted Git object metadata was incomplete for indexed.txt"

    with monkeypatch.context() as patch:
        tree_records = b"100644 blob oid-present\tpresent.txt\x00100644 blob oid-unused\tunused.txt\x00"
        responses = iter(
            (
                (completed(), None),
                (completed(stdout=tree_records), None),
                (completed(stdout=b"oid-present 3\n"), None),
            )
        )
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: next(responses))
        entries, warning = sdir.deleted_git_metadata(tmp_path, state, {"present.txt", "absent.txt"})
        assert entries == [sdir.DeletedGitEntry("present.txt", "file", 3)] and warning is None


def test_coverage_load_git_markers_branches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = scan_state(tmp_path)

    broken_link = tmp_path / "broken-link"
    broken_link.symlink_to(tmp_path / "missing-target")
    original_resolve = Path.resolve
    with monkeypatch.context() as patch:

        def fail_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            if self == broken_link:
                raise OSError("resolve failed")
            return original_resolve(self, *args, **kwargs)

        patch.setattr(Path, "resolve", fail_resolve)
        patch.setattr(sdir, "git_root_for", lambda *_args: (None, "root error"))
        patch.setattr(sdir, "has_git_metadata", lambda *_args: True)
        assert sdir.load_git_markers(broken_link, state) == ({}, [], "root error")

    target_dir = tmp_path / "target-dir"
    target_dir.mkdir()
    directory_link = tmp_path / "directory-link"
    directory_link.symlink_to(target_dir, target_is_directory=True)
    with monkeypatch.context() as patch:
        patch.setattr(sdir, "git_root_for", lambda *_args: (tmp_path, None))
        patch.setattr(sdir, "executable_git_config_warning", lambda *_args: "unsafe config")
        assert sdir.load_git_markers(directory_link, state) == ({}, [], "unsafe config")

    target_file = tmp_path / "target-file"
    target_file.write_text("data", encoding="utf-8")
    file_link = tmp_path / "file-link"
    file_link.symlink_to(target_file)
    with monkeypatch.context() as patch:
        patch.setattr(sdir, "git_root_for", lambda *_args: (tmp_path, None))
        patch.setattr(sdir, "executable_git_config_warning", lambda *_args: None)
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (completed(stdout=b" M file-link\0"), None))
        assert sdir.load_git_markers(file_link, state) == ({".": "[M]"}, [], None)

    with monkeypatch.context() as patch:
        patch.setattr(sdir, "git_root_for", lambda *_args: (tmp_path, None))
        patch.setattr(sdir, "executable_git_config_warning", lambda *_args: None)
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (None, None))
        assert sdir.load_git_markers(tmp_path, state) == ({}, [], "git markers could not be rendered")

    with monkeypatch.context() as patch:
        patch.setattr(sdir, "git_root_for", lambda *_args: (tmp_path, None))
        patch.setattr(sdir, "executable_git_config_warning", lambda *_args: None)
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (completed(2), None))
        assert sdir.load_git_markers(tmp_path, state) == ({}, [], "git markers unavailable: git status failed")

    nested = tmp_path / "sub"
    nested.mkdir()
    status = b" M sub\0 M sub/inside.txt\0 M other.txt\0 D sub/deleted.txt\0 D other-deleted.txt\0"
    deleted = [
        sdir.DeletedGitEntry("sub/deleted.txt", "file", 2),
        sdir.DeletedGitEntry("other-deleted.txt", "file", 3),
    ]
    with monkeypatch.context() as patch:
        patch.setattr(sdir, "git_root_for", lambda *_args: (tmp_path, None))
        patch.setattr(sdir, "executable_git_config_warning", lambda *_args: None)
        patch.setattr(sdir, "run_git", lambda *_args, **_kwargs: (completed(stdout=status), None))
        patch.setattr(sdir, "deleted_git_metadata", lambda *_args: (deleted, "deleted warning"))
        markers, deleted_entries, warning = sdir.load_git_markers(nested, state)
        assert markers == {".": "[M]", "inside.txt": "[M]", "deleted.txt": "[D]"}
        assert deleted_entries == [sdir.DeletedGitEntry("deleted.txt", "file", 2)]
        assert warning == "deleted warning"
