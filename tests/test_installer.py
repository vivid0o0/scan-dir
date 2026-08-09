from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
RUNTIME = ROOT / "sdir.py"
VERSION = re.search(r'^VERSION = "([^"]+)"$', RUNTIME.read_text(), re.MULTILINE).group(1)


def paths(base: Path) -> dict[str, Path]:
    result = {
        "home": base / "home",
        "tmp": base / "tmp",
        "app": base / "app",
        "bin": base / "bin",
        "state": base / "state",
        "config": base / "config",
    }
    result["home"].mkdir()
    result["tmp"].mkdir()
    return result


def install_args(
    p: dict[str, Path],
    *,
    config: Path | None = None,
    state: Path | None = None,
) -> list[str]:
    return [
        "--source",
        str(RUNTIME),
        "--app-dir",
        str(p["app"]),
        "--bin-dir",
        str(p["bin"]),
        "--state-dir",
        str(state or p["state"]),
        "--config-dir",
        str(config or p["config"]),
        "--tmp-dir",
        str(p["tmp"]),
        "--no-path-repair",
        "--no-active-bridge",
        "--color",
        "never",
        "--logo",
        "text",
        "--quiet",
    ]


def run_installer(
    p: dict[str, Path],
    arguments: list[str],
    *,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(p["home"]),
            "PATH": os.environ["PATH"],
            "PYTHON": sys.executable,
            "XDG_CONFIG_HOME": str(p["home"] / ".config"),
            "XDG_DATA_HOME": str(p["home"] / ".local" / "share"),
            "XDG_STATE_HOME": str(p["home"] / ".local" / "state"),
        }
    )
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        ["bash", str(INSTALLER), *arguments],
        cwd=ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def run_install(
    p: dict[str, Path],
    *extra: str,
    config: Path | None = None,
    state: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_installer(p, [*install_args(p, config=config, state=state), *extra])


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def snapshot_tree(path: Path) -> dict[str, tuple[str, int]]:
    return {item.name: (digest(item), stat.S_IMODE(item.stat().st_mode)) for item in path.iterdir() if item.is_file()}


def assert_installed(p: dict[str, Path], config: Path | None = None) -> None:
    expected_config = config or p["config"]
    primary = p["bin"] / "sdir"
    alias = p["bin"] / "scan-dir"
    assert (
        subprocess.run([primary, "version"], text=True, capture_output=True, check=True).stdout.strip()
        == f"sdir {VERSION}"
    )
    assert (
        subprocess.run([alias, "version"], text=True, capture_output=True, check=True).stdout.strip()
        == f"sdir {VERSION}"
    )
    assignment = next(line for line in primary.read_text().splitlines() if line.startswith("export SDIR_CONFIG_DIR="))
    resolved_config = subprocess.run(
        ["bash", "-c", f'{assignment}; printf "%s" "$SDIR_CONFIG_DIR"'],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    assert resolved_config == str(expected_config)
    assert f"config_dir={expected_config}" in (p["app"] / "install-manifest.env").read_text()
    assert stat.S_IMODE(primary.stat().st_mode) == 0o755
    assert stat.S_IMODE((p["app"] / "install-manifest.env").stat().st_mode) == 0o600


def test_dry_run_has_no_managed_side_effects(tmp_path: Path) -> None:
    p = paths(tmp_path)
    result = run_install(p, "--dry-run")
    assert result.returncode == 0, result.stderr
    for name in ("app", "bin", "state", "config"):
        assert not p[name].exists()
    assert list(p["tmp"].iterdir()) == []


def test_install_creates_repairs_and_preserves_default_user_config(tmp_path: Path) -> None:
    p = paths(tmp_path)
    first = run_install(p)
    assert first.returncode == 0, first.stderr
    user_config = p["config"] / "config.yaml"
    assert user_config.read_bytes() == (ROOT / "config.yaml").read_bytes()
    assert stat.S_IMODE(user_config.stat().st_mode) == 0o600

    status = subprocess.run([p["bin"] / "sdir", "status"], text=True, capture_output=True, check=True)
    # Status rendering is lossless but may wrap a long path to the terminal
    # width. Rejoin continuation whitespace before checking the path value.
    compact_status = re.sub(r"\n +", "", status.stdout)
    assert str(user_config) in compact_status

    custom = b"scan-styling: minimal\n"
    user_config.write_bytes(custom)
    preserved = run_install(p)
    assert preserved.returncode == 0, preserved.stderr
    assert user_config.read_bytes() == custom

    user_config.unlink()
    repaired = run_install(p)
    assert repaired.returncode == 0, repaired.stderr
    assert user_config.read_bytes() == (ROOT / "config.yaml").read_bytes()
    assert stat.S_IMODE(user_config.stat().st_mode) == 0o600


def test_darwin_default_user_config_matches_readme_path(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-paths.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    command = """
source "$1"
HOME=$2
PLATFORM=darwin
APP_DIR_OVERRIDE=$3
BIN_DIR_OVERRIDE=$4
STATE_DIR_OVERRIDE=$5
CONFIG_DIR_OVERRIDE=
TMP_ROOT_OVERRIDE=$6
SDIR_APP_DIR=
SDIR_BIN_DIR=
SDIR_STATE_DIR=
SDIR_CONFIG_DIR=
SDIR_TMP_ROOT=
setup_paths
printf 'config=%s\nprevious_config=%s\nlegacy_config=%s\napp=%s\nstate=%s\n' "$CONFIG_DIR" "$PREVIOUS_CONFIG_DIR" "$LEGACY_CONFIG_DIR" "$APP_DIR" "$STATE_DIR"
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            str(p["home"]),
            str(p["app"]),
            str(p["bin"]),
            str(p["state"]),
            str(p["tmp"]),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert values["config"] == str(p["home"] / ".config" / "scan-dir")
    assert values["previous_config"] == str(p["home"] / "Library" / "Application Support" / "scan-dir" / "config")
    assert values["legacy_config"] == str(
        p["home"] / "Library" / "Application Support" / "project-summarizer" / "config"
    )
    assert values["app"] == str(p["app"])
    assert values["state"] == str(p["state"])


def test_previous_darwin_scan_dir_config_migrates_without_overwrite(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-migration.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    config_dir = p["home"] / ".config" / "scan-dir"
    previous_dir = p["home"] / "Library" / "Application Support" / "scan-dir" / "config"
    previous_dir.mkdir(parents=True)
    previous_dir.chmod(0o700)
    previous = previous_dir / "config.yaml"
    previous.write_text("scan-styling: minimal\n", encoding="utf-8")

    command = """
source "$1"
HOME=$2
CONFIG_DIR=$3
PREVIOUS_CONFIG_DIR=$4
PYTHON_BIN=$5
DRY_RUN=0
migrate_previous_user_config
finalize
"""
    first = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            str(p["home"]),
            str(config_dir),
            str(previous_dir),
            sys.executable,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    migrated = config_dir / "config.yaml"
    assert migrated.read_text(encoding="utf-8") == "scan-styling: minimal\n"
    assert stat.S_IMODE(migrated.stat().st_mode) == 0o600
    assert not previous.exists()

    previous_dir.mkdir(parents=True, exist_ok=True)
    previous_dir.chmod(0o700)
    previous.write_text("scan-styling: low\n", encoding="utf-8")
    migrated.write_text("scan-styling: full\n", encoding="utf-8")
    migrated.chmod(0o600)

    conflict = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            str(p["home"]),
            str(config_dir),
            str(previous_dir),
            sys.executable,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert conflict.returncode == 0, conflict.stderr
    assert migrated.read_text(encoding="utf-8") == "scan-styling: full\n"
    assert previous.read_text(encoding="utf-8") == "scan-styling: low\n"
    assert "differs from the existing Scan Dir configuration and was preserved" in conflict.stderr


def test_previous_darwin_config_migration_rolls_back_and_preserves_concurrent_change(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-migration-rollback.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    config_dir = p["home"] / ".config" / "scan-dir"
    previous_dir = p["home"] / "Library" / "Application Support" / "scan-dir" / "config"
    previous_dir.mkdir(parents=True)
    previous_dir.chmod(0o700)
    previous = previous_dir / "config.yaml"
    previous.write_text("scan-styling: minimal\n", encoding="utf-8")
    migrated = config_dir / "config.yaml"

    common = ["bash", str(testable_installer), str(config_dir), str(previous_dir), sys.executable]
    rollback_command = """
source "$1"
CONFIG_DIR=$2
PREVIOUS_CONFIG_DIR=$3
PYTHON_BIN=$4
DRY_RUN=0
migrate_previous_user_config
false
"""
    rolled_back = subprocess.run(
        ["bash", "-c", rollback_command, *common],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert rolled_back.returncode == 1, rolled_back.stderr
    assert previous.read_text(encoding="utf-8") == "scan-styling: minimal\n"
    assert not migrated.exists()

    concurrent_command = """
source "$1"
CONFIG_DIR=$2
PREVIOUS_CONFIG_DIR=$3
PYTHON_BIN=$4
DRY_RUN=0
migrate_previous_user_config
printf 'scan-styling: full\\n' > "$PREVIOUS_CONFIG_DIR/config.yaml"
finalize
"""
    concurrent = subprocess.run(
        ["bash", "-c", concurrent_command, *common],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert concurrent.returncode == 0, concurrent.stderr
    assert previous.read_text(encoding="utf-8") == "scan-styling: full\n"
    assert migrated.read_text(encoding="utf-8") == "scan-styling: minimal\n"
    assert "changed after migration and was preserved" in concurrent.stderr


def test_legacy_config_migration_rolls_back_without_losing_user_data(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-legacy-config-rollback.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    config_dir = p["home"] / ".config" / "scan-dir"
    legacy_dir = p["home"] / ".config" / "project-summarizer"
    legacy_dir.mkdir(parents=True)
    legacy_dir.chmod(0o700)
    legacy = legacy_dir / "config.yaml"
    legacy.write_text("scan-styling: low\n", encoding="utf-8")
    migrated = config_dir / "config.yaml"
    command = """
source "$1"
CONFIG_DIR=$2
LEGACY_CONFIG_DIR=$3
PYTHON_BIN=$4
DRY_RUN=0
migrate_legacy_user_config
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), str(config_dir), str(legacy_dir), sys.executable],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert legacy.read_text(encoding="utf-8") == "scan-styling: low\n"
    assert not migrated.exists()


def test_previous_darwin_migration_preserves_concurrent_destination_on_rollback(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-previous-destination-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    config_dir = p["home"] / ".config" / "scan-dir"
    previous_dir = p["home"] / "Library" / "Application Support" / "scan-dir" / "config"
    previous_dir.mkdir(parents=True)
    previous_dir.chmod(0o700)
    previous = previous_dir / "config.yaml"
    previous.write_text("scan-styling: minimal\n", encoding="utf-8")
    migrated = config_dir / "config.yaml"
    command = """
source "$1"
CONFIG_DIR=$2
PREVIOUS_CONFIG_DIR=$3
PYTHON_BIN=$4
DRY_RUN=0
migrate_previous_user_config
printf 'concurrent previous destination bytes\\n' > "$CONFIG_DIR/config.yaml"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), str(config_dir), str(previous_dir), sys.executable],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert migrated.read_text(encoding="utf-8") == "concurrent previous destination bytes\n"
    assert previous.read_text(encoding="utf-8") == "scan-styling: minimal\n"
    assert "created rollback target changed after installer write and was preserved" in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_legacy_migration_preserves_concurrent_destination_on_rollback(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-legacy-destination-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    config_dir = p["home"] / ".config" / "scan-dir"
    legacy_dir = p["home"] / ".config" / "project-summarizer"
    legacy_dir.mkdir(parents=True)
    legacy_dir.chmod(0o700)
    legacy = legacy_dir / "config.yaml"
    legacy.write_text("scan-styling: low\n", encoding="utf-8")
    migrated = config_dir / "config.yaml"
    command = """
source "$1"
CONFIG_DIR=$2
LEGACY_CONFIG_DIR=$3
PYTHON_BIN=$4
DRY_RUN=0
migrate_legacy_user_config
printf 'concurrent legacy destination bytes\\n' > "$CONFIG_DIR/config.yaml"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), str(config_dir), str(legacy_dir), sys.executable],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert migrated.read_text(encoding="utf-8") == "concurrent legacy destination bytes\n"
    assert legacy.read_text(encoding="utf-8") == "scan-styling: low\n"
    assert "created rollback target changed after installer write and was preserved" in result.stderr


