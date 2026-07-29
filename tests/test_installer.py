from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
RUNTIME = ROOT / "prs.py"
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
        timeout=30,
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
    primary = p["bin"] / "prs"
    alias = p["bin"] / "project-summarizer"
    assert (
        subprocess.run([primary, "version"], text=True, capture_output=True, check=True).stdout.strip()
        == f"prs {VERSION}"
    )
    assert (
        subprocess.run([alias, "version"], text=True, capture_output=True, check=True).stdout.strip()
        == f"prs {VERSION}"
    )
    assignment = next(line for line in primary.read_text().splitlines() if line.startswith("export PRS_CONFIG_DIR="))
    resolved_config = subprocess.run(
        ["bash", "-c", f'{assignment}; printf "%s" "$PRS_CONFIG_DIR"'],
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


def test_install_is_idempotent_and_reconfigures(tmp_path: Path) -> None:
    p = paths(tmp_path)
    first = run_install(p)
    assert first.returncode == 0, first.stderr
    assert_installed(p)
    app_inode = p["app"].stat().st_ino
    primary_digest = digest(p["bin"] / "prs")

    second = run_install(p)
    assert second.returncode == 0, second.stderr
    assert p["app"].stat().st_ino == app_inode
    assert digest(p["bin"] / "prs") == primary_digest

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
    runtime = p["app"] / "prs.py"
    primary = p["bin"] / "prs"
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
    foreign = p["bin"] / "prs"
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


def write_mv_fault_shim(directory: Path) -> Path:
    real_mv = shutil.which("mv")
    assert real_mv is not None
    shim = directory / "mv"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "source_path=${1:-}\n"
        "destination=${2:-}\n"
        "if [[ -n ${FAIL_COMMIT_DESTINATION:-} && $destination == $FAIL_COMMIT_DESTINATION "
        "&& $source_path == *'.tmp.'* ]]; then exit 91; fi\n"
        "if [[ ${FAIL_ROLLBACK_APP:-0} == 1 && $source_path == */.app.rollback.*/original ]]; then exit 92; fi\n"
        'exec "$REAL_MV" "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    return Path(real_mv)


def test_post_commit_failure_rolls_back(tmp_path: Path) -> None:
    p = paths(tmp_path)
    assert run_install(p).returncode == 0
    marker = p["app"] / "unexpected"
    marker.write_text("force a repair\n")
    app_before = snapshot_tree(p["app"])
    wrappers_before = snapshot_tree(p["bin"])
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    real_mv = write_mv_fault_shim(fake_bin)

    failed = run_installer(
        p,
        install_args(p),
        env_overrides={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REAL_MV": str(real_mv),
            "FAIL_COMMIT_DESTINATION": str(p["bin"] / "prs"),
        },
    )
    assert failed.returncode != 0
    assert snapshot_tree(p["app"]) == app_before
    assert snapshot_tree(p["bin"]) == wrappers_before
    assert marker.exists()

    repaired = run_install(p)
    assert repaired.returncode == 0, repaired.stderr
    assert not marker.exists()
    assert_installed(p)


def test_failed_rollback_preserves_previous_installation_backup(tmp_path: Path) -> None:
    p = paths(tmp_path)
    assert run_install(p).returncode == 0
    marker = p["app"] / "previous-release-marker"
    marker.write_text("preserve me\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    real_mv = write_mv_fault_shim(fake_bin)

    failed = run_installer(
        p,
        install_args(p),
        env_overrides={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "REAL_MV": str(real_mv),
            "FAIL_COMMIT_DESTINATION": str(p["bin"] / "prs"),
            "FAIL_ROLLBACK_APP": "1",
        },
    )
    assert failed.returncode == 70
    preserved = list(tmp_path.glob(".app.rollback.*/original/previous-release-marker"))
    assert len(preserved) == 1
    assert preserved[0].read_text(encoding="utf-8") == "preserve me\n"
    assert str(preserved[0].parent) in failed.stderr
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
    bin_lock = p["bin"] / ".project-summarizer.install.lock"
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
        [p["bin"] / "prs", "status"],
        env={**os.environ, "COLUMNS": "1000", "NO_COLOR": "1"},
        text=True,
        capture_output=True,
        check=True,
    )
    assert str(user_config) in status.stdout


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
    mixed_args.extend(["--source-url", "https://example.com/prs.py"])
    mixed = run_installer(p, mixed_args)
    assert mixed.returncode != 0 and "mutually exclusive" in mixed.stderr


def test_custom_remote_requires_digests_before_download(tmp_path: Path) -> None:
    p = paths(tmp_path)
    arguments = install_args(p)
    source_index = arguments.index("--source")
    arguments[source_index : source_index + 2] = ["--source-url", "https://example.com/prs.py"]
    result = run_installer(p, arguments)
    assert result.returncode != 0
    assert "custom remote packages require" in result.stderr
    assert not p["app"].exists()

    arguments[source_index + 1] = "https://user@example.com/prs.py"
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
        assert content.count("# >>> project-summarizer PATH >>>") == 1
        assert content.count("# <<< project-summarizer PATH <<<") == 1
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
    for command in ("prs", "project-summarizer"):
        bridge = active_bin / command
        assert "project-summarizer managed active PATH bridge" in bridge.read_text(encoding="utf-8")
        version = subprocess.run([bridge, "version"], text=True, capture_output=True, check=True).stdout.strip()
        assert version == f"prs {VERSION}"


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


def test_custom_remote_package_install_uses_pinned_digests(tmp_path: Path) -> None:
    p = paths(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${1:-} == --help ]]; then
  printf '%s\n' '--max-filesize'
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

    arguments = install_args(p)
    source_index = arguments.index("--source")
    arguments[source_index : source_index + 2] = [
        "--source-url",
        "https://example.com/releases/prs.py",
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
            "FAKE_PACKAGE_DIR": str(ROOT),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stderr
    assert_installed(p)


def test_python_download_fallback_installs_pinned_remote_package(tmp_path: Path) -> None:
    p = paths(tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ ${1:-} == --help ]]; then
  printf '%s\n' 'curl help without required size option'
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
if [[ $# -ge 8 && ${1:-} == -S && ${2:-} == - && ${3:-} == https://* ]]; then
  script=$(cat)
  grep -Fq 'signal.setitimer(signal.ITIMER_REAL, remaining)' <<< "$script"
  grep -Fq 'for attempt in range(retries + 1)' <<< "$script"
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
        "https://example.com/releases/prs.py",
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
    assert "deadline = time.monotonic() + total_timeout" in capture.read_text(encoding="utf-8")
    assert_installed(p)


def test_foreign_marker_text_does_not_claim_command_ownership(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["bin"].mkdir()
    p["bin"].chmod(0o755)
    foreign = p["bin"] / "prs"
    foreign.write_text(
        "#!/bin/sh\n# project-summarizer managed command\necho foreign\n",
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
    foreign = active_bin / "prs"
    foreign.write_text(
        "#!/usr/bin/env bash\n# project-summarizer managed active PATH bridge\nprintf 'foreign\\n'\n",
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


def test_legacy_managed_install_migrates_without_force(tmp_path: Path) -> None:
    p = paths(tmp_path)
    p["app"].mkdir()
    p["app"].chmod(0o700)
    p["bin"].mkdir()
    p["bin"].chmod(0o755)
    (p["app"] / ".managed").write_text(
        "project-summarizer managed command\n",
        encoding="utf-8",
    )
    (p["app"] / ".installer-version").write_text("2026.07.26.1\n", encoding="utf-8")
    for command in ("prs", "project-summarizer"):
        wrapper = p["bin"] / command
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            f"# {command} -- Project Summarizer command\n"
            "# project-summarizer managed command\n"
            "set -Eeuo pipefail\n"
            f"APP_DIR={p['app']}\n"
            "PYTHON_BIN=/usr/bin/python3\n"
            'SOURCE_FILE="$APP_DIR/prs.py"\n'
            'exec "$PYTHON_BIN" -S "$SOURCE_FILE" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)

    result = run_install(p)
    assert result.returncode == 0, result.stderr
    assert_installed(p)


def test_mktemp_templates_are_busybox_compatible() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    templates = re.findall(r'mktemp(?: -d)? "([^"]*XXXXXX[^"]*)"', installer)
    assert templates
    assert all(template.endswith("XXXXXX") for template in templates), templates


def test_all_python_bootstrap_backends_are_bounded() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    bootstrap = installer.split("install_python() {", 1)[1].split("\n}\nensure_python()", 1)[0]
    for manager in ("brew", "apt-get", "dnf", "yum", "pacman", "zypper", "apk", "pkg"):
        assert manager in bootstrap
    assert "run_interruptible_timeout" in bootstrap
    assert bootstrap.count("run_privileged_timeout") >= 8
    assert "run_interruptible run_privileged" not in bootstrap


def test_interruptible_timeout_terminates_slow_child(tmp_path: Path) -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    helpers = installer.split("start_interruptible_child() {", 1)[1].split("\n\nrun_privileged_timeout()", 1)[0]
    harness = tmp_path / "timeout-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "CHILD_PID=''\n"
        "WATCHDOG_PID=''\n"
        "CHILD_PROCESS_GROUP=0\n"
        f"start_interruptible_child() {{{helpers}\n"
        "started=$SECONDS\n"
        "set +e\n"
        "run_interruptible_timeout 1 sleep 30\n"
        "status=$?\n"
        "set -e\n"
        "elapsed=$((SECONDS-started))\n"
        "[[ $status -ne 0 && $elapsed -lt 8 ]]\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_interruptible_timeout_terminates_descendant_process_group(tmp_path: Path) -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    helpers = installer.split("start_interruptible_child() {", 1)[1].split("\n\nrun_privileged_timeout()", 1)[0]
    descendant_pid = tmp_path / "descendant.pid"
    harness = tmp_path / "descendant-timeout-harness.sh"
    harness.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "CHILD_PID=''\n"
        "WATCHDOG_PID=''\n"
        "CHILD_PROCESS_GROUP=0\n"
        f"start_interruptible_child() {{{helpers}\n"
        "worker() {\n"
        "  sleep 30 &\n"
        f"  printf '%s\\n' \"$!\" > {descendant_pid!s}\n"
        "  wait\n"
        "}\n"
        "set +e\n"
        "run_interruptible_timeout 1 worker\n"
        "status=$?\n"
        "set -e\n"
        "[[ $status -ne 0 ]]\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["bash", str(harness)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    pid = int(descendant_pid.read_text(encoding="ascii").strip())
    probe = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )
    # A missing process is ideal. A zombie has already terminated and merely
    # awaits collection by the container/session init process.
    assert probe.returncode != 0 or probe.stdout.strip().startswith("Z"), probe.stdout
