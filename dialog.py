"""Main plugin window: layouts from the Layout Manager, parameters, rendering.

The list is synchronised with the Layout Manager of the current project (adding,
removing or renaming a layout refreshes the table). Layouts from other project
files can be added as well.
"""

import hashlib
import json
import os
import re
import shutil
import time
from contextlib import contextmanager
from datetime import datetime

from qgis.core import Qgis, QgsApplication, QgsProject
from qgis.PyQt.QtCore import QRegularExpression, Qt, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices, QFont, QRegularExpressionValidator
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import settings as S
from . import ui
from .cache_location import cache_root, cleanup_stale
from .i18n import message, num, tr
from .live_cache import LiveCache
from .outputs_panel import OutputsPanel, human_size
from .perf_analysis import run_summary
from .profile_report import ProfileReport
from .project_reader import LayoutInfo, find_projects, is_project_file, read_layouts
from .qgis_process_locator import find_qgis_process
from .runner import CANCELED, DONE, FAILED, RUNNING, CacheJob, RenderJob, RenderManager
from .settings_dialog import SettingsDialog
from .worker.prepare_cache import _is_remote
from .worker.render_layout import parse_pages

C_LAYOUT, C_PROJECT, C_FORMAT, C_DPI, C_ATLAS, C_PAGES, C_PARTS, C_OUTDIR, C_STATUS = range(9)
PAGES_RE = r"^\s*(\d*\s*-?\s*\d*)(\s*,\s*\d*\s*-?\s*\d*)*\s*$"
INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
CURRENT = None  # project_path of rows from the current project
ROW_KEYS = ("format", "dpi", "atlas", "pages", "parts", "output_dir", "enabled")
MISSING_LAYERS_TEXT = (
    "Some layers were unavailable in the rendering processes – {n} print(s) may be missing data. Layers that "
    "work in QGIS but not in the processes usually need a login kept only in this session (master password, "
    "password typed when connecting). With the project cache on, vector layers of the current project are "
    "copied from this session, so they are not affected."
)


def columns():
    return [tr("Layout"), tr("Project"), tr("Format"), "DPI", tr("Atlas"), tr("Pages"), tr("Processes"),
            tr("Output folder"), tr("Status")]


def icon(name):
    return QgsApplication.getThemeIcon(name)


def safe_name(text):
    text = INVALID_CHARS.sub("_", text).strip().strip(".")
    return text or "map"


