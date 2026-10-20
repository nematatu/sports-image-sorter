from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING
from uuid import uuid4

from .files import _outside_source, _read_file, protected_path, safe_source_file, sha256_file

if TYPE_CHECKING:
    from .core import Database


@dataclass(frozen=True)
class Content:
    sha256: str
    label: str
    rows: tuple[dict, ...]

    def describe(self) -> dict:
        labels = sorted({row["label"] for row in self.rows})
        return {"sha256": self.sha256, "label": self.label,
                "image_ids": [row["id"] for row in self.rows],
                "conflicting_labels": labels if len(labels) > 1 else []}


@dataclass(frozen=True)
class OutputPlan:
    contents: tuple[Content, ...]

    def describe(self) -> dict:
        contents = [content.describe() for content in self.contents]
        return {"source_images": sum(len(content.rows) for content in self.contents),
                "unique_images": len(contents),
                "conflicting_groups": sum(bool(content["conflicting_labels"]) for content in contents),
                "contents": contents}

    def signature(self) -> tuple:
        return tuple((content.sha256, content.label,
                      tuple((row["id"], row["label"], row["relpath"], row["size"], row["mtime_ns"])
                            for row in content.rows)) for content in self.contents)


def build_output_plan(rows: list[dict]) -> OutputPlan:
    groups = defaultdict(list)
    for row in rows:
        groups[row["sha256"]].append(row)
    contents = []
    for digest, aliases in sorted(groups.items()):
        labels = {row["label"] for row in aliases}
        label = next(iter(labels)) if len(labels) == 1 else "未分類"
        contents.append(Content(digest, label, tuple(sorted(aliases, key=lambda row: row["id"]))))
    return OutputPlan(tuple(contents))


@dataclass(frozen=True)
class OutputFile:
    relative: str
    sha256: str
    identity: tuple

    @property
    def is_partial(self) -> bool:
        name = Path(self.relative).name
        return name.startswith(".sports-sorter-") and name.endswith(".partial")

    @property
    def label(self) -> str | None:
        parts = Path(self.relative).parts
        return parts[0] if len(parts) == 2 else None


def _identity(path: Path) -> tuple:
    current = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode):
        raise ValueError("Output contains a symbolic link or non-regular file")
    return current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns


def _target(root: Path, relative: str) -> Path:
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError("Invalid relative path")
    return protected_path(root / part)


