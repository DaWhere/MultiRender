"""Registration of the plugin in the QGIS interface."""

import os

from qgis.PyQt.QtCore import QTimer
from qgis.PyQt.QtGui import QIcon

try:
    from qgis.PyQt.QtWidgets import QAction
except ImportError:  # Qt6
    from qgis.PyQt.QtGui import QAction

from . import i18n
from . import settings as S

MENU = "&MultiRender"


class MultiRenderPlugin:
    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.dialog = None
        self.actions = []
        self._rebuild_pending = False

    def initGui(self):  # noqa: N802
        i18n.set_language(S.get("language"))
        icon = QIcon(os.path.join(self.plugin_dir, "icon.svg"))
        main = QAction(icon, "", self.iface.mainWindow())
        main.setObjectName("multiRenderOpen")
        main.triggered.connect(self.show_dialog)
        settings = QAction("", self.iface.mainWindow())
        settings.setObjectName("multiRenderSettings")
        settings.triggered.connect(self.show_settings)
        self.actions = [main, settings]
        self._retranslate_actions()

        self.iface.addToolBarIcon(main)
        for a in self.actions:
            self.iface.addPluginToMenu(MENU, a)

    def _retranslate_actions(self):
        main, settings = self.actions
        main.setText(i18n.tr("Render layouts in parallel…"))
        settings.setText(i18n.tr("Settings…"))

    def unload(self):
        self._close_dialog()
        for a in self.actions:
            self.iface.removePluginMenu(MENU, a)
        if self.actions:
            self.iface.removeToolBarIcon(self.actions[0])
        self.actions = []

    # ---------------------------------------------------------------- dialog
    def _close_dialog(self):
        if self.dialog is not None:
            self.dialog.shutdown()
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = None

    def show_dialog(self):
        if self._rebuild_pending and self.dialog is not None and not self.dialog.is_busy():
            self._close_dialog()  # the language changed while rendering – rebuild now
            self._rebuild_pending = False
        if self.dialog is None:
            from .dialog import MainDialog

            self.dialog = MainDialog(self.iface, self.plugin_dir, self.iface.mainWindow())
            # deferred: the dialog may be rebuilt, so not from inside its own method
            self.dialog.settingsSaved.connect(
                lambda changed: QTimer.singleShot(0, lambda: self._on_settings_saved(changed)))
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def show_settings(self):
        from .settings_dialog import SettingsDialog

        parent = self.dialog if self.dialog is not None and self.dialog.isVisible() else self.iface.mainWindow()
        dlg = SettingsDialog(parent)
        accepted = dlg.exec()
        if self.dialog is not None:
            self.dialog.reload_settings()
        if accepted:
            self._on_settings_saved(dlg.language_changed)

    def _on_settings_saved(self, language_changed):
        if not language_changed:
            return
        i18n.set_language(S.get("language"))
        self._retranslate_actions()
        if self.dialog is None:
            return
        if self.dialog.is_busy():
            self._rebuild_pending = True  # rebuilt when reopened after rendering
            return
        visible = self.dialog.isVisible()
        self._close_dialog()
        if visible:
            self.show_dialog()
