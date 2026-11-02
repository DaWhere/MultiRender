"""Standard dialogs with buttons in the language of the plugin.

Qt translates standard buttons (OK, Cancel, Yes, No, Restore Defaults…) according
to the language of QGIS, not of the plugin – so their texts are set here explicitly.
"""

from qgis.PyQt.QtWidgets import QDialogButtonBox, QInputDialog, QMessageBox

from .i18n import tr

SB = QMessageBox.StandardButton
DB = QDialogButtonBox.StandardButton


def _labels():
    return {"Ok": tr("OK"), "Cancel": tr("Cancel"), "Yes": tr("Yes"), "No": tr("No"), "Close": tr("Close"),
            "RestoreDefaults": tr("Restore defaults")}


def translate_buttons(box):
    """Sets the texts of the standard buttons of a QDialogButtonBox or QMessageBox."""
    enum = SB if isinstance(box, QMessageBox) else DB
    for name, text in _labels().items():
        button = box.button(getattr(enum, name))
        if button is not None:
            button.setText(text)
    return box


def message_box(parent, icon, title, text, buttons=SB.Ok, default=None):
    box = QMessageBox(icon, title, text, buttons, parent)
    if default is not None:
        box.setDefaultButton(default)
    return translate_buttons(box)


def _run(box):
    box.exec()
    clicked = box.clickedButton()
    # standardButton() returns the enum in PyQt5 and PyQt6 alike (exec() returns a bare int in PyQt6)
    return box.standardButton(clicked) if clicked is not None else SB.Cancel


def info(parent, title, text):
    _run(message_box(parent, QMessageBox.Icon.Information, title, text))


def warn(parent, title, text):
    _run(message_box(parent, QMessageBox.Icon.Warning, title, text))


def ask(parent, title, text, buttons=SB.Yes | SB.No):
    """Question; returns the clicked standard button (Cancel/No when the window is closed)."""
    result = _run(message_box(parent, QMessageBox.Icon.Question, title, text, buttons))
    if result == SB.Cancel and not buttons & SB.Cancel:
        return SB.No
    return result


def text_input(parent, title, label, text=""):
    """QInputDialog.getText with translated buttons; returns (text, ok)."""
    dlg = QInputDialog(parent)
    dlg.setWindowTitle(title)
    dlg.setLabelText(label)
    dlg.setTextValue(text)
    dlg.setOkButtonText(tr("OK"))
    dlg.setCancelButtonText(tr("Cancel"))
    ok = bool(dlg.exec())
    return dlg.textValue(), ok