def _inventory(root: Path) -> list[OutputFile]:
    if not root.exists():
        return []
    if not root.is_dir():
        raise ValueError("Output must be a directory")
    files = []
    def onerror(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        for name in dirs:
            entry = Path(directory) / name
            if entry.is_symlink() or not entry.is_dir():
                raise ValueError("Output contains a symbolic link or special directory")
        for name in names:
            entry = protected_path(Path(directory) / name)
            before = _identity(entry)
            if name == ".DS_Store":
                continue
            digest = sha256_file(entry)
            if _identity(entry) != before:
                raise ValueError("Output changed during inventory")
            files.append(OutputFile(entry.relative_to(root).as_posix(), digest, before))
    return sorted(files, key=lambda file: file.relative)


def _output_path(db: Database, path: str | Path) -> Path:
    result = protected_path(path)
    _outside_source(result, db.source)
    if db.path == result or db.path in result.parents or result in db.path.parents:
        raise ValueError("Output and state database must not overlap")
    return result


@contextmanager
def _output_lock(path: Path):
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("Output lock must be a regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _source_intact(db: Database, row: dict) -> bool:
    try:
        source = safe_source_file(db, row)
        before = _identity(source)
        digest = sha256_file(source)
        return (before == _identity(source) and digest == row["sha256"]
                and before[2:] == (row["size"], row["mtime_ns"]))
    except (OSError, ValueError):
        return False


def _validate_sources(db: Database, plan: OutputPlan):
    if any(not _source_intact(db, row) for content in plan.contents for row in content.rows):
        raise ValueError("Source changed since scan; output was not reconciled")


def _select(plan: OutputPlan, files: list[OutputFile]) -> tuple[dict[str, OutputFile], list[OutputFile]]:
    wanted = {content.sha256: content.label for content in plan.contents}
    selected = {}
    extras = []
    for file in files:
        if (not file.is_partial and file.sha256 not in selected and file.sha256 in wanted
                and file.label == wanted[file.sha256]):
            selected[file.sha256] = file
        else:
            extras.append(file)
    return selected, extras


def _copy_content(db: Database, root: Path, content: Content) -> OutputFile:
    source = safe_source_file(db, content.rows[0])
    folder = protected_path(root / content.label)
    folder.mkdir(parents=True, exist_ok=True)
    serial = 0
    while True:
        tail = "" if serial == 0 else f"__{content.sha256[:12]}_{serial}"
        target = protected_path(folder / f"{source.stem}{tail}{source.suffix.lower()}")
        if not target.exists():
            break
        serial += 1
    temporary = protected_path(folder / f".sports-sorter-{uuid4().hex}.partial")
    with _read_file(source) as incoming, temporary.open("xb") as outgoing:
        for block in iter(lambda: incoming.read(1024 * 1024), b""):
            outgoing.write(block)
        outgoing.flush()
        os.fsync(outgoing.fileno())
    if sha256_file(temporary) != content.sha256 or not _source_intact(db, content.rows[0]):
        raise ValueError("Copy/source integrity check failed; partial output was retained")
    # link() refuses an existing destination. Only this run's temporary link is removed.
    os.link(temporary, target, follow_symlinks=False)
    temporary.unlink()
    return OutputFile(target.relative_to(root).as_posix(), sha256_file(target), _identity(target))


def _archive_files(db: Database, root: Path, extras: list[OutputFile]) -> Path | None:
    if not extras:
        return None
    archive = _output_path(db, root.with_name(f"{root.name}-archive-{uuid4().hex}"))
    archive.mkdir(mode=0o700)
    for file in extras:
        source = _target(root, file.relative)
        if _identity(source) != file.identity or sha256_file(source) != file.sha256:
            raise ValueError("Output changed before archival; reconciliation stopped")
        destination = _target(archive, file.relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError("Archive destination already exists")
        os.rename(source, destination)
        with db.conn:
            db.conn.execute("UPDATE copies SET output_root=?,status='archived' WHERE output_root=? AND target_relpath=?",
                            (str(archive), str(root), file.relative))
    return archive


def copy_images(db: Database, output: str | Path, engine_key: str | None = None, dry_run: bool = False) -> dict:
    from .core import effective_rows

    root = _output_path(db, output)
    lock = _output_path(db, root.with_name(f".{root.name}.sports-sorter.lock"))
    plan = build_output_plan(effective_rows(db, engine_key))
    _validate_sources(db, plan)
    selected, extras = _select(plan, _inventory(root))
    if dry_run:
        return {**plan.describe(), "would_copy": len(plan.contents) - len(selected),
                "existing_verified": len(selected), "would_archive": len(extras)}
    root.parent.mkdir(parents=True, exist_ok=True)
    with _output_lock(lock):
        _validate_sources(db, plan)
        selected, extras = _select(plan, _inventory(root))
        existing = len(selected)
        for content in plan.contents:
            if content.sha256 not in selected:
                selected[content.sha256] = _copy_content(db, root, content)
        _validate_sources(db, plan)
        if build_output_plan(effective_rows(db, engine_key)).signature() != plan.signature():
            raise ValueError("Classification changed during copy; rerun before archival")
        for content in plan.contents:
            if sha256_file(_target(root, selected[content.sha256].relative)) != content.sha256:
                raise ValueError("Canonical output changed; prior copies were retained")
        archive = _archive_files(db, root, extras)
        _validate_sources(db, plan)
        if build_output_plan(effective_rows(db, engine_key)).signature() != plan.signature():
            raise ValueError("Classification changed during archival; rerun reconciliation")
        final_selected, remaining = _select(plan, _inventory(root))
        if remaining or len(final_selected) != len(plan.contents):
            raise ValueError("Output changed during reconciliation; verification failed")
        with db.conn:
            db.conn.execute("UPDATE copies SET status='missing' WHERE output_root=?", (str(root),))
            for content in plan.contents:
                file = selected[content.sha256]
                db.conn.execute("""INSERT INTO copies (image_id,label,output_root,target_relpath,sha256,status)
                    VALUES (?,?,?,?,?,'complete') ON CONFLICT(output_root,target_relpath) DO UPDATE SET
                    image_id=excluded.image_id,label=excluded.label,sha256=excluded.sha256,status='complete'""",
                    (content.rows[0]["id"], content.label, str(root), file.relative, content.sha256))
        return {**plan.describe(), "copied": len(selected) - existing, "existing_verified": existing,
                "archived": len(extras), "archive": str(archive) if archive else None}


def verify(db: Database, output: str | Path, engine_key: str | None = None) -> dict:
    from .core import effective_rows

    root = _output_path(db, output)
    plan = build_output_plan(effective_rows(db, engine_key))
    originals = {row["id"]: _source_intact(db, row) for content in plan.contents for row in content.rows}
    try:
        files = _inventory(root)
    except (OSError, ValueError) as error:
        return {**plan.describe(), "ok": False, "images": len(originals),
                "output_errors": [type(error).__name__],
                "originals_unchanged": sum(originals.values()),
                "originals_changed_or_missing": sum(not intact for intact in originals.values())}
    selected, extras = _select(plan, files)
    by_digest = defaultdict(list)
    for file in files:
        by_digest[file.sha256].append(file)
    problems = [row["id"] for content in plan.contents for row in content.rows
                if not originals[row["id"]] or content.sha256 not in selected]
    covered = sum(len(content.rows) for content in plan.contents if content.sha256 in selected)
    return {**plan.describe(), "ok": not problems and not extras, "images": len(originals),
            "originals_unchanged": sum(originals.values()),
            "originals_changed_or_missing": sum(not intact for intact in originals.values()),
            "output_files": len(files), "verified_copies": len(selected),
            "covered_images": covered, "missing_images": len(originals) - covered,
            "duplicate_copies": sum(len(group) - 1 for group in by_digest.values()),
            "cross_label_groups": sum(len({file.label for file in group}) > 1 for group in by_digest.values()),
            "unexpected_files": [file.relative for file in extras], "problem_image_ids": problems}
