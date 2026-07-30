from __future__ import annotations

import contextlib
import errno
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SDIR = ROOT / "sdir.py"

spec = importlib.util.spec_from_file_location("scan_dir_runtime", SDIR)
assert spec and spec.loader
sdir = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = sdir
spec.loader.exec_module(sdir)


def run_sdir(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    runtime_env = os.environ.copy()
    runtime_env.update({"NO_COLOR": "1", "COLUMNS": "100", "PYTHONDONTWRITEBYTECODE": "1"})
    if env:
        runtime_env.update(env)

    previous_cwd = Path.cwd()
    previous_env = os.environ.copy()
    stdout = io.StringIO()
    stderr = io.StringIO()
    try:
        os.environ.clear()
        os.environ.update(runtime_env)
        if cwd is not None:
            os.chdir(cwd)
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            returncode = sdir.run(args)
    finally:
        os.chdir(previous_cwd)
        os.environ.clear()
        os.environ.update(previous_env)
    return subprocess.CompletedProcess(
        args=[str(SDIR), *args],
        returncode=returncode,
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
    )


def run_sdir_subprocess(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None, timeout: float = 10
) -> subprocess.CompletedProcess[str]:
    runtime_env = os.environ.copy()
    runtime_env.update({"NO_COLOR": "1", "COLUMNS": "100", "PYTHONDONTWRITEBYTECODE": "1"})
    if env:
        runtime_env.update(env)
    return subprocess.run(
        [sys.executable, "-S", str(SDIR), *args],
        cwd=cwd,
        env=runtime_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "src" / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    (tmp_path / "tests" / "a.py").write_text("test\n", encoding="utf-8")
    (tmp_path / "note.txt").write_text("note", encoding="utf-8")
    (tmp_path / "empty.txt").write_bytes(b"")
    (tmp_path / "binary.bin").write_bytes(b"abc\0def")
    (tmp_path / ".hidden" / "secret.txt").write_text("secret\n", encoding="utf-8")
    os.symlink("src", tmp_path / "src-link")
    os.symlink("missing", tmp_path / "broken-link")
    return tmp_path


def test_no_site_runtime_smoke() -> None:
    result = run_sdir_subprocess("version")
    assert result.returncode == 0
    assert result.stdout.strip() == f"sdir {sdir.VERSION}"
    assert result.stderr == ""


def test_commands_and_invalid_attached_flags(tmp_path: Path) -> None:
    assert run_sdir("version").stdout.strip() == f"sdir {sdir.VERSION}"
    assert "Scan Dir" in run_sdir("help").stdout
    assert run_sdir("help", "extra").returncode == 2
    assert run_sdir("--help=garbage").returncode == 2
    assert run_sdir("--version=garbage").returncode == 2
    for name in ("help", "version", "status"):
        command_path = tmp_path / name
        command_path.mkdir()
        (command_path / "x.txt").write_text("x", encoding="utf-8")
        for arguments in ((f"./{name}",), ("--", name)):
            result = run_sdir(*arguments, cwd=tmp_path)
            assert result.returncode == 0, (arguments, result.stderr)
            assert "x.txt" in result.stdout


def test_filtering_flat_and_summary(tree: Path) -> None:
    only = run_sdir(str(tree), "--only", "-e", ".py", "--scan-styling", "minimal", "--scan-emojis", "false")
    assert only.returncode == 0
    tree_section = only.stdout.split("\nlargest ", 1)[0]
    assert "src/a.py" not in tree_section  # hierarchy uses basenames
    assert "a.py" in tree_section and "note.txt" not in tree_section

    flat = run_sdir(str(tree), "--scan-data", "lines,size,type", "--scan-styling", "minimal", "--scan-emojis", "false")
    assert flat.returncode == 0
    assert "src/a.py" in flat.stdout and "tests/a.py" in flat.stdout

    summary = run_sdir(str(tree), "--scan-data", "summary", "--scan-styling", "minimal", "--scan-emojis", "false")
    assert summary.returncode == 0 and "largest" in summary.stdout


def test_visibility_symlinks_binary_and_special(tree: Path) -> None:
    hidden = run_sdir(
        str(tree), "--ignore-hidden", "--ignore-empty", "--scan-styling", "minimal", "--scan-emojis", "false"
    )
    assert hidden.returncode == 0
    assert ".hidden" not in hidden.stdout and "empty.txt" not in hidden.stdout
    assert "broken-link" in hidden.stdout
    assert "src-link" in hidden.stdout
    assert "src-link/a.py" not in hidden.stdout
    assert "?L" in hidden.stdout

    fifo = tree / "pipe"
    os.mkfifo(fifo)
    special = run_sdir(str(tree), "--scan-styling", "minimal", "--scan-emojis", "false")
    assert special.returncode == 0
    assert "unsupported special filesystem entry skipped" in special.stdout


def test_sanitizes_hostile_names(tmp_path: Path) -> None:
    (tmp_path / "line\nbreak.txt").write_text("x", encoding="utf-8")
    (tmp_path / "bidi\u202etxt").write_text("x", encoding="utf-8")
    raw = os.fsencode(tmp_path) + b"/invalid-\xff"
    invalid_name_created = False
    try:
        fd = os.open(raw, os.O_WRONLY | os.O_CREAT, 0o600)
    except OSError as exc:
        if exc.errno != errno.EILSEQ:
            raise
    else:
        try:
            os.write(fd, b"x")
        finally:
            os.close(fd)
        invalid_name_created = True
    result = run_sdir(str(tmp_path), "--scan-styling", "minimal", "--scan-emojis", "false")
    assert result.returncode == 0
    assert "line\\u000Abreak.txt" in result.stdout
    assert "bidi<U+202E>txt" in result.stdout
    if invalid_name_created:
        assert "invalid-\\xFF" in result.stdout


def test_config_layers_and_validation(tmp_path: Path) -> None:
    (tmp_path / "keep.py").write_text("x", encoding="utf-8")
    (tmp_path / "drop.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sdir.yaml").write_text(
        "extensions: [.txt]\nscan-styling: minimal\nscan-emojis: false\n", encoding="utf-8"
    )
    result = run_sdir(str(tmp_path))
    assert result.returncode == 0 and "drop.txt" not in result.stdout and "keep.py" in result.stdout

    explicit = tmp_path / "explicit.yaml"
    explicit.write_text("extensions: [.py]\n", encoding="utf-8")
    result = run_sdir(str(tmp_path), "--config", str(explicit))
    assert result.returncode == 0 and "keep.py" not in result.stdout

    (tmp_path / ".sdir.yaml").write_text("names: []\n", encoding="utf-8")
    conflict = run_sdir(str(tmp_path))
    assert conflict.returncode == 2 and "multiple project configuration" in conflict.stderr

    invalid = run_sdir(str(tmp_path), "--scan-timeout", "nan")
    assert invalid.returncode == 2


def init_git(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)


def test_git_markers_deleted_and_ignored_directory(tmp_path: Path) -> None:
    init_git(tmp_path)
    (tmp_path / "tracked.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "deleted.txt").write_text("gone\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored-dir/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (tmp_path / "tracked.txt").write_text("new\n", encoding="utf-8")
    (tmp_path / "deleted.txt").unlink()
    (tmp_path / "ignored-dir").mkdir()
    (tmp_path / "ignored-dir" / "x.txt").write_text("ignored\n", encoding="utf-8")
    result = run_sdir(str(tmp_path), "--full", "--scan-styling", "minimal", "--scan-emojis", "false")
    assert result.returncode == 0
    assert "tracked.txt [M]" in result.stdout
    assert "deleted deleted.txt [D]" in result.stdout
    assert "ignored-dir/ [!]" in result.stdout


def test_deleted_paths_respect_ignored_ancestor_name(tmp_path: Path) -> None:
    init_git(tmp_path)
    (tmp_path / "vendor").mkdir()
    (tmp_path / "vendor" / "gone.txt").write_text("gone\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    (tmp_path / "vendor" / "gone.txt").unlink()
    result = run_sdir(str(tmp_path), "--ignore", "-n", "vendor", "--scan-styling", "minimal", "--scan-emojis", "false")
    assert result.returncode == 0
    assert "vendor/gone.txt" not in result.stdout


def test_rendered_lines_fit_narrow_terminal(tree: Path) -> None:
    result = run_sdir(str(tree), "--scan-styling", "minimal", "--scan-emojis", "false", env={"COLUMNS": "16"})
    assert result.returncode == 0
    too_wide = [(line, sdir.cell_width(line)) for line in result.stdout.splitlines() if sdir.cell_width(line) > 16]
    assert not too_wide


def test_help_status_and_version_fit_narrow_terminal() -> None:
    for command in ("help", "status", "version"):
        result = run_sdir(command, env={"COLUMNS": "16"})
        assert result.returncode == 0
        too_wide = [(line, sdir.cell_width(line)) for line in result.stdout.splitlines() if sdir.cell_width(line) > 16]
        assert not too_wide


def test_yaml_parser_documented_subset(tmp_path: Path) -> None:
    source = tmp_path / "config.yaml"
    payload = sdir.parse_config_yaml(
        "names: ['a#b', \"c\\n\"] # comment\nignore-hidden: yes\nscan-timeout: 1.5\n",
        source,
    )
    assert payload == {"names": ["a#b", "c\n"], "ignore-hidden": True, "scan-timeout": 1.5}
    with pytest.raises(sdir.ConfigError):
        sdir.parse_config_yaml("names: [a,]\n", source)
    with pytest.raises(sdir.ConfigError):
        sdir.parse_config_yaml("names: []\nnames: []\n", source)


def test_timeout_returns_a_usable_partial_result(tmp_path: Path) -> None:
    for index in range(100):
        (tmp_path / f"file-{index:03d}.txt").write_text("content\n", encoding="utf-8")
    result = run_sdir(
        str(tmp_path),
        "--scan-timeout",
        "0.000000001",
        "--scan-data",
        "summary",
        "--scan-styling",
        "minimal",
        "--scan-emojis",
        "false",
    )
    assert result.returncode == 0
    assert "timeout reached; partial result shown" in result.stdout
    assert "Traceback" not in result.stderr


def test_single_file_root_and_deep_directory_tree(tmp_path: Path) -> None:
    single = tmp_path / "single.txt"
    single.write_text("one\ntwo\n", encoding="utf-8")
    file_result = run_sdir(
        str(single),
        "--scan-data",
        "tree,lines,size,type",
        "--scan-styling",
        "minimal",
        "--scan-emojis",
        "false",
    )
    assert file_result.returncode == 0
    assert "single.txt" in file_result.stdout and "2L" in file_result.stdout

    current = tmp_path / "deep"
    current.mkdir()
    for _ in range(150):
        current = current / "d"
        current.mkdir()
    (current / "leaf.txt").write_text("leaf\n", encoding="utf-8")
    deep_result = run_sdir(
        str(tmp_path / "deep"),
        "--scan-data",
        "summary",
        "--scan-styling",
        "minimal",
        "--scan-emojis",
        "false",
    )
    assert deep_result.returncode == 0
    assert "1 file" in deep_result.stdout


def test_headless_auto_copy_is_nonfatal(tmp_path: Path) -> None:
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    env = os.environ.copy()
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "WSL_INTEROP"):
        env.pop(name, None)
    env.update({"NO_COLOR": "1", "PATH": str(empty_path), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run(
        [sys.executable, "-S", str(SDIR), str(tmp_path), "--scan-data", "tree", "--auto-copy", "true"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert "auto-copy skipped" in result.stderr
    assert "file.txt" in result.stdout


def test_closed_downstream_pipe_exits_cleanly(tmp_path: Path) -> None:
    for index in range(5000):
        (tmp_path / f"entry-{index:04d}.txt").touch()

    producer = subprocess.Popen(
        [
            sys.executable,
            "-S",
            str(SDIR),
            str(tmp_path),
            "--full",
            "--scan-data",
            "tree",
            "--scan-styling",
            "minimal",
            "--scan-emojis",
            "false",
        ],
        env={**os.environ, "NO_COLOR": "1", "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert producer.stdout is not None
    consumer = subprocess.Popen(
        ["head", "-n", "1"],
        stdin=producer.stdout,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    producer.stdout.close()
    consumer_stderr = consumer.communicate(timeout=10)[1]
    producer_stderr = producer.communicate(timeout=10)[1]
    assert consumer.returncode == 0, consumer_stderr.decode(errors="replace")
    assert producer.returncode == 0, producer_stderr.decode(errors="replace")


def test_narrow_terminal_preserves_requested_metadata(tree: Path) -> None:
    result = run_sdir(
        str(tree),
        "--scan-data",
        "tree,lines,size,type,modified",
        "--scan-styling",
        "minimal",
        "--scan-emojis",
        "false",
        env={"COLUMNS": "16"},
    )
    assert result.returncode == 0
    for label in ("entries:", "lines:", "size:", "modified:"):
        assert label in result.stdout
    assert all(sdir.cell_width(line) <= 16 for line in result.stdout.splitlines())


def test_narrow_terminal_preserves_git_markers(tmp_path: Path) -> None:
    init_git(tmp_path)
    tracked = tmp_path / "very-long-tracked-file-name.txt"
    tracked.write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)
    tracked.write_text("changed\n", encoding="utf-8")

    result = run_sdir(
        str(tmp_path),
        "--full",
        "--scan-data",
        "tree,git",
        "--scan-styling",
        "minimal",
        "--scan-emojis",
        "false",
        env={"COLUMNS": "16"},
    )
    assert result.returncode == 0
    assert "[M]" in result.stdout
    assert all(sdir.cell_width(line) <= 16 for line in result.stdout.splitlines())


@pytest.mark.parametrize(
    ("label", "arguments"),
    [
        ("default", ()),
        ("ignore", ("--ignore",)),
        ("only-shortcut", ("--only", ".txt")),
        ("full", ("--full",)),
        ("paths-short", ("-f", "src")),
        ("paths-long", ("--paths", "src")),
        ("types-short", ("-t", "file")),
        ("types-long", ("--types", "file")),
        ("extensions-short", ("-e", ".txt")),
        ("extensions-long", ("--extensions", ".txt")),
        ("names-short", ("-n", "file.txt")),
        ("names-long", ("--names", "file.txt")),
        ("ignore-hidden", ("--ignore-hidden",)),
        ("include-hidden", ("--include-hidden",)),
        ("ignore-empty", ("--ignore-empty",)),
        ("include-empty", ("--include-empty",)),
        ("styling-full", ("--scan-styling", "full")),
        ("styling-low", ("--scan-styling", "low")),
        ("styling-minimal", ("--scan-styling", "minimal")),
        ("emojis-true", ("--scan-emojis", "true")),
        ("emojis-false", ("--scan-emojis", "false")),
        ("data-tree", ("--scan-data", "tree")),
        ("data-lines", ("--scan-data", "lines")),
        ("data-size", ("--scan-data", "size")),
        ("data-modified", ("--scan-data", "modified")),
        ("data-type", ("--scan-data", "type")),
        ("data-git", ("--scan-data", "git")),
        ("data-summary", ("--scan-data", "summary")),
        ("timeout", ("--scan-timeout", "5")),
        ("auto-copy-false", ("--auto-copy", "false")),
        ("project-auto", ("--project-config", "auto")),
        ("project-ignore", ("--project-config", "ignore")),
        ("project-require", ("--project-config", "require")),
    ],
)
def test_every_scan_option_accepts_a_valid_interaction(tmp_path: Path, label: str, arguments: tuple[str, ...]) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "file.txt").write_text("content\n", encoding="utf-8")
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")
    (tmp_path / ".sdir.yaml").write_text("scan-styling: minimal\nscan-emojis: false\n", encoding="utf-8")
    result = run_sdir(str(tmp_path), *arguments, env={"SDIR_CONFIG_DIR": str(tmp_path / "user-config")})
    assert result.returncode == 0, f"{label}: {result.stderr}"
    assert result.stdout, label


@pytest.mark.parametrize(
    "arguments",
    [
        ("--scan-styling", "invalid"),
        ("--scan-emojis", "maybe"),
        ("--scan-data", "unknown"),
        ("--scan-timeout", "0"),
        ("--scan-timeout", "nan"),
        ("--auto-copy", "maybe"),
        ("--project-config", "invalid"),
        ("--ignore-hidden", "--include-hidden"),
        ("--ignore-empty", "--include-empty"),
        ("--ignore", "--only", ".txt"),
        ("--full", "--names", "file.txt"),
    ],
)
def test_every_bounded_choice_and_exclusive_group_rejects_invalid_interactions(
    tmp_path: Path, arguments: tuple[str, ...]
) -> None:
    (tmp_path / "file.txt").write_text("content\n", encoding="utf-8")
    result = run_sdir(str(tmp_path), *arguments, env={"SDIR_CONFIG_DIR": str(tmp_path / "user-config")})
    assert result.returncode == 2
    assert result.stderr.startswith("sdir:")


def test_every_command_entrypoint_and_help_alias() -> None:
    for arguments in (("help",), ("-h",), ("--help",), ("version",), ("--version",), ("status",)):
        result = run_sdir(*arguments)
        assert result.returncode == 0, (arguments, result.stderr)
        assert result.stdout


def test_project_config_can_be_ignored_and_cannot_enable_auto_copy(tmp_path: Path) -> None:
    (tmp_path / ".sdir.yaml").write_text(
        "names: [secret.txt]\nauto-copy: true\n",
        encoding="utf-8",
    )
    (tmp_path / "secret.txt").write_text("secret\n", encoding="utf-8")
    (tmp_path / "public.txt").write_text("public\n", encoding="utf-8")

    automatic = run_sdir(
        str(tmp_path),
        "--scan-data",
        "tree",
        "--scan-styling",
        "minimal",
        "--scan-emojis",
        "false",
    )
    assert automatic.returncode == 0
    assert "secret.txt" not in automatic.stdout
    assert "auto-copy" not in automatic.stderr

    ignored = run_sdir(
        str(tmp_path),
        "--project-config",
        "ignore",
        "--scan-data",
        "tree",
        "--scan-styling",
        "minimal",
        "--scan-emojis",
        "false",
    )
    assert ignored.returncode == 0
    assert "secret.txt" in ignored.stdout

    missing = tmp_path / "without-config"
    missing.mkdir()
    required = run_sdir(str(missing), "--project-config", "require")
    assert required.returncode == 2
    assert "project configuration is required" in required.stderr
