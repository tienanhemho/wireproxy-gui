from PyQt6 import QtWidgets, QtGui

class EditProfileDialog(QtWidgets.QDialog):
    def __init__(self, parent: QtWidgets.QWidget, current_name: str, conf_content: str):
        super().__init__(parent)
        self.setWindowTitle("Edit WireGuard Profile")
        self.setModal(True)
        self.resize(700, 500)

        self.name_edit = QtWidgets.QLineEdit(current_name)
        self.name_edit.setPlaceholderText("Profile name (without .conf)")

        self.conf_edit = QtWidgets.QPlainTextEdit()
        self.conf_edit.setPlainText(conf_content)
        self.conf_edit.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.SystemFont.FixedFont)
        self.conf_edit.setFont(font)

        form = QtWidgets.QFormLayout()
        form.addRow("Profile Name:", self.name_edit)

        layout = QtWidgets.QVBoxLayout()
        layout.addLayout(form)
        layout.addWidget(self.conf_edit, 1)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.setLayout(layout)

    def get_profile_name(self) -> str:
        return self.name_edit.text().strip()

    def get_conf_content(self) -> str:
        return self.conf_edit.toPlainText()
