"""Panel with the list of finished prints and packing them into a ZIP archive."""

import os
import zipfile
from datetime import datetime

from qgis.core import Qgis, QgsApplication, QgsTask
from qgis.PyQt.QtCore import Qt, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import ui
from .i18n import num, tr

PATH_ROLE = Qt.ItemDataRole.UserRole
STORED_EXT = {".png", ".jpg", ".jpeg", ".pdf", ".webp", ".tif", ".tiff"}  # already compressed


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} B" if unit == "B" else f"{num(n)} {unit}"
        n /= 1024
    return f"{num(n)} GB"


def _common_dir(files):
    """Common folder of the files, or None (e.g. files on different Windows drives)."""
    try:
        return os.path.commonpath([os.path.dirname(os.path.abspath(f)) for f in files])
    except ValueError:
        return None


def _arcnames(files):
    """Paths inside the archive: relative to the common folder, without name clashes."""
    base = _common_dir(files)
    used, out = set(), []
    for f in files:
        name = os.path.relpath(f, base) if base else os.path.basename(f)
        stem, ext = os.path.splitext(name)
        candidate, n = name, 1
        while candidate.lower() in used:
            candidate = f"{stem}_{n}{ext}"
            n += 1
        used.add(candidate.lower())
        out.append(candidate)
    return out


def _zip_files(task, files, target):
    """Runs in the background (QgsTask) – does not touch the interface."""
    total = len(files)
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for i, (f, arc) in enumerate(zip(files, _arcnames(files))):
            if task.isCanceled():
                raise RuntimeError("canceled")
            # PNG/JPG/PDF/TIFF are already compressed – deflate would only slow it down
            method = zipfile.ZIP_STORED if os.path.splitext(f)[1].lower() in STORED_EXT else zipfile.ZIP_DEFLATED
            zf.write(f, arc, compress_type=method)
            task.setProgress(100.0 * (i + 1) / total)
    return target


