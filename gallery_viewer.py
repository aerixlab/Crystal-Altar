#!/usr/bin/env python3
"""
Crystal Altar
--------------
A gallery-style (not file-manager-style) image viewer.
Pick a folder -> browse a smooth thumbnail grid -> click to open a
full-screen lightbox with crossfade transitions, zoom/pan, and a
filmstrip for quick navigation.

Supported: .jpg .jpeg .png .bmp .gif .webp .svg .studio .studio3

Note on Silhouette Studio files (.studio / .studio3):
These are a proprietary, undocumented binary format. There is no public
library that fully parses them. This viewer uses a best-effort heuristic
that scans the raw file for an embedded PNG/JPEG preview image (many
Silhouette Studio versions embed one for their own library thumbnails).
If no embedded preview is found, a placeholder card is shown instead
with the filename - you can still open the file in Silhouette Studio
itself via double-click / "Open with default app".

Install:
    pip install PySide6

Run:
    python gallery_viewer.py
"""

import os
import sys
import subprocess
from pathlib import Path

from PySide6.QtCore import (
    Qt, QSize, QThread, Signal, QRectF, QEasingCurve, QPropertyAnimation,
    QPoint
)
from PySide6.QtGui import (
    QIcon, QPixmap, QImage, QImageReader, QPainter, QColor, QFont,
    QKeySequence, QShortcut
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtSvgWidgets import QGraphicsSvgItem
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QStackedWidget, QListWidget,
    QListWidgetItem, QGraphicsView, QGraphicsScene, QGraphicsPixmapItem,
    QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QSlider, QPushButton,
    QFileDialog, QGraphicsOpacityEffect, QScroller, QSizePolicy, QFrame,
    QMenu, QInputDialog, QMessageBox, QComboBox
)

try:
    from send2trash import send2trash
    HAS_TRASH = True
except ImportError:
    HAS_TRASH = False

RASTER_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp"}
SVG_EXTS = {".svg"}
STUDIO_EXTS = {".studio", ".studio3"}
ALL_EXTS = RASTER_EXTS | SVG_EXTS | STUDIO_EXTS

ACCENT = "#8a5cff"
BG = "#121218"
CARD_BG = "#1b1b24"
CARD_HOVER = "#262633"
NAV_GREY = "#c9c9d2"
NAV_HOVER_BG = "rgba(255,255,255,0.08)"


def load_app_icon() -> QIcon:
    """The app icon is embedded as base64 so the script stays a single file."""
    import base64
    try:
        data = base64.b64decode(_ICON_B64)
        img = QImage.fromData(data)
        if not img.isNull():
            return QIcon(QPixmap.fromImage(img))
    except Exception:
        pass
    return QIcon()



# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def classify(path: str) -> str:
    ext = Path(path).suffix.lower()
    if ext in RASTER_EXTS:
        return "raster"
    if ext in SVG_EXTS:
        return "svg"
    if ext in STUDIO_EXTS:
        return "studio"
    return "other"


def make_placeholder_image(label_text: str, size: int, color: str = ACCENT) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    rect = QRectF(size * 0.06, size * 0.06, size * 0.88, size * 0.88)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawRoundedRect(rect, size * 0.12, size * 0.12)
    painter.setPen(QColor("white"))
    font = QFont()
    font.setBold(True)
    font.setPointSizeF(max(9, size / 9))
    painter.setFont(font)
    painter.drawText(rect, Qt.AlignCenter, label_text)
    painter.end()
    return img


def _find_all_png_blobs(data: bytes):
    """Byte-level scan only - no decoding here, so this stays cheap
    even on large files."""
    blobs = []
    sig = b"\x89PNG\r\n\x1a\n"
    start = 0
    while True:
        idx = data.find(sig, start)
        if idx == -1:
            break
        end = data.find(b"IEND", idx)
        if end == -1:
            break
        end += 4 + 4  # 'IEND' + CRC
        blobs.append(data[idx:end])
        start = end
    return blobs


def _find_all_jpeg_blobs(data: bytes):
    blobs = []
    sig = b"\xff\xd8\xff"
    start = 0
    while True:
        idx = data.find(sig, start)
        if idx == -1:
            break
        end = data.find(b"\xff\xd9", idx)
        if end == -1:
            break
        end += 2
        blobs.append(data[idx:end])
        start = end
    return blobs


def extract_studio_preview_bytes(path: str):
    """Best-effort scan for an embedded PNG or JPEG blob inside a
    Silhouette Studio file. Returns raw bytes or None.

    Silhouette Studio files sometimes embed more than one preview image
    (e.g. a tiny icon for its own library list, plus a larger one). This
    picks the largest by raw byte size - a cheap proxy for resolution
    that avoids decoding every candidate (decoding each one to compare
    pixel dimensions was tried before and was too slow on large files)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None

    candidates = _find_all_png_blobs(data) + _find_all_jpeg_blobs(data)
    if not candidates:
        return None
    return max(candidates, key=len)



def render_svg_thumbnail(path: str, size: int) -> QImage:
    renderer = QSvgRenderer(path)
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    if renderer.isValid():
        vb = renderer.viewBoxF()
        if vb.width() > 0 and vb.height() > 0:
            scale = min(size / vb.width(), size / vb.height())
            w, h = vb.width() * scale, vb.height() * scale
        else:
            w = h = size
        target = QRectF((size - w) / 2, (size - h) / 2, w, h)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.Antialiasing)
        renderer.render(painter, target)
        painter.end()
    return img


# ----------------------------------------------------------------------
# Background thumbnail loader
# ----------------------------------------------------------------------
class ThumbnailLoader(QThread):
    thumbnail_ready = Signal(int, QImage)

    def __init__(self, entries, size=170):
        super().__init__()
        self.entries = entries  # list of (row_index, path, kind)
        self.size = size
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for row, path, kind in self.entries:
            if self._stop:
                return
            img = None
            try:
                if kind == "raster":
                    reader = QImageReader(path)
                    reader.setAutoTransform(True)
                    orig = reader.size()
                    if orig.isValid():
                        orig.scale(self.size, self.size, Qt.KeepAspectRatio)
                        reader.setScaledSize(orig)
                    img = reader.read()
                elif kind == "svg":
                    img = render_svg_thumbnail(path, self.size)
                elif kind == "studio":
                    blob = extract_studio_preview_bytes(path)
                    if blob:
                        candidate = QImage.fromData(blob)
                        if not candidate.isNull():
                            # Fast (nearest-neighbor) instead of smooth:
                            # these previews are dense fields of small,
                            # closely-spaced dots. Smooth/blended scaling
                            # bleeds each dot's anti-aliased edge into its
                            # neighbors, which merges them into a solid
                            # blob instead of keeping them distinct - that
                            # merging is what reads as "thick and dark".
                            img = candidate.scaled(
                                self.size, self.size,
                                Qt.KeepAspectRatio, Qt.FastTransformation
                            )
                    if img is None or img.isNull():
                        img = make_placeholder_image("STUDIO", self.size, "#ff8a5c")
            except Exception:
                img = None

            if img is None or img.isNull():
                img = make_placeholder_image("?", self.size, "#555566")

            self.thumbnail_ready.emit(row, img)


# ----------------------------------------------------------------------
# Zoom / pan preview surface (used inside the lightbox)
# ----------------------------------------------------------------------
class PreviewView(QGraphicsView):
    nav_requested = Signal(int)  # -1 = previous, +1 = next

    def __init__(self):
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = None
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setBackgroundBrush(QColor(BG))
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setStyleSheet("background: transparent; border: none;")
        self.setMouseTracking(True)

    def clear(self):
        self._scene.clear()
        self._item = None

    def show_raster(self, pixmap: QPixmap, force_crisp: bool = False):
        self._scene.clear()
        self._item = QGraphicsPixmapItem(pixmap)
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self._force_crisp = force_crisp
        self._scene.addItem(self._item)
        self._scene.setSceneRect(self._item.boundingRect())
        self.fit_to_window()

    def show_svg(self, path: str):
        self._scene.clear()
        self._item = QGraphicsSvgItem(path)
        self._force_crisp = False
        self._scene.addItem(self._item)
        self._scene.setSceneRect(self._item.boundingRect())
        self.fit_to_window()

    def _update_transform_mode(self):
        # Above ~1:1 zoom, switch off smoothing so pixels stay crisp
        # instead of turning into a soft blur when scaled up.
        # force_crisp (used for Silhouette Studio previews - dense
        # fields of small dots) skips smoothing entirely: blended
        # scaling merges neighboring dots into a solid blob instead of
        # keeping them distinct, which reads as "thick and dark".
        if self._item is None:
            return
        if getattr(self, "_force_crisp", False):
            smooth = False
        else:
            scale = self.transform().m11()
            smooth = scale <= 1.01
        self.setRenderHint(QPainter.SmoothPixmapTransform, smooth)
        if isinstance(self._item, QGraphicsPixmapItem):
            self._item.setTransformationMode(
                Qt.SmoothTransformation if smooth else Qt.FastTransformation
            )

    def fit_to_window(self):
        if self._item is None:
            return
        self.resetTransform()
        self.fitInView(self._item, Qt.KeepAspectRatio)
        self._update_transform_mode()

    def actual_size(self):
        if self._item is None:
            return
        self.resetTransform()
        self._update_transform_mode()

    def zoom_by(self, factor):
        if self._item is None:
            return
        self.scale(factor, factor)
        self._update_transform_mode()

    def wheelEvent(self, event):
        if self._item is None:
            return
        angle = event.angleDelta().y()
        factor = 1.15 if angle > 0 else 1 / 1.15
        self.zoom_by(factor)

    def mousePressEvent(self, event):
        # Click on the blank margin left/right of the image (not the
        # image itself) navigates prev/next instead of starting a pan.
        if event.button() == Qt.LeftButton and self._item is not None:
            scene_pos = self.mapToScene(event.pos())
            item_rect = self._item.sceneBoundingRect()
            if scene_pos.x() < item_rect.left():
                self.nav_requested.emit(-1)
                return
            if scene_pos.x() > item_rect.right():
                self.nav_requested.emit(1)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        # Swap the cursor to a pointing hand over the clickable blank
        # margin, vs. the open hand (pan) cursor over the image itself.
        if self._item is not None:
            scene_pos = self.mapToScene(event.pos())
            item_rect = self._item.sceneBoundingRect()
            if scene_pos.x() < item_rect.left() or scene_pos.x() > item_rect.right():
                self.viewport().setCursor(Qt.PointingHandCursor)
            else:
                self.viewport().setCursor(Qt.OpenHandCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.viewport().setCursor(Qt.OpenHandCursor)
        super().leaveEvent(event)


# ----------------------------------------------------------------------
# Gallery grid (smooth kinetic scrolling, card-styled thumbnails)
# ----------------------------------------------------------------------
class GalleryGrid(QListWidget):
    def __init__(self):
        super().__init__()
        self.setViewMode(QListWidget.IconMode)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(14)
        self.setSelectionMode(QListWidget.SingleSelection)
        self.setFrameShape(QListWidget.NoFrame)
        self.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        QScroller.grabGesture(self.viewport(), QScroller.LeftMouseButtonGesture)
        self.setStyleSheet(f"""
            QListWidget {{
                background: {BG};
                outline: none;
            }}
            QListWidget::item {{
                background: {CARD_BG};
                border-radius: 12px;
                padding: 6px;
                color: #d8d8e2;
            }}
            QListWidget::item:hover {{
                background: {CARD_HOVER};
            }}
            QListWidget::item:selected {{
                background: {CARD_HOVER};
                border: 2px solid {ACCENT};
            }}
        """)


# ----------------------------------------------------------------------
# Lightbox (full gallery-page overlay with crossfade + filmstrip)
# ----------------------------------------------------------------------
class NavZone(QPushButton):
    """A clickable strip for prev/next navigation - wider than a tiny
    arrow icon so it's easy to hit, but visually quiet (no big highlight
    box on hover, just the arrow brightening)."""

    def __init__(self, arrow_char: str, width: int = 64):
        super().__init__(arrow_char)
        self.setCursor(Qt.PointingHandCursor)
        self.setFlat(True)
        self.setFixedWidth(width)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent;
                color: {NAV_GREY};
                border: none;
                font-size: 30px;
                font-weight: 300;
            }}
            QPushButton:hover {{
                color: white;
            }}
            QPushButton:pressed {{
                color: #888;
            }}
        """)


class Lightbox(QWidget):
    closed = Signal()
    open_requested = Signal(str)
    reveal_requested = Signal(str)
    rename_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.setStyleSheet(f"background: {BG};")
        self.entries = []      # list of (path, kind)
        self.current_index = -1
        self._anim = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)

        # top bar
        top = QHBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setStyleSheet("color:#eee; font-size:14px; font-weight:600;")
        top.addWidget(self.title_label, 1)

        self.info_label = QLabel("")
        self.info_label.setStyleSheet("color:#9a9aab; font-size:12px;")
        top.addWidget(self.info_label)

        close_btn = QPushButton("Close  ✕")
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(self._btn_style())
        close_btn.clicked.connect(self.close_lightbox)
        top.addWidget(close_btn)
        layout.addLayout(top)

        # main preview area with wide, edge-to-edge nav zones on each side
        mid = QHBoxLayout()
        mid.setSpacing(0)
        self.prev_btn = NavZone("‹")
        self.next_btn = NavZone("›")
        self.prev_btn.clicked.connect(self.show_prev)
        self.next_btn.clicked.connect(self.show_next)

        self.preview = PreviewView()
        self.opacity_effect = QGraphicsOpacityEffect(self.preview)
        self.preview.setGraphicsEffect(self.opacity_effect)
        self.opacity_effect.setOpacity(1.0)

        mid.addWidget(self.prev_btn)
        mid.addWidget(self.preview, 1)
        mid.addWidget(self.next_btn)
        layout.addLayout(mid, 1)

        self.preview.setContextMenuPolicy(Qt.CustomContextMenu)
        self.preview.customContextMenuRequested.connect(self._show_context_menu)
        self.preview.nav_requested.connect(self._on_preview_nav)

        # zoom controls
        zoom_bar = QHBoxLayout()
        zoom_bar.addStretch(1)
        for text, slot in [
            ("Fit", lambda: self.preview.fit_to_window()),
            ("100%", lambda: self.preview.actual_size()),
            ("−", lambda: self.preview.zoom_by(0.8)),
            ("+", lambda: self.preview.zoom_by(1.25)),
        ]:
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(self._btn_style())
            btn.clicked.connect(slot)
            zoom_bar.addWidget(btn)
        zoom_bar.addStretch(1)
        layout.addLayout(zoom_bar)

        # filmstrip
        self.filmstrip = QListWidget()
        self.filmstrip.setFlow(QListWidget.LeftToRight)
        self.filmstrip.setWrapping(False)
        self.filmstrip.setFixedHeight(84)
        self.filmstrip.setIconSize(QSize(64, 64))
        self.filmstrip.setFrameShape(QListWidget.NoFrame)
        self.filmstrip.setSpacing(6)
        self.filmstrip.setStyleSheet(f"""
            QListWidget {{ background:{BG}; outline:none; }}
            QListWidget::item {{ border-radius:8px; padding:2px; }}
            QListWidget::item:selected {{ border:2px solid {ACCENT}; }}
        """)
        QScroller.grabGesture(self.filmstrip.viewport(), QScroller.LeftMouseButtonGesture)
        self.filmstrip.currentRowChanged.connect(self._on_filmstrip_row)
        layout.addWidget(self.filmstrip)

        QShortcut(QKeySequence(Qt.Key_Left), self, activated=self.show_prev)
        QShortcut(QKeySequence(Qt.Key_Right), self, activated=self.show_next)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.close_lightbox)

    @staticmethod
    def _btn_style():
        return f"""
            QPushButton {{
                background:{CARD_BG}; color:#ddd; border:none;
                border-radius:8px; padding:6px 14px; font-size:12px;
            }}
            QPushButton:hover {{ background:{CARD_HOVER}; }}
        """

    def set_entries(self, entries, icons_by_index, start_index):
        self.entries = entries
        self.filmstrip.blockSignals(True)
        self.filmstrip.clear()
        for path, kind in entries:
            item = QListWidgetItem(icons_by_index.get(id(path), QIcon()), "")
            self.filmstrip.addItem(item)
        self.filmstrip.blockSignals(False)
        self.show_index(start_index)

    def set_filmstrip_icon(self, index, icon: QIcon):
        item = self.filmstrip.item(index)
        if item:
            item.setIcon(icon)

    def show_index(self, index):
        if not (0 <= index < len(self.entries)):
            return
        self.current_index = index
        path, kind = self.entries[index]

        def apply():
            if kind == "svg":
                self.preview.show_svg(path)
            elif kind == "raster":
                pm = QPixmap(path)
                if pm.isNull():
                    pm = QPixmap.fromImage(make_placeholder_image("?", 400, "#555566"))
                self.preview.show_raster(pm)
            else:  # studio
                blob = extract_studio_preview_bytes(path)
                pm = None
                if blob:
                    img = QImage.fromData(blob)
                    if not img.isNull():
                        pm = QPixmap.fromImage(img)
                if pm is None:
                    pm = QPixmap.fromImage(make_placeholder_image("STUDIO FILE", 500, "#ff8a5c"))
                self.preview.show_raster(pm, force_crisp=True)

            self.title_label.setText(os.path.basename(path))
            try:
                size_bytes = os.path.getsize(path)
                size_str = self._human_size(size_bytes)
            except OSError:
                size_str = "?"
            self.info_label.setText(f"{kind.upper()}  ·  {size_str}")

        self._crossfade(apply)

        self.filmstrip.blockSignals(True)
        self.filmstrip.setCurrentRow(index)
        self.filmstrip.scrollToItem(self.filmstrip.currentItem())
        self.filmstrip.blockSignals(False)

    def _crossfade(self, mid_callback):
        if self._anim is not None:
            self._anim.stop()

        fade_out = QPropertyAnimation(self.opacity_effect, b"opacity")
        fade_out.setDuration(120)
        fade_out.setStartValue(self.opacity_effect.opacity())
        fade_out.setEndValue(0.0)
        fade_out.setEasingCurve(QEasingCurve.OutCubic)

        def on_out_finished():
            mid_callback()
            fade_in = QPropertyAnimation(self.opacity_effect, b"opacity")
            fade_in.setDuration(160)
            fade_in.setStartValue(0.0)
            fade_in.setEndValue(1.0)
            fade_in.setEasingCurve(QEasingCurve.InCubic)
            self._anim = fade_in
            fade_in.start()

        fade_out.finished.connect(on_out_finished)
        self._anim = fade_out
        fade_out.start()

    def _on_filmstrip_row(self, row):
        if row >= 0 and row != self.current_index:
            self.show_index(row)

    def show_next(self):
        if self.entries:
            self.show_index((self.current_index + 1) % len(self.entries))

    def show_prev(self):
        if self.entries:
            self.show_index((self.current_index - 1) % len(self.entries))

    def _on_preview_nav(self, direction):
        if direction < 0:
            self.show_prev()
        else:
            self.show_next()

    def close_lightbox(self):
        self.closed.emit()

    def _show_context_menu(self, pos):
        if not (0 <= self.current_index < len(self.entries)):
            return
        path, kind = self.entries[self.current_index]
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background:{CARD_BG}; color:#ddd; border:1px solid #333; padding:4px; }}
            QMenu::item {{ padding:6px 20px; border-radius:4px; }}
            QMenu::item:selected {{ background:{ACCENT}; color:white; }}
        """)
        menu.addAction("Open Real File", lambda: self.open_requested.emit(path))
        menu.addSeparator()
        menu.addAction("Rename", lambda: self.rename_requested.emit(path))
        menu.addAction("Delete", lambda: self.delete_requested.emit(path))
        menu.addSeparator()
        menu.addAction("Show in Folder", lambda: self.reveal_requested.emit(path))
        menu.addAction("Copy Path", lambda: QApplication.clipboard().setText(path))
        menu.exec(self.preview.mapToGlobal(pos))

    @staticmethod
    def _human_size(n):
        for unit in ["B", "KB", "MB", "GB"]:
            if n < 1024:
                return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
            n /= 1024
        return f"{n:.1f}TB"


# ----------------------------------------------------------------------
# Main window
# ----------------------------------------------------------------------
class GalleryWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Crystal Altar")
        self.resize(1300, 850)
        self.setStyleSheet(f"background:{BG};")

        self.folder = None
        self.entries = []          # (path, kind)
        self.icons = {}            # path -> QIcon
        self.loader = None
        self.thumb_size = 170

        self.stack = QStackedWidget()
        self.setCentralWidget(self.stack)

        self._build_gallery_page()
        self._build_lightbox_page()

    # ---------------- gallery (grid) page ----------------
    def _build_gallery_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        top = QHBoxLayout()
        open_btn = QPushButton("📁  Open Folder")
        open_btn.setCursor(Qt.PointingHandCursor)
        open_btn.setStyleSheet(f"""
            QPushButton {{
                background:{ACCENT}; color:white; border:none;
                border-radius:10px; padding:10px 18px; font-size:13px; font-weight:600;
            }}
            QPushButton:hover {{ background:#9d75ff; }}
        """)
        open_btn.clicked.connect(self.choose_folder)
        top.addWidget(open_btn)

        self.path_label = QLabel("No folder selected")
        self.path_label.setStyleSheet("color:#9a9aab; font-size:12px;")
        top.addWidget(self.path_label, 1)

        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search filename...")
        self.search_box.setFixedWidth(220)
        self.search_box.setStyleSheet(f"""
            QLineEdit {{
                background:{CARD_BG}; color:#ddd; border:1px solid #333;
                border-radius:8px; padding:6px 10px; font-size:12px;
            }}
        """)
        self.search_box.textChanged.connect(self._apply_filter)
        top.addWidget(self.search_box)

        sort_label = QLabel("Sort")
        sort_label.setStyleSheet("color:#9a9aab; font-size:12px;")
        top.addWidget(sort_label)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["Name", "Date modified", "Size", "Type"])
        self.sort_combo.setStyleSheet(f"""
            QComboBox {{
                background:{CARD_BG}; color:#ddd; border:1px solid #333;
                border-radius:8px; padding:5px 10px; font-size:12px;
            }}
            QComboBox QAbstractItemView {{
                background:{CARD_BG}; color:#ddd; selection-background-color:{ACCENT};
            }}
        """)
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        top.addWidget(self.sort_combo)

        size_label = QLabel("Size")
        size_label.setStyleSheet("color:#9a9aab; font-size:12px;")
        top.addWidget(size_label)
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setMinimum(100)
        self.size_slider.setMaximum(320)
        self.size_slider.setValue(self.thumb_size)
        self.size_slider.setFixedWidth(120)
        self.size_slider.valueChanged.connect(self._on_thumb_size_changed)
        top.addWidget(self.size_slider)

        layout.addLayout(top)

        self.grid = GalleryGrid()
        self.grid.setIconSize(QSize(self.thumb_size, self.thumb_size))
        self.grid.itemDoubleClicked.connect(self._on_thumbnail_activated)
        self.grid.itemActivated.connect(self._on_thumbnail_activated)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._show_grid_context_menu)
        layout.addWidget(self.grid, 1)

        self.count_label = QLabel("")
        self.count_label.setStyleSheet("color:#666677; font-size:11px;")
        layout.addWidget(self.count_label)

        # empty-state hint shown via count_label / path_label already

        self.stack.addWidget(page)

    def _build_lightbox_page(self):
        self.lightbox = Lightbox()
        self.lightbox.closed.connect(self._close_lightbox)
        self.lightbox.open_requested.connect(self._open_externally)
        self.lightbox.reveal_requested.connect(self._reveal_in_folder)
        self.lightbox.rename_requested.connect(self._rename_path_and_refresh)
        self.lightbox.delete_requested.connect(self._delete_path_and_refresh)
        self.stack.addWidget(self.lightbox)

    # ---------------- context menu (grid) ----------------
    def _show_grid_context_menu(self, pos):
        item = self.grid.itemAt(pos)
        if item is None:
            return
        path = item.data(Qt.UserRole)
        menu = QMenu(self)
        menu.setStyleSheet(f"""
            QMenu {{ background:{CARD_BG}; color:#ddd; border:1px solid #333; padding:4px; }}
            QMenu::item {{ padding:6px 20px; border-radius:4px; }}
            QMenu::item:selected {{ background:{ACCENT}; color:white; }}
        """)
        menu.addAction("Preview", lambda: self._on_thumbnail_activated(item))
        menu.addAction("Open Real File", lambda: self._open_externally(path))
        menu.addSeparator()
        menu.addAction("Rename", lambda: self._rename_item(item))
        menu.addAction("Delete", lambda: self._delete_item(item))
        menu.addSeparator()
        menu.addAction("Show in Folder", lambda: self._reveal_in_folder(path))
        menu.addAction("Copy Path", lambda: QApplication.clipboard().setText(path))
        menu.exec(self.grid.mapToGlobal(pos))

    def _rename_item(self, item):
        path = item.data(Qt.UserRole)
        if self._prompt_rename(path):
            self.load_folder(self.folder)

    def _delete_item(self, item):
        path = item.data(Qt.UserRole)
        if self._prompt_delete(path):
            self.load_folder(self.folder)

    # ---------------- context menu (lightbox) ----------------
    def _rename_path_and_refresh(self, path):
        if self._prompt_rename(path):
            self._close_lightbox()
            self.load_folder(self.folder)

    def _delete_path_and_refresh(self, path):
        if self._prompt_delete(path):
            self._close_lightbox()
            self.load_folder(self.folder)

    # ---------------- shared file-op helpers ----------------
    def _prompt_rename(self, path):
        old_name = os.path.basename(path)
        new_name, ok = QInputDialog.getText(self, "Rename", "New name:", text=old_name)
        if not ok or not new_name or new_name == old_name:
            return None
        new_path = os.path.join(os.path.dirname(path), new_name)
        try:
            os.rename(path, new_path)
            return new_path
        except OSError as e:
            QMessageBox.warning(self, "Rename failed", str(e))
            return None

    def _prompt_delete(self, path):
        warn = "" if HAS_TRASH else "\n(send2trash not installed - this is permanent)"
        reply = QMessageBox.question(
            self, "Confirm delete",
            f"Delete '{os.path.basename(path)}'?{warn}",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return False
        try:
            if HAS_TRASH:
                send2trash(path)
            else:
                os.remove(path)
            return True
        except OSError as e:
            QMessageBox.warning(self, "Delete failed", str(e))
            return False

    @staticmethod
    def _open_externally(path):
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore
        elif sys.platform == "darwin":
            subprocess.call(["open", path])
        else:
            subprocess.call(["xdg-open", path])

    def _reveal_in_folder(self, path):
        self._open_externally(os.path.dirname(path))

    # ---------------- folder loading ----------------
    @staticmethod
    def _scan_recursive(folder):
        """Walk the folder tree and collect every supported image,
        including everything inside subfolders."""
        results = []
        for root, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in files:
                if name.startswith("."):
                    continue
                path = os.path.join(root, name)
                kind = classify(path)
                if kind == "other":
                    continue
                results.append((path, kind))
        return results

    def _sort_entries(self, entries):
        mode = self.sort_combo.currentText()

        def key(entry):
            path, kind = entry
            if mode == "Date modified":
                try:
                    return -os.path.getmtime(path)  # newest first
                except OSError:
                    return 0
            if mode == "Size":
                try:
                    return -os.path.getsize(path)  # largest first
                except OSError:
                    return 0
            if mode == "Type":
                return (Path(path).suffix.lower(), path.lower())
            return path.lower()  # Name (default - same as before Sort existed)

        return sorted(entries, key=key)

    def _on_sort_changed(self):
        if self.folder:
            self.load_folder(self.folder)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose Folder", self.folder or str(Path.home()))
        if folder:
            self.load_folder(folder)

    def load_folder(self, folder):
        self.folder = folder
        self.path_label.setText(folder)
        self.setWindowTitle(f"Crystal Altar - {os.path.basename(folder) or folder}")

        if self.loader is not None:
            self.loader.stop()
            self.loader.wait()
            self.loader = None

        self.grid.clear()
        self.icons.clear()
        self.entries = []

        try:
            scanned = self._sort_entries(self._scan_recursive(folder))
        except OSError as e:
            self.count_label.setText(f"Cannot open folder: {e}")
            return

        placeholder_icon = QIcon(QPixmap.fromImage(make_placeholder_image("", self.thumb_size, "#2a2a35")))
        load_jobs = []

        for path, kind in scanned:
            row = self.grid.count()
            name = os.path.basename(path)
            rel = os.path.relpath(path, folder)
            item = QListWidgetItem(placeholder_icon, Path(name).stem)
            # show subfolder in the tooltip so nested items stay identifiable
            item.setToolTip(rel)
            item.setData(Qt.UserRole, path)
            item.setData(Qt.UserRole + 1, kind)
            item.setSizeHint(QSize(self.thumb_size + 24, self.thumb_size + 44))
            item.setTextAlignment(Qt.AlignHCenter)
            self.grid.addItem(item)
            self.entries.append((path, kind))
            load_jobs.append((row, path, kind))

        self.count_label.setText(f"{len(self.entries)} images")
        self._apply_filter(self.search_box.text())

        if load_jobs:
            self.loader = ThumbnailLoader(load_jobs, self.thumb_size)
            self.loader.thumbnail_ready.connect(self._on_thumbnail_ready)
            self.loader.start()

    def _on_thumbnail_ready(self, row, image: QImage):
        item = self.grid.item(row)
        if item is None:
            return
        icon = QIcon(QPixmap.fromImage(image))
        item.setIcon(icon)
        path = item.data(Qt.UserRole)
        self.icons[path] = icon
        if self.lightbox.entries:
            self.lightbox.set_filmstrip_icon(row, icon)

    def _apply_filter(self, text):
        text = text.lower().strip()
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            item.setHidden(bool(text) and text not in item.text().lower())

    def _on_thumb_size_changed(self, value):
        self.thumb_size = value
        self.grid.setIconSize(QSize(value, value))
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            item.setSizeHint(QSize(value + 24, value + 44))
        if self.folder:
            self.load_folder(self.folder)

    # ---------------- lightbox open/close ----------------
    def _on_thumbnail_activated(self, item: QListWidgetItem):
        path = item.data(Qt.UserRole)
        index = next((i for i, (p, k) in enumerate(self.entries) if p == path), 0)
        icons_by_id = {id(p): self.icons.get(p, QIcon()) for p, k in self.entries}
        self.lightbox.set_entries(self.entries, icons_by_id, index)
        self.stack.setCurrentWidget(self.lightbox)

    def _close_lightbox(self):
        # Keep the gallery grid selection in sync with whatever image
        # the user last landed on via next/previous inside the lightbox.
        if 0 <= self.lightbox.current_index < len(self.lightbox.entries):
            last_path, _kind = self.lightbox.entries[self.lightbox.current_index]
            for i in range(self.grid.count()):
                item = self.grid.item(i)
                if item.data(Qt.UserRole) == last_path:
                    self.grid.setCurrentItem(item)
                    self.grid.scrollToItem(item, QListWidget.PositionAtCenter)
                    break
        self.stack.setCurrentIndex(0)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Crystal Altar")
    icon = load_app_icon()
    app.setWindowIcon(icon)
    win = GalleryWindow()
    win.setWindowIcon(icon)
    win.show()
    sys.exit(app.exec())


_ICON_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAADax0lEQVR42uz9adBuV3YWCD5r733OeadvvLOupNSUg1M52JmJjZ02"
    "lmxsYzB4AImyC0dBERVAVFUbOqKqm54kOTqi6Q4ICqKh4Efb1ZiC4grssstOG09SGjsxONN2pqXMdE6apTt/0zues/da/WOP5/2u"
    "KKojlXml/E7E1b36xnc4ew3Pep5nASfXyXVynVwn18l1cp1cJ9fJdXKdXCfXyXVynVwn18l1cp1cJ9fJdXKdXCfXyXVynVwn18l1"
    "cp1cJ9fJdXKdXCfXyXVynVwn18l1cp1cJ9fJdXKdXCfXyXVynVwn18l1cp1cJ9fJdXKdXCfXyXVynVwn18l1cp1cJ9fJdXKdXCfX"
    "yXVyfRUvOnkJ3uKXCEn45+OPh/f78fzp4p/5348Djz8OSTcIpR9xcp0EgJPr9jrfQo8/Dnr8ceBJgM48Dbr2EARPAo8+Cga+DIeX"
    "CI/931g9+CDo2TMg4Gk8eO0hefYRyOOA0EmAOAkAJ9dX5rA/+STUmTP+kD8C8P/a4XvqKTHnz18fPnetGQzH1VbXdpVTjWpbgB0U"
    "tK39nWBcreCUBtckdn9hD3c3uwW295ffce+9y//wCRe6dAnq2TOgB69BHnkETEQAcBIYTgLAyfX/7/XYY6IefBB05gzooYfgbnXY"
    "P/7xV0Yz3jm1sHKBtb5oO9yrDb2NQee1kgsMtUskYyYakGACQBOBlCIBQBCpACEQsTCYRRggEZEZKUxFaCGWZyJ81Yl+TYm8CoPn"
    "DfOLStGrw2bx6sPfsLN/y8f/lJgiIJwEg5MAcHL9x2b4hx8mu/75jz0ju8vF4utWTO8Vpgeh6N1E6m0MnNFKbTYDhboBjAZEAGGA"
    "GWABRATMTpSCKE2iyL/5Iv4/7OECiAiBAEVKgZT/OUUqD1+OrgW6zq6E6bpjPCdOviDsPsMkHx/b+ee/96FTL4n0q4THnoJ+8NqT"
    "8sgjj5wEhJMAcHLFLP/QQ1BPPwR+gohzMAD94m/t38dq+IFK44+JwvuU0Nt1ZS4MhgSl/Nc5BgAGRJiUsNb+Z1DA8IgACIjIf48i"
    "IFTnFP4BEUkBQ1Is8oHDCYkTAXMIFkQSbhoShgZpBe0fh7PAYg7Yrt0H8xcg+CgM/Vbl5p/4M9+2+2L5vC9dEg0AJ9XBSQD4msz0"
    "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"
    "D97nT3teY3Hm9jO0urpqMMPtzwLAjfjebpzaEMAprJ6JweCn3/3hm1HhtR0pv2Ehz75qeW5xaS5XKDJG0RWmW2SNtFZVQhBTzMzt"
    "VEBIgsgJKgfJjCByAVlYw0vhfO9Egq+3rYJE9cfP82l6fBcziYjjTyS7nfYBamMbdRUDtf3i2jhKdIiBTBCGGawrpmpiVNMomkyA"
    "K/0Rdsajx0bG3FuCf/PSs/j337L6ij3/0LNrZ9W9uNfM5vizAPC0Kg/OnIE4dw6cGkr+ws98/Nk81HeLmr8mZ/EVvTz7/KOLhzCX"
    "SyjFyDuss0xqSWBItr0DP94HIJjJu94K529v3XEZpAhCOgVhZ5lN3h3H3dYKCN4OzX8POog2w2BtVZNtluEuHZHUG0zWSbwBqtJk"
    "rDOqSmC3X+LqaNQfaf2JkvmPWeEPJvP8X1//prt2001/Eie9Au/stJ8FgGdeMPit33q8d/n+zTtk2XxxBvryjOiLe0X+eUvzC6KT"
    "KWSKkSlCpqCzXGgpBAtiEpJtI0ESWYowE4lENJS1y9qtzJlnAwo/SaCU7XvQftvfwbt9kwGzIWbroyC0ZslGkjYSk4qxN5pgdzgY"
    "1DXfPyrrPymZ/2xwSN33LW+96+H0fdjYYLly7l6aNfVmAeAZHwxw5gymCSsf+tBH58ymeIEZ1S8lxityojtygc/vZOLmxd686BUF"
    "FBgSDYquhMoEhGLOldBSWHs8KYhJeO0AIqlcecDGBYCW5md09DIMNsaGCact0tQsoQVMQygrYKIZw6rEuKpGpdYXxtqcqzV/tBLm"
    "YwPWH/3W7/7iB6df79m1s2rr9i0+derUrKE3CwCzNR0McBp0GveK2+/f4msx2H75Z/94GaPsOabkF5LG84SgL8hJPKdQ8oRS4rBg"
    "mu/lheyoDIVSyHOJXAlbAihAKQHBDDbmGqN+K3SqmWEM0GhG1dQY1zXGk4lhpt1a86ZmPF42zcM1zCdqmPvRpYe3cnn+LW95xWj6"
    "9dx7+l65dftJPnfuNM9q+lkAmK3/loAApjMbZ8TKuVME3IvPmSoTcN9fPN7b/Msrhze3J4c6gm6i0qwoIRezjjzOVZNnuTqSSZoD"
    "A7punD04MRMJEEupJMNQv67LbVmoGlLsSZHvN7raoYz7daU38wW1ufvK4srqHXdU13q+GxssV1Zgn+u9s3p+FgBm6wnLEs7cDjp3"
    "7l76XJnCEx6YTp0RKy9aoa3bt/jcuXN8ev00z2i3swAwW09hYDh9+jQBp3H77Wfo3LkVAoCT7v+3bj/JOHMGOHWNB59x30+dwsq5"
    "e1vXw9btJxk4g1PnTjFOe/3Q2Uafrdmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardma"
    "rdmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardmardma"
    "rdmardmardmardmardn6f7P+b1AvhwbWkw9fAAAAAElFTkSuQmCC"
)


if __name__ == "__main__":
    main()
