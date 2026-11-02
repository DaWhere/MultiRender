"""Queue of rendering jobs run in parallel as separate qgis_process processes."""

import itertools
import json
import os
import re
import sys
import tempfile
import time
from collections import deque

from qgis.PyQt.QtCore import QObject, QProcess, QProcessEnvironment, pyqtSignal

from .i18n import message, tr
from .settings import MAX_PARALLEL
from .qgis_process_locator import IS_WIN, NO_WINDOW, build_command

MARK = "@@MR:"

PROGRESS_LINE = re.compile(r"^[\d.]+$")  # "0...10...20" lines printed by qgis_process
PARAM_LINE = re.compile(r"^[A-Z][A-Z0-9_]*:\s")  # echo of the input parameters ("RESULT_FILE: …")
CRASH_CODES = {0xC0000005: "access violation", 0xC0000409: "stack buffer overrun", 0xC00000FD: "stack overflow",
               0xC0000374: "heap corruption"}
QUEUED, RUNNING, DONE, FAILED, CANCELED = "queued", "running", "done", "failed", "canceled"


class BaseJob:
    """Job run as `qgis_process run <script> --PARAM=…`."""

    _ids = itertools.count(1)
    script = ""

    def __init__(self, project_path):
        self.id = next(self._ids)
        self.project_path = project_path
        self.status = QUEUED
        self.progress = 0
        self.files = []
        self.warnings = []
        self.result = {}
        self.error = ""
        self.output_tail = deque(maxlen=40)
        self.stage = None  # last step reported by the worker (payload)
        self.warning_codes = set()
        self.started = None
        self.finished = None
        self.row = None
        self.result_file = ""
        self._buffer = ""
        self._process = None

    def arguments(self):
        raise NotImplementedError

    @property
    def script_arg(self):
        """Script path for qgis_process.

        qgis_process pastes it into Python code without escaping, so on Windows
        sequences such as \\U (C:\\Users) or \\r (\\render_layout.py) would break the path.
        Windows accepts forward slashes without problems.
        """
        return os.path.abspath(self.script).replace("\\", "/")

    @property
    def label(self):
        return os.path.basename(self.project_path)

    @property
    def duration(self):
        if self.started is None:
            return 0.0
        return (self.finished or time.time()) - self.started

    @property
    def is_final(self):
        return self.status in (DONE, FAILED, CANCELED)


class RenderJob(BaseJob):
    def __init__(self, project_path, layout, fmt, dpi, atlas, output_base, options, script="",
                 pages="", part=1, parts=1, mode="render"):
        super().__init__(project_path)
        self.mode = mode  # "render" | "profile"
        self.pages = pages
        self.part = int(part)
        self.parts = int(parts)
        self.script = script
        self.source_project = project_path  # original (project_path may point to the cache)
        self.layout = layout
        self.fmt = fmt
        self.dpi = int(dpi)
        self.atlas = bool(atlas)
        self.output_base = output_base
        self.options = dict(options)

    @property
    def label(self):
        extra = ", " + tr("pages {pages}", pages=self.pages) if self.pages else ""
        piece = " – " + tr("part {part}/{parts}", part=self.part, parts=self.parts) if self.parts > 1 else ""
        return (f"{os.path.basename(self.source_project)} / {self.layout} "
                f"[{self.fmt.upper()}, {self.dpi} dpi{extra}]{piece}")

    def arguments(self):
        return [
            "run",
            self.script_arg,
            f"--PROJECT={self.project_path}",
            f"--LAYOUT={self.layout}",
            f"--FORMAT={self.fmt}",
            f"--DPI={self.dpi}",
            f"--OUTPUT_BASE={self.output_base}",
            f"--ATLAS={1 if self.atlas else 0}",
            f"--PAGES={self.pages}",
            f"--PART={self.part}",
            f"--PARTS={self.parts}",
            f"--MODE={self.mode}",
            f"--OPTIONS={json.dumps(self.options)}",
            f"--RESULT_FILE={self.result_file}",
        ]