def test_default_config_preserves_concurrent_destination_on_rollback(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-default-config-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    config_dir = p["home"] / ".config" / "scan-dir"
    target = config_dir / "config.yaml"
    command = """
source "$1"
HOME=$2
APP_DIR=$3
CONFIG_DIR=$4
PREVIOUS_CONFIG_DIR=$5
LEGACY_CONFIG_DIR=$6
PYTHON_BIN=$7
DRY_RUN=0
ensure_default_user_config
printf 'concurrent default destination bytes\\n' > "$CONFIG_DIR/config.yaml"
false
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            str(p["home"]),
            str(ROOT),
            str(config_dir),
            str(tmp_path / "absent-previous"),
            str(tmp_path / "absent-legacy"),
            sys.executable,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.read_text(encoding="utf-8") == "concurrent default destination bytes\n"
    assert "created rollback target changed after installer write and was preserved" in result.stderr


def test_profile_rollback_preserves_concurrent_destination_and_original_backup(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-profile-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    profile = p["home"] / ".bashrc"
    profile.write_text("export ORIGINAL_PROFILE=1\n", encoding="utf-8")
    p["bin"].mkdir()
    p["bin"].chmod(0o755)
    command = """
source "$1"
HOME=$2
BIN_DIR=$3
PYTHON_BIN=$4
DRY_RUN=0
edit_profile "$HOME/.bashrc"
printf 'concurrent profile bytes\\n' > "$HOME/.bashrc"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), str(p["home"]), str(p["bin"]), sys.executable],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert profile.read_text(encoding="utf-8") == "concurrent profile bytes\n"
    backups = list(p["home"].glob("..bashrc.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "export ORIGINAL_PROFILE=1\n"
    assert "rollback target changed after installer write" in result.stderr
    assert str(profile) in result.stderr
    assert str(backups[0]) in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_created_config_rollback_preserves_replacement_symlink_without_following(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-default-symlink-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    config_dir = p["home"] / ".config" / "scan-dir"
    target = config_dir / "config.yaml"
    external = tmp_path / "external-user-data"
    external.write_text("external concurrent bytes\n", encoding="utf-8")
    command = """
source "$1"
HOME=$2
APP_DIR=$3
CONFIG_DIR=$4
PREVIOUS_CONFIG_DIR=$5
LEGACY_CONFIG_DIR=$6
PYTHON_BIN=$7
EXTERNAL=$8
DRY_RUN=0
ensure_default_user_config
rm "$CONFIG_DIR/config.yaml"
ln -s "$EXTERNAL" "$CONFIG_DIR/config.yaml"
false
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            str(p["home"]),
            str(ROOT),
            str(config_dir),
            str(tmp_path / "absent-previous"),
            str(tmp_path / "absent-legacy"),
            sys.executable,
            str(external),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.is_symlink()
    assert os.readlink(target) == str(external)
    assert external.read_text(encoding="utf-8") == "external concurrent bytes\n"
    assert "created rollback target changed after installer write and was preserved" in result.stderr


def test_backed_up_directory_rollback_preserves_concurrent_destination_and_original_backup(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-directory-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-app"
    target.mkdir()
    (target / "original.txt").write_text("original app bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
PRIVATE="$TARGET.private"
mkdir "$PRIVATE"
printf 'installer replacement bytes\n' > "$PRIVATE/current.txt"
backup_target "$TARGET"
publish_backed_target "$PRIVATE" "$TARGET" 'managed app'
printf 'concurrent app bytes\n' > "$TARGET/current.txt"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert (target / "current.txt").read_text(encoding="utf-8") == "concurrent app bytes\n"
    backups = list(tmp_path.glob(".managed-app.rollback.*/original/original.txt"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original app bytes\n"
    assert "preserved current target" in result.stderr
    assert str(backups[0].parents[0]) in result.stderr


def test_backed_up_file_rollback_preserves_concurrent_write_after_preflight_fingerprint(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-rollback-preflight-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-file"
    target.write_text("original bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
PRIVATE="$TARGET.private"
printf 'installer replacement bytes\n' > "$PRIVATE"
backup_target "$TARGET"
publish_backed_target "$PRIVATE" "$TARGET" 'managed test target'
eval "$(declare -f path_fingerprint | sed '1s/path_fingerprint/original_path_fingerprint/')"
RACE_DONE=0
path_fingerprint() {
  original_path_fingerprint "$1" || return 1
  if [[ "$1" == "$TARGET" && "$RACE_DONE" == 0 ]]; then
    RACE_DONE=1
    printf 'concurrent third-party bytes\n' > "$TARGET"
  fi
  return 0
}
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.read_text(encoding="utf-8") == "concurrent third-party bytes\n"
    backups = list(tmp_path.glob(".managed-file.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original bytes\n"
    assert "rollback target changed during quarantine" in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_backed_up_file_rollback_preserves_current_destination_when_backup_content_drifts(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-backup-content-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-file"
    target.write_text("original bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
PRIVATE="$TARGET.private"
printf 'installer replacement bytes\n' > "$PRIVATE"
backup_target "$TARGET"
publish_backed_target "$PRIVATE" "$TARGET" 'managed test target'
printf 'drifted rollback backup bytes\n' > "${BACKUP_FILES[0]}"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.read_text(encoding="utf-8") == "installer replacement bytes\n"
    backups = list(tmp_path.glob(".managed-file.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "drifted rollback backup bytes\n"
    assert "rollback backup changed after creation" in result.stderr
    assert str(target) in result.stderr
    assert str(backups[0]) in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_backed_up_file_rollback_preserves_replacement_backup_symlink_without_following(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-backup-symlink-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-file"
    target.write_text("original bytes\n", encoding="utf-8")
    external = tmp_path / "external-backup-target"
    external.write_text("external bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
EXTERNAL=$4
PRIVATE="$TARGET.private"
printf 'installer replacement bytes\n' > "$PRIVATE"
backup_target "$TARGET"
publish_backed_target "$PRIVATE" "$TARGET" 'managed test target'
BACKUP=${BACKUP_FILES[0]}
rm "$BACKUP"
ln -s "$EXTERNAL" "$BACKUP"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target), str(external)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.read_text(encoding="utf-8") == "installer replacement bytes\n"
    backups = list(tmp_path.glob(".managed-file.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].is_symlink()
    assert os.readlink(backups[0]) == str(external)
    assert external.read_text(encoding="utf-8") == "external bytes\n"
    assert "rollback backup changed after creation" in result.stderr
    assert str(target) in result.stderr
    assert str(backups[0]) in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_backup_target_detects_change_after_preflight_without_losing_current_bytes(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-backup-fence-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-file"
    target.write_text("original bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
eval "$(declare -f path_fingerprint | sed '1s/path_fingerprint/original_path_fingerprint/')"
RACE_DONE=0
path_fingerprint() {
  original_path_fingerprint "$1" || return 1
  if [[ "$1" == "$TARGET" && "$RACE_DONE" == 0 ]]; then
    RACE_DONE=1
    printf 'concurrent bytes after preflight\n' > "$TARGET"
  fi
  return 0
}
backup_target "$TARGET"
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert target.read_text(encoding="utf-8") == "concurrent bytes after preflight\n"
    assert not list(tmp_path.glob(".managed-file.rollback.*"))
    assert "rollback target changed while backup was being fenced" in result.stderr


def test_profile_publication_never_clobbers_concurrent_recreation(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-profile-publication-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    home = tmp_path / "home"
    home.mkdir()
    profile = home / ".bashrc"
    profile.write_text("original profile\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
HOME=$3
PROFILE=$4
BIN_DIR=$5
eval "$(declare -f atomic_move_noreplace | sed '1s/atomic_move_noreplace/original_atomic_move_noreplace/')"
atomic_move_noreplace() {
  if [[ "$2" == "$PROFILE" && "$1" == *'.bashrc.tmp.'* ]]; then
    printf 'concurrent profile\n' > "$PROFILE"
  fi
  original_atomic_move_noreplace "$@"
}
edit_profile "$PROFILE"
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            sys.executable,
            str(home),
            str(profile),
            str(tmp_path / "bin"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert profile.read_text(encoding="utf-8") == "concurrent profile\n"
    backups = list(home.glob("..bashrc.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original profile\n"
    assert "shell profile destination changed during publication" in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_migrated_config_retirement_preserves_write_after_duplicate_check(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-retirement-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    old = tmp_path / "legacy" / "config.yaml"
    new = tmp_path / "current" / "config.yaml"
    old.parent.mkdir()
    new.parent.mkdir()
    old.write_text("same: value\n", encoding="utf-8")
    new.write_text("same: value\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
OLD=$3
NEW=$4
queue_migrated_user_config_retirement 'legacy user configuration' "$OLD" "$NEW"
eval "$(declare -f atomic_move_noreplace | sed '1s/atomic_move_noreplace/original_atomic_move_noreplace/')"
RACE_DONE=0
atomic_move_noreplace() {
  if [[ "$1" == "$OLD" && "$RACE_DONE" == 0 ]]; then
    RACE_DONE=1
    printf 'concurrent user edit\n' > "$OLD"
  fi
  original_atomic_move_noreplace "$@"
}
retire_migrated_user_configs
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(old), str(new)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert old.read_text(encoding="utf-8") == "concurrent user edit\n"
    assert new.read_text(encoding="utf-8") == "same: value\n"
    assert "changed during retirement and was restored without overwriting it" in result.stderr


def test_cleanup_preserves_replacement_at_old_temp_name(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-cleanup-name-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "temporary"
    target.write_text("installer temp\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
append_cleanup "$TARGET" file
rm -f "$TARGET"
printf 'concurrent replacement\n' > "$TARGET"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert target.read_text(encoding="utf-8") == "concurrent replacement\n"
    assert "cleanup path changed after creation and was preserved" in result.stderr


def test_finalize_preserves_rollback_backup_that_drifted_after_commit(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-finalize-backup-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-file"
    target.write_text("original bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
PRIVATE="$TARGET.private"
printf 'installed bytes\n' > "$PRIVATE"
backup_target "$TARGET"
publish_backed_target "$PRIVATE" "$TARGET" 'managed test target'
printf 'concurrent backup edit\n' > "${BACKUP_FILES[0]}"
finalize
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "installed bytes\n"
    backups = list(tmp_path.glob(".managed-file.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "concurrent backup edit\n"
    assert "rollback backup changed during successful cleanup and was restored" in result.stderr


def test_stale_lock_change_after_preflight_is_restored_not_taken_over(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-stale-lock-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    lock_dir = tmp_path / ".scan-dir.install.lock"
    lock_dir.mkdir()
    (lock_dir / "pid").write_text("999999999\n", encoding="utf-8")
    (lock_dir / "token").write_text("stale\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
LOCK_DIR=$3
eval "$(declare -f path_fingerprint | sed '1s/path_fingerprint/original_path_fingerprint/')"
RACE_DONE=0
path_fingerprint() {
  original_path_fingerprint "$1" || return 1
  if [[ "$1" == "$LOCK_DIR" && "$RACE_DONE" == 0 ]]; then
    RACE_DONE=1
    printf 'concurrent lock owner\n' > "$LOCK_DIR/token"
  fi
  return 0
}
acquire_single_lock "$LOCK_DIR"
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(lock_dir)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert (lock_dir / "token").read_text(encoding="utf-8") == "concurrent lock owner\n"
    assert "install lock changed during stale-lock quarantine and was restored" in result.stderr
    assert not list(tmp_path.glob(".install.lock.stale.*"))


def test_lock_release_preserves_replacement_lock_directory(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-lock-release-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    lock_dir = tmp_path / ".scan-dir.install.lock"
    command = r"""
source "$1"
PYTHON_BIN=$2
LOCK_DIR=$3
acquire_single_lock "$LOCK_DIR"
rm -rf "$LOCK_DIR"
mkdir "$LOCK_DIR"
printf 'foreign-pid\n' > "$LOCK_DIR/pid"
printf 'foreign-token\n' > "$LOCK_DIR/token"
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(lock_dir)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert (lock_dir / "token").read_text(encoding="utf-8") == "foreign-token\n"
    assert "install lock changed before release and was preserved" in result.stderr


def test_backed_publication_does_not_adopt_mutation_during_post_publish_window(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-publish-window-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-file"
    target.write_text("original bytes\n", encoding="utf-8")
    private = tmp_path / "private-replacement"
    private.write_text("installer bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
PRIVATE=$4
backup_target "$TARGET"
eval "$(declare -f atomic_move_noreplace | sed '1s/atomic_move_noreplace/original_atomic_move_noreplace/')"
atomic_move_noreplace() {
  local rc=0
  original_atomic_move_noreplace "$@" || rc=$?
  if (( rc == 0 )) && [[ "$1" == "$PRIVATE" && "$2" == "$TARGET" ]]; then
    printf 'concurrent bytes during publish\n' > "$TARGET"
  fi
  return "$rc"
}
publish_backed_target "$PRIVATE" "$TARGET" 'managed test target'
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target), str(private)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.read_text(encoding="utf-8") == "concurrent bytes during publish\n"
    backups = list(tmp_path.glob(".managed-file.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original bytes\n"
    assert "managed test target changed during publication and was preserved" in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_backed_publication_keeps_private_expected_state_after_postcheck(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-publish-postcheck-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "managed-file"
    target.write_text("original bytes\n", encoding="utf-8")
    private = tmp_path / "private-replacement"
    private.write_text("installer bytes\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
PRIVATE=$4
backup_target "$TARGET"
eval "$(declare -f path_fingerprint | sed '1s/path_fingerprint/original_path_fingerprint/')"
ARMED=0
path_fingerprint() {
  original_path_fingerprint "$1" || return 1
  if [[ "$1" == "$TARGET" && "$ARMED" == 1 ]]; then
    ARMED=2
    printf 'concurrent bytes after postcheck\n' > "$TARGET"
  fi
  return 0
}
ARMED=1
publish_backed_target "$PRIVATE" "$TARGET" 'managed test target'
false
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target), str(private)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.read_text(encoding="utf-8") == "concurrent bytes after postcheck\n"
    backups = list(tmp_path.glob(".managed-file.rollback.*/original"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "original bytes\n"
    assert "rollback target changed after installer write" in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_created_directory_publication_preserves_concurrent_mutation(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-created-dir-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "created-dir"
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
eval "$(declare -f atomic_move_noreplace | sed '1s/atomic_move_noreplace/original_atomic_move_noreplace/')"
atomic_move_noreplace() {
  local rc=0
  original_atomic_move_noreplace "$@" || rc=$?
  if (( rc == 0 )) && [[ "$2" == "$TARGET" && "$1" == *'.created-dir.create.'* ]]; then
    printf 'concurrent child\n' > "$TARGET/concurrent"
  fi
  return "$rc"
}
ensure_dir "$TARGET" 700
"""
    result = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 70, result.stderr
    assert target.is_dir()
    assert (target / "concurrent").read_text(encoding="utf-8") == "concurrent child\n"
    assert "created directory is not empty after rollback and was preserved" in result.stderr
    assert "rollback was incomplete" in result.stderr


def test_legacy_cleanup_restores_command_replaced_after_ownership_validation(tmp_path: Path) -> None:
    p = paths(tmp_path)
    legacy = create_legacy_install(p)
    testable_installer = tmp_path / "install-legacy-ownership-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    primary = p["bin"] / "prs"
    command = r"""
source "$1"
PYTHON_BIN=$2
CONFIG_DIR=$3
PREVIOUS_CONFIG_DIR=$4
LEGACY_CONFIG_DIR=$5
LEGACY_APP_DIR=$6
LEGACY_STATE_DIR=$7
LEGACY_BIN_DIR=$8
APP_DIR=$9
STATE_DIR=${10}
PRIMARY=${11}
DRY_RUN=0
eval "$(declare -f path_fingerprint | sed '1s/path_fingerprint/original_path_fingerprint/')"
PRIMARY_CALLS=0
path_fingerprint() {
  original_path_fingerprint "$1" || return 1
  if [[ "$1" == "$PRIMARY" ]]; then
    PRIMARY_CALLS=$((PRIMARY_CALLS + 1))
    if (( PRIMARY_CALLS == 2 )); then
      printf '#!/bin/sh\necho concurrent foreign command\n' > "$PRIMARY"
      chmod 755 "$PRIMARY"
    fi
  fi
  return 0
}
cleanup_legacy_install
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            sys.executable,
            str(p["config"]),
            str(tmp_path / "absent-previous-config"),
            str(legacy["config"]),
            str(legacy["app"]),
            str(legacy["state"]),
            str(p["bin"]),
            str(p["app"]),
            str(p["state"]),
            str(primary),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert primary.read_text(encoding="utf-8") == "#!/bin/sh\necho concurrent foreign command\n"
    assert legacy["app"].is_dir()
    assert legacy["state"].is_dir()
    assert "legacy primary command changed after ownership validation and was preserved" in result.stderr


def test_migration_snapshot_refuses_symlink_swap_before_copy(tmp_path: Path) -> None:
    p = paths(tmp_path)
    testable_installer = tmp_path / "install-migration-symlink-race.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    previous_dir = p["home"] / "previous-config"
    previous_dir.mkdir(parents=True)
    previous_dir.chmod(0o700)
    previous = previous_dir / "config.yaml"
    previous.write_text("scan-styling: minimal\n", encoding="utf-8")
    secret = tmp_path / "secret"
    secret.write_text("must not migrate\n", encoding="utf-8")
    target_dir = p["home"] / ".config" / "scan-dir"
    command = r"""
source "$1"
PYTHON_BIN=$2
CONFIG_DIR=$3
PREVIOUS_CONFIG_DIR=$4
SECRET=$5
DRY_RUN=0
eval "$(declare -f snapshot_owned_user_file | sed '1s/snapshot_owned_user_file/original_snapshot_owned_user_file/')"
snapshot_owned_user_file() {
  rm -f "$1"
  ln -s "$SECRET" "$1"
  original_snapshot_owned_user_file "$@"
}
migrate_previous_user_config
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            sys.executable,
            str(target_dir),
            str(previous_dir),
            str(secret),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert previous.is_symlink()
    assert previous.resolve() == secret
    assert not (target_dir / "config.yaml").exists()
    assert "changed while it was being copied and was preserved" in result.stderr


def test_install_is_idempotent_and_reconfigures(tmp_path: Path) -> None:
    p = paths(tmp_path)
    first = run_install(p)
    assert first.returncode == 0, first.stderr
    assert_installed(p)
    app_inode = p["app"].stat().st_ino
    primary_digest = digest(p["bin"] / "sdir")

    second = run_install(p)
    assert second.returncode == 0, second.stderr
    assert p["app"].stat().st_ino == app_inode
    assert digest(p["bin"] / "sdir") == primary_digest

    alternate_config = tmp_path / "alternate-config"
    changed = run_install(p, config=alternate_config)
    assert changed.returncode == 0, changed.stderr
    assert_installed(p, alternate_config)
    assert p["app"].stat().st_ino != app_inode

    alternate_state = tmp_path / "alternate-state"
    state_changed = run_install(p, config=alternate_config, state=alternate_state)
    assert state_changed.returncode == 0, state_changed.stderr
    assert f"state_dir={alternate_state}" in (p["app"] / "install-manifest.env").read_text()


def test_integrity_repairs_content_modes_and_layout(tmp_path: Path) -> None:
    p = paths(tmp_path)
    assert run_install(p).returncode == 0
    runtime = p["app"] / "sdir.py"
    primary = p["bin"] / "sdir"
    runtime.write_text("corrupted\n")
    primary.chmod(0o777)
    (p["app"] / "unexpected").write_text("foreign\n")

    repaired = run_install(p)
    assert repaired.returncode == 0, repaired.stderr
    assert runtime.read_bytes() == RUNTIME.read_bytes()
    assert stat.S_IMODE(primary.stat().st_mode) == 0o755
    assert not (p["app"] / "unexpected").exists()
    assert_installed(p)


def test_foreign_command_requires_force(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["bin"].mkdir()
    p["bin"].chmod(0o755)
    foreign = p["bin"] / "sdir"
    foreign.write_text("#!/bin/sh\necho foreign\n")
    foreign.chmod(0o755)

    refused = run_install(p)
    assert refused.returncode != 0
    assert "refusing to replace foreign command" in refused.stderr
    assert foreign.read_text().endswith("echo foreign\n")
    assert not p["app"].exists()

    forced = run_install(p, "--force")
    assert forced.returncode == 0, forced.stderr
    assert_installed(p)


def test_bad_checksum_fails_before_commit(tmp_path: Path) -> None:
    p = paths(tmp_path)
    failed = run_install(p, "--sha256", "0" * 64)
    assert failed.returncode != 0
    assert "runtime checksum mismatch" in failed.stderr
    assert not p["app"].exists()
    assert not p["bin"].exists()


def test_post_commit_failure_rolls_back(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-post-commit-failure.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    app = tmp_path / "app"
    app.mkdir()
    (app / "old").write_text("old app\n", encoding="utf-8")
    wrapper = tmp_path / "sdir"
    wrapper.write_text("old wrapper\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
APP=$3
WRAPPER=$4
APP_PRIVATE="$APP.private"
mkdir "$APP_PRIVATE"
printf 'replacement app\n' > "$APP_PRIVATE/new"
backup_target "$APP"
publish_backed_target "$APP_PRIVATE" "$APP" 'application runtime'
backup_target "$WRAPPER"
make_sibling_temp "$WRAPPER"
TEMP=$TEMP_PATH
printf 'replacement wrapper\n' > "$TEMP"
eval "$(declare -f atomic_move_noreplace | sed '1s/atomic_move_noreplace/original_atomic_move_noreplace/')"
atomic_move_noreplace() {
  if [[ "$1" == "$TEMP" && "$2" == "$WRAPPER" ]]; then
    ATOMIC_MOVE_STATUS='injected publication failure'
    return 13
  fi
  original_atomic_move_noreplace "$@"
}
atomic_move_noreplace "$TEMP" "$WRAPPER" || fail "injected post-commit publication failure"
"""
    failed = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(app), str(wrapper)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed.returncode == 1, failed.stderr
    assert (app / "old").read_text(encoding="utf-8") == "old app\n"
    assert not (app / "new").exists()
    assert wrapper.read_text(encoding="utf-8") == "old wrapper\n"
    assert not list(tmp_path.glob(".app.rollback.*"))
    assert not list(tmp_path.glob(".sdir.rollback.*"))


def test_failed_rollback_preserves_previous_installation_backup(tmp_path: Path) -> None:
    testable_installer = tmp_path / "install-rollback-move-failure.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    target = tmp_path / "app"
    target.mkdir()
    marker = target / "previous-release-marker"
    marker.write_text("preserve me\n", encoding="utf-8")
    command = r"""
source "$1"
PYTHON_BIN=$2
TARGET=$3
PRIVATE="$TARGET.private"
mkdir "$PRIVATE"
printf 'replacement\n' > "$PRIVATE/current"
backup_target "$TARGET"
publish_backed_target "$PRIVATE" "$TARGET" 'application runtime'
eval "$(declare -f atomic_move_noreplace | sed '1s/atomic_move_noreplace/original_atomic_move_noreplace/')"
atomic_move_noreplace() {
  if [[ "$1" == */.app.rollback.*/original && "$2" == "$TARGET" ]]; then
    ATOMIC_MOVE_STATUS='injected rollback move failure'
    return 13
  fi
  original_atomic_move_noreplace "$@"
}
false
"""
    failed = subprocess.run(
        ["bash", "-c", command, "bash", str(testable_installer), sys.executable, str(target)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert failed.returncode == 70, failed.stderr
    preserved = list(tmp_path.glob(".app.rollback.*/original/previous-release-marker"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "preserve me\n"
    assert str(preserved[0].parent) in failed.stderr
    assert "injected rollback move failure" in failed.stderr
    assert "rollback was incomplete" in failed.stderr


def test_secures_owned_world_writable_managed_directory(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["bin"].mkdir(mode=0o777)
    p["bin"].chmod(0o777)
    result = run_install(p)
    assert result.returncode == 0, result.stderr
    assert stat.S_IMODE(p["bin"].stat().st_mode) == 0o755
    assert_installed(p)


def test_rejects_overlapping_managed_paths(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["config"] = p["app"] / "config"
    result = run_install(p)
    assert result.returncode != 0
    assert "managed directories must not overlap" in result.stderr
    assert not p["app"].exists()


def write_live_lock(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    (path / "token").write_text("external-owner\n", encoding="utf-8")


def test_lock_identity_is_independent_of_state_directory(tmp_path: Path) -> None:
    p = paths(tmp_path)
    app_lock = p["app"].parent / f".{p['app'].name}.install.lock"
    write_live_lock(app_lock)
    alternate_state = tmp_path / "different-state"
    try:
        result = run_install(p, state=alternate_state)
        assert result.returncode != 0
        assert f"another install is running: pid {os.getpid()}" in result.stderr
        assert not p["app"].exists()
        assert not alternate_state.exists()
    finally:
        shutil.rmtree(app_lock, ignore_errors=True)


def test_lock_identity_protects_shared_command_directory(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["bin"].mkdir()
    p["bin"].chmod(0o755)
    bin_lock = p["bin"] / ".scan-dir.install.lock"
    write_live_lock(bin_lock)

    alternate = dict(p)
    alternate["app"] = tmp_path / "different-app"
    alternate["state"] = tmp_path / "different-state"
    alternate["config"] = tmp_path / "different-config"
    app_lock = alternate["app"].parent / f".{alternate['app'].name}.install.lock"
    try:
        result = run_install(alternate)
        assert result.returncode != 0
        assert f"another install is running: pid {os.getpid()}" in result.stderr
        assert not alternate["app"].exists()
        assert not app_lock.exists()
    finally:
        shutil.rmtree(bin_lock, ignore_errors=True)


def test_stale_target_locks_are_quarantined_and_replaced(tmp_path: Path) -> None:
    p = paths(tmp_path)
    app_lock = p["app"].parent / f".{p['app'].name}.install.lock"
    app_lock.mkdir()
    (app_lock / "pid").write_text("999999999\n", encoding="utf-8")
    (app_lock / "token").write_text("stale\n", encoding="utf-8")

    result = run_install(p)
    assert result.returncode == 0, result.stderr
    assert not app_lock.exists()
    assert not list(tmp_path.glob(".install.lock.stale.*"))
    assert_installed(p)


def test_wrapper_quotes_paths_with_spaces_and_apostrophes(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["app"] = tmp_path / "app's runtime"
    p["bin"] = tmp_path / "command's bin"
    p["state"] = tmp_path / "state's files"
    p["config"] = tmp_path / "config's files"

    result = run_install(p)
    assert result.returncode == 0, result.stderr
    assert_installed(p)
    user_config = p["config"] / "config.yaml"
    user_config.write_text("scan-styling: minimal\n", encoding="utf-8")
    status = subprocess.run(
        [p["bin"] / "sdir", "status"],
        env={**os.environ, "COLUMNS": "1000", "NO_COLOR": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert str(user_config) in status.stdout


def test_generated_wrapper_maps_missing_runtime_to_documented_runtime_failure(tmp_path: Path) -> None:
    p = paths(tmp_path)
    installed = run_install(p)
    assert installed.returncode == 0, installed.stderr
    wrapper = p["bin"] / "sdir"
    runtime = p["app"] / "sdir.py"
    runtime.unlink()

    result = subprocess.run(
        [wrapper, "--version"],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "managed runtime missing" in result.stderr


def test_generated_wrapper_maps_unavailable_python_to_documented_runtime_failure(tmp_path: Path) -> None:
    p = paths(tmp_path)
    shim_dir = tmp_path / "python-shim"
    shim_dir.mkdir()
    shim = shim_dir / "chosen-python"
    shim.write_text(
        f'#!/usr/bin/env bash\nexec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    installed = run_installer(
        p,
        install_args(p),
        env_overrides={"PYTHON": str(shim)},
    )
    assert installed.returncode == 0, installed.stderr
    wrapper = p["bin"] / "sdir"
    assert f"PYTHON_BIN={shlex.quote(str(shim))}" in wrapper.read_text(encoding="utf-8")

    shim.unlink()
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    result = subprocess.run(
        ["/bin/bash", wrapper, "--version"],
        env={**os.environ, "PATH": str(empty_path)},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Python 3.10+ is unavailable" in result.stderr


def test_installer_help_and_argument_validation(tmp_path: Path) -> None:
    p = paths(tmp_path)
    help_result = run_installer(p, ["--help"])
    assert help_result.returncode == 0
    assert "--source-url" in help_result.stdout and "--no-active-bridge" in help_result.stdout

    unknown = run_installer(p, ["--unknown"])
    assert unknown.returncode != 0 and "unknown option" in unknown.stderr

    relative_args = install_args(p)
    relative_args[relative_args.index("--app-dir") + 1] = "relative"
    relative = run_installer(p, relative_args)
    assert relative.returncode != 0 and "explicit managed path must be absolute" in relative.stderr

    mixed_args = install_args(p)
    mixed_args.extend(["--source-url", "https://example.com/sdir.py"])
    mixed = run_installer(p, mixed_args)
    assert mixed.returncode != 0 and "mutually exclusive" in mixed.stderr


def test_every_installer_logo_color_and_help_interaction(tmp_path: Path) -> None:
    p = paths(tmp_path)
    for mode in ("auto", "text", "small", "medium", "large"):
        arguments = install_args(p)
        arguments[arguments.index("--logo") + 1] = mode
        arguments.append("--dry-run")
        result = run_installer(p, arguments)
        assert result.returncode == 0, (mode, result.stderr)
        assert not p["app"].exists()
    for mode in ("auto", "always", "never"):
        arguments = install_args(p)
        arguments[arguments.index("--color") + 1] = mode
        arguments.append("--dry-run")
        result = run_installer(p, arguments)
        assert result.returncode == 0, (mode, result.stderr)
        assert not p["app"].exists()

    short_help = run_installer(p, ["-h"])
    assert short_help.returncode == 0
    assert "Usage:" in short_help.stdout
    for option, value, message in (
        ("--logo", "huge", "invalid logo mode"),
        ("--color", "sometimes", "invalid color mode"),
    ):
        invalid = run_installer(p, [option, value])
        assert invalid.returncode != 0
        assert message in invalid.stderr


def test_custom_remote_requires_digests_before_download(tmp_path: Path) -> None:
    p = paths(tmp_path)
    arguments = install_args(p)
    source_index = arguments.index("--source")
    arguments[source_index : source_index + 2] = ["--source-url", "https://example.com/sdir.py"]
    result = run_installer(p, arguments)
    assert result.returncode != 0
    assert "custom remote packages require" in result.stderr
    assert not p["app"].exists()

    arguments[source_index + 1] = "https://user@example.com/sdir.py"
    invalid = run_installer(p, arguments)
    assert invalid.returncode != 0
    assert "absolute HTTPS file URL" in invalid.stderr


def test_shell_path_repair_is_idempotent_and_preserves_profile(tmp_path: Path) -> None:
    p = paths(tmp_path)
    bashrc = p["home"] / ".bashrc"
    bashrc.write_bytes(b"export KEEP=1\n")
    bashrc.chmod(0o640)
    arguments = install_args(p)
    arguments.remove("--no-path-repair")

    first = run_installer(p, arguments, env_overrides={"SHELL": "/bin/bash", "PATH": "/usr/bin:/bin"})
    assert first.returncode == 0, first.stderr
    second = run_installer(p, arguments, env_overrides={"SHELL": "/bin/bash", "PATH": "/usr/bin:/bin"})
    assert second.returncode == 0, second.stderr

    for profile in (bashrc, p["home"] / ".profile"):
        content = profile.read_text(encoding="utf-8")
        assert content.count("# >>> scan-dir PATH >>>") == 1
        assert content.count("# <<< scan-dir PATH <<<") == 1
        assert str(p["bin"]) in content
    assert bashrc.read_text(encoding="utf-8").startswith("export KEEP=1\n")
    assert stat.S_IMODE(bashrc.stat().st_mode) == 0o640


def test_active_path_bridge_is_safe_and_functional(tmp_path: Path) -> None:
    p = paths(tmp_path)
    active_bin = p["home"] / ".local" / "bin"
    active_bin.mkdir(parents=True)
    active_bin.chmod(0o755)
    arguments = install_args(p)
    arguments.remove("--no-active-bridge")
    result = run_installer(
        p,
        arguments,
        env_overrides={"PATH": f"{active_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    for command in ("sdir", "scan-dir"):
        bridge = active_bin / command
        assert "scan-dir managed active PATH bridge" in bridge.read_text(encoding="utf-8")
        version = subprocess.run([bridge, "version"], text=True, capture_output=True, check=True).stdout.strip()
        assert version == f"sdir {VERSION}"


def test_managed_install_with_unverifiable_version_requires_force(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["app"].mkdir()
    p["app"].chmod(0o700)
    (p["app"] / ".managed").write_text("scan-dir managed command\n", encoding="utf-8")
    sentinel = p["app"] / "preserve-until-forced"
    sentinel.write_text("old managed data\n", encoding="utf-8")

    refused = run_install(p)
    assert refused.returncode != 0
    assert "managed installation version cannot be verified" in refused.stderr
    assert sentinel.read_text(encoding="utf-8") == "old managed data\n"

    forced = run_install(p, "--force")
    assert forced.returncode == 0, forced.stderr
    assert not sentinel.exists()
    assert_installed(p)


def test_local_package_is_auto_detected_beside_installer(tmp_path: Path) -> None:
    p = paths(tmp_path)
    arguments = install_args(p)
    source_index = arguments.index("--source")
    del arguments[source_index : source_index + 2]

    result = run_installer(p, arguments)
    assert result.returncode == 0, result.stderr
    assert_installed(p)


def test_local_package_streams_files_larger_than_former_size_ceiling(tmp_path: Path) -> None:
    p = paths(tmp_path)
    package = tmp_path / "large-package"
    package.mkdir()
    runtime = package / "sdir.py"
    runtime.write_bytes(RUNTIME.read_bytes() + b"\n# " + b"x" * (11 * 1024 * 1024) + b"\n")
    shutil.copy2(ROOT / "config.yaml", package / "config.yaml")
    shutil.copy2(ROOT / "SKILL.md", package / "SKILL.md")

    arguments = install_args(p)
    arguments[arguments.index("--source") + 1] = str(runtime)
    result = run_installer(p, arguments)

    assert result.returncode == 0, result.stderr
    assert (p["app"] / "sdir.py").stat().st_size == runtime.stat().st_size
    assert_installed(p)


def test_newer_installation_requires_force_for_downgrade(tmp_path: Path) -> None:
    p = paths(tmp_path)
    assert run_install(p).returncode == 0
    manifest = p["app"] / "install-manifest.env"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(
        re.sub(r"^installer_version=.*$", "installer_version=9999.1", text, flags=re.MULTILINE),
        encoding="utf-8",
    )

    refused = run_install(p)
    assert refused.returncode != 0
    assert f"refusing downgrade from 9999.1 to {VERSION}" in refused.stderr
    assert "installer_version=9999.1" in manifest.read_text(encoding="utf-8")

    forced = run_install(p, "--force")
    assert forced.returncode == 0, forced.stderr
    assert_installed(p)


def write_fake_package_curl(fake_bin: Path) -> None:
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${1:-} == --help ]]; then
  printf '%s\n' '--proto-redir'
  exit 0
fi
output=''
url=''
while [[ $# -gt 0 ]]; do
  case "$1" in
    --output) shift; output=${1:-} ;;
    https://*) url=$1 ;;
  esac
  shift
done
[[ -n "$output" && -n "$url" ]]
cp "$FAKE_PACKAGE_DIR/${url##*/}" "$output"
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)


def pinned_remote_args(p: dict[str, Path]) -> list[str]:
    arguments = install_args(p)
    source_index = arguments.index("--source")
    arguments[source_index : source_index + 2] = [
        "--source-url",
        "https://example.com/releases/sdir.py",
        "--sha256",
        digest(RUNTIME),
        "--config-sha256",
        digest(ROOT / "config.yaml"),
        "--skill-sha256",
        digest(ROOT / "SKILL.md"),
    ]
    return arguments


def test_custom_remote_package_install_uses_pinned_digests(tmp_path: Path) -> None:
    p = paths(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_fake_package_curl(fake_bin)

    arguments = pinned_remote_args(p)
    result = run_installer(
        p,
        arguments,
        env_overrides={
            "FAKE_PACKAGE_DIR": str(ROOT),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stderr
    assert_installed(p)


def test_remote_package_streams_files_larger_than_former_size_ceiling(tmp_path: Path) -> None:
    p = paths(tmp_path)
    package = tmp_path / "large-remote-package"
    package.mkdir()
    runtime = package / "sdir.py"
    runtime.write_bytes(RUNTIME.read_bytes() + b"\n# " + b"x" * (11 * 1024 * 1024) + b"\n")
    shutil.copy2(ROOT / "config.yaml", package / "config.yaml")
    shutil.copy2(ROOT / "SKILL.md", package / "SKILL.md")

    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_fake_package_curl(fake_bin)
    arguments = pinned_remote_args(p)
    arguments[arguments.index("--sha256") + 1] = digest(runtime)

    result = run_installer(
        p,
        arguments,
        env_overrides={
            "FAKE_PACKAGE_DIR": str(package),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stderr
    assert (p["app"] / "sdir.py").stat().st_size == runtime.stat().st_size
    assert_installed(p)


def test_pinned_remote_package_dry_run_downloads_validates_and_commits_nothing(tmp_path: Path) -> None:
    p = paths(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    write_fake_package_curl(fake_bin)
    arguments = [*pinned_remote_args(p), "--dry-run"]

    result = run_installer(
        p,
        arguments,
        env_overrides={
            "FAKE_PACKAGE_DIR": str(ROOT),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stderr
    for name in ("app", "bin", "state", "config"):
        assert not p[name].exists()
    assert list(p["tmp"].iterdir()) == []


def test_python_download_fallback_installs_pinned_remote_package(tmp_path: Path) -> None:
    p = paths(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${1:-} == --help ]]; then
  printf '%s\n' 'curl help without required redirect option'
  exit 0
fi
exit 99
""",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
printf '%s\n' "$#|$*" >> "$WRAPPER_TRACE"
if [[ $# -ge 4 && ${1:-} == -S && ${2:-} == - && ${3:-} == https://* ]]; then
  script=$(cat)
  grep -Fq 'class HTTPSOnly' <<< "$script"
  grep -Fq 'response.read(1024 * 1024)' <<< "$script"
  printf '%s' "$script" > "$DOWNLOADER_CAPTURE"
  cp "$FAKE_PACKAGE_DIR/${3##*/}" "$4"
  exit 0
fi
exec "$REAL_PYTHON" "$@"
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    capture = tmp_path / "downloader.py"
    trace = tmp_path / "python-wrapper.trace"

    arguments = install_args(p)
    source_index = arguments.index("--source")
    arguments[source_index : source_index + 2] = [
        "--source-url",
        "https://example.com/releases/sdir.py",
        "--sha256",
        digest(RUNTIME),
        "--config-sha256",
        digest(ROOT / "config.yaml"),
        "--skill-sha256",
        digest(ROOT / "SKILL.md"),
    ]
    result = run_installer(
        p,
        arguments,
        env_overrides={
            "DOWNLOADER_CAPTURE": str(capture),
            "FAKE_PACKAGE_DIR": str(ROOT),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "PYTHON": str(fake_python),
            "REAL_PYTHON": sys.executable,
            "WRAPPER_TRACE": str(trace),
        },
    )
    diagnostic = result.stderr + (trace.read_text(encoding="utf-8") if trace.exists() else "no wrapper trace")
    assert result.returncode == 0, diagnostic
    assert "class HTTPSOnly" in capture.read_text(encoding="utf-8")
    assert_installed(p)


def test_foreign_marker_text_does_not_claim_command_ownership(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["bin"].mkdir()
    p["bin"].chmod(0o755)
    foreign = p["bin"] / "sdir"
    foreign.write_text(
        "#!/bin/sh\n# scan-dir managed command\necho foreign\n",
        encoding="utf-8",
    )
    foreign.chmod(0o755)

    refused = run_install(p)
    assert refused.returncode != 0
    assert "refusing to replace foreign command" in refused.stderr
    assert foreign.read_text(encoding="utf-8").endswith("echo foreign\n")
    assert not p["app"].exists()


def test_foreign_bridge_marker_does_not_claim_bridge_ownership(tmp_path: Path) -> None:
    p = paths(tmp_path)
    active_bin = p["home"] / ".local" / "bin"
    active_bin.mkdir(parents=True)
    foreign = active_bin / "sdir"
    foreign.write_text(
        "#!/usr/bin/env bash\n# scan-dir managed active PATH bridge\nprintf 'foreign\\n'\n",
        encoding="utf-8",
    )
    foreign.chmod(0o755)
    arguments = install_args(p)
    arguments.remove("--no-active-bridge")

    result = run_installer(
        p,
        arguments,
        env_overrides={"PATH": f"{active_bin}:{os.environ['PATH']}"},
    )
    assert result.returncode == 0, result.stderr
    assert foreign.read_text(encoding="utf-8").endswith("printf 'foreign\\n'\n")
    assert_installed(p)


def legacy_paths(
    p: dict[str, Path],
    *,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Path]:
    if sys.platform == "darwin":
        base = p["home"] / "Library" / "Application Support" / "project-summarizer"
        return {"app": base / "app", "state": base / "state", "config": base / "config"}

    overrides = env_overrides or {}
    data_base = Path(overrides.get("XDG_DATA_HOME", p["home"] / ".local" / "share"))
    state_base = Path(overrides.get("XDG_STATE_HOME", p["home"] / ".local" / "state"))
    config_base = Path(overrides.get("XDG_CONFIG_HOME", p["home"] / ".config"))
    return {
        "app": data_base / "project-summarizer",
        "state": state_base / "project-summarizer",
        "config": config_base / "project-summarizer",
    }


def create_legacy_install(
    p: dict[str, Path],
    *,
    foreign_wrapper: bool = False,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, Path]:
    legacy = legacy_paths(p, env_overrides=env_overrides)
    legacy["app"].mkdir(parents=True)
    legacy["app"].chmod(0o700)
    (legacy["app"] / ".managed").write_text("project-summarizer managed command\n", encoding="utf-8")
    (legacy["app"] / ".installer-version").write_text("2026.07.29.2\n", encoding="utf-8")
    (legacy["app"] / "prs.py").write_text("# legacy runtime\n", encoding="utf-8")
    (legacy["app"] / "config.yaml").write_text("scan-styling: low\n", encoding="utf-8")
    (legacy["app"] / "SKILL.md").write_text("legacy skill\n", encoding="utf-8")

    logs = legacy["state"] / "logs"
    logs.mkdir(parents=True)
    legacy["state"].chmod(0o700)
    logs.chmod(0o700)
    (logs / "install.log").write_text("legacy log\n", encoding="utf-8")

    legacy["config"].mkdir(parents=True)
    legacy["config"].chmod(0o700)
    (legacy["config"] / "config.yaml").write_text("scan-styling: minimal\n", encoding="utf-8")

    p["bin"].mkdir()
    p["bin"].chmod(0o755)
    for command in ("prs", "project-summarizer"):
        wrapper = p["bin"] / command
        if foreign_wrapper and command == "prs":
            wrapper.write_text("#!/bin/sh\necho foreign\n", encoding="utf-8")
        else:
            wrapper.write_text(
                "#!/usr/bin/env bash\n"
                f"# {command} -- Project Summarizer command\n"
                "# project-summarizer managed command\n"
                "set -Eeuo pipefail\n"
                f"APP_DIR={legacy['app']}\n"
                "PYTHON_BIN=/usr/bin/python3\n"
                'SOURCE_FILE="$APP_DIR/prs.py"\n'
                'exec "$PYTHON_BIN" -S "$SOURCE_FILE" "$@"\n',
                encoding="utf-8",
            )
        wrapper.chmod(0o755)
    return legacy


def test_legacy_managed_install_migrates_config_and_is_removed(tmp_path: Path) -> None:
    p = paths(tmp_path)
    legacy = create_legacy_install(p)

    result = run_install(p)
    assert result.returncode == 0, result.stderr
    assert_installed(p)
    assert (p["config"] / "config.yaml").read_text(encoding="utf-8") == "scan-styling: minimal\n"
    assert stat.S_IMODE((p["config"] / "config.yaml").stat().st_mode) == 0o600
    assert not legacy["app"].exists()
    assert not legacy["state"].exists()
    assert not legacy["config"].exists()
    assert not (p["bin"] / "prs").exists()
    assert not (p["bin"] / "project-summarizer").exists()


def test_legacy_owned_cleanup_rolls_back_entire_install_before_finalize(tmp_path: Path) -> None:
    p = paths(tmp_path)
    legacy = create_legacy_install(p)
    testable_installer = tmp_path / "install-legacy-cleanup-rollback.sh"
    source = INSTALLER.read_text(encoding="utf-8")
    assert source.endswith('main "$@"\n')
    testable_installer.write_text(source.removesuffix('main "$@"\n'), encoding="utf-8")

    command = """
source "$1"
PYTHON_BIN=$2
CONFIG_DIR=$3
PREVIOUS_CONFIG_DIR=$4
LEGACY_CONFIG_DIR=$5
LEGACY_APP_DIR=$6
LEGACY_STATE_DIR=$7
LEGACY_BIN_DIR=$8
APP_DIR=$9
STATE_DIR=${10}
DRY_RUN=0
cleanup_legacy_install
false
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(testable_installer),
            sys.executable,
            str(p["config"]),
            str(tmp_path / "absent-previous-config"),
            str(legacy["config"]),
            str(legacy["app"]),
            str(legacy["state"]),
            str(p["bin"]),
            str(p["app"]),
            str(p["state"]),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert legacy["app"].is_dir()
    assert legacy["state"].is_dir()
    assert (legacy["config"] / "config.yaml").read_text(encoding="utf-8") == "scan-styling: minimal\n"
    assert (p["bin"] / "prs").is_file()
    assert (p["bin"] / "project-summarizer").is_file()
    assert not (p["config"] / "config.yaml").exists()


def test_legacy_migration_respects_custom_xdg_roots(tmp_path: Path) -> None:
    if sys.platform == "darwin":
        pytest.skip("Darwin legacy installs use Library/Application Support instead of XDG roots")

    p = paths(tmp_path)
    env_overrides = {
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
    }
    legacy = create_legacy_install(p, env_overrides=env_overrides)

    result = run_installer(p, install_args(p), env_overrides=env_overrides)
    assert result.returncode == 0, result.stderr
    assert_installed(p)
    assert (p["config"] / "config.yaml").read_text(encoding="utf-8") == "scan-styling: minimal\n"
    assert not legacy["app"].exists()
    assert not legacy["state"].exists()
    assert not legacy["config"].exists()


def test_legacy_cleanup_preserves_unproven_command_and_runtime(tmp_path: Path) -> None:
    p = paths(tmp_path)
    legacy = create_legacy_install(p, foreign_wrapper=True)

    result = run_install(p)
    assert result.returncode == 0, result.stderr
    assert_installed(p)
    assert legacy["app"].exists()
    assert legacy["state"].exists()
    assert (p["bin"] / "prs").read_text(encoding="utf-8") == "#!/bin/sh\necho foreign\n"


def test_legacy_config_conflict_is_preserved(tmp_path: Path) -> None:
    p = paths(tmp_path)
    legacy = create_legacy_install(p)
    p["config"].mkdir()
    (p["config"] / "config.yaml").write_text("scan-styling: full\n", encoding="utf-8")

    result = run_install(p)
    assert result.returncode == 0, result.stderr
    assert (p["config"] / "config.yaml").read_text(encoding="utf-8") == "scan-styling: full\n"
    assert (legacy["config"] / "config.yaml").read_text(encoding="utf-8") == "scan-styling: minimal\n"


def test_mktemp_templates_are_busybox_compatible() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    templates = re.findall(r'mktemp(?: -d)? "([^"]*XXXXXX[^"]*)"', installer)
    assert templates
    assert all(template.endswith("XXXXXX") for template in templates), templates


def test_python_bootstrap_and_download_paths_have_no_artificial_process_limits() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    bootstrap = installer.split("install_python() {", 1)[1].split("\n}\nensure_python()", 1)[0]
    for manager in ("brew", "apt-get", "dnf", "yum", "pacman", "zypper", "apk", "pkg"):
        assert manager in bootstrap
    assert "run_interruptible brew install python" in bootstrap
    assert bootstrap.count("run_privileged ") >= 8

    forbidden = (
        "NETWORK_CONNECT_TIMEOUT",
        "NETWORK_TOTAL_TIMEOUT",
        "NETWORK_RETRIES",
        "MAX_DOWNLOAD_BYTES",
        "PACKAGE_MANAGER_TOTAL_TIMEOUT",
        "run_interruptible_timeout",
        "run_privileged_timeout",
        "--connect-timeout",
        "--max-time",
        "--max-filesize",
        "Acquire::Retries",
        "Dpkg::Lock::Timeout",
        "signal.setitimer",
        "for attempt in range(retries + 1)",
    )
    assert all(item not in installer for item in forbidden)