class OutputsPanel(QWidget):
    message = pyqtSignal(str)

    def __init__(self, iface=None, parent=None):
        super().__init__(parent)
        self.iface = iface
        self._task = None
        self._locked = False
        self._sizes = {}  # path -> size; fast duplicate check and total

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        head.addWidget(QLabel(f"<b>{tr('Prints')}</b>"))
        head.addStretch(1)
        self.count = QLabel()
        head.addWidget(self.count)
        lay.addLayout(head)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels([tr("File"), tr("Layout"), tr("Size")])
        self.tree.setRootIsDecorated(False)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self._open_path(item.data(0, PATH_ROLE)))
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        lay.addWidget(self.tree, 1)

        row1 = QHBoxLayout()
        self.btn_open = QPushButton(QgsApplication.getThemeIcon("/mActionFileOpen.svg"), tr("Open"))
        self.btn_open.clicked.connect(self.open_selected)
        self.btn_folder = QPushButton(tr("Show folder"))
        self.btn_folder.clicked.connect(self.show_folder)
        row1.addWidget(self.btn_open)
        row1.addWidget(self.btn_folder)
        lay.addLayout(row1)
        row2 = QHBoxLayout()
        self.btn_zip = QPushButton(QgsApplication.getThemeIcon("/mActionFileSave.svg"), tr("Pack to ZIP…"))
        self.btn_zip.setToolTip(tr("Pack the selected files (or all of them when nothing is selected)"))
        self.btn_zip.clicked.connect(self.zip_files)
        self.btn_clear = QPushButton(tr("Clear list"))
        self.btn_clear.setToolTip(tr("Removes the entries from the list – the files stay on disk"))
        self.btn_clear.clicked.connect(self.clear)
        row2.addWidget(self.btn_zip)
        row2.addWidget(self.btn_clear)
        lay.addLayout(row2)
        self._update_buttons()

    # ------------------------------------------------------------------
    def add_files(self, files, layout_name=""):
        items = []
        for f in files:
            if f in self._sizes:
                continue
            try:
                size = os.path.getsize(f)
            except OSError:
                continue
            self._sizes[f] = size
            item = QTreeWidgetItem([os.path.basename(f), layout_name, human_size(size)])
            item.setData(0, PATH_ROLE, f)
            item.setToolTip(0, f)
            items.append(item)
        if items:
            self.tree.addTopLevelItems(items)  # one refresh instead of many
            self._update_buttons()

    def files(self, selected_only=False):
        if selected_only:
            paths = [it.data(0, PATH_ROLE) for it in self.tree.selectedItems()]
        else:
            paths = list(self._sizes)
        return [p for p in paths if os.path.isfile(p)]

    def clear(self):
        self.tree.clear()
        self._sizes.clear()
        self._update_buttons()

    def set_locked(self, locked):
        """While rendering the list can be browsed, but not packed or cleared."""
        self._locked = locked
        self._update_buttons()

    def _update_buttons(self):
        n = len(self._sizes)
        self.count.setText(tr("{n} files · {size}", n=n, size=human_size(sum(self._sizes.values()))))
        self.btn_open.setEnabled(bool(self.tree.selectedItems()))
        self.btn_folder.setEnabled(n > 0)
        self.btn_zip.setEnabled(n > 0 and self._task is None and not self._locked)
        self.btn_clear.setEnabled(n > 0 and not self._locked)

    @staticmethod
    def _open_path(path):
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_selected(self):
        for f in self.files(selected_only=True)[:10]:
            self._open_path(f)

    def show_folder(self):
        files = self.files(selected_only=True) or self.files()
        if files:
            self._open_path(os.path.dirname(files[0]))

    # ------------------------------------------------------------- ZIP
    def zip_files(self):
        files = self.files(selected_only=True) or self.files()
        if not files:
            return
        default_dir = _common_dir(files) or os.path.dirname(files[0])
        name = f"prints_{datetime.now():%Y-%m-%d_%H%M%S}.zip"
        target, _ = QFileDialog.getSaveFileName(
            self, tr("Pack prints to ZIP"), os.path.join(default_dir, name), tr("ZIP archive (*.zip)")
        )
        if not target:
            return
        if not target.lower().endswith(".zip"):
            target += ".zip"
        if os.path.abspath(target) in {os.path.abspath(f) for f in files}:
            ui.warn(self, "ZIP", tr("The archive cannot overwrite a file that is being packed."))
            return

        self.message.emit(tr("Packing {n} files into {path}…", n=len(files), path=target))
        self.btn_zip.setText(tr("Packing…"))
        self._task = QgsTask.fromFunction(
            tr("MultiRender – ZIP ({n} files)", n=len(files)), _zip_files, files, target,
            on_finished=self._zip_finished,
        )
        self._update_buttons()
        QgsApplication.taskManager().addTask(self._task)

    def _zip_finished(self, exception, result=None):
        self._task = None
        self.btn_zip.setText(tr("Pack to ZIP…"))
        self._update_buttons()
        if exception is not None or not result:
            self.message.emit("✗ " + tr("Packing failed: {error}", error=exception))
            ui.warn(self, "ZIP", tr("Could not create the archive:\n{error}", error=exception))
            return
        size = human_size(os.path.getsize(result))
        self.message.emit("✓ " + tr("Archive created: {path} ({size})", path=result, size=size))
        if self.iface is not None:
            self.iface.messageBar().pushMessage(
                "MultiRender", tr("ZIP archive created: {name} ({size})", name=os.path.basename(result), size=size),
                Qgis.MessageLevel.Success, 6,
            )
        ans = ui.ask(self, "ZIP", tr("Archive created ({size}):\n{path}\n\nOpen the folder?",
                                                   size=size, path=result))
        if ans == QMessageBox.StandardButton.Yes:
            self._open_path(os.path.dirname(result))
