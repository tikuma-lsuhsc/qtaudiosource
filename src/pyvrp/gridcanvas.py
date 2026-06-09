from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget


class GridCanvas(QWidget):
    # Signal emitted whenever a cell color changes (x, y, QColor)
    cell_changed = Signal(int, int, QColor)

    def __init__(self, rows=16, cols=16, cell_size=30):
        super().__init__()
        self.rows = rows
        self.cols = cols
        self.cell_size = cell_size

        # Track the active drawing color
        self.current_color = QColor(0, 0, 0)

        # Initialize grid data structure with a white background
        self.grid_data = [
            [QColor(255, 255, 255) for _ in range(cols)] for _ in range(rows)
        ]

        # Enforce minimum size based on initial cell sizes
        self.setMinimumSize(cols * cell_size, rows * cell_size)

    def set_current_color(self, color):
        """Updates the color used for drawing."""
        self.current_color = QColor(color)

    def clear_canvas(self):
        """Resets all cells back to white."""
        self.grid_data = [
            [QColor(255, 255, 255) for _ in range(self.cols)] for _ in range(self.rows)
        ]
        self.update()

    def paintEvent(self, event):
        """Renders the grid background and colored cells dynamically."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # Calculate exact cell dimensions based on current widget size
        cell_width = self.width() / self.cols
        cell_height = self.height() / self.rows

        # Draw grid cells
        for r in range(self.rows):
            for c in range(self.cols):
                # Calculate geometry bounding box for the cell
                x = int(c * cell_width)
                y = int(r * cell_height)
                w = int((c + 1) * cell_width) - x
                h = int((r + 1) * cell_height) - y
                rect = QRect(x, y, w, h)

                # Fill cell color
                painter.fillRect(rect, QBrush(self.grid_data[r][c]))

                # Draw grid borders
                painter.setPen(QPen(QColor(200, 200, 200), 1, Qt.PenStyle.SolidLine))
                painter.drawRect(rect)

    def mousePressEvent(self, event):
        """Handles initial click to color a single cell."""
        if event.button() == Qt.MouseButton.LeftButton:
            self._color_cell_at_pos(event.position())

    def mouseMoveEvent(self, event):
        """Handles click-and-drag mechanics to paint continuously."""
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._color_cell_at_pos(event.position())

    def _color_cell_at_pos(self, pos):
        """Helper method to translate cursor position into row/column indices."""
        cell_width = self.width() / self.cols
        cell_height = self.height() / self.rows

        col = int(pos.x() // cell_width)
        row = int(pos.y() // cell_height)

        # Safety boundaries check
        if 0 <= row < self.rows and 0 <= col < self.cols:
            # Only update and emit if the color is actually changing
            if self.grid_data[row][col] != self.current_color:
                self.grid_data[row][col] = QColor(self.current_color)
                self.cell_changed.emit(row, col, self.current_color)
                self.update()  # Triggers a repaint
