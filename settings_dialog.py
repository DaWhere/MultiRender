"""Plugin settings window (tabs: General, New projects, Export, Project cache, Advanced)."""

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import settings as S
from . import ui
from .i18n import LANGUAGES, tr
from .qgis_process_locator import find_qgis_process, test_qgis_process

TEMPLATE_HELP = (
    "Available fields: {project}, {layout}, {format}, {dpi}, {date}, {time}, {index}.\n"
    "For an atlas the name points to a folder, and the files in it are named by the atlas expression."
)


def _hint(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setStyleSheet("color: palette(mid);")
    return label


def _path_row(edit, slot):
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)
    btn = QPushButton("…")
    btn.clicked.connect(slot)
    lay.addWidget(btn)
    return w, lay


def _tab(parent_tabs, title):
    page = QWidget()
    lay = QVBoxLayout(page)
    parent_tabs.addTab(page, title)
    return lay


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.language_changed = False
        self.setWindowTitle(tr("MultiRender – settings"))
        self.setMinimumWidth(620)
        outer = QVBoxLayout(self)
        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        # --- General
        root = _tab(tabs, tr("General"))
        f = QFormLayout()
        root.addLayout(f)
        self.language = QComboBox()
        for code, name in LANGUAGES:
            self.language.addItem(name, code)
        f.addRow(tr("Language:"), self.language)
        self.max_parallel = QSpinBox()
        self.max_parallel.setRange(1, S.MAX_PARALLEL)
        self.max_parallel.setToolTip(tr("How many qgis_process processes run at the same time. CPU cores: {cores}.",
                                        cores=os.cpu_count() or "?"))
        f.addRow(tr("Simultaneous renders:"), self.max_parallel)
        self.output_dir = QLineEdit()
        row, _ = _path_row(self.output_dir, self._pick_dir)
        f.addRow(tr("Default output folder:"), row)
        self.remember = QCheckBox(tr("Remember the settings of each layout"))
        self.remember.setToolTip(tr("Format, DPI, pages, processes and folder of every layout come back "
                                    "after reopening the project or QGIS."))
        f.addRow("", self.remember)
        self.forget = QPushButton(tr("Clear remembered layout settings"))
        self.forget.clicked.connect(self._forget_rows)
        f.addRow("", self.forget)
        root.addStretch(1)

        # --- New projects (defaults for layouts without saved settings)
        root = _tab(tabs, tr("New projects"))
        root.addWidget(_hint(tr("Values used for layouts that have no saved settings yet, "
                                "e.g. in a newly opened project.")))
        f = QFormLayout()
        root.addLayout(f)
        self.format = QComboBox()
        for key, label, _ext in S.FORMATS:
            self.format.addItem(label, key)
        f.addRow(tr("Format:"), self.format)
        self.dpi = QSpinBox()
        self.dpi.setRange(25, 2400)
        self.dpi.setSingleStep(50)
        self.dpi.setSuffix(" dpi")
        f.addRow(tr("Resolution:"), self.dpi)
        self.use_atlas = QCheckBox(tr("Export the atlas if the layout has one"))
        f.addRow("", self.use_atlas)
        self.parts = QSpinBox()
        self.parts.setRange(1, S.MAX_PARALLEL)
        self.parts.setToolTip(tr("Into how many parallel processes to split one layout (atlas / several pages)"))
        f.addRow(tr("Processes per layout:"), self.parts)
        self.follow_last = QCheckBox(tr("Update these defaults with the values last chosen in the table"))
        self.follow_last.setToolTip(tr("Folder, DPI, format and processes chosen for a layout become "
                                       "the defaults for the next new projects."))
        f.addRow("", self.follow_last)

        g = QGroupBox(tr("File names"))
        gf = QFormLayout(g)
        self.naming = QComboBox()
        self.naming.addItem(tr("Layout name (al-jerozolimskie, al-jerozolimskie_1, _2…)"), "layout")
        self.naming.addItem(tr("From template"), "template")
        self.naming.setToolTip(tr("Only the layout name, without the QGIS file name:\n"
                                  "one page → Layout.png; several pages → Layout_1.png, Layout_2.png…;\n"
                                  "atlas → Layout_<sheet no.>. The same name from different projects → “Layout (2)”."))
        gf.addRow(tr("Names:"), self.naming)
        self.template = QLineEdit()
        gf.addRow(tr("Template:"), self.template)
        self.naming.currentIndexChanged.connect(
            lambda *_: self.template.setEnabled(self.naming.currentData() == "template")
        )
        gf.addRow("", _hint(tr(TEMPLATE_HELP)))
        self.overwrite = QCheckBox(tr("Overwrite existing files (otherwise a number is added)"))
        gf.addRow("", self.overwrite)
        root.addWidget(g)
        root.addStretch(1)

        # --- Export
        root = _tab(tabs, tr("Export"))
        f = QFormLayout()
        root.addLayout(f)
        self.world_file = QCheckBox(tr("Create a georeferencing file (world file)"))
        f.addRow(tr("Raster:"), self.world_file)
        self.crop = QCheckBox(tr("Crop to content"))
        f.addRow("", self.crop)
        self.antialias = QCheckBox(tr("Antialiasing"))
        f.addRow("", self.antialias)
        self.pdf_georef = QCheckBox(tr("Include georeference (GeoPDF)"))
        f.addRow("PDF:", self.pdf_georef)
        self.pdf_vector = QCheckBox(tr("Force vector output"))
        f.addRow("", self.pdf_vector)
        self.atlas_single = QCheckBox(tr("Atlas as a single PDF file"))
        f.addRow("", self.atlas_single)
        self.text_render = QComboBox()
        self.text_render.addItem(tr("Always as text"), "text")
        self.text_render.addItem(tr("Always as outlines"), "outlines")
        f.addRow(tr("Text (PDF/SVG):"), self.text_render)
        self.metadata = QCheckBox(tr("Save project metadata in the files"))
        f.addRow("", self.metadata)
        root.addStretch(1)

        # --- Project cache
        root = _tab(tabs, tr("Project cache"))
        f = QFormLayout()
        root.addLayout(f)
        self.cache_mode = QComboBox()
        self.cache_mode.addItem(tr("Remote and database layers (PostGIS, WFS, Oracle…)"), "remote")
        self.cache_mode.addItem(tr("All vector layers"), "all_vector")
        self.cache_mode.addItem(tr("Off – every print fetches the data itself"), "off")
        f.addRow(tr("Cache:"), self.cache_mode)
        self.cache_extent = QCheckBox(tr("Only features within the extent of the layout maps (faster, smaller file)"))
        f.addRow("", self.cache_extent)
        self.cache_skip = QCheckBox(tr("Skip remote layers not used by the printed layouts (faster project loading)"))
        self.cache_skip.setToolTip(tr("They are replaced by empty local copies, so the rendering processes do not "
                                      "connect to them. Layers referenced by expressions or joins are kept."))
        f.addRow("", self.cache_skip)
        self.cache_reuse = QCheckBox(tr("Reuse the cache if the project has not changed"))
        self.cache_reuse.setToolTip(tr("Off = the data is fetched fresh for every run"))
        f.addRow("", self.cache_reuse)
        root.addWidget(_hint(tr("For the current project the cache is built inside this QGIS session, from the "
                                "layers you are already connected to, into a local GeoPackage on disk (system "
                                "temporary folder). All parallel prints read from that copy, so they need no "
                                "database logins. WMS/XYZ basemaps use the shared QGIS network cache.")))
        root.addStretch(1)

        # --- Advanced
        root = _tab(tabs, tr("Advanced"))
        f = QFormLayout()
        root.addLayout(f)
        self.qgis_process = QLineEdit()
        self.qgis_process.setPlaceholderText(tr("detected automatically"))
        row, lay = _path_row(self.qgis_process, self._pick_qp)
        b = QPushButton(tr("Detect"))
        b.clicked.connect(self._detect_qp)
        lay.addWidget(b)
        b = QPushButton(tr("Test"))
        b.clicked.connect(self._test_qp)
        lay.addWidget(b)
        f.addRow("qgis_process:", row)
        self.trust = QCheckBox(tr("Fast project loading (trust layer metadata)"))
        f.addRow("", self.trust)
        self.fail_invalid = QCheckBox(tr("Stop the job when layers are missing"))
        f.addRow("", self.fail_invalid)
        root.addStretch(1)

        bb = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(self._restore)
        ui.translate_buttons(bb)
        outer.addWidget(bb)

        self._load()

    # ------------------------------------------------------------------
    @staticmethod
    def _set_combo(combo, value):
        combo.setCurrentIndex(max(0, combo.findData(value)))

    def _fields(self):
        """setting key -> (getter, setter) for every simple field."""
        def check(w):
            return w.isChecked, w.setChecked

        def spin(w):
            return w.value, w.setValue

        def combo(w):
            return w.currentData, lambda v, w=w: self._set_combo(w, v)

        def text(w):
            return lambda w=w: w.text().strip(), w.setText

        return {
            "language": combo(self.language),
            "max_parallel": spin(self.max_parallel),
            "output_dir": text(self.output_dir),
            "remember": check(self.remember),
            "format": combo(self.format),
            "dpi": spin(self.dpi),
            "use_atlas": check(self.use_atlas),
            "parts": spin(self.parts),
            "defaults_follow_last": check(self.follow_last),
            "naming": combo(self.naming),
            "filename_template": text(self.template),
            "overwrite": check(self.overwrite),
            "world_file": check(self.world_file),
            "crop_to_content": check(self.crop),
            "antialiasing": check(self.antialias),
            "pdf_georeference": check(self.pdf_georef),
            "pdf_force_vector": check(self.pdf_vector),
            "atlas_single_pdf": check(self.atlas_single),
            "text_render": combo(self.text_render),
            "export_metadata": check(self.metadata),
            "cache_mode": combo(self.cache_mode),
            "cache_extent_filter": check(self.cache_extent),
            "cache_reuse": check(self.cache_reuse),
            "cache_skip_unused": check(self.cache_skip),
            "qgis_process": text(self.qgis_process),
            "trust_layer_metadata": check(self.trust),
            "fail_on_invalid_layers": check(self.fail_invalid),
        }

    def _load(self, defaults=False):
        for key, (_get, put) in self._fields().items():
            put(S.DEFAULTS[key] if defaults else S.get(key))
        self.template.setEnabled(self.naming.currentData() == "template")

    def _restore(self):
        self._load(defaults=True)

    def _save(self):
        tpl = self.template.text().strip() or S.DEFAULTS["filename_template"]
        try:
            tpl.format(project="p", layout="l", format="png", dpi=1, date="d", time="t", index=1)
        except (KeyError, IndexError, ValueError) as e:
            ui.warn(self, tr("File name template"),
                                tr("Invalid template: {error}", error=e) + "\n\n" + tr(TEMPLATE_HELP))
            return
        if not self.output_dir.text().strip():
            self.output_dir.setText(S.DEFAULTS["output_dir"])
        self.template.setText(tpl)
        old_language = S.get("language")
        for key, (read, _put) in self._fields().items():
            S.set(key, read())
        self.language_changed = S.get("language") != old_language
        self.accept()

    def _forget_rows(self):
        S.set("row_memory", "{}")
        ui.info(self, tr("Remembered settings"), tr("Remembered layout settings were cleared."))

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, tr("Output folder"), self.output_dir.text())
        if d:
            self.output_dir.setText(d)

    def _pick_qp(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Choose qgis_process"), self.qgis_process.text())
        if path:
            self.qgis_process.setText(path)

    def _detect_qp(self):
        path = find_qgis_process(refresh=True)
        if path:
            self.qgis_process.setText(path)
        else:
            ui.warn(self, "qgis_process", tr("Could not find qgis_process. Choose the file manually."))

    def _test_qp(self):
        path = self.qgis_process.text().strip() or find_qgis_process()
        self.setCursor(Qt.CursorShape.WaitCursor)
        try:
            ok, text = test_qgis_process(path)
        finally:
            self.unsetCursor()
        box = ui.info if ok else ui.warn
        box(self, "qgis_process", f"{path}\n\n{text}")
