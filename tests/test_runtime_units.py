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
    assert sdir.format_entries(dir_node) == "26f 14d 3l"


def test_terminal_width_and_styling_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "1")
    assert sdir.terminal_columns() == sdir.TERMINAL_WIDTH_MINIMUM
    monkeypatch.setenv("COLUMNS", "999999")
    assert sdir.terminal_columns() == sdir.TERMINAL_WIDTH_MAXIMUM
    monkeypatch.setenv("COLUMNS", "invalid")
    monkeypatch.setattr(os, "get_terminal_size", lambda _fd: os.terminal_size((77, 24)))
    assert sdir.terminal_columns() == 77

    assert sdir.char_cell_width("\u0301") == 0
    assert sdir.char_cell_width("界") == 2
    assert sdir.cell_width("a界") == 3
    assert sdir.truncate_cells("abcdef", 4) == "abc…"
    assert sdir.truncate_cells("abcdef", 1) == "…"
    assert sdir.pad_cells("a", 3) == "a  "
    assert sdir.wrap_cells("alpha beta", 6) == ["alpha", "beta"]
    assert sdir.wrap_cells("x", 0) == [""]
    styled = sdir.style("x", sdir.ANSI_BOLD, enabled=True)
    assert sdir.strip_ansi(styled) == "x"
    assert sdir.style_git_marker("[M]", False) == "[M]"
    assert sdir.row_style_for_node(node("dir"), True) == (sdir.ANSI_CYAN, sdir.ANSI_BOLD)
    assert sdir.row_style_for_node(node("link"), True) == (sdir.ANSI_MAGENTA, sdir.ANSI_BOLD)
    assert sdir.row_style_for_node(node(), True) == ()


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
    monkeypatch.setattr(
        sdir.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("tool", 1)),
    )
    found, message, environment = sdir.run_clipboard_command(("tool",), "text")
    assert found and environment and "timed out" in str(message)
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


def test_help_explains_scan_data_layout() -> None:
    help_text = " ".join(sdir.render_help(color=False).split())
    assert "Omitting tree uses flat paths; summary adds the summary block." in help_text


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


def test_config_reads_are_bounded_and_reject_symlinks(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"#" * (sdir.CONFIG_MAX_BYTES + 1))
    with pytest.raises(sdir.ConfigError, match="exceeds"):
        sdir.load_yaml_payload(oversized)

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
