#!/usr/bin/env python3
"""Build a deterministic Scan Dir release archive and checksum file."""

from __future__ import annotations

import argparse
import datetime
import errno
import fcntl
import gzip
import hashlib
import io
import os
import re
import stat
import tarfile
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

from verify_release import load_release_snapshot, verify_release

ROOT = Path(__file__).resolve().parents[1]
RELEASE_DATE_PATTERN = re.compile(r'^VERSION = "(\d{4}-\d{2}-\d{2})"$', re.MULTILINE)
PACKAGE_FILES: tuple[tuple[str, int], ...] = (
    ("LICENSE", 0o644),
    ("README.md", 0o644),
    ("SKILL.md", 0o644),
    ("config.yaml", 0o644),
    ("install.sh", 0o755),
    ("sdir.py", 0o755),
)


def fail(message: str) -> NoReturn:
    raise SystemExit(f"release build failed: {message}")


def runtime_release_date(snapshot: Mapping[str, bytes]) -> str:
    try:
        runtime_text = snapshot["sdir.py"].decode("utf-8")
    except KeyError:
        fail("release snapshot is missing sdir.py")
    except UnicodeError as exc:
        fail(f"sdir.py is not valid UTF-8: {exc}")
    match = RELEASE_DATE_PATTERN.search(runtime_text)
    if match is None:
        fail("runtime release identity is missing or is not a canonical ISO date YYYY-MM-DD")
    release_date = match.group(1)
    try:
        datetime.date.fromisoformat(release_date)
    except ValueError:
        fail(f"runtime release identity is not a real calendar date: {release_date}")
    return release_date


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_file(archive: tarfile.TarFile, package_root: str, filename: str, data: bytes, mode: int, epoch: int) -> None:
    info = tarfile.TarInfo(f"{package_root}/{filename}")
    info.size = len(data)
    info.mode = mode
    info.mtime = epoch
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    archive.addfile(info, io.BytesIO(data))


def prepare_output_directory(output_dir: Path) -> None:
    if os.path.lexists(output_dir):
        if output_dir.is_symlink() or not output_dir.is_dir():
            fail(f"output directory is not a real directory: {output_dir}")
        return
    output_dir.mkdir(parents=True, exist_ok=False)
    if output_dir.is_symlink() or not output_dir.is_dir():
        fail(f"output directory is not a real directory: {output_dir}")


@contextmanager
def locked_output_directory(output_dir: Path) -> Iterator[None]:
    """Hold an exclusive lock on the real output directory for one build."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(output_dir, flags)
    except OSError as exc:
        fail(f"unable to open output directory {output_dir}: {exc}")
    try:
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISDIR(info.st_mode):
                fail(f"output directory is not a real directory: {output_dir}")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail(f"another release build is already writing to: {output_dir}")
        except OSError as exc:
            fail(f"unable to secure output directory {output_dir}: {exc}")
        yield
    finally:
        os.close(descriptor)


def sync_directory(path: Path) -> None:
    """Persist directory-entry changes where the host filesystem supports it."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        try:
            os.fsync(descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL)}:
                raise
    finally:
        os.close(descriptor)


def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        sync_directory(path.parent)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def build_release(output_dir: Path) -> tuple[Path, Path]:
    snapshot = load_release_snapshot()
    verified_release_date = verify_release(snapshot)
    release_date = runtime_release_date(snapshot)
    if release_date != verified_release_date:
        fail(f"release verifier returned inconsistent release date: {verified_release_date}")
    parsed_release_date = datetime.date.fromisoformat(release_date)
    epoch = int(
        datetime.datetime.combine(parsed_release_date, datetime.time.min, tzinfo=datetime.timezone.utc).timestamp()
    )
    package_root = f"scan-dir-{release_date}"
    prepare_output_directory(output_dir)
    with locked_output_directory(output_dir):
        archive_path = output_dir / f"{package_root}.tar.gz"
        checksum_path = output_dir / "SHA256SUMS"

        for filename, _mode in PACKAGE_FILES:
            if filename not in snapshot:
                fail(f"release snapshot is missing {filename}")

        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{archive_path.name}.", dir=output_dir)
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as raw_output:
                with gzip.GzipFile(
                    filename="", mode="wb", compresslevel=9, fileobj=raw_output, mtime=epoch
                ) as compressed:
                    with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                        for filename, mode in PACKAGE_FILES:
                            add_file(archive, package_root, filename, snapshot[filename], mode, epoch)
                raw_output.flush()
                os.fsync(raw_output.fileno())
            archive_digest = sha256(temporary_path)
            os.replace(temporary_path, archive_path)
            sync_directory(output_dir)
        finally:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

        atomic_write(checksum_path, f"{archive_digest}  {archive_path.name}\n".encode("ascii"))
    return archive_path, checksum_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(os.path.abspath(os.fspath(args.output_dir)))
    try:
        archive_path, checksum_path = build_release(output_dir)
    except OSError as exc:
        fail(f"operating system error: {exc}")
    print(archive_path)
    print(checksum_path)


if __name__ == "__main__":
    main()
