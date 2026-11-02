"""Finding the qgis_process executable of the running QGIS installation."""

import glob
import os
import shutil
import subprocess
import sys

from qgis.core import QgsApplication

from .i18n import tr

IS_WIN = sys.platform.startswith("win")
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def _candidate_dirs():
    app_dir = QgsApplication.applicationDirPath()
    prefix = QgsApplication.prefixPath()
    dirs = []
    for base in (app_dir, prefix):
        if not base:
            continue
        dirs += [base, os.path.join(base, "bin")]
        p = base
        for _ in range(3):
            p = os.path.dirname(p)
            dirs.append(os.path.join(p, "bin"))
            if IS_WIN:
                dirs += sorted(glob.glob(os.path.join(p, "apps", "qgis*", "bin")))
    seen, out = set(), []
    for d in dirs:
        d = os.path.normpath(d)
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            out.append(d)
    return out


_FOUND = None  # search result kept for the QGIS session


def find_qgis_process(refresh=False):
    """Path to qgis_process or an empty string (the result is cached)."""
    global _FOUND
    if _FOUND and not refresh and os.path.isfile(_FOUND):
        return _FOUND  # an empty result is not cached – search again next time
    _FOUND = _search_qgis_process()
    return _FOUND


def _search_qgis_process():
    if IS_WIN:
        # .exe first: the process inherits the environment of the running QGIS,
        # while running a .bat through cmd.exe complicates passing arguments.
        names = ["qgis_process.exe"]
        bats = ["qgis_process-qgis-ltr.bat", "qgis_process-qgis.bat", "qgis_process-qgis-dev.bat"]
    else:
        names, bats = ["qgis_process"], []

    dirs = _candidate_dirs()
    for name in names + bats:
        for d in dirs:
            path = os.path.join(d, name)
            if os.path.isfile(path):
                return path
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    if IS_WIN:
        for d in dirs:
            hits = sorted(glob.glob(os.path.join(d, "qgis_process*.bat")))
            if hits:
                return hits[0]
    return ""


def build_command(qgis_process, args):
    """(program, arguments) – .bat/.cmd files are run through cmd.exe."""
    if IS_WIN and qgis_process.lower().endswith((".bat", ".cmd")):
        return os.environ.get("COMSPEC", "cmd.exe"), ["/c", qgis_process] + list(args)
    return qgis_process, list(args)


def test_qgis_process(path, timeout=90):
    """Runs `qgis_process --version`. Returns (ok, text)."""
    if not path or not os.path.isfile(path):
        return False, tr("File not found.")
    program, args = build_command(path, ["--version"])
    try:
        cp = subprocess.run(
            [program] + args,
            capture_output=True,
            timeout=timeout,
            creationflags=NO_WINDOW if IS_WIN else 0,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    text = (cp.stdout or b"").decode("utf-8", "replace") + (cp.stderr or b"").decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return cp.returncode == 0, "\n".join(lines[-8:]) or tr("exit code {code}", code=cp.returncode)