def same_path(a, b):
    return bool(a) and bool(b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


COPY_SUFFIX = re.compile(r"^(.*\S)\s+\((\d+)\)$")


def copy_name(name, existing):
    """Next free copy name: “name (1)”, “name (2)”… – a copy of a copy continues the same series."""
    match = COPY_SUFFIX.match(name)
    base = match.group(1) if match and match.group(1) in existing else name
    n = 1
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def live_layouts():
    """Print layouts from the Layout Manager of the current project."""
    manager = QgsProject.instance().layoutManager()
    try:
        layouts = manager.printLayouts()
    except AttributeError:
        layouts = [lay for lay in manager.layouts() if hasattr(lay, "atlas")]
    out = []
    for lay in layouts:
        try:
            atlas = bool(lay.atlas() and lay.atlas().enabled())
        except Exception:  # noqa: BLE001
            atlas = False
        try:
            dpi = int(lay.renderContext().dpi())
        except Exception:  # noqa: BLE001
            dpi = 0
        try:
            pages = lay.pageCollection().pageCount()
        except Exception:  # noqa: BLE001
            pages = 0
        features = -1
        if atlas:
            try:
                cov = lay.atlas().coverageLayer()
                # counting features in PostGIS/WFS is a server query – skipped so that
                # refreshing the list never blocks QGIS
                if cov is not None and not _is_remote(cov):
                    features = cov.featureCount()
            except Exception:  # noqa: BLE001
                features = -1
        out.append(LayoutInfo(name=lay.name(), atlas=atlas, dpi=dpi, pages=pages, features=features))
    return sorted(out, key=lambda li: li.name.lower())


class TaskRow:
    """One table row = one layout + its export parameters."""

    def __init__(self, layout, project_path=CURRENT):
        self.layout = layout
        self.project_path = project_path  # None = current project
        self.jobs = []
        self.item = None
        self.project_item = None
        self.format_combo = None
        self.dpi_spin = None
        self.atlas_check = None
        self.pages_edit = None
        self.parts_spin = None
        self.outdir_item = None
        self.status_bar = None
        self.copy_no = 0  # 0 = the original row, n = “name (n)” – a duplicated row of the same layout

    @property
    def label(self):
        return f"{self.layout.name} ({self.copy_no})" if self.copy_no else self.layout.name

    @property
    def is_current(self):
        return self.project_path is CURRENT

    @property
    def enabled(self):
        return self.item.checkState() == Qt.CheckState.Checked

    def values(self):
        return {
            "project": self.project_path,
            "layout": self.layout.name,
            "format": self.format_combo.currentData(),
            "dpi": self.dpi_spin.value(),
            "atlas": self.atlas_check.isChecked(),
            "pages": self.pages_edit.text().strip(),
            "parts": self.parts_spin.value(),
            "output_dir": self.outdir_item.text(),
            "enabled": self.enabled,
            "copy": self.copy_no,
        }


class MainDialog(QDialog):
    settingsSaved = pyqtSignal(bool)  # argument: the language was changed

    def __init__(self, iface, plugin_dir, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.rows = []
        self._render_script = os.path.join(plugin_dir, "worker", "render_layout.py")
        self._cache_script = os.path.join(plugin_dir, "worker", "prepare_cache.py")
        self.manager = RenderManager(self)
        self.manager.jobStarted.connect(self._on_job_started)
        self.manager.jobProgress.connect(self._on_job_progress)
        self.manager.jobFinished.connect(self._on_job_finished)
        self.manager.allFinished.connect(self._on_all_finished)
        self.manager.message.connect(self.log)
        self._run_started = 0.0
        self._last_dir = ""
        self._sync_pending = False
        self._phase = None  # None | "cache" | "render"
        self._render_jobs = []
        self._cache_dirs = []
        self._cache_seconds = 0.0
        self._qp = ""
        self._pending_values = {}  # settings for copies of layouts
        self._loading = False  # True while widgets are filled in code (no "remember" writes)
        self._batch = 0  # >0: many table changes at once
        self._row_memory = None  # remembered layout settings (loaded lazily)
        self._follow_last = S.get("defaults_follow_last")
        self._dirty_rows = set()
        self._connections = []
        self._profile_row = None
        self._profile_job = None
        self._profile_cache_dir = None
        self._cache_jobs = []  # qgis_process cache jobs of this run (other projects)
        self._live = None  # cache of the current project built in this session
        self._cache_pending = 0
        self._live_count = 0
        self._missing_prints = 0
        # progress from the processes comes very often – the interface refreshes every 150 ms
        self._ui_timer = QTimer(self)
        self._ui_timer.setSingleShot(True)
        self._ui_timer.setInterval(150)
        self._ui_timer.timeout.connect(self._flush_ui)
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(150)
        self._sync_timer.timeout.connect(self.sync_current_project)

        self.setWindowTitle(tr("MultiRender – parallel map rendering"))
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint)
        self.resize(1300, 720)
        self.setAcceptDrops(True)
        self._build_ui()
        self._connect_project()
        self.sync_current_project()

    def is_busy(self):
        return self._phase is not None

    # ============================================== sync with the project
    def _connect_project(self):
        prj = QgsProject.instance()
        lm = prj.layoutManager()
        self._connections = [
            (lm.layoutAdded, self._schedule_sync),
            (lm.layoutRemoved, self._schedule_sync),
            (lm.layoutRenamed, self._schedule_sync),
            (prj.readProject, self._schedule_sync),
            (prj.cleared, self._schedule_sync),
            (prj.fileNameChanged, self._update_project_labels),
        ]
        for sig, slot in self._connections:
            sig.connect(slot)

    def _disconnect_project(self):
        for sig, slot in self._connections:
            try:
                sig.disconnect(slot)
            except (TypeError, RuntimeError):
                pass
        self._connections = []

    def _schedule_sync(self, *_args):
        if self.is_busy():
            self._sync_pending = True
        else:
            self._sync_timer.start()

    def showEvent(self, event):  # noqa: N802
        # e.g. enabling the atlas in the layout designer emits no signal – refresh on show
        super().showEvent(event)
        self._schedule_sync()

    def sync_current_project(self):
        """Rebuilds the rows of the current project, keeping their settings."""
        if self.is_busy():
            self._sync_pending = True
            return
        previous = {}
        for row in self.rows:
            if row.is_current:
                previous.setdefault(row.layout.name, []).append(row.values())
        with self._batch_updates():
            self._remove_current_rows()
            defaults = self._defaults()
            pos = 0
            for info in live_layouts():
                pending = self._pending_values.pop(info.name, None)
                fresh = [pending] if pending else [self._remembered(CURRENT, info.name, defaults)]
                for values in previous.get(info.name) or fresh:
                    self._insert_row(TaskRow(info), values, pos)
                    pos += 1
            self._update_project_labels()

    @contextmanager
    def _batch_updates(self):
        """Many table changes = one repaint and one summary update."""
        self._batch += 1
        if self._batch == 1:
            self.table.setUpdatesEnabled(False)
        try:
            yield
        finally:
            self._batch -= 1
            if self._batch == 0:
                self.table.setUpdatesEnabled(True)
                self._update_summary()

    @contextmanager
    def _quiet_edits(self):
        """Changing many rows at once: no settings write for every widget."""
        loading, self._loading = self._loading, True
        try:
            with self._batch_updates():
                yield
        finally:
            self._loading = loading

    def _remove_current_rows(self):
        for i in reversed(range(len(self.rows))):
            if self.rows[i].is_current:
                self.table.removeRow(i)
                del self.rows[i]

    def _update_project_labels(self, *_args):
        prj = QgsProject.instance()
        path = prj.fileName()
        if path:
            label = tr("Current · {name}", name=os.path.basename(path))
            tip = path + ("\n" + tr("(unsaved changes – you will be asked to save)") if prj.isDirty() else "")
        else:
            label, tip = tr("Current · unsaved"), tr("Save the project before rendering")
        for row in self.rows:
            if row.is_current:
                row.project_item.setText(label)
                row.project_item.setToolTip(tip)

    # ================================================================== UI
    @staticmethod
    def _button(text, slot, icon_name=None, tip=None):
        b = QToolButton()
        b.setText(text)
        b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        if icon_name:
            b.setIcon(icon(icon_name))
        if tip:
            b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _build_ui(self):
        root = QVBoxLayout(self)

        # --- toolbar
        self.toolbar_w = QWidget()
        bar = QHBoxLayout(self.toolbar_w)
        bar.setContentsMargins(0, 0, 0, 0)
        self.btn_refresh = self._button(tr("Refresh"), self.sync_current_project, "/mActionRefresh.svg",
                                        tr("Reload the list from the Layout Manager"))
        self.btn_manager = self._button(tr("Layout Manager"), self.open_layout_manager, "/mActionLayoutManager.svg")
        self.btn_edit = self._button(tr("Editor"), self.open_designer_selected, "/mActionEditNodesItem.svg",
                                     tr("Open the selected layout in the layout designer (or double-click its name)"))
        self.btn_copy = self._button(tr("Copy layout"), self.copy_layout_selected, "/mActionEditCopy.svg",
                                     tr("Create a copy of the layout in the project (as in the Layout Manager)"))
        self.btn_profile = self._button(tr("Analyze performance"), self.profile_selected, "/mActionStatisticalSummary.svg",
                                        tr("Measure where the rendering of the selected layout spends its time"))
        self.btn_all = self._button(tr("Select all"), lambda: self._check_all(True))
        self.btn_none = self._button(tr("Select none"), lambda: self._check_all(False))
        self.btn_dup = self._button(tr("Duplicate"), self.duplicate_selected, "/mActionDuplicateLayer.svg",
                                    tr("Copy the selected rows – e.g. the same map in another format"))
        self.btn_remove = self._button(tr("Remove"), self.remove_selected, "/mActionRemove.svg",
                                       tr("Delete the ticked layouts (the checkbox next to the name) from the Layout "
                                          "Manager of the current project. Rows of other projects are only removed "
                                          "from the list."))
        self.btn_add = self._button(tr("Other projects…"), self.add_projects_dialog, "/mActionFileOpen.svg",
                                    tr("Add layouts from other .qgs/.qgz files"))
        self.btn_add_dir = self._button(tr("From folder…"), self.add_folder_dialog, "/mActionNewFolder.svg")
        self.btn_load = self._button(tr("Load list…"), self.load_list, "/mActionFileOpen.svg")
        self.btn_save = self._button(tr("Save list…"), self.save_list, "/mActionFileSave.svg")
        self.btn_settings = self._button(tr("Settings…"), self.open_settings, "/mActionOptions.svg")
        for w in (self.btn_refresh, self.btn_manager, self.btn_edit, self.btn_copy, self.btn_profile, self.btn_all,
                  self.btn_none, self.btn_dup, self.btn_remove):
            bar.addWidget(w)
        bar.addStretch(1)
        for w in (self.btn_add, self.btn_add_dir, self.btn_load, self.btn_save, self.btn_settings):
            bar.addWidget(w)
        root.addWidget(self.toolbar_w)

        # --- bulk edit
        self.bulk_w = QWidget()
        bulk = QHBoxLayout(self.bulk_w)
        bulk.setContentsMargins(0, 0, 0, 0)
        bulk.addWidget(QLabel(tr("For selected:")))
        self.bulk_format = QComboBox()
        self.bulk_format.addItem(tr("— format —"), None)
        for key, label, _ in S.FORMATS:
            self.bulk_format.addItem(label, key)
        bulk.addWidget(self.bulk_format)
        self.bulk_dpi = QSpinBox()
        self.bulk_dpi.setRange(0, 2400)
        self.bulk_dpi.setSingleStep(50)
        self.bulk_dpi.setSpecialValueText(tr("— dpi —"))
        self.bulk_dpi.setSuffix(" dpi")
        bulk.addWidget(self.bulk_dpi)
        self.bulk_parts = QSpinBox()
        self.bulk_parts.setRange(0, S.MAX_PARALLEL)
        self.bulk_parts.setSpecialValueText(tr("— processes —"))
        self.bulk_parts.setPrefix("× ")
        self.bulk_parts.setToolTip(tr("Split the layout into parallel processes"))
        bulk.addWidget(self.bulk_parts)
        b = QPushButton(tr("Apply"))
        b.clicked.connect(self.apply_bulk)
        bulk.addWidget(b)
        b = QPushButton(tr("Folder…"))
        b.clicked.connect(self.bulk_output_dir)
        bulk.addWidget(b)
        b = QPushButton(tr("Restore defaults"))
        b.setToolTip(tr("Format, DPI, atlas, processes and folder from the default settings"))
        b.clicked.connect(self.apply_defaults_to_selected)
        bulk.addWidget(b)
        bulk.addStretch(1)
        root.addWidget(self.bulk_w)

        # --- table + outputs panel + log
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, C_STATUS + 1)
        self.table.setHorizontalHeaderLabels(columns())
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(C_LAYOUT, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(C_PROJECT, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(C_OUTDIR, QHeaderView.ResizeMode.Stretch)
        for c in (C_FORMAT, C_DPI, C_ATLAS):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setColumnWidth(C_PROJECT, 200)
        self.table.setColumnWidth(C_STATUS, 190)
        self.table.cellDoubleClicked.connect(self._on_double_click)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        self.table.itemSelectionChanged.connect(self._update_buttons)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.table, 1)
        self.summary = QLabel()
        ll.addWidget(self.summary)

        self.outputs = OutputsPanel(self.iface)
        self.outputs.message.connect(self.log)
        hsplit = QSplitter(Qt.Orientation.Horizontal)
        hsplit.addWidget(left)
        hsplit.addWidget(self.outputs)
        hsplit.setStretchFactor(0, 3)
        hsplit.setStretchFactor(1, 1)
        hsplit.setSizes([820, 330])
        split.addWidget(hsplit)

        mono = QFont("Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFont(mono)
        split.addWidget(self.log_view)
        split.setStretchFactor(0, 4)
        split.setStretchFactor(1, 1)
        root.addWidget(split, 1)

        hint = QLabel(tr("The list comes from the Layout Manager of the current project and refreshes "
                         "automatically. Double-click the name – layout designer, the folder column – change "
                         "the folder, the status column – open the results. Right-click – more options."))
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid);")
        root.addWidget(hint)

        # --- bottom bar
        bottom = QHBoxLayout()
        # the number of parallel processes is shown only – it is changed in the settings
        self.parallel_label = QLabel()
        self.parallel_label.setToolTip(tr("Number of processes running at the same time – change it in "
                                          "Settings → General."))
        self._show_parallel()
        bottom.addWidget(self.parallel_label)
        self.remember = QCheckBox(tr("Remember settings"))
        self.remember.setToolTip(tr("Format, DPI, pages, processes and folder of every layout come back "
                                    "after reopening the project or QGIS."))
        self.remember.setChecked(S.get("remember"))
        self.remember.toggled.connect(self._on_remember_toggled)
        bottom.addWidget(self.remember)
        self.total = QProgressBar()
        self.total.setRange(0, 1000)
        self.total.setFormat("")
        bottom.addWidget(self.total, 1)
        # clock of the whole run (cache + rendering), ticks every second
        self.clock = QLabel("⏱ 00:00")
        self.clock.setToolTip(tr("Time of the whole run: project cache + rendering"))
        self.clock.setFont(mono)
        self.clock.setMinimumWidth(self.clock.fontMetrics().horizontalAdvance("⏱ 00:00:00") + 8)
        bottom.addWidget(self.clock)
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(1000)
        self._clock_timer.timeout.connect(self._tick_clock)
        self.btn_open = QPushButton(icon("/mActionFileOpen.svg"), tr("Open folder"))
        self.btn_open.clicked.connect(self.open_output_dir)
        bottom.addWidget(self.btn_open)
        self.btn_cancel = QPushButton(tr("Stop"))
        self.btn_cancel.clicked.connect(self.cancel)
        bottom.addWidget(self.btn_cancel)
        self.btn_run = QPushButton(icon("/mActionStart.svg"), tr("Render"))
        self.btn_run.setDefault(True)
        self.btn_run.clicked.connect(self.run)
        bottom.addWidget(self.btn_run)
        root.addLayout(bottom)

    # ======================================================= remembering
    @staticmethod
    def _memory_key(project_path, layout_name):
        path = project_path if project_path is not CURRENT else QgsProject.instance().fileName()
        if not path:
            return None
        return f"{os.path.normcase(os.path.abspath(path))}::{layout_name}"

    def _memory(self):
        if self._row_memory is None:
            self._row_memory = S.load_row_memory()
        return self._row_memory

    def _remembered(self, project_path, layout_name, defaults):
        """Remembered settings of the layout, or the defaults for new projects."""
        values = dict(defaults)
        if self.remember.isChecked():
            key = self._memory_key(project_path, layout_name)
            saved = self._memory().get(key) if key else None
            if saved:
                values.update({k: v for k, v in saved.items() if k in values})
        return values

    def _remember_default(self, key, value):
        """A value chosen in the table becomes the default for new projects (if enabled)."""
        if self._follow_last and value and not self._loading:
            S.set(key, value)

    def _on_item_changed(self, item):
        if item.column() == C_OUTDIR and item.text().strip():
            self._remember_default("output_dir", item.text().strip())
        self._update_summary()

    def _on_remember_toggled(self, on):
        S.set("remember", on)
        if on:
            self._save_rows_memory()

    def _save_rows_memory(self):
        if not self.remember.isChecked():
            return
        data = self._memory()
        seen = set()
        for row in self.rows:
            key = self._memory_key(row.project_path, row.layout.name)
            if not key or key in seen:  # duplicated rows – the first one is remembered
                continue
            seen.add(key)
            values = row.values()
            data.pop(key, None)  # move to the end (the newest)
            data[key] = {k: values[k] for k in ROW_KEYS}
        self._row_memory = S.save_row_memory(data)

    @staticmethod
    def _defaults():
        return {
            "format": S.get("format"),
            "dpi": S.get("dpi"),
            "atlas": S.get("use_atlas"),
            "output_dir": S.get("output_dir"),
            "pages": "",
            "parts": S.get("parts"),
            "enabled": True,
        }

    # ================================================================ rows
    def _insert_row(self, row, values, position=None):
        loading, self._loading = self._loading, True
        self.table.blockSignals(True)
        r = self.table.rowCount() if position is None else position
        self.table.insertRow(r)
        self.rows.insert(r, row)
        info = row.layout

        try:
            row.copy_no = max(0, int(values.get("copy") or 0))
        except (TypeError, ValueError):
            row.copy_no = 0
        item = QTableWidgetItem(row.label)
        item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if values.get("enabled", True) else Qt.CheckState.Unchecked)
        item.setIcon(icon("/mIconLayout.svg"))
        if info.atlas:
            item.setToolTip(tr("Layout with an atlas"))
        row.item = item
        self.table.setItem(r, C_LAYOUT, item)

        proj = QTableWidgetItem()
        proj.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        if row.is_current:
            font = proj.font()
            font.setBold(True)
            proj.setFont(font)
        else:
            proj.setText(os.path.basename(row.project_path))
            proj.setToolTip(row.project_path)
        row.project_item = proj
        self.table.setItem(r, C_PROJECT, proj)

        fmt = QComboBox()
        for key, label, _ in S.FORMATS:
            fmt.addItem(label, key)
        fmt.setCurrentIndex(max(0, fmt.findData(values.get("format"))))
        row.format_combo = fmt
        self.table.setCellWidget(r, C_FORMAT, fmt)

        dpi = QSpinBox()
        dpi.setRange(25, 2400)
        dpi.setSingleStep(50)
        dpi.setValue(int(values.get("dpi", 300)))
        dpi.setFrame(False)
        if info.dpi:
            dpi.setToolTip(tr("Resolution saved in the layout: {dpi} dpi", dpi=info.dpi))
        row.dpi_spin = dpi
        self.table.setCellWidget(r, C_DPI, dpi)

        holder = QWidget()
        hl = QHBoxLayout(holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        chk = QCheckBox()
        chk.setChecked(bool(values.get("atlas", True)) and info.atlas)
        chk.setEnabled(info.atlas)
        chk.setToolTip(tr("Export all atlas sheets") if info.atlas else tr("The layout has no atlas enabled"))
        hl.addWidget(chk)
        row.atlas_check = chk
        self.table.setCellWidget(r, C_ATLAS, holder)

        pages = QLineEdit(values.get("pages", ""))
        pages.setFrame(False)
        pages.setValidator(QRegularExpressionValidator(QRegularExpression(PAGES_RE), pages))
        pages.setPlaceholderText(self._pages_placeholder(info, chk.isChecked()))
        pages.setToolTip(tr("Range of pages (or atlas sheets), e.g. 2-5 or 1,3,7-9. Empty = all."))
        row.pages_edit = pages
        self.table.setCellWidget(r, C_PAGES, pages)

        parts = QSpinBox()
        parts.setRange(1, S.MAX_PARALLEL)
        parts.setFrame(False)
        parts.setValue(int(values.get("parts") or 1))
        parts.setToolTip(tr("Into how many parallel processes to split this layout.\n"
                            "Atlas: the sheets are split into equal parts (e.g. 1–30, 31–60…).\n"
                            "Several pages: the pages are shared between processes (PNG/JPG/TIFF/BMP)."))
        row.parts_spin = parts
        self.table.setCellWidget(r, C_PARTS, parts)

        out = QTableWidgetItem(values.get("output_dir") or S.get("output_dir"))
        out.setToolTip(tr("Double-click to change the folder"))
        out.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        row.outdir_item = out
        self.table.setItem(r, C_OUTDIR, out)

        bar = QProgressBar()
        bar.setRange(0, 100)
        row.status_bar = bar
        self.table.setCellWidget(r, C_STATUS, bar)
        self._set_status(row, 0, tr("Ready to render"))

        # a value chosen in the table may become the new default
        fmt.currentIndexChanged.connect(lambda *_a, w=fmt: self._remember_default("format", w.currentData()))
        dpi.valueChanged.connect(lambda v: self._remember_default("dpi", v))
        parts.valueChanged.connect(lambda v: self._remember_default("parts", v))
        chk.toggled.connect(lambda on, rw=row: self._on_atlas_toggled(rw, on))

        self.table.blockSignals(False)
        self._loading = loading
        if not self._batch:
            self._update_summary()

    def _on_atlas_toggled(self, row, on):
        row.pages_edit.setPlaceholderText(self._pages_placeholder(row.layout, on))
        if row.layout.atlas and self._follow_last and not self._loading:
            S.set("use_atlas", on)

    @staticmethod
    def _pages_placeholder(layout, atlas_on):
        if atlas_on and layout.atlas and layout.features >= 0:
            return tr("all (~{n} sheets)", n=layout.features)
        if layout.pages > 1:
            return tr("all ({n} pages)", n=layout.pages)
        return tr("all")

    @staticmethod
    def _set_status(row, value, text, color=None):
        bar = row.status_bar
        bar.setValue(int(value))
        bar.setFormat(text)
        bar.setToolTip(text)
        bar.setStyleSheet(f"QProgressBar::chunk {{ background-color: {color}; }}" if color else "")

    def _selected_rows(self):
        idx = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.rows[i] for i in idx if i < len(self.rows)]

    def _rows_or_all(self):
        return self._selected_rows() or list(self.rows)

    def _update_summary(self):
        active = sum(1 for r in self.rows if r.enabled)
        current = sum(1 for r in self.rows if r.is_current)
        self.summary.setText(tr("Layouts: {total} (current project: {current}) · selected: {active}",
                                total=len(self.rows), current=current, active=active))
        self._update_buttons()

    def _update_buttons(self):
        busy = self.is_busy()
        # while working everything is greyed out and locked – except “Stop”
        for w in (self.toolbar_w, self.bulk_w, self.table, self.remember):
            w.setEnabled(not busy)
        self.outputs.set_locked(busy)
        self.btn_run.setEnabled(not busy and any(r.enabled for r in self.rows))
        self.btn_run.setText(tr("Rendering…") if busy else tr("Render"))
        self.btn_cancel.setEnabled(busy)
        if not busy:
            sel = self._selected_rows()
            one = len(sel) == 1
            self.btn_edit.setEnabled(one)
            self.btn_copy.setEnabled(one and sel[0].is_current)
            self.btn_profile.setEnabled(one)

    # ========================================================= list actions
    def open_layout_manager(self):
        if self.iface is not None:
            self.iface.showLayoutManager()

    def _check_all(self, state):
        state = Qt.CheckState.Checked if state else Qt.CheckState.Unchecked
        with self._batch_updates():
            self.table.blockSignals(True)
            for row in self._rows_or_all():
                row.item.setCheckState(state)
            self.table.blockSignals(False)

    def add_project(self, path, values_by_layout=None):
        """Adds the layouts of another project file (one row each)."""
        path = os.path.abspath(path)
        name = os.path.basename(path)
        if same_path(QgsProject.instance().fileName(), path):
            self.log(tr("{name} is the current project – its layouts are already listed.", name=name))
            return 0
        try:
            layouts = read_layouts(path)
        except Exception as e:  # noqa: BLE001
            self.log("⚠ " + tr("{name}: read error: {error}", name=name, error=e))
            return 0
        if not layouts:
            self.log("⚠ " + tr("{name}: no print layouts", name=name))
            return 0
        with self._batch_updates():
            defaults = self._defaults()
            for info in layouts:
                values = (values_by_layout or {}).get(info.name) or self._remembered(path, info.name, defaults)
                self._insert_row(TaskRow(info, path), values)
        return len(layouts)

    def add_projects_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr("Add layouts from other projects"), self._last_dir,
                                                tr("QGIS projects (*.qgz *.qgs)"))
        if paths:
            self._last_dir = os.path.dirname(paths[0])
            with self._batch_updates():
                for p in paths:
                    self.add_project(p)

    def add_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, tr("Folder with projects"), self._last_dir)
        if not folder:
            return
        self._last_dir = folder
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            with self._batch_updates():
                n = sum(self.add_project(p) for p in find_projects(folder))
        finally:
            self.unsetCursor()
        self.log(tr("Added {n} layouts from projects in {folder}", n=n, folder=folder))

    def duplicate_selected(self):
        with self._batch_updates():
            for row in self._selected_rows():
                pos = self.rows.index(row) + 1
                taken = {o.copy_no for o in self.rows
                         if o.layout.name == row.layout.name and o.project_path == row.project_path}
                n = 1
                while n in taken:  # next free number: “name (1)”, “name (2)”…
                    n += 1
                self._insert_row(TaskRow(row.layout, row.project_path), {**row.values(), "copy": n}, pos)
            self._update_project_labels()

    def remove_selected(self):
        """Rows ticked next to the layout name (the same checkbox that chooses what to render)."""
        ticked = [r for r in self.rows if r.enabled]
        if not ticked:
            ui.info(self, tr("Delete layouts"), tr("Tick the layouts to delete (the checkbox next to the name)."))
            return
        self.remove_rows(ticked)

    def remove_rows(self, selected):
        """Current project: deletes the layouts from the Layout Manager (after a confirmation).
        A duplicated row whose layout stays in other rows, and rows of other projects, leave only the list."""
        if not selected:
            return
        chosen = set(map(id, selected))
        to_delete = sorted({r.layout.name for r in selected if r.is_current and all(
            id(o) in chosen for o in self.rows if o.is_current and o.layout.name == r.layout.name)})
        if to_delete:
            listing = "\n".join(f"• {n}" for n in to_delete[:15])
            if len(to_delete) > 15:
                listing += f"\n… (+{len(to_delete) - 15})"
            ans = ui.ask(self, tr("Delete layouts"),
                         tr("Delete {n} layout(s) from the Layout Manager of the current project?\n\n{names}\n\n"
                            "The change becomes permanent when you save the project.", n=len(to_delete), names=listing))
            if ans != QMessageBox.StandardButton.Yes:
                return
        with self._batch_updates():
            for i in reversed(range(len(self.rows))):  # rows that only leave the list
                row = self.rows[i]
                if id(row) in chosen and not (row.is_current and row.layout.name in to_delete):
                    self.table.removeRow(i)
                    del self.rows[i]
        if to_delete:
            manager = QgsProject.instance().layoutManager()
            deleted = []
            for name in to_delete:
                layout = manager.layoutByName(name)
                if layout is not None and manager.removeLayout(layout):
                    deleted.append(name)
            if deleted:
                self.log(tr("Deleted from the Layout Manager: {names} (save the project to keep the change)",
                            names=", ".join(deleted)))
            self.sync_current_project()  # their rows disappear together with the layouts

    def apply_bulk(self):
        rows = self._selected_rows()
        if not rows:
            ui.info(self, tr("Bulk edit"), tr("Select rows in the table."))
            return
        fmt = self.bulk_format.currentData()
        dpi = self.bulk_dpi.value()
        parts = self.bulk_parts.value()
        with self._quiet_edits():
            for row in rows:
                if fmt:
                    row.format_combo.setCurrentIndex(row.format_combo.findData(fmt))
                if dpi:
                    row.dpi_spin.setValue(dpi)
                if parts:
                    row.parts_spin.setValue(parts)
        # one settings write instead of one per row
        for key, value in (("format", fmt), ("dpi", dpi), ("parts", parts)):
            self._remember_default(key, value)

    def bulk_output_dir(self):
        rows = self._rows_or_all()
        if not rows:
            return
        d = QFileDialog.getExistingDirectory(self, tr("Output folder"), rows[0].outdir_item.text())
        if d:
            with self._quiet_edits():
                for row in rows:
                    row.outdir_item.setText(d)
            self._remember_default("output_dir", d)

    def apply_defaults_to_selected(self):
        d = self._defaults()
        with self._quiet_edits():
            for row in self._rows_or_all():
                row.format_combo.setCurrentIndex(max(0, row.format_combo.findData(d["format"])))
                row.dpi_spin.setValue(d["dpi"])
                row.atlas_check.setChecked(d["atlas"] and row.layout.atlas)
                row.parts_spin.setValue(d["parts"])
                row.pages_edit.clear()
                row.outdir_item.setText(d["output_dir"])

    def save_list(self):
        if not self.rows:
            return
        path, _ = QFileDialog.getSaveFileName(self, tr("Save list settings"), self._last_dir,
                                              tr("Task list (*.json)"))
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        data = {"plugin": "multirender", "version": 1, "tasks": [r.values() for r in self.rows]}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        self.log(tr("List saved: {path}", path=path))

    def load_list(self):
        """Loads list settings; entries with project=null refer to the current project."""
        path, _ = QFileDialog.getOpenFileName(self, tr("Load list settings"), self._last_dir,
                                              tr("Task list (*.json)"))
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as fh:
                tasks = json.load(fh).get("tasks", [])
        except (OSError, ValueError, AttributeError) as e:
            ui.warn(self, tr("Task list"), tr("Cannot load the file: {error}", error=e))
            return
        base = os.path.dirname(path)
        current, others = {}, {}
        for t in tasks:
            p = t.get("project")
            if p is None:
                current.setdefault(t.get("layout"), []).append(t)
            else:
                if not os.path.isabs(p):
                    p = os.path.join(base, p)
                others.setdefault(p, {})[t.get("layout")] = t

        # current project: layouts from the list get its settings, the others are unchecked
        live = {li.name: li for li in live_layouts()}
        with self._batch_updates():
            self._remove_current_rows()
            defaults = self._defaults()
            pos = 0
            for name, info in live.items():
                for values in current.get(name) or [{**defaults, "enabled": not current}]:
                    self._insert_row(TaskRow(info), values, pos)
                    pos += 1
            missing = [n for n in current if n not in live]
            if missing:
                self.log("⚠ " + tr("Not in the current project: {names}", names=", ".join(missing)))
            for p, by_layout in others.items():
                if os.path.isfile(p):
                    self.add_project(p, by_layout)
                else:
                    self.log("⚠ " + tr("Skipped – file not found: {path}", path=p))
            self._update_project_labels()
        self.log(tr("List loaded: {path}", path=path))

    # ================================================ designer and layout copy
    def _table_menu(self, pos):
        if self.is_busy():
            return
        r = self.table.rowAt(pos.y())
        if r < 0 or r >= len(self.rows):
            return
        if r not in {i.row() for i in self.table.selectedIndexes()}:
            self.table.selectRow(r)
        row = self.rows[r]
        menu = QMenu(self)
        menu.addAction(icon("/mActionEditNodesItem.svg"), tr("Open in the layout designer"),
                       lambda: self.open_designer(row))
        act = menu.addAction(icon("/mActionEditCopy.svg"), tr("Copy layout…"), lambda: self.copy_layout(row))
        act.setEnabled(row.is_current)
        menu.addAction(icon("/mActionStatisticalSummary.svg"), tr("Analyze performance"), lambda: self.profile(row))
        menu.addSeparator()
        menu.addAction(tr("Duplicate row (other format/DPI)"), self.duplicate_selected)
        menu.addAction(icon("/mActionRemove.svg"),
                       tr("Delete layout…") if row.is_current else tr("Remove from the list"),
                       lambda: self.remove_rows(self._selected_rows()))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _one_selected(self):
        sel = self._selected_rows()
        return sel[0] if len(sel) == 1 else None

    def open_designer_selected(self):
        row = self._one_selected()
        if row:
            self.open_designer(row)

    def copy_layout_selected(self):
        row = self._one_selected()
        if row:
            self.copy_layout(row)

    @staticmethod
    def _layout_object(name):
        return QgsProject.instance().layoutManager().layoutByName(name)

    def open_designer(self, row):
        """Opens the layout in the QGIS layout designer."""
        if self.iface is None:
            return
        title = tr("Layout designer")
        if not row.is_current:
            prj = QgsProject.instance()
            ans = ui.ask(
                self, title,
                tr("Layout “{name}” belongs to another project:\n{path}\n\n"
                   "Open that project in QGIS (instead of the current one)?",
                   name=row.layout.name, path=row.project_path),
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
            if prj.isDirty():
                ans = ui.ask(
                    self, title, tr("The current project has unsaved changes. Save it?"),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                )
                if ans == QMessageBox.StandardButton.Cancel:
                    return
                if ans == QMessageBox.StandardButton.Yes and not prj.write():
                    ui.warn(self, title, tr("Saving failed: {error}", error=prj.error()))
                    return
                prj.setDirty(False)  # so that QGIS does not ask again
            name = row.layout.name
            self.iface.addProject(row.project_path)
            layout = self._layout_object(name)
        else:
            layout = self._layout_object(row.layout.name)
        if layout is None:
            ui.warn(self, title, tr("Layout “{name}” not found.", name=row.layout.name))
            self.sync_current_project()
            return
        self.iface.openLayoutDesigner(layout)

    def copy_layout(self, row):
        """Copy of a layout in the current project; the new row inherits the settings."""
        title = tr("Copy layout")
        if not row.is_current:
            ui.info(self, title, tr("Only layouts of the current project can be copied."))
            return
        layout = self._layout_object(row.layout.name)
        if layout is None:
            self.sync_current_project()
            return
        lm = QgsProject.instance().layoutManager()
        existing = {lay.name() for lay in lm.layouts()}
        proposal = copy_name(row.layout.name, existing)
        name, ok = ui.text_input(self, title, tr("Name of the new layout:"), proposal)
        name = name.strip()
        if not ok or not name:
            return
        if name in existing:
            ui.warn(self, title, tr("Layout “{name}” already exists.", name=name))
            return
        self._pending_values[name] = {**row.values(), "copy": 0}
        new_layout = lm.duplicateLayout(layout, name)
        if new_layout is None:
            self._pending_values.pop(name, None)
            ui.warn(self, title, tr("Copying the layout failed."))
            return
        self.log(tr("Copied layout “{src}” as “{dst}” (save the project to keep it)", src=row.layout.name, dst=name))
        self.sync_current_project()
        ans = ui.ask(self, title, tr("Open “{name}” in the layout designer?", name=name))
        if ans == QMessageBox.StandardButton.Yes and self.iface is not None:
            self.iface.openLayoutDesigner(new_layout)

    # ============================================================ drag & drop
    def dragEnterEvent(self, event):  # noqa: N802
        if not self.is_busy() and event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        with self._batch_updates():
            for url in event.mimeData().urls():
                p = url.toLocalFile()
                if os.path.isdir(p):
                    for f in find_projects(p):
                        self.add_project(f)
                elif is_project_file(p):
                    self.add_project(p)
        event.acceptProposedAction()

    def _on_double_click(self, r, c):
        if r >= len(self.rows):
            return
        row = self.rows[r]
        if c == C_LAYOUT and not self.is_busy():
            self.open_designer(row)
        elif c == C_OUTDIR and not self.is_busy():
            d = QFileDialog.getExistingDirectory(self, tr("Output folder"), row.outdir_item.text())
            if d:
                row.outdir_item.setText(d)
        elif c == C_STATUS:
            files = [f for j in row.jobs for f in j.files]
            target = os.path.dirname(files[0]) if files else row.outdir_item.text()
            if os.path.isdir(target):
                QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    # ============================================================ rendering
    @staticmethod
    def _qgis_process():
        path = S.get("qgis_process")
        if path and os.path.isfile(path):
            return path
        return find_qgis_process()

    def _current_project_file(self):
        """Path of the saved current project, or None (= abort)."""
        prj = QgsProject.instance()
        title = tr("Current project")
        if not prj.fileName():
            ans = ui.ask(self, title, tr("The current project is not saved, and rendering reads the "
                                                       "project file.\nSave it now?"))
            if ans == QMessageBox.StandardButton.Yes and self.iface is not None:
                self.iface.actionSaveProjectAs().trigger()
            return prj.fileName() or None
        if prj.isDirty():
            ans = ui.ask(
                self, title,
                tr("The project has unsaved changes. Rendering uses the file on disk.\n\n"
                   "Yes – save and render\nNo – render the last saved version"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return None
            if ans == QMessageBox.StandardButton.Yes and not prj.write():
                ui.warn(self, title, tr("Saving failed: {error}", error=prj.error()))
                return None
        return prj.fileName()

    def _output_base(self, row, project_path, fmt, dpi, index, now, used, overwrite, folder, naming):
        out_dir = row.outdir_item.text().strip() or S.get("output_dir")
        if naming == "layout":
            # only the layout name, e.g. al-jerozolimskie → al-jerozolimskie_1, _2…
            name = row.layout.name
        else:
            name = self._template_name(os.path.splitext(os.path.basename(project_path))[0], row.layout.name,
                                       fmt, dpi, index, now)
        return self._unique_base(out_dir, name, fmt, used, overwrite, folder, numbered=naming == "layout")

    @staticmethod
    def _template_name(project, layout_name, fmt, dpi, index, now):
        try:
            return S.get("filename_template").format(
                project=project, layout=layout_name, format=fmt, dpi=dpi,
                date=now.strftime("%Y-%m-%d"), time=now.strftime("%H%M%S"), index=index,
            )
        except (KeyError, IndexError, ValueError):
            return f"{project}_{layout_name}"

    @staticmethod
    def _unique_base(out_dir, name, fmt, used, overwrite, folder, numbered=False):
        """Base name without clashes. numbered=True: files end with _1, _2 (pages),
        so further versions get “ (2)”, “ (3)” to avoid confusion with page numbers."""
        name = "_".join(safe_name(part) for part in re.split(r"[\\/]", name) if part.strip())
        base = os.path.join(out_dir, name)
        ext = S.FORMAT_EXT[fmt]

        def key(b):
            return os.path.normcase(os.path.abspath(b + ("" if folder else ext)))

        def taken(b):
            if key(b) in used:
                return True
            if overwrite:
                return False
            if folder:
                return os.path.exists(b)
            return os.path.exists(b + ext) or (numbered and os.path.exists(b + "_1" + ext))

        candidate, n = base, 1
        while taken(candidate):
            n += 1
            candidate = f"{base} ({n})" if numbered else f"{base}_{n - 1}"
        used.add(key(candidate))
        return candidate

    def _effective_parts(self, row, fmt, atlas):
        """Number of parts, taking into account what can be split."""
        parts = row.parts_spin.value()
        spec = row.pages_edit.text().strip()
        name = row.layout.name
        if not atlas and fmt in ("pdf", "svg"):
            if parts > 1 or spec:
                self.log("ℹ " + tr("{name}: {fmt} without an atlas is exported as a whole (splitting and page "
                                   "ranges work for atlases and PNG/JPG/TIFF/BMP)", name=name, fmt=fmt.upper()))
            return 1, ""
        known = row.layout.features if atlas else row.layout.pages
        if known > 0:
            count = len(parse_pages(spec, known))
            if count == 0:
                self.log("⚠ " + tr("{name}: range “{spec}” contains no page (there are {n})",
                                   name=name, spec=spec, n=known))
                return 0, spec
            parts = min(parts, count)
        if not atlas and row.layout.pages == 1:
            parts = 1
        if atlas and fmt == "pdf" and S.get("atlas_single_pdf") and (parts > 1 or spec):
            self.log("ℹ " + tr("{name}: atlas split into parts – one PDF per sheet instead of a single PDF",
                               name=name))
        return parts, spec

    def _build_jobs(self, current_path):
        now = datetime.now()
        options = S.export_options()
        naming = options["naming"]
        overwrite = S.get("overwrite")
        used = set()
        jobs = []
        n_layout = 0
        for row in self.rows:
            row.jobs = []
            if not row.enabled:
                continue
            project_path = current_path if row.is_current else row.project_path
            fmt = row.format_combo.currentData()
            dpi = row.dpi_spin.value()
            atlas = row.atlas_check.isChecked() and row.layout.atlas
            parts, spec = self._effective_parts(row, fmt, atlas)
            if parts == 0:
                continue
            n_layout += 1
            paged = parts > 1 or bool(spec)
            folder = atlas and naming != "layout"  # atlas in a subfolder only with the template
            # one base name shared by all parts of a layout
            base = self._output_base(row, project_path, fmt, dpi, n_layout, now, used, overwrite, folder, naming)
            for part in range(1, parts + 1):
                job = RenderJob(project_path, row.layout.name, fmt, dpi, atlas, base, options,
                                self._render_script, pages=spec if paged else "", part=part, parts=parts)
                job.row = row
                row.jobs.append(job)
                jobs.append(job)
        return jobs

    def _validate_pages(self):
        for row in self.rows:
            spec = row.pages_edit.text().strip()
            if row.enabled and spec:
                try:
                    parse_pages(spec, 100000)
                except ValueError:
                    ui.warn(self, tr("Pages"),
                                        tr("Invalid page range “{spec}” in layout “{name}”.\nExample: 2-5,8,10-12",
                                           spec=spec, name=row.layout.name))
                    return False
        return True

    def run(self):
        if self.is_busy():
            return
        if not any(r.enabled for r in self.rows):
            ui.info(self, tr("Rendering"), tr("Select layouts to render."))
            return
        if not self._validate_pages():
            return
        qp = self._qgis_process()
        if not qp:
            ui.warn(self, "qgis_process", tr("qgis_process not found.\nSet it in the plugin settings."))
            return
        current_path = ""
        if any(r.enabled and r.is_current for r in self.rows):
            current_path = self._current_project_file()
            if not current_path:
                return
            self._update_project_labels()

        self._save_rows_memory()
        jobs = self._build_jobs(current_path)
        if not jobs:
            ui.info(self, tr("Rendering"), tr("No pages to print in the selected ranges."))
            return
        for d in {j.output_base if j.atlas and j.options["naming"] != "layout" else os.path.dirname(j.output_base)
                  for j in jobs}:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                ui.warn(self, tr("Output folder"),
                                    tr("Cannot create the folder:\n{path}\n\n{error}", path=d, error=e))
                return

        self._qp = qp
        self._render_jobs = jobs
        self._cache_dirs = []
        self._cache_seconds = 0.0
        self._run_started = time.time()
        self._start_clock()
        self.total.setValue(0)

        self._cache_jobs, self._live = self._build_cache_jobs(jobs)
        n_cache = len(self._cache_jobs) + (1 if self._live else 0)
        if not n_cache:
            self._start_render()
            return
        for row in self.rows:
            if row.jobs:
                self._set_status(row, 0, tr("Waiting for the project cache"))
        self._phase = "cache"
        self._cache_pending = n_cache
        self._update_buttons()
        self.log("▶ " + tr("Project cache: {n} project(s) – layers are fetched once for all prints", n=n_cache))
        if self._live is not None:
            self.log("   " + tr("Current project: the cache is built in this QGIS session ({n} layers)",
                               n=self._live_count))
            self._live.progress.connect(lambda _v: self._update_total())
            self._live.finished.connect(lambda _ok: QTimer.singleShot(0, self._cache_part_done))
            self._live.start()
        if self._cache_jobs:
            self.manager.start(self._cache_jobs, qp, self._parallel())

    # ---------------------------------------------------- performance analysis
    def profile_selected(self):
        row = self._one_selected()
        if row:
            self.profile(row)

    def profile(self, row):
        """Measures one layout (first atlas sheet) without exporting anything."""
        if self.is_busy():
            return
        qp = self._qgis_process()
        if not qp:
            ui.warn(self, "qgis_process", tr("qgis_process not found.\nSet it in the plugin settings."))
            return
        project_path = row.project_path
        if row.is_current:
            project_path = self._current_project_file()
            if not project_path:
                return
            self._update_project_labels()
        fmt = row.format_combo.currentData()
        atlas = row.atlas_check.isChecked() and row.layout.atlas
        base = os.path.join(cache_root(), "profile", safe_name(row.layout.name))
        job = RenderJob(project_path, row.layout.name, fmt, row.dpi_spin.value(), atlas, base, S.export_options(),
                        self._render_script, mode="profile")
        self._profile_row, self._profile_job = row, job
        self._qp = qp
        self._run_started = time.time()
        self._start_clock()
        self.total.setValue(0)
        self._phase = "profile"
        self._update_buttons()
        self.log("▶ " + tr("Analyzing the performance of “{name}” ({fmt}, {dpi} dpi)…",
                           name=row.layout.name, fmt=fmt.upper(), dpi=job.dpi))
        # measure what a real run renders: the current project through the session cache
        options = S.cache_options()
        if row.is_current and options["cache_mode"] != "off":
            cache_dir = os.path.join(cache_root(), "profile_" + hashlib.sha1(project_path.encode()).hexdigest()[:12])
            shutil.rmtree(cache_dir, ignore_errors=True)
            live = LiveCache(project_path, os.path.join(cache_dir, os.path.basename(project_path)),
                             [row.layout.name], options, self)
            if live.prepare():
                self._live, self._profile_cache_dir = live, cache_dir
                live.progress.connect(lambda _v: self._update_total())
                live.finished.connect(lambda _ok: QTimer.singleShot(0, self._profile_after_cache))
                live.start()
                return
        self.manager.start([job], qp, 1)

    def _profile_after_cache(self):
        live, self._live = self._live, None
        job = self._profile_job
        if live is None or job is None:
            return
        res = live.result
        if res.get("canceled"):
            job.status = CANCELED
            self._finish_profile()
            return
        if "error" in res:
            self.log("⚠ " + tr("{label}: {error} – rendering from the original project", label=live.label,
                               error=res["error"]))
        else:
            job.project_path = live.cache_project
            job.cache_seconds = live.duration
        self.manager.start([job], self._qp, 1)

    def _finish_profile(self):
        self._stop_clock()
        self._phase = None
        self._update_buttons()
        self._update_total()
        if self._sync_pending:  # the Layout Manager changed while measuring
            self._sync_pending = False
            self._sync_timer.start()
        job, self._profile_job = self._profile_job, None
        row, self._profile_row = self._profile_row, None
        if self._profile_cache_dir:
            shutil.rmtree(self._profile_cache_dir, ignore_errors=True)
            self._profile_cache_dir = None
        if job is None or job.status != DONE or not job.result.get("profile"):
            if job is not None and job.status == FAILED:
                self.log("✗ " + tr("Performance analysis failed: {error}", error=job.error))
                ui.warn(self, tr("Analyze performance"),
                                    tr("Performance analysis failed: {error}", error=job.error))
            return
        self.log("✓ " + tr("Performance analysis finished in {secs} s", secs=num(job.duration)))
        profile = dict(job.result["profile"], cache_seconds=getattr(job, "cache_seconds", 0.0))
        report = ProfileReport(job.layout, profile, job.fmt, job.dpi, self._parallel(),
                               S.get("cache_mode") != "off", self)
        report.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        report.show()
        if row is not None and row in self.rows:
            self.table.selectRow(self.rows.index(row))

    # ------------------------------------------------------------ cache
    def _build_cache_jobs(self, jobs):
        """Cache per project, only for its printed layouts; reuse by setting.

        Returns (qgis_process jobs for other projects, LiveCache for the current project or None).
        """
        options = S.cache_options()
        if options["cache_mode"] == "off":
            return [], None
        live = None
        reuse = S.get("cache_reuse")
        root = cache_root()
        if not reuse:
            cleanup_stale(root)
        by_project = {}
        for job in jobs:
            by_project.setdefault(job.source_project, []).append(job)
        cache_jobs = []
        for project, pjobs in by_project.items():
            name = os.path.basename(project)
            try:
                mtime = os.path.getmtime(project)
            except OSError:
                continue
            layouts = sorted({j.layout for j in pjobs})
            current = same_path(QgsProject.instance().fileName(), project)
            key = hashlib.sha1(
                json.dumps([os.path.abspath(project), mtime, options, layouts], sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
            cache_dir = os.path.join(root, key)
            cache_project = os.path.join(cache_dir, name)
            if reuse and os.path.isfile(cache_project):
                self.log(tr("Cache {name}: reusing the existing one (project unchanged)", name=name))
                for j in pjobs:
                    j.project_path = cache_project
                continue
            if current:
                # layers of the open project are already connected – copy them here, in this session
                candidate = LiveCache(project, cache_project, layouts, options, self)
                count = candidate.prepare()
                if candidate.dynamic:
                    self.log("   " + message({"c": "cache_dynamic"}))
                if not count:
                    self.log(tr("Cache {name}: no layers to cache – skipped", name=name))
                    continue
                live, self._live_count = candidate, count
            shutil.rmtree(cache_dir, ignore_errors=True)
            if not reuse:
                self._cache_dirs.append(cache_dir)  # one-off cache – removed after the run
            if current:
                live.render_jobs = pjobs
                continue
            cj = CacheJob(project, cache_project, options, layouts, self._cache_script)
            cj.render_jobs = pjobs
            cache_jobs.append(cj)
        return cache_jobs, live

    def _cache_part_done(self):
        """One kind of cache finished (session cache or the qgis_process jobs)."""
        self._cache_pending -= 1
        if self._cache_pending <= 0 and self._phase == "cache":
            self._after_cache()

    def _after_cache(self):
        self._cache_seconds = time.time() - self._run_started
        cache_jobs = self._cache_jobs
        live, self._live = self._live, None
        canceled = any(cj.status == CANCELED for cj in cache_jobs)
        if live is not None:
            res = live.result
            if res.get("canceled"):
                canceled = True
            elif "error" in res:
                self.log("⚠ " + tr("{label}: {error} – rendering from the original project",
                                   label=live.label, error=res["error"]))
            else:
                for j in live.render_jobs:
                    j.project_path = live.cache_project
                self.log("✓ " + tr("{label}: {cached} layers local, {skipped} skipped, {size} ({secs} s)",
                                   label=live.label, cached=res["cached"], skipped=res["skipped"],
                                   size=human_size(res["bytes"]), secs=num(live.duration)))
                if res.get("placeholders"):
                    self.log("   " + tr("{n} remote layers not used by the printed layouts were replaced by empty "
                                       "copies", n=res["placeholders"]))
                for w in res.get("warnings", []):
                    self.log("⚠ " + message(w))
        for cj in cache_jobs:
            if cj.status == DONE:
                for j in cj.render_jobs:
                    j.project_path = cj.cache_project
                res = cj.result or {}
                self.log("✓ " + tr("{label}: {cached} layers local, {skipped} skipped, {size} ({secs} s)",
                                   label=cj.label, cached=res.get("cached", 0), skipped=res.get("skipped", 0),
                                   size=human_size(res.get("bytes", 0)), secs=num(cj.duration)))
            elif cj.status == FAILED:
                self.log("⚠ " + tr("{label}: {error} – rendering from the original project",
                                   label=cj.label, error=cj.error))
        self._cache_jobs = []
        if canceled:
            for j in self._render_jobs:
                j.status, j.error = CANCELED, tr("Canceled")
                self._row_state(j.row)
            self._finish_run()
            return
        self._start_render()

    def _start_render(self):
        jobs = self._render_jobs
        for row in self.rows:
            if row.jobs:
                self._set_status(row, 0, tr("In queue"))
        self._phase = "render"
        self._update_buttons()
        self.log("▶ " + tr("Rendering: {n} jobs, {p} parallel processes",
                           n=len(jobs), p=min(self._parallel(), len(jobs))))
        self.manager.start(jobs, self._qp, self._parallel())
        self._update_total()

    def cancel(self):
        if self.is_busy():
            self.log("■ " + tr("Stopping…"))
            if self._live is not None:
                self._live.cancel()
            self.manager.cancel()

    @staticmethod
    def _parallel():
        return S.get("max_parallel")

    def _show_parallel(self):
        self.parallel_label.setText(tr("Parallel processes: {n}", n=self._parallel()))

    # ------------------------------------------------------ manager signals
    def _row_state(self, row):
        if row is None or not row.jobs or row not in self.rows:
            return
        jobs = row.jobs
        n = len(jobs)
        done = sum(j.status == DONE for j in jobs)
        failed = [j for j in jobs if j.status == FAILED]
        canceled = sum(j.status == CANCELED for j in jobs)
        running = sum(j.status == RUNNING for j in jobs)
        value = sum(100 if j.is_final else j.progress for j in jobs) / n
        parts_txt = " " + tr("({done}/{n} parts)", done=done, n=n) if n > 1 else ""
        if all(j.is_final for j in jobs):
            files = sum(len(j.files) for j in jobs)
            starts = [j.started for j in jobs if j.started]
            ends = [j.finished for j in jobs if j.finished]
            secs = (max(ends) - min(starts)) if starts and ends else 0.0
            if failed:
                self._set_status(row, 100, tr("Error{parts} – hover for details", parts=parts_txt), "#c0392b")
                row.status_bar.setToolTip("\n".join(sorted({j.error for j in failed})))
            elif canceled:
                self._set_status(row, value, tr("Canceled{parts}", parts=parts_txt), "#999")
            else:
                warn = [w for j in jobs for w in j.warnings]
                self._set_status(row, 100, tr("Done · {files} file(s) · {secs} s", files=files, secs=num(secs)),
                                 "#d4a017" if warn else "#3c9a4b")
                if warn:
                    row.status_bar.setToolTip("\n".join(dict.fromkeys(warn)))
        elif running:
            self._set_status(row, value, tr("Rendering {pct}%{parts}", pct=f"{value:.0f}", parts=parts_txt))
        else:
            self._set_status(row, value, tr("In queue{parts}", parts=parts_txt))

    def _update_total(self):
        if self._phase == "cache" or self._live is not None:  # cache jobs of this run + the session cache
            values = [100 if j.is_final else j.progress for j in self._cache_jobs]
            if self._live is not None:
                values.append(self._live.percent)
            finished = sum(1 for v in values if v >= 100)
            total = len(values)
        else:
            jobs = self.manager.jobs()
            values = [100 if j.is_final else j.progress for j in jobs]
            finished, total = sum(j.is_final for j in jobs), len(jobs)
        if not total:
            return
        value = sum(values) / total
        self.total.setValue(int(value * 10))
        phase = {"cache": tr("Cache"), "profile": tr("Analysis")}.get(self._phase, tr("Prints"))
        self.total.setFormat(f"{phase}: {finished}/{total} · {value:.0f}%")

    def _on_job_started(self, job):
        self._row_state(job.row)
        self._update_total()

    def _on_job_progress(self, job):
        self._dirty_rows.add(job.row)
        if not self._ui_timer.isActive():
            self._ui_timer.start()

    def _flush_ui(self):
        rows, self._dirty_rows = self._dirty_rows, set()
        for row in rows:
            self._row_state(row)
        self._update_total()

    def _on_job_finished(self, job):
        self._dirty_rows.discard(job.row)
        if self._phase == "render":
            if job.status == DONE:
                target = job.files[0] if len(job.files) == 1 else tr("{n} files", n=len(job.files))
                self.log(f"✓ {job.label} → {target} ({num(job.duration)} s)")
                self.outputs.add_files(job.files, job.layout)
            elif job.status == FAILED:
                self.log(f"✗ {job.label}: {job.error}")
                tail = list(job.output_tail)[-12:]
                if tail:
                    self.log("    " + "\n    ".join(tail))
            self._row_state(job.row)
        self._update_total()

    def _on_all_finished(self):
        if self._phase == "cache":
            # the signal comes from inside the manager – continue after returning
            QTimer.singleShot(0, self._cache_part_done)
            return
        if self._phase == "profile":
            QTimer.singleShot(0, self._finish_profile)
            return
        self._finish_run()

    # ------------------------------------------------------------- clock
    @staticmethod
    def _format_elapsed(seconds):
        seconds = int(seconds)
        h, rest = divmod(seconds, 3600)
        m, sec = divmod(rest, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"

    def _start_clock(self):
        self.clock.setStyleSheet("")
        self._tick_clock()
        self._clock_timer.start()

    def _tick_clock(self):
        self.clock.setText("⏱ " + self._format_elapsed(time.time() - self._run_started))

    def _stop_clock(self):
        self._clock_timer.stop()
        self._tick_clock()  # the exact final time stays visible
        self.clock.setStyleSheet("font-weight: bold;")

    def _finish_run(self):
        self._stop_clock()
        self._ui_timer.stop()
        self._flush_ui()  # the last pending progress before the summary
        jobs = self._render_jobs
        ok = sum(j.status == DONE for j in jobs)
        failed = sum(j.status == FAILED for j in jobs)
        canceled = sum(j.status == CANCELED for j in jobs)
        files = sum(len(j.files) for j in jobs if j.status == DONE)
        elapsed = time.time() - self._run_started
        render_time = max(0.001, elapsed - self._cache_seconds)
        speedup = ", " + tr("speed-up ×{x}", x=num(sum(j.duration for j in jobs) / render_time)) if ok > 1 else ""
        self.log("■ " + tr("Finished: {ok} OK, {failed} errors, {canceled} canceled in {secs} s{speedup}",
                           ok=ok, failed=failed, canceled=canceled, secs=num(elapsed), speedup=speedup))
        self._missing_prints = sum(1 for j in jobs if j.status == DONE and "invalid_layers" in j.warning_codes)
        if self._missing_prints:
            self.log("⚠ " + tr(MISSING_LAYERS_TEXT, n=self._missing_prints))
        summary, tip = run_summary([j.result for j in jobs if j.status == DONE and j.result],
                                   any(j.parts > 1 for j in jobs))
        if summary:
            self.log("   " + summary)
        if tip:
            self.log("ℹ " + tip)

        self._phase = None
        for d in self._cache_dirs:  # one-off cache
            shutil.rmtree(d, ignore_errors=True)
        self._cache_dirs = []
        self._update_total()
        self._update_buttons()
        layouts_ok = sum(1 for r in self.rows if r.jobs and all(j.status == DONE for j in r.jobs))
        self._show_summary(files, failed, canceled, layouts_ok, elapsed)
        if self._sync_pending:
            self._sync_pending = False
            self._sync_timer.start()

    def _show_summary(self, prints, failed, canceled, layouts_ok, elapsed):
        text = tr("Printed {n} prints in {secs} s.", n=prints, secs=num(elapsed))
        details = []
        if layouts_ok and layouts_ok != prints:
            details.append(tr("Layouts: {n}", n=layouts_ok))
        if self._cache_seconds >= 0.5:
            details.append(tr("including the project cache: {secs} s", secs=num(self._cache_seconds)))
        if self._missing_prints:
            details.append(tr("Prints with unavailable layers: {n} – the maps may be incomplete (see the log)",
                              n=self._missing_prints))
        if failed:
            details.append(tr("Errors: {n} (details in the log)", n=failed))
        if canceled:
            details.append(tr("Canceled: {n}", n=canceled))
        problem = failed or canceled or self._missing_prints
        box = ui.message_box(self, QMessageBox.Icon.Warning if problem else QMessageBox.Icon.Information,
                             "MultiRender", text)
        if details:
            box.setInformativeText("\n".join(details))
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.show()
        if self.iface is not None:
            level = Qgis.MessageLevel.Warning if problem else Qgis.MessageLevel.Success
            self.iface.messageBar().pushMessage("MultiRender", text, level, 8)

    # ================================================================ other
    def log(self, text):
        self.log_view.appendPlainText(f"[{datetime.now():%H:%M:%S}] {text}")

    def open_output_dir(self):
        rows = self._selected_rows() or self.rows
        target = rows[0].outdir_item.text() if rows else S.get("output_dir")
        if not os.path.isdir(target):
            target = S.get("output_dir")
        os.makedirs(target, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(target))

    def open_settings(self):
        dlg = SettingsDialog(self)
        accepted = dlg.exec()
        self.reload_settings()
        if accepted:
            self.log(tr("Settings saved (defaults apply to new rows – use “Restore defaults” to apply them "
                        "to existing rows)."))
            self.settingsSaved.emit(dlg.language_changed)

    def reload_settings(self):
        """Re-reads settings that may have changed in the settings window."""
        self._row_memory = None  # remembered layouts may have been cleared
        self._follow_last = S.get("defaults_follow_last")
        self.remember.blockSignals(True)
        self.remember.setChecked(S.get("remember"))
        self.remember.blockSignals(False)
        self._show_parallel()
        self.manager.set_max_parallel(self._parallel())

    def closeEvent(self, event):  # noqa: N802
        self._save_rows_memory()
        # the window only hides – rendering may continue in the background
        if self.is_busy() and self.iface is not None:
            self.iface.messageBar().pushMessage("MultiRender", tr("Rendering continues in the background."),
                                                Qgis.MessageLevel.Info, 4)
        event.accept()

    def shutdown(self):
        try:
            self._save_rows_memory()
        except RuntimeError:
            pass
        self._disconnect_project()
        if self.is_busy():
            self.manager.cancel()
