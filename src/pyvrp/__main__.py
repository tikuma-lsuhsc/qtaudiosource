import sys

from PySide6 import QtWidgets

from .pyvrp import AudioRecorderGUI

if __name__ == "__main__":
    app = QtWidgets.QApplication([])

    app.setApplicationName("Voice Range Profiler")

    widget = AudioRecorderGUI()
    widget.show()

    sys.exit(app.exec())
