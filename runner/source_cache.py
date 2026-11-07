"""Optional exact Mathlib sources. Stable per-pin flocks serialize selection and GC.

Readers take SH before looking up the pathname; publishers and collectors take EX.
All acquisitions are nonblocking. No global lock or network operation is needed here:
misses use the normal private fetch, which may then seed this cache. A lease covers
only the private source copy, never inference. Old Worker snapshot collectors do not
enumerate this namespace. No lock inode is ever unlinked.
"""
import contextlib
try:
    import fcntl
except ImportError:  # Cache-disabled Review remains usable on platforms without flock.
    fcntl = None
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time

try:
    from .source_tree import copy_tree
except ImportError:
    from source_tree import copy_tree

URL = "https://github.com/leanprover-community/mathlib4"
# Match the existing shared-repository publication-age policy, not time since use.
PUBLICATION_AGE = 86400
PIN = re.compile(r"[0-9a-f]{40}")
OWNER = dict(version=1, uid=getattr(os, "getuid", lambda: None)(), repository=URL)


def _supported():
    return fcntl is not None and hasattr(os, "getuid") and hasattr(os, "O_NOFOLLOW")


def _notice(error):
    print(f"tauceti-review: optional Mathlib source cache unavailable: {error}", file=sys.stderr)


def _directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError(f"not an owned cache directory: {path}")


def _root(directory, *, create=False):
    if not Path(directory).is_absolute():
        raise ValueError("source cache path must be absolute")
    root = Path(directory) / "exact-sources-v1"
    if create:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _directory(root)
    if root.stat().st_mode & 0o022:
        raise ValueError("source cache must not be writable by other users")
    # An empty ownership directory is an atomic record: no partially written JSON
    # or temporary file can prevent recovery after a crash during initialization.
    marker = root / ("owner-" + str(os.getuid()))
    if not marker.exists() and create:
        if any(root.iterdir()) and not marker.is_dir():
            raise ValueError(f"refusing to adopt nonempty source cache: {root}")
        marker.mkdir(exist_ok=True, mode=0o700)
        _sync(root)
    _directory(marker)
    return root


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _lock(root, pin, *, write=False, create=True):
    if fcntl is None:
        raise OSError("source cache requires file locks")
    locks = root / "locks"
    if create:
        locks.mkdir(exist_ok=True, mode=0o700)
    _directory(locks)
    flags = os.O_NOFOLLOW | (os.O_CREAT | os.O_RDWR if create else os.O_RDONLY)
    fd = os.open(locks / pin, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise ValueError("unrecognized source cache lock")
        fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _owned(entry, pin):
    _directory(entry)
    # These reserved names belong to this marked cache namespace. A crash may
    # precede writing staging readiness, or follow deleting readiness from trash.
    if entry.name in (pin + ".staging", pin + ".trash"):
        return True
    marker = entry / "ready.json"
    return (not marker.is_symlink()
            and json.loads(marker.read_text()) == dict(OWNER, revision=pin))


def _ready(entry, pin):
    return (_owned(entry, pin) and not (entry / "source").is_symlink()
            and (entry / "source/Mathlib.lean").is_file()
            and (entry / "source/Mathlib").is_dir())


def _remove(entry):
    # Only our owned staging/trash is passed here, with this pin held exclusively.
    for directory, _, _ in os.walk(entry):
        os.chmod(directory, 0o700)
    shutil.rmtree(entry)


@contextlib.contextmanager
def borrowed(directory, pin):
    """Yield an immutable source or None; caller errors during copying remain visible."""
    held = contextlib.ExitStack()
    source = None
    root = None
    try:
        if directory and _supported() and PIN.fullmatch(pin):
            root = _root(directory)
            held.enter_context(_lock(root, pin))
            entry = root / pin
            if _ready(entry, pin):
                source = entry / "source"
    except FileNotFoundError:
        pass
    except BlockingIOError:
        pass
    except (OSError, ValueError) as error:
        _notice(error)
    if source is None:
        held.close()
    try:
        yield source
    finally:
        held.close()
        if root is not None:
            for error in collect(directory)["errors"]:
                _notice(error)


def publish(directory, pin, checkout):
    """Best effort seeding from an already fetched private exact checkout; no network."""
    if not directory or not _supported() or not PIN.fullmatch(pin):
        return
    try:
        head = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
        dirty = subprocess.check_output(["git", "-C", str(checkout), "status", "--porcelain", "--ignored"], text=True)
        if head != pin or dirty:
            raise ValueError("private Mathlib source is not the clean exact commit")
        root = _root(directory, create=True)
        with _lock(root, pin, write=True):
            entry = root / pin
            if entry.exists():
                return  # Never replace a published tree, including an invalid one.
            for suffix in ("staging", "trash"):
                old = root / (pin + "." + suffix)
                if old.exists():
                    if not _owned(old, pin):
                        raise ValueError(f"unrecognized source cache staging/trash: {old}")
                    _remove(old)
            stage = root / (pin + ".staging")
            stage.mkdir(mode=0o700)
            try:
                (stage / "ready.json").write_text(json.dumps(dict(OWNER, revision=pin)))
                copy_tree(checkout, stage / "source")
                if not _ready(stage, pin):
                    raise ValueError("Mathlib source is incomplete")
                for directory_path, dirs, files in os.walk(stage, topdown=False):
                    for name in dirs + files:
                        path = Path(directory_path) / name
                        if path.is_symlink():
                            try:
                                resolved = path.resolve()
                            except RuntimeError as error:
                                raise ValueError("source contains a symlink loop") from error
                            if not resolved.is_relative_to((stage / "source").resolve()):
                                raise ValueError("source symlink leaves the immutable tree")
                        elif path.is_file():
                            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
                            _sync(path)
                    Path(directory_path).chmod(0o555)
                    _sync(Path(directory_path))
                stage.rename(entry)
                _sync(root)
            finally:
                if stage.exists():
                    _remove(stage)
    except BlockingIOError:
        pass
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        _notice(error)


def collect(directory, *, now=None, dry=False):
    """Collect owned, unleased old publications and interrupted staging/trash."""
    result = dict(removed=[], protected=[], errors=[])
    if not directory or not _supported():
        return result
    try:
        root = _root(directory)
    except FileNotFoundError:
        return result
    except (OSError, ValueError) as error:
        result["errors"].append(str(error))
        return result
    now = time.time() if now is None else now
    try:
        entries = list(root.iterdir())
    except OSError as error:
        result["errors"].append(str(error))
        return result
    for entry in entries:
        pin, _, suffix = entry.name.partition(".")
        if not PIN.fullmatch(pin) or suffix not in ("", "staging", "trash"):
            continue
        try:
            # Avoid exclusive-lock churn on young hot entries; recheck after locking.
            if not suffix and now - entry.lstat().st_mtime <= PUBLICATION_AGE:
                continue
            with _lock(root, pin, write=True, create=not dry):
                if not _owned(entry, pin):
                    raise ValueError(f"unrecognized source cache entry: {entry}")
                if not suffix and now - entry.stat().st_mtime <= PUBLICATION_AGE:
                    continue
                if not dry:
                    trash = root / (pin + ".trash")
                    if entry != trash:
                        if trash.exists():
                            if not _owned(trash, pin):
                                raise ValueError(f"unrecognized source cache trash: {trash}")
                            _remove(trash)
                        entry.rename(trash)
                        _sync(root)
                    _remove(trash)
                result["removed"].append(str(entry))
        except FileNotFoundError:
            continue
        except BlockingIOError:
            result["protected"].append(str(entry))
        except (OSError, ValueError) as error:
            result["errors"].append(str(error))
    return result