class CacheJob(BaseJob):
    """Project cache: local copy of layers + copy of the project."""

    def __init__(self, project_path, cache_project, options, layouts, script=""):
        super().__init__(project_path)
        self.script = script
        self.cache_project = cache_project
        self.options = dict(options)
        self.layouts = sorted(layouts)  # only the printed layouts
        self.render_jobs = []

    @property
    def label(self):
        return tr("Cache: {name}", name=os.path.basename(self.project_path))

    def arguments(self):
        return [
            "run",
            self.script_arg,
            f"--PROJECT={self.project_path}",
            f"--OUTPUT_PROJECT={self.cache_project}",
            f"--LAYOUTS={json.dumps(self.layouts, ensure_ascii=False)}",
            f"--OPTIONS={json.dumps(self.options)}",
            f"--RESULT_FILE={self.result_file}",
        ]


class RenderManager(QObject):
    jobStarted = pyqtSignal(object)
    jobProgress = pyqtSignal(object)
    jobFinished = pyqtSignal(object)
    allFinished = pyqtSignal()
    message = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.qgis_process = ""
        self._queue = deque()
        self._running = {}
        self._jobs = []
        self._max = 1
        self._tmpdir = None
        self._active = False
        self._env = None

    # ------------------------------------------------------------- public
    def is_running(self):
        return bool(self._running or self._queue)

    def jobs(self):
        return list(self._jobs)

    def start(self, jobs, qgis_process, max_parallel):
        if self.is_running():
            raise RuntimeError("Rendering is already running")
        self.qgis_process = qgis_process
        self._max = max(1, min(MAX_PARALLEL, int(max_parallel)))
        self._jobs = list(jobs)
        self._active = True
        self._queue = deque(self._jobs)
        self._tmpdir = tempfile.mkdtemp(prefix="multirender_")
        self._env = self._environment()  # once per run, not per process
        for job in self._jobs:
            job.result_file = os.path.join(self._tmpdir, f"job_{job.id}.json")
        self._fill()

    def set_max_parallel(self, n):
        self._max = max(1, min(MAX_PARALLEL, int(n)))
        if self.is_running():
            self._fill()

    def cancel(self):
        while self._queue:
            job = self._queue.popleft()
            job.status = CANCELED
            job.error = tr("Canceled")
            self.jobFinished.emit(job)
        for job in list(self._running.values()):
            job.status = CANCELED
            job.error = tr("Canceled")
            proc = job._process
            if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
                proc.kill()
        # _on_finished completes the cleanup after the processes are killed
        if not self._running:
            self._finish_all()

    # ------------------------------------------------------------ internal
    def _fill(self):
        while self._queue and len(self._running) < self._max:
            self._launch(self._queue.popleft())
        if not self._queue and not self._running:
            self._finish_all()

    def _environment(self):
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        if sys.platform.startswith("linux") and not (env.contains("DISPLAY") or env.contains("WAYLAND_DISPLAY")):
            env.insert("QT_QPA_PLATFORM", "offscreen")
        return env

    def _launch(self, job):
        proc = QProcess(self)
        job._process = proc
        program, args = build_command(self.qgis_process, job.arguments())
        proc.setProgram(program)
        proc.setArguments(args)
        proc.setProcessEnvironment(self._env)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        if os.path.isdir(os.path.dirname(job.project_path)):
            proc.setWorkingDirectory(os.path.dirname(job.project_path))
        if IS_WIN:
            self._hide_console(proc)

        proc.readyReadStandardOutput.connect(lambda j=job: self._on_output(j))
        # *_ – works regardless of the selected overload of the finished signal
        proc.finished.connect(lambda *_a, j=job, p=proc: self._on_finished(j, p.exitCode(), p.exitStatus()))
        proc.errorOccurred.connect(lambda err, j=job: self._on_error(j, err))

        job.status = RUNNING
        job.started = time.time()
        self._running[job.id] = job
        self.jobStarted.emit(job)
        proc.start()

    @staticmethod
    def _hide_console(proc):
        """No flashing console windows on Windows (when PyQt provides it)."""
        try:
            def modifier(cpa):
                cpa.flags |= NO_WINDOW

            proc.setCreateProcessArgumentsModifier(modifier)
        except Exception:  # noqa: BLE001
            pass

    def _on_output(self, job):
        proc = job._process
        if proc is None:
            return
        data = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        job._buffer += data.replace("\r\n", "\n").replace("\r", "\n")
        *lines, job._buffer = job._buffer.split("\n")
        for line in lines:
            self._handle_line(job, line)

    def _handle_line(self, job, line):
        line = line.strip()
        if not line:
            return
        pos = line.find(MARK)
        if pos < 0:
            job.output_tail.append(line)
            return
        kind, _, value = line[pos + len(MARK):].partition(":")
        if kind == "progress":
            try:
                job.progress = int(value)
            except ValueError:
                return
            self.jobProgress.emit(job)
        elif kind == "stage":
            job.stage = self._payload(value)
        elif kind in ("warning", "info"):
            payload = self._payload(value)
            text = message(payload)
            if kind == "warning":
                if isinstance(payload, dict):
                    job.warning_codes.add(payload.get("c"))
                job.warnings.append(text)
                self.message.emit(f"⚠ {job.label}: {text}")
            else:
                self.message.emit(f"   {text}")

    @staticmethod
    def _payload(value):
        try:
            return json.loads(value)
        except ValueError:
            return value

    def _read_result(self, job):
        try:
            with open(job.result_file, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _on_error(self, job, err):
        if err == QProcess.ProcessError.FailedToStart and not job.is_final:
            job.status = FAILED
            job.error = tr("Cannot start qgis_process: {path}", path=self.qgis_process)
            self._complete(job)

    def _on_finished(self, job, code, status):
        if job._buffer:
            self._handle_line(job, job._buffer)
            job._buffer = ""
        if job.status == CANCELED:
            self._complete(job)
            return
        if job.is_final:
            return
        result = self._read_result(job)
        crashed = status == QProcess.ExitStatus.CrashExit
        if result and result.get("ok") and code == 0 and not crashed:
            job.status = DONE
            job.result = result
            job.files = result.get("files", [])
            job.warnings = [message(w) for w in result.get("warnings", [])] or job.warnings
            job.warning_codes |= {w.get("c") for w in result.get("warnings", []) if isinstance(w, dict)}
            job.progress = 100
        else:
            job.status = FAILED
            job.result = result or {}
            if result and result.get("error"):
                job.error = message(result["error"])
            else:
                job.error = self._no_result_error(job, code, crashed)
        self._complete(job)

    @staticmethod
    def _no_result_error(job, code, crashed):
        """Message for a process that ended without writing its result."""
        tail = [ln for ln in job.output_tail if not PROGRESS_LINE.match(ln) and not PARAM_LINE.match(ln)]
        keywords = ("error", "not a valid", "exception", "traceback", "failed")
        important = [ln for ln in tail if any(k in ln.lower() for k in keywords)]
        code = code & 0xFFFFFFFF if isinstance(code, int) else code
        reason = CRASH_CODES.get(code)
        code_text = f"0x{code:08X}" if isinstance(code, int) and code > 0xFFFF else str(code)
        if reason:
            code_text += f", {reason}"
        stage = message(job.stage) if job.stage else tr("starting qgis_process")
        text = tr("The process ended without a result (exit code {code}) – last step: {stage}",
                  code=code_text, stage=stage)
        if important:
            text += f" · {important[-1]}"
        elif crashed:
            text += " · " + tr("qgis_process crashed")
        return text

    def _complete(self, job):
        job.finished = time.time()
        self._running.pop(job.id, None)
        proc, job._process = job._process, None
        if proc is not None:
            proc.deleteLater()
        try:
            if job.result_file and os.path.exists(job.result_file):
                os.remove(job.result_file)
        except OSError:
            pass
        self.jobFinished.emit(job)
        self._fill()

    def _finish_all(self):
        if not self._active:
            return
        self._active = False
        if self._tmpdir:
            try:
                os.rmdir(self._tmpdir)
            except OSError:
                pass
            self._tmpdir = None
        self.allFinished.emit()
