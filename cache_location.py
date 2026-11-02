"""Miejsce bufora projektu na dysku (folder tymczasowy systemu)."""

import os
import shutil
import tempfile
import time

SUBDIR = "multirender_cache"


def cache_root():
    return os.path.join(tempfile.gettempdir(), SUBDIR)


def cleanup_stale(root, max_age_hours=12):
    """Usuwa bufory po przerwanych sesjach."""
    if not os.path.isdir(root):
        return 0
    limit = time.time() - max_age_hours * 3600
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < limit:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed
