#!/usr/bin/env python3
"""
Name Sign Generator - PySide6 GUI.

Live 2D preview with QPainter, exports STLs via CadQuery in a background thread.
"""

import math
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFontComboBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import Qt, QThread, Signal, QRectF
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QTextCharFormat,
)

from namesign import SignParams, StyledRun, auto_font_sizes, _calc_line_positions, CHAR_WIDTH_RATIO


# ---------------------------------------------------------------------------
# Outline sampling for proper offset preview
# ---------------------------------------------------------------------------


def _build_offset_concave_path(hw, hh, r, d, n_arc=16):
    """Build a QPainterPath for the concave outline offset inward by d.

    Exact geometry: straight edges move inward by d, concave arcs keep
    their center at the plate corner but grow radius to r+d.  Junction
    points are computed analytically.
    """
    R = r + d  # offset arc radius
    path = QPainterPath()

    if hw <= d or hh <= d or R <= 0:
        if hw > d and hh > d:
            path.addRect(QRectF(-(hw - d), -(hh - d), 2 * (hw - d), 2 * (hh - d)))
        return path

    if d >= R:
        path.addRect(QRectF(-(hw - d), -(hh - d), 2 * (hw - d), 2 * (hh - d)))
        return path

    s = math.sqrt(R * R - d * d)  # horizontal/vertical distance from corner to junction

    # Check that edges have positive length
    if hw <= s or hh <= s:
        path.addRect(QRectF(-(hw - d), -(hh - d), 2 * (hw - d), 2 * (hh - d)))
        return path

    # Start at bottom-left junction with bottom edge
    path.moveTo(-hw + s, -hh + d)

    # Bottom edge
    path.lineTo(hw - s, -hh + d)

    # BR arc: center (hw, -hh), from (hw-s, -hh+d) to (hw-d, -hh+s)
    _add_offset_arc(path, hw, -hh, R, hw - s, -hh + d, hw - d, -hh + s, n_arc)

    # Right edge
    path.lineTo(hw - d, hh - s)

    # TR arc: center (hw, hh), from (hw-d, hh-s) to (hw-s, hh-d)
    _add_offset_arc(path, hw, hh, R, hw - d, hh - s, hw - s, hh - d, n_arc)

    # Top edge
    path.lineTo(-hw + s, hh - d)

    # TL arc: center (-hw, hh), from (-hw+s, hh-d) to (-hw+d, hh-s)
    _add_offset_arc(path, -hw, hh, R, -hw + s, hh - d, -hw + d, hh - s, n_arc)

    # Left edge
    path.lineTo(-hw + d, -hh + s)

    # BL arc: center (-hw, -hh), from (-hw+d, -hh+s) to (-hw+s, -hh+d)
    _add_offset_arc(path, -hw, -hh, R, -hw + d, -hh + s, -hw + s, -hh + d, n_arc)

    path.closeSubpath()
    return path


def _add_offset_arc(path, cx, cy, R, x1, y1, x2, y2, n_seg):
    """Add arc segment from (x1,y1) to (x2,y2) on circle (cx,cy,R), clockwise."""
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    delta = a2 - a1
    if delta > 0:
        delta -= 2 * math.pi  # ensure clockwise sweep
    for i in range(1, n_seg + 1):
        t = i / n_seg
        a = a1 + delta * t
        path.lineTo(cx + R * math.cos(a), cy + R * math.sin(a))


# ---------------------------------------------------------------------------
# Preview widget
# ---------------------------------------------------------------------------


class PreviewWidget(QWidget):
    """2D preview of the name sign using QPainter."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.params = SignParams()
        self.setMinimumSize(300, 200)

    def set_params(self, params):
        self.params = params
        self.update()

    def _build_outline_path(self, w, h, r, style):
        """Build a QPainterPath for the sign outline."""
        path = QPainterPath()
        hw, hh = w / 2, h / 2

        if style == "rounded" and r > 0.1:
            r = min(r, hw - 0.1, hh - 0.1)
            path.addRoundedRect(QRectF(-hw, -hh, w, h), r, r)
        elif style == "concave" and r > 0.1:
            r = min(r, hw - 0.1, hh - 0.1)
            segments = 16
            path.moveTo(-hw + r, -hh)
            path.lineTo(hw - r, -hh)
            self._add_concave_arc(path, hw, -hh, r, 180, 90, segments)
            path.lineTo(hw, hh - r)
            self._add_concave_arc(path, hw, hh, r, 270, 180, segments)
            path.lineTo(-hw + r, hh)
            self._add_concave_arc(path, -hw, hh, r, 0, -90, segments)
            path.lineTo(-hw, -hh + r)
            self._add_concave_arc(path, -hw, -hh, r, 90, 0, segments)
            path.closeSubpath()
        else:
            path.addRect(QRectF(-hw, -hh, w, h))

        return path

    @staticmethod
    def _add_concave_arc(path, cx, cy, r, start_deg, end_deg, segments):
        """Add a concave arc to the path (arc centered at corner)."""
        for i in range(1, segments + 1):
            t = i / segments
            angle = math.radians(start_deg + (end_deg - start_deg) * t)
            x = cx + r * math.cos(angle)
            y = cy + r * math.sin(angle)
            path.lineTo(x, y)

    def _build_border_paths(self, p):
        """Build outer and inner border paths with proper constant-distance offset.

        For concave style, uses exact analytical offset geometry.
        For rounded/none, uses the simpler dimension-based approach (which is
        already correct for convex corners).
        """
        off = p.border_offset
        bw = p.border_width
        r = p.corner_radius
        hw, hh = p.width / 2, p.height / 2

        if p.border_style == "concave" and r > 0.1:
            r_clamped = min(r, hw - 0.1, hh - 0.1)
            outer_path = _build_offset_concave_path(hw, hh, r_clamped, off)
            inner_path = _build_offset_concave_path(hw, hh, r_clamped, off + bw)
        else:
            # For rounded corners, offset = shrink dimensions + reduce radius
            # This is geometrically correct for convex arcs
            outer_w = p.width - 2 * off
            outer_h = p.height - 2 * off
            outer_r = max(0, r - off)
            inner_w = outer_w - 2 * bw
            inner_h = outer_h - 2 * bw
            inner_r = max(0, outer_r - bw)

            outer_path = self._build_outline_path(
                outer_w, outer_h, outer_r, p.border_style
            )
            if inner_w > 0 and inner_h > 0:
                inner_path = self._build_outline_path(
                    inner_w, inner_h, inner_r, p.border_style
                )
            else:
                inner_path = None

        return outer_path, inner_path

    def paintEvent(self, event):
        p = self.params
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background
        painter.fillRect(self.rect(), QColor(240, 240, 240))

        if p.width <= 0 or p.height <= 0:
            return

        # Calculate scale to fit widget with margins
        margin = 30
        avail_w = self.width() - 2 * margin
        avail_h = self.height() - 2 * margin
        if avail_w <= 0 or avail_h <= 0:
            return

        scale = min(avail_w / p.width, avail_h / p.height)

        # Center in widget, flip Y so positive Y is up
        painter.translate(self.width() / 2, self.height() / 2)
        painter.scale(scale, -scale)

        # Draw plate outline (white filled)
        outline_path = self._build_outline_path(
            p.width, p.height, p.corner_radius, p.border_style
        )
        painter.setPen(QPen(QColor(180, 180, 180), 0.5 / scale))
        painter.setBrush(QColor(255, 255, 255))
        painter.drawPath(outline_path)

        # Draw border frame
        if p.border_style != "none" and p.border_width > 0:
            outer_path, inner_path = self._build_border_paths(p)

            if outer_path and not outer_path.isEmpty():
                if inner_path and not inner_path.isEmpty():
                    border_path = outer_path - inner_path
                else:
                    border_path = outer_path

                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(0, 0, 0))
                painter.drawPath(border_path)

        # Draw text
        line_texts = [l for l in p.lines if l.strip()]
        if line_texts:
            sizes = (
                p.sizes
                if p.sizes and len(p.sizes) == len(p.lines)
                else auto_font_sizes(p)
            )
            # Build line_data for non-empty lines (text, size, line_index)
            non_empty = []
            for i, line in enumerate(p.lines):
                if line.strip():
                    s = sizes[i] if i < len(sizes) else sizes[-1]
                    non_empty.append((line.strip(), s, i))

            line_data = [(t, s) for t, s, _ in non_empty]
            y_positions = _calc_line_positions(line_data, p.line_spacing)

            # Flip Y back for text rendering (QPainter text is Y-down)
            painter.save()
            painter.scale(1, -1)

            for idx, (text, font_size, line_i) in enumerate(non_empty):
                y_center = y_positions[idx]
                screen_y = -y_center

                # Get runs for this line (styled or global)
                if p.styled_lines and line_i < len(p.styled_lines):
                    runs = p.styled_lines[line_i]
                else:
                    runs = [StyledRun(text=text, bold=p.bold, italic=p.italic, underline=p.underline)]

                # Measure total line width and per-run widths
                pixel_size = max(1, int(font_size))
                run_widths = []
                total_w = 0.0
                max_h = 0.0
                for run in runs:
                    font = QFont(p.font)
                    font.setPixelSize(pixel_size)
                    font.setBold(run.bold)
                    font.setItalic(run.italic)
                    fm = QFontMetricsF(font)
                    w = fm.horizontalAdvance(run.text)
                    h = fm.height()
                    run_widths.append(w)
                    total_w += w
                    max_h = max(max_h, h)

                # Draw each run
                x = -total_w / 2
                for j, run in enumerate(runs):
                    if not run.text:
                        continue
                    font = QFont(p.font)
                    font.setPixelSize(pixel_size)
                    font.setBold(run.bold)
                    font.setItalic(run.italic)
                    font.setUnderline(run.underline)
                    painter.setFont(font)
                    painter.setPen(QColor(0, 0, 0))

                    w = run_widths[j]
                    painter.drawText(
                        QRectF(x, screen_y - max_h / 2, w, max_h),
                        Qt.AlignLeft | Qt.AlignVCenter,
                        run.text,
                    )
                    x += w

            painter.restore()

        painter.end()


# ---------------------------------------------------------------------------
# Parameter panel
# ---------------------------------------------------------------------------


class ParameterPanel(QWidget):
    """Left panel with all sign parameters."""

    parametersChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        container = QWidget()
        self._layout = QVBoxLayout(container)
        self._layout.setSpacing(10)
        self._layout.setContentsMargins(8, 8, 8, 8)

        self._create_text_group()
        self._create_style_group()
        self._create_dimensions_group()
        self._create_border_group()
        self._create_spacing_group()
        self._create_export_group()

        self._layout.addStretch()

        scroll.setWidget(container)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(scroll)

        self.setMinimumWidth(260)
        self.setMaximumWidth(360)

    def _make_spinbox(self, min_val, max_val, default, decimals=1, step=1.0, suffix=" mm"):
        sb = QDoubleSpinBox()
        sb.setRange(min_val, max_val)
        sb.setDecimals(decimals)
        sb.setSingleStep(step)
        sb.setValue(default)
        sb.setSuffix(suffix)
        sb.valueChanged.connect(self._on_changed)
        return sb

    def _create_text_group(self):
        group = QGroupBox("Text")
        layout = QVBoxLayout(group)

        # Formatting toolbar
        toolbar = QHBoxLayout()
        toolbar.setSpacing(2)

        self.bold_btn = QToolButton()
        self.bold_btn.setText("B")
        self.bold_btn.setCheckable(True)
        self.bold_btn.setFixedSize(28, 28)
        self.bold_btn.setStyleSheet("QToolButton { font-weight: bold; }")
        self.bold_btn.clicked.connect(self._toggle_bold)
        toolbar.addWidget(self.bold_btn)

        self.italic_btn = QToolButton()
        self.italic_btn.setText("I")
        self.italic_btn.setCheckable(True)
        self.italic_btn.setFixedSize(28, 28)
        self.italic_btn.setStyleSheet("QToolButton { font-style: italic; }")
        self.italic_btn.clicked.connect(self._toggle_italic)
        toolbar.addWidget(self.italic_btn)

        self.underline_btn = QToolButton()
        self.underline_btn.setText("U")
        self.underline_btn.setCheckable(True)
        self.underline_btn.setFixedSize(28, 28)
        self.underline_btn.setStyleSheet("QToolButton { text-decoration: underline; }")
        self.underline_btn.clicked.connect(self._toggle_underline)
        toolbar.addWidget(self.underline_btn)

        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Rich text editor
        self.text_edit = QTextEdit()
        self.text_edit.setPlainText("Her bor\nOla Nordmann")
        self.text_edit.setMaximumHeight(100)
        self.text_edit.textChanged.connect(self._on_changed)
        self.text_edit.cursorPositionChanged.connect(self._update_format_buttons)
        layout.addWidget(self.text_edit)

        self._layout.addWidget(group)

    def _apply_format(self, fmt):
        """Apply a QTextCharFormat to the current selection or toggle for typing."""
        cursor = self.text_edit.textCursor()
        if cursor.hasSelection():
            cursor.mergeCharFormat(fmt)
            self.text_edit.setTextCursor(cursor)
        else:
            self.text_edit.mergeCurrentCharFormat(fmt)
        self._on_changed()

    def _toggle_bold(self):
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold if self.bold_btn.isChecked() else QFont.Normal)
        self._apply_format(fmt)

    def _toggle_italic(self):
        fmt = QTextCharFormat()
        fmt.setFontItalic(self.italic_btn.isChecked())
        self._apply_format(fmt)

    def _toggle_underline(self):
        fmt = QTextCharFormat()
        fmt.setFontUnderline(self.underline_btn.isChecked())
        self._apply_format(fmt)

    def _update_format_buttons(self):
        """Update B/I/U button states to match current cursor format."""
        fmt = self.text_edit.currentCharFormat()
        self.bold_btn.setChecked(fmt.fontWeight() >= QFont.Bold)
        self.italic_btn.setChecked(fmt.fontItalic())
        self.underline_btn.setChecked(fmt.fontUnderline())

    def _create_style_group(self):
        group = QGroupBox("Style")
        layout = QVBoxLayout(group)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Border Style"))
        self.border_style_combo = QComboBox()
        self.border_style_combo.addItems(["Concave plaque", "Rounded", "None"])
        self.border_style_combo.currentIndexChanged.connect(self._on_changed)
        row1.addWidget(self.border_style_combo)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Font"))
        self.font_combo = QFontComboBox()
        self.font_combo.setCurrentFont(QFont("Arial"))
        self.font_combo.currentFontChanged.connect(self._on_changed)
        row2.addWidget(self.font_combo)
        layout.addLayout(row2)

        self._layout.addWidget(group)

    def _create_dimensions_group(self):
        group = QGroupBox("Dimensions")
        layout = QVBoxLayout(group)

        for label_text, attr, min_v, max_v, default, dec, step in [
            ("Width", "width_spin", 40, 300, 180, 0, 5),
            ("Height", "height_spin", 20, 200, 120, 0, 5),
            ("Thickness", "thickness_spin", 1, 10, 3.0, 1, 0.5),
            ("Text depth", "text_depth_spin", 0.2, 2.0, 0.6, 1, 0.1),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            sb = self._make_spinbox(min_v, max_v, default, dec, step)
            setattr(self, attr, sb)
            row.addWidget(sb)
            layout.addLayout(row)

        self._layout.addWidget(group)

    def _create_border_group(self):
        group = QGroupBox("Border")
        layout = QVBoxLayout(group)

        for label_text, attr, min_v, max_v, default, dec, step in [
            ("Offset", "border_offset_spin", 0, 30, 6, 0, 1),
            ("Width", "border_width_spin", 0, 10, 2, 1, 0.5),
            ("Corner radius", "corner_radius_spin", 0, 40, 12, 0, 1),
        ]:
            row = QHBoxLayout()
            row.addWidget(QLabel(label_text))
            sb = self._make_spinbox(min_v, max_v, default, dec, step)
            setattr(self, attr, sb)
            row.addWidget(sb)
            layout.addLayout(row)

        self._layout.addWidget(group)

    def _create_spacing_group(self):
        group = QGroupBox("Text Layout")
        layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel("Line spacing"))
        self.line_spacing_spin = self._make_spinbox(0.8, 3.0, 1.3, 1, 0.1, suffix="")
        row.addWidget(self.line_spacing_spin)
        layout.addLayout(row)

        self._layout.addWidget(group)

    def _create_export_group(self):
        self.export_btn = QPushButton("Export STLs")
        self.export_btn.setMinimumHeight(36)
        self._layout.addWidget(self.export_btn)

    def _on_changed(self):
        self.parametersChanged.emit()

    def get_border_style_str(self):
        idx = self.border_style_combo.currentIndex()
        return ["concave", "rounded", "none"][idx]

    def get_styled_lines(self):
        """Extract per-run styled text from the QTextEdit."""
        doc = self.text_edit.document()
        styled_lines = []
        for i in range(doc.blockCount()):
            block = doc.findBlockByNumber(i)
            runs = []
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.isValid():
                    fmt = fragment.charFormat()
                    runs.append(StyledRun(
                        text=fragment.text(),
                        bold=fmt.fontWeight() >= QFont.Bold,
                        italic=fmt.fontItalic(),
                        underline=fmt.fontUnderline(),
                    ))
                it += 1
            if not runs:
                runs = [StyledRun(text="")]
            styled_lines.append(runs)
        return styled_lines

    def get_params(self):
        styled_lines = self.get_styled_lines()
        lines = ["".join(run.text for run in runs) for runs in styled_lines]
        if not any(l.strip() for l in lines):
            lines = [""]
            styled_lines = [[StyledRun(text="")]]

        return SignParams(
            lines=lines,
            styled_lines=styled_lines,
            sizes=None,  # Auto
            width=self.width_spin.value(),
            height=self.height_spin.value(),
            thickness=self.thickness_spin.value(),
            text_depth=self.text_depth_spin.value(),
            font=self.font_combo.currentFont().family(),
            border_style=self.get_border_style_str(),
            corner_radius=self.corner_radius_spin.value(),
            border_offset=self.border_offset_spin.value(),
            border_width=self.border_width_spin.value(),
            line_spacing=self.line_spacing_spin.value(),
        )


# ---------------------------------------------------------------------------
# Export thread
# ---------------------------------------------------------------------------


class ExportThread(QThread):
    """Background thread for CadQuery STL generation."""

    finished = Signal(str, str)  # black_file, white_file
    error = Signal(str)
    progress = Signal(str)

    def __init__(self, params, output_dir, prefix, parent=None):
        super().__init__(parent)
        self.params = params
        self.output_dir = output_dir
        self.prefix = prefix

    def run(self):
        try:
            self.progress.emit("Importing CadQuery...")
            from namesign import generate_sign, export_stl

            self.progress.emit("Generating geometry...")
            black, white = generate_sign(self.params)

            black_file = str(Path(self.output_dir) / f"{self.prefix}_black.stl")
            white_file = str(Path(self.output_dir) / f"{self.prefix}_white.stl")

            if black is not None:
                self.progress.emit("Exporting black piece...")
                export_stl(black, black_file)
            else:
                black_file = ""

            self.progress.emit("Exporting white piece...")
            export_stl(white, white_file)

            self.finished.emit(black_file, white_file)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Name Sign Generator")
        self.setMinimumSize(900, 600)

        self.export_thread = None

        self._setup_central_widget()
        self._setup_status_bar()

        self.parameter_panel.parametersChanged.connect(self._update_preview)
        self.parameter_panel.export_btn.clicked.connect(self._on_export)

        self._update_preview()

    def _setup_central_widget(self):
        central = QWidget()
        self.setCentralWidget(central)

        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal)

        self.parameter_panel = ParameterPanel()
        splitter.addWidget(self.parameter_panel)

        self.preview = PreviewWidget()
        splitter.addWidget(self.preview)

        splitter.setSizes([300, 600])
        layout.addWidget(splitter)

    def _setup_status_bar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_label = QLabel("Ready")
        self.status_bar.addWidget(self.status_label, stretch=1)

    def _update_preview(self):
        params = self.parameter_panel.get_params()
        self.preview.set_params(params)

    def _on_export(self):
        if self.export_thread is not None and self.export_thread.isRunning():
            return

        params = self.parameter_panel.get_params()

        default_dir = str(Path.home() / "Desktop")
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export STL files (prefix)",
            str(Path(default_dir) / "namesign"),
            "All Files (*)",
        )
        if not file_path:
            return

        output_dir = str(Path(file_path).parent)
        prefix = Path(file_path).stem

        self.parameter_panel.export_btn.setEnabled(False)
        self.status_label.setText("Generating...")

        self.export_thread = ExportThread(params, output_dir, prefix)
        self.export_thread.finished.connect(self._on_export_finished)
        self.export_thread.error.connect(self._on_export_error)
        self.export_thread.progress.connect(self._on_export_progress)
        self.export_thread.start()

    def _on_export_progress(self, msg):
        self.status_label.setText(msg)

    def _on_export_finished(self, black_file, white_file):
        self.parameter_panel.export_btn.setEnabled(True)
        self.status_label.setText("Export complete!")

        msg = "STL files exported:\n\n"
        if black_file:
            msg += f"Black: {black_file}\n"
        msg += f"White: {white_file}"

        QMessageBox.information(self, "Export Complete", msg)

    def _on_export_error(self, error_msg):
        self.parameter_panel.export_btn.setEnabled(True)
        self.status_label.setText(f"Export failed: {error_msg}")
        QMessageBox.critical(
            self, "Export Error", f"Failed to generate STLs:\n\n{error_msg}"
        )


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
