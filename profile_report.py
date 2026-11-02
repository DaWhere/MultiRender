"""Window with the result of the performance analysis of one layout."""

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import ui
from .i18n import tr
from .perf_analysis import analyze, mb, pct, report_text, secs

NUMERIC = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter


def _table(headers):
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return table


def _bar(share):
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setValue(int(share * 1000))
    bar.setFormat(f"{pct(share)}%")
    return bar


def _cell(table, row, col, text, numeric=False):
    item = QTableWidgetItem(text)
    if numeric:
        item.setTextAlignment(NUMERIC)
    table.setItem(row, col, item)


class ProfileReport(QDialog):
    def __init__(self, name, profile, fmt, dpi, parallel, cache_on, parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Performance – {name}", name=name))
        self.resize(900, 640)
        analysis = analyze(profile, parallel, cache_on)
        self._text = report_text(name, profile, analysis, fmt, dpi)
        lay = QVBoxLayout(self)

        head = tr("Performance of “{name}” – {fmt}, {dpi} dpi", name=name, fmt=fmt.upper(), dpi=dpi)
        sub = tr("Measured time: {secs} · peak memory of the process: {mem}",
                 secs=secs(analysis["total"]), mem=mb(profile.get("peak_mem", 0)))
        if profile.get("atlas_sheet"):
            sub += " · " + tr("Measured on the first atlas sheet.")
        if profile.get("cache_seconds"):
            sub += "<br>" + tr("Measured with the project cache (built in {secs}, once per run).",
                               secs=secs(profile["cache_seconds"]))
        lay.addWidget(QLabel(f"<b>{head}</b><br>{sub}"))

        stages = _table([tr("Stage"), tr("Time"), tr("Share")])
        for r, (label, t, share) in enumerate(analysis["stages"]):
            stages.insertRow(r)
            _cell(stages, r, 0, label)
            _cell(stages, r, 1, secs(t), numeric=True)
            stages.setCellWidget(r, 2, _bar(share))
        stages.setColumnWidth(2, 220)
        stages.setMaximumHeight(stages.verticalHeader().defaultSectionSize() * 6 + 8)
        lay.addWidget(stages)

        lay.addWidget(QLabel(f"<b>{tr('Layers – slowest first')}</b>"))
        layers = _table([tr("Layer"), tr("Source"), tr("Features"), tr("Total"), tr("Data"), tr("Drawing"),
                         tr("Share")])
        for r, e in enumerate(analysis["layers"]):
            layers.insertRow(r)
            name_text = e["name"] + (" " + tr("(not visible at this scale)") if e.get("hidden") else "")
            _cell(layers, r, 0, name_text)
            _cell(layers, r, 1, e["provider"] + (" · " + tr("remote") if e.get("remote") else ""))
            _cell(layers, r, 2, str(e.get("features", 0)) if e["kind"] == "vector" else "–", numeric=True)
            _cell(layers, r, 3, secs(e["render"]), numeric=True)
            _cell(layers, r, 4, secs(e["fetch"]) if e["kind"] == "vector" else "–", numeric=True)
            _cell(layers, r, 5, secs(e["draw"]), numeric=True)
            layers.setCellWidget(r, 6, _bar(e["share"]))
        layers.setColumnWidth(6, 160)
        lay.addWidget(layers, 1)

        lay.addWidget(QLabel(f"<b>{tr('What to improve')}</b>"))
        tips = QLabel("<ul>" + "".join(f"<li>{t}</li>" for t in analysis["tips"]) + "</ul>")
        tips.setWordWrap(True)
        tips.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(tips)
        note = QLabel(tr("Every value is a separate measurement, so the numbers are approximate. “Data” is the time "
                         "of reading the features, “Drawing” the rest of the layer time."))
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        lay.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy = QPushButton(tr("Copy report"))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(self._text))
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.close)
        ui.translate_buttons(buttons)
        lay.addWidget(buttons)

    def report(self):
        return self._text
