# test_release_tools.py -- Project Summarizer release tooling tests
# Verifies deterministic archive bytes, package contents, metadata, and checksums.
# Tags: tests, release, packaging, reproducibility
# 2026-07-28

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "tools" / "build_release.py"
VERSION = re.search(r'^VERSION = "([^"]+)"$', (ROOT / "prs.py").read_text(), re.MULTILINE).group(1)
EXPECTED_FILES = {
    "LICENSE": 0o644,
    "README.md": 0o644,
    "SKILL.md": 0o644,
    "config.yaml": 0o644,
    "install.sh": 0o755,
    "prs.py": 0o755,
}


def run_builder(output_dir: Path) -> tuple[Path, Path]:
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output-dir", str(output_dir)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    archive, checksums = result.stdout.splitlines()
    return Path(archive), Path(checksums)


def test_release_archive_is_reproducible_and_complete(tmp_path: Path) -> None:
    first_archive, first_checksums = run_builder(tmp_path / "first")
    second_archive, second_checksums = run_builder(tmp_path / "second")
    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert first_checksums.read_text() == second_checksums.read_text()

    digest = hashlib.sha256(first_archive.read_bytes()).hexdigest()
    assert first_checksums.read_text(encoding="ascii") == f"{digest}  {first_archive.name}\n"

    package_root = f"project-summarizer-{VERSION}"
    with tarfile.open(first_archive, "r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [f"{package_root}/{filename}" for filename in EXPECTED_FILES]
        for member in members:
            filename = Path(member.name).name
            assert member.isfile()
            assert member.mode == EXPECTED_FILES[filename]
            assert member.uid == 0 and member.gid == 0
            assert member.uname == "" and member.gname == ""
            extracted = archive.extractfile(member)
            assert extracted is not None
            assert extracted.read() == (ROOT / filename).read_bytes()


def test_release_builder_rejects_invalid_embedded_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for filename in EXPECTED_FILES:
        shutil.copy2(ROOT / filename, project / filename)
    shutil.copytree(ROOT / "tools", project / "tools")
    installer = project / "install.sh"
    installer.write_text(
        re.sub(
            r"^readonly DEFAULT_RUNTIME_SHA256='[0-9a-f]{64}'$",
            "readonly DEFAULT_RUNTIME_SHA256='" + "0" * 64 + "'",
            installer.read_text(encoding="utf-8"),
            count=1,
            flags=re.MULTILINE,
        ),
        encoding="utf-8",
    )
    output = project / "dist"
    result = subprocess.run(
        [sys.executable, str(project / "tools" / "build_release.py"), "--output-dir", str(output)],
        cwd=project,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "does not match prs.py" in result.stderr
    assert not output.exists()


def test_release_checksum_replaces_symlink_without_touching_target(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("unchanged\n", encoding="utf-8")
    checksum = output / "SHA256SUMS"
    checksum.symlink_to(victim)
    archive, written_checksum = run_builder(output)
    assert archive.is_file()
    assert written_checksum == checksum
    assert not checksum.is_symlink()
    assert victim.read_text(encoding="utf-8") == "unchanged\n"


def test_release_builder_rejects_symlinked_output_directory(tmp_path: Path) -> None:
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(real_output, target_is_directory=True)
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output-dir", str(linked_output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "not a real directory" in result.stderr
    assert list(real_output.iterdir()) == []


def test_release_builder_archives_the_verified_snapshot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    for filename in EXPECTED_FILES:
        shutil.copy2(ROOT / filename, project / filename)
    shutil.copytree(ROOT / "tools", project / "tools")
    script = r"""
import hashlib
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path("tools").resolve()))
import build_release

original_verify = build_release.verify_release

def racing_verify(snapshot):
    version = original_verify(snapshot)
    with Path("prs.py").open("ab") as handle:
        handle.write(b"\n# changed after verification\n")
    return version

build_release.verify_release = racing_verify
archive, _checksums = build_release.build_release(Path("dist"))
with tarfile.open(archive, "r:gz") as package:
    member = next(item for item in package.getmembers() if item.name.endswith("/prs.py"))
    extracted = package.extractfile(member)
    assert extracted is not None
    archived = extracted.read()
installer = Path("install.sh").read_text(encoding="utf-8")
embedded = installer.split("readonly DEFAULT_RUNTIME_SHA256='")[1].split("'", 1)[0]
print(embedded == hashlib.sha256(archived).hexdigest())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=project,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert result.stdout.strip() == "True"


def test_release_builder_rejects_concurrent_output_writer(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time; "
                "fd=os.open(sys.argv[1], os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0)); "
                "fcntl.flock(fd, fcntl.LOCK_EX); "
                "print('locked', flush=True); "
                "time.sleep(30)"
            ),
            str(output),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        result = subprocess.run(
            [sys.executable, str(BUILDER), "--output-dir", str(output)],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0
        assert "another release build is already writing" in result.stderr
        assert "Traceback" not in result.stderr
        assert list(output.iterdir()) == []
    finally:
        holder.terminate()
        holder.communicate(timeout=5)


def test_release_builder_reports_unreplaceable_output_without_traceback(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    archive_path = output / f"project-summarizer-{VERSION}.tar.gz"
    archive_path.mkdir()
    result = subprocess.run(
        [sys.executable, str(BUILDER), "--output-dir", str(output)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode != 0
    assert "release build failed: operating system error" in result.stderr
    assert "Traceback" not in result.stderr
    assert archive_path.is_dir()
    assert not (output / "SHA256SUMS").exists()
