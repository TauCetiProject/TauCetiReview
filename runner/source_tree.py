"""Private writable source copies, with optional filesystem extent sharing."""
import os
import shutil
import stat
import sys


def copy_file(src, dst):
    cloned = False
    if sys.platform == "linux" and stat.S_ISREG(os.stat(src).st_mode):
        import fcntl
        try:
            with open(src, "rb") as incoming, open(dst, "wb") as outgoing:
                fcntl.ioctl(outgoing.fileno(), 0x40049409, incoming.fileno())  # Linux FICLONE
            cloned = True
        except OSError:
            pass
    if not cloned:
        shutil.copyfile(src, dst)
    # Metadata errors stay visible; they must not trigger a second data copy into
    # a destination whose immutable source mode may already have been restored.
    try:
        shutil.copystat(src, dst)
    finally:
        os.chmod(dst, stat.S_IMODE(os.stat(dst).st_mode) | stat.S_IWUSR)
    return dst


def copy_tree(src, dst, *, ignore_extra=()):
    os.makedirs(dst)  # Refuse existing destinations before touching their permissions.
    try:
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True, copy_function=copy_file,
                        ignore=shutil.ignore_patterns(".git", *ignore_extra))
    finally:
        # copytree restores directory modes as it finishes each child. Repair partial
        # copies too, so normal cleanup and a private-fetch retry can remove them.
        for directory, _, _ in os.walk(dst):
            os.chmod(directory, stat.S_IMODE(os.stat(directory).st_mode) | stat.S_IRWXU)
