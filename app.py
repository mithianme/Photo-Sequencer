"""
Photo Sequencer - drop photos in, drag them into the order you want, export the
whole set in that exact order.

The order lives in one place: the row order of the grid. Everything else in here
is either getting photographs into that grid, or writing it back out.
"""

import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from PySide6.QtCore import (
    QMimeData, QObject, QPoint, QRect, QRectF, QRunnable, QSettings, QSize, Qt,
    QThread, QThreadPool, QTimer, Signal, Slot,
)
from PySide6.QtGui import (
    QColor, QDrag, QFont, QFontDatabase, QIcon, QImage, QKeySequence, QPainter,
    QPainterPath, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QProgressDialog, QPushButton, QRadioButton, QSlider,
    QSpinBox, QStyle, QStyledItemDelegate, QVBoxLayout, QWidget,
)

APP_NAME = "Photo Sequencer"
APP_VERSION = "1.0"

# Formats Pillow reads out of the box. HEIC needs the pillow-heif package and is
# left out deliberately rather than half-working.
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".jfif", ".png", ".webp", ".tif", ".tiff",
    ".bmp", ".gif", ".ppm", ".pgm",
}

# Thumbnails are decoded once at this size and scaled down for display, so the
# size slider costs nothing.
THUMB_CACHE_PX = 480

ROLE_PHOTO = Qt.ItemDataRole.UserRole + 1
MIME_INTERNAL = "application/x-photo-sequencer-rows"

# --- palette ---------------------------------------------------------------
# Warm paper, soft ink, and one muted clay accent. Quiet on purpose - the
# photographs are the loud thing.
PAPER = "#FBF8F4"
PAPER_2 = "#F3ECE3"
PAPER_3 = "#EBE1D5"
INK = "#211E1A"
INK_SOFT = "#4E463E"
MUTED = "#877C72"
LINE = "#E3D8CA"
ACCENT = "#96775A"
ACCENT_2 = "#C4A98C"
ON_DARK = "#FDFBF8"

# Both typefaces are SIL Open Font License, so they ship inside the .exe
# (fonts/). load_fonts() falls back to Georgia and Segoe UI when the files are
# not there.
SERIF = "Cormorant Garamond"
SANS = "Jost"
SERIF_FAMILY = "Georgia"
SANS_FAMILY = "Segoe UI"

# What the widgets ask for, mapped onto that palette.
BG = PAPER_2            # the canvas thumbnails sit on
PANEL = PAPER           # toolbar, status bar, dialogs
CARD = PAPER            # one photograph's card
CARD_SEL = "#FFFFFF"    # ...once it is selected
TEXT = INK
TEXT_DIM = MUTED
BORDER = LINE


def natural_key(text):
    """Sort key that puts IMG_2 before IMG_10."""
    return [int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", text)]


def human_size(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


@dataclass
class Photo:
    path: Path
    size: int = 0
    mtime: float = 0.0
    taken: float = 0.0          # EXIF DateTimeOriginal, falls back to mtime
    taken_checked: bool = False
    pixmap: QPixmap = None
    failed: bool = False
    requested: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def has_meta(self):
        return any(self.meta.values())

    @property
    def name(self):
        return self.path.name

    @property
    def stem(self):
        return self.path.stem

    @property
    def suffix(self):
        return self.path.suffix

    @classmethod
    def load(cls, path):
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return None
        return cls(path=p, size=st.st_size, mtime=st.st_mtime, taken=st.st_mtime)


class ThumbSignals(QObject):
    done = Signal(str, QImage)
    failed = Signal(str)


class ThumbJob(QRunnable):
    """Decodes one photograph to a thumbnail off the GUI thread."""

    def __init__(self, path, signals):
        super().__init__()
        self.path = str(path)
        self.signals = signals

    @Slot()
    def run(self):
        try:
            with Image.open(self.path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((THUMB_CACHE_PX, THUMB_CACHE_PX), Image.LANCZOS)
                img = img.convert("RGB")
                data = img.tobytes("raw", "RGB")
                qimg = QImage(
                    data, img.width, img.height, img.width * 3,
                    QImage.Format.Format_RGB888,
                ).copy()   # copy: `data` dies with this scope
        except Exception:
            self.signals.failed.emit(self.path)
            return
        self.signals.done.emit(self.path, qimg)


def read_taken(path):
    """EXIF capture time as a timestamp, or 0 if the file does not carry one."""
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
            if raw:
                return datetime.strptime(str(raw), "%Y:%m:%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return 0.0


# ---------------------------------------------------------------------------
#  Grid
# ---------------------------------------------------------------------------

class PhotoDelegate(QStyledItemDelegate):
    """Draws one photograph as a card: thumbnail, position badge, filename."""

    LABEL_H = 26
    PAD = 8

    def __init__(self, grid):
        super().__init__(grid)
        self.grid = grid

    def sizeHint(self, option, index):
        s = self.grid.cell_px
        return QSize(s, s + self.LABEL_H)

    def paint(self, painter, option, index):
        photo = index.data(ROLE_PHOTO)
        if photo is None:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        rect = option.rect.adjusted(4, 4, -4, -4)

        card = QPainterPath()
        card.addRoundedRect(QRectF(rect), 3, 3)
        painter.fillPath(card, QColor(CARD_SEL if selected else CARD))
        if not selected:
            painter.setPen(QPen(QColor(LINE), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)

        img_rect = rect.adjusted(
            self.PAD, self.PAD, -self.PAD, -(self.LABEL_H + self.PAD // 2)
        )
        if photo.pixmap is not None and not photo.pixmap.isNull():
            pm = photo.pixmap
            scaled = pm.size().scaled(
                img_rect.size(), Qt.AspectRatioMode.KeepAspectRatio
            )
            target = QRect(QPoint(0, 0), scaled)
            target.moveCenter(img_rect.center())
            clip = QPainterPath()
            clip.addRoundedRect(QRectF(target), 2, 2)
            painter.save()
            painter.setClipPath(clip)
            painter.drawPixmap(target, pm)
            painter.restore()
        else:
            painter.setPen(QColor(TEXT_DIM))
            painter.drawText(
                img_rect, Qt.AlignmentFlag.AlignCenter,
                "unreadable" if photo.failed else "...",
            )

        # Position badge - the whole point of the tool, so it stays legible.
        f = QFont(painter.font())
        f.setBold(True)
        f.setPointSizeF(max(7.5, min(10.0, self.grid.cell_px / 16)))
        painter.setFont(f)
        label = str(index.row() + 1)
        w = max(20, painter.fontMetrics().horizontalAdvance(label) + 12)
        badge = QRect(img_rect.left(), img_rect.top(), w, 19)
        bpath = QPainterPath()
        bpath.addRoundedRect(QRectF(badge), 9.5, 9.5)
        painter.fillPath(bpath, QColor(ACCENT) if selected
                         else QColor(0x21, 0x1E, 0x1A, 185))
        painter.setPen(QColor(ON_DARK))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)

        if photo.has_meta:
            box = QRect(img_rect.right() - 19, img_rect.top(), 19, 19)
            bg = QPainterPath()
            bg.addRoundedRect(QRectF(box), 9.5, 9.5)
            painter.fillPath(bg, QColor(ACCENT))
            tag = QPainterPath()
            left, top = box.left() + 4.5, box.top() + 5.5
            right, bottom = box.right() - 4.0, box.bottom() - 5.0
            tag.moveTo(left, (top + bottom) / 2)
            tag.lineTo(left + 4.0, top)
            tag.lineTo(right, top)
            tag.lineTo(right, bottom)
            tag.lineTo(left + 4.0, bottom)
            tag.closeSubpath()
            painter.fillPath(tag, QColor(ON_DARK))
            painter.setPen(QPen(QColor(ACCENT), 1.4))
            painter.drawPoint(int(left + 4.6), int((top + bottom) / 2))

        f.setBold(False)
        f.setPointSizeF(8.0)
        painter.setFont(f)
        text_rect = QRect(
            rect.left() + 6, rect.bottom() - self.LABEL_H,
            rect.width() - 12, self.LABEL_H - 4,
        )
        elided = painter.fontMetrics().elidedText(
            photo.name, Qt.TextElideMode.ElideMiddle, text_rect.width()
        )
        painter.setPen(QColor(TEXT if selected else TEXT_DIM))
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, elided)

        if selected:
            painter.setPen(QPen(QColor(ACCENT), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(QRectF(rect).adjusted(1, 1, -1, -1), 3, 3)

        painter.restore()


class PhotoGrid(QListWidget):
    """
    Reorderable thumbnail grid.

    Two things are done by hand here. The drop position, because in an icon-mode
    view it has to be worked out from where the pointer actually is, and owning
    it means the model is never left holding a half-finished move. And the start
    of the drag, because the view is in Movement.Static - the only mode that
    reliably reflows every thumbnail back into model order after a move, but one
    that also switches the view to DropOnly and so would never begin a drag on
    its own.
    """

    orderChanged = Signal()
    filesDropped = Signal(list, int)
    countChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cell_px = 168
        self._drop_index = None
        self._by_path = {}
        self._press_pos = QPoint()
        self._press_item = None

        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setSpacing(4)
        self.setUniformItemSizes(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setSelectionRectVisible(True)
        # Movement.Static is the one mode that reliably reflows every thumbnail
        # back into model order after a move. Setting it also switches dragging
        # off and takes drops off the viewport, so both go back on here - and it
        # has to be the viewport, because that is the widget a drag lands on.
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(False)      # we draw our own caret
        self.setItemDelegate(PhotoDelegate(self))
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(24)
        self.setFrameShape(QFrame.Shape.NoFrame)

        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(2, min(6, os.cpu_count() or 4)))
        self.signals = ThumbSignals()
        self.signals.done.connect(self._thumb_ready)
        self.signals.failed.connect(self._thumb_failed)

        # Only decode what is on screen (plus a screen either side), so a
        # thousand-photo import does not queue a thousand decodes at once.
        self._sweep = QTimer(self)
        self._sweep.setInterval(90)
        self._sweep.setSingleShot(True)
        self._sweep.timeout.connect(self._request_visible)
        self.verticalScrollBar().valueChanged.connect(lambda _: self._sweep.start())

    # --- contents ---------------------------------------------------------

    def photos(self):
        return [self.item(i).data(ROLE_PHOTO) for i in range(self.count())]

    def _make_item(self, photo):
        item = QListWidgetItem()
        item.setData(ROLE_PHOTO, photo)
        item.setToolTip(
            f"{photo.name}\n{human_size(photo.size)}\n{photo.path.parent}"
        )
        item.setSizeHint(QSize(self.cell_px, self.cell_px + PhotoDelegate.LABEL_H))
        return item

    def collect(self, paths):
        """Expands a mixed list of files and folders into image paths."""
        found = []
        for raw in paths:
            p = Path(raw)
            if p.is_dir():
                subs = [s for s in p.rglob("*")
                        if s.is_file() and s.suffix.lower() in IMAGE_EXTS]
                subs.sort(key=lambda x: natural_key(str(x)))
                found.extend(subs)
            elif p.is_file() and p.suffix.lower() in IMAGE_EXTS:
                found.append(p)
        return found

    def add_paths(self, paths, at=None):
        """Adds files/folders, skipping anything already in the list."""
        existing = {str(p.path).lower() for p in self.photos()}
        added = 0
        skipped = 0
        for p in self.collect(paths):
            key = str(p).lower()
            if key in existing:
                skipped += 1
                continue
            photo = Photo.load(p)
            if photo is None:
                skipped += 1
                continue
            existing.add(key)
            item = self._make_item(photo)
            if at is None:
                self.addItem(item)
            else:
                self.insertItem(at + added, item)
            self._by_path[str(p)] = photo
            added += 1

        if added:
            self.countChanged.emit()
            self.orderChanged.emit()
            self.viewport().update()
            self._sweep.start()
        return added, skipped

    def remove_selected(self):
        rows = sorted((self.row(i) for i in self.selectedItems()), reverse=True)
        for r in rows:
            item = self.takeItem(r)
            photo = item.data(ROLE_PHOTO)
            self._by_path.pop(str(photo.path), None)
            del item
        if rows:
            self.countChanged.emit()
            self.orderChanged.emit()
            self.viewport().update()
        return len(rows)

    def clear_all(self):
        self.clear()
        self._by_path.clear()
        self.countChanged.emit()
        self.orderChanged.emit()

    def set_cell_px(self, px):
        self.cell_px = int(px)
        h = self.cell_px + PhotoDelegate.LABEL_H
        for i in range(self.count()):
            self.item(i).setSizeHint(QSize(self.cell_px, h))
        self.doItemsLayout()
        self.viewport().update()
        self._sweep.start()

    def sort_photos(self, key, reverse=False):
        keep = {id(self.item(i)) for i in range(self.count())
                if self.item(i).isSelected()}
        items = [self.takeItem(0) for _ in range(self.count())]
        items.sort(key=lambda it: key(it.data(ROLE_PHOTO)), reverse=reverse)
        for it in items:
            self.addItem(it)
            if id(it) in keep:
                it.setSelected(True)
        self.orderChanged.emit()
        self.viewport().update()
        self._sweep.start()

    def reverse_order(self):
        items = [self.takeItem(0) for _ in range(self.count())]
        for it in reversed(items):
            self.addItem(it)
        self.orderChanged.emit()
        self.viewport().update()
        self._sweep.start()

    # --- thumbnails -------------------------------------------------------

    def _request_visible(self):
        if not self.count():
            return
        vp_h = self.viewport().height()
        for i in range(self.count()):
            photo = self.item(i).data(ROLE_PHOTO)
            if photo.requested or photo.failed or photo.pixmap is not None:
                continue
            r = self.visualRect(self.model().index(i, 0))
            if r.bottom() < -vp_h or r.top() > vp_h * 2:
                continue
            photo.requested = True
            self.pool.start(ThumbJob(photo.path, self.signals))

    @Slot(str, QImage)
    def _thumb_ready(self, path, image):
        photo = self._by_path.get(path)
        if photo is None:
            return
        photo.pixmap = QPixmap.fromImage(image)
        self.viewport().update()

    @Slot(str)
    def _thumb_failed(self, path):
        photo = self._by_path.get(path)
        if photo is not None:
            photo.failed = True
            self.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sweep.start()

    def showEvent(self, event):
        super().showEvent(event)
        self._sweep.start()

    # --- drag and drop ----------------------------------------------------

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_pos = event.position().toPoint()
            self._press_item = self.itemAt(self._press_pos)
        else:
            self._press_item = None

    def mouseMoveEvent(self, event):
        far_enough = (
            self._press_item is not None
            and event.buttons() & Qt.MouseButton.LeftButton
            and (event.position().toPoint() - self._press_pos).manhattanLength()
            >= QApplication.startDragDistance()
        )
        if not far_enough:
            super().mouseMoveEvent(event)
            return
        item = self._press_item
        self._press_item = None
        # Drop out of rubber-band selection, which is what the view would
        # otherwise do with this movement.
        self.setState(QAbstractItemView.State.NoState)
        if not item.isSelected():
            self.clearSelection()
            item.setSelected(True)
        self.startDrag(Qt.DropAction.MoveAction)

    def mouseReleaseEvent(self, event):
        self._press_item = None
        super().mouseReleaseEvent(event)

    def startDrag(self, supported_actions):
        items = self.selectedItems()
        if not items:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(MIME_INTERNAL, b"1")
        drag.setMimeData(mime)
        drag.setPixmap(self._drag_pixmap(items))
        drag.setHotSpot(QPoint(28, 28))
        # The drag is built and run here rather than left to the base class so
        # that nothing removes rows behind our back when it finishes.
        drag.exec(Qt.DropAction.MoveAction)
        self._drop_index = None
        self.viewport().update()

    def _drag_pixmap(self, items):
        side = 96
        pm = QPixmap(side, side)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, side, side), 8, 8)
        p.fillPath(path, QColor(CARD))
        photo = items[0].data(ROLE_PHOTO)
        if photo.pixmap is not None and not photo.pixmap.isNull():
            inner = QRect(4, 4, side - 8, side - 8)
            scaled = photo.pixmap.size().scaled(
                inner.size(), Qt.AspectRatioMode.KeepAspectRatio
            )
            target = QRect(QPoint(0, 0), scaled)
            target.moveCenter(inner.center())
            p.setClipRect(inner)
            p.drawPixmap(target, photo.pixmap)
            p.setClipping(False)
        p.setPen(QPen(QColor(ACCENT), 2))
        p.drawRoundedRect(QRectF(1, 1, side - 2, side - 2), 8, 8)
        if len(items) > 1:
            badge = QRect(side - 34, 6, 28, 20)
            bp = QPainterPath()
            bp.addRoundedRect(QRectF(badge), 10, 10)
            p.fillPath(bp, QColor(ACCENT))
            f = QFont(p.font())
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor("#ffffff"))
            p.drawText(badge, Qt.AlignmentFlag.AlignCenter, str(len(items)))
        p.end()
        return pm

    def _insert_index(self, pos):
        """Row the drop should land on, from where the pointer actually is."""
        n = self.count()
        if n == 0:
            return 0
        idx = self.indexAt(pos)
        if idx.isValid():
            r = self.visualRect(idx)
            return idx.row() + (1 if pos.x() > r.center().x() else 0)
        for i in range(n):
            r = self.visualRect(self.model().index(i, 0))
            if pos.y() < r.top():
                return i
            if pos.y() <= r.bottom() and pos.x() < r.center().x():
                return i
        return n

    @staticmethod
    def _has_files(mime):
        if not mime.hasUrls():
            return False
        return any(u.isLocalFile() for u in mime.urls())

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime.hasFormat(MIME_INTERNAL) or self._has_files(mime):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if not (mime.hasFormat(MIME_INTERNAL) or self._has_files(mime)):
            event.ignore()
            return
        pos = event.position().toPoint()
        self._drop_index = self._insert_index(pos)
        self._autoscroll(pos)
        self.viewport().update()
        event.acceptProposedAction()

    def _autoscroll(self, pos):
        """Creeps the view along when a drag is held near the top or bottom."""
        margin = 44
        bar = self.verticalScrollBar()
        if pos.y() < margin:
            bar.setValue(bar.value() - bar.singleStep())
        elif pos.y() > self.viewport().height() - margin:
            bar.setValue(bar.value() + bar.singleStep())

    def dragLeaveEvent(self, event):
        self._drop_index = None
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        target = self._insert_index(event.position().toPoint())
        self._drop_index = None
        internal = mime.hasFormat(MIME_INTERNAL) and event.source() is self
        paths = ([u.toLocalFile() for u in mime.urls() if u.isLocalFile()]
                 if self._has_files(mime) else [])

        if self.apply_drop(target, internal, paths):
            event.setDropAction(Qt.DropAction.MoveAction if internal
                                else Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()
        self.viewport().update()

    def apply_drop(self, target, internal, paths):
        """
        Everything a drop does, kept apart from the event that carried it.

        Splitting it here means the reorder can be driven straight from a test,
        rather than only through a real drag that needs a real mouse.
        """
        if internal:
            self._move_selection_to(target)
            return True
        if paths:
            self.filesDropped.emit(paths, target)
            return True
        return False

    def _move_selection_to(self, target):
        rows = sorted(self.row(i) for i in self.selectedItems())
        if not rows:
            return
        # Anchor on the item the drop landed in front of, then find where that
        # item ended up once the dragged ones were pulled out.
        anchor = self.item(target) if target < self.count() else None
        if anchor is not None and anchor.isSelected():
            return                                  # dropped onto itself
        moved = [self.takeItem(r) for r in reversed(rows)][::-1]
        at = self.count() if anchor is None else self.row(anchor)
        for offset, it in enumerate(moved):
            self.insertItem(at + offset, it)
        self.clearSelection()
        for it in moved:
            it.setSelected(True)
        if moved:
            self.scrollToItem(moved[0], QAbstractItemView.ScrollHint.EnsureVisible)
        self.orderChanged.emit()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._drop_index is None or not self.count():
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        i = self._drop_index
        if i < self.count():
            r = self.visualRect(self.model().index(i, 0))
            x = r.left() - 2
        else:
            r = self.visualRect(self.model().index(self.count() - 1, 0))
            x = r.right() + 2
        bar = QRectF(x - 1.5, r.top() + 4, 3, max(10, r.height() - 8))
        path = QPainterPath()
        path.addRoundedRect(bar, 1.5, 1.5)
        p.fillPath(path, QColor(ACCENT))
        p.end()



# ---------------------------------------------------------------------------
#  Metadata
# ---------------------------------------------------------------------------
#
# Written into the exported copies, never into the originals. The fields chosen
# are the ones Windows itself shows under File > Properties > Details, so what
# is set here is what the photographer and their client actually see.

try:
    import piexif
except ImportError:                                    # metadata simply hides
    piexif = None

# key, label, help text, tag in the 0th IFD written as UCS-2, tag written as
# plain bytes (or None)
META_FIELDS = [
    ("title", "Title", "Shows as Title in Windows file properties.", 40091, None),
    ("description", "Description", "The caption. Shows as Comments.", 40092, 270),
    ("tags", "Tags", "Keywords, separated by semicolons.", 40094, None),
    ("author", "Photographer", "Shows as Authors.", 40093, 315),
    ("copyright", "Copyright", "e.g. (c) 2026 Your Name. All rights reserved.",
     None, 33432),
]
META_KEYS = [f[0] for f in META_FIELDS]

# Formats piexif can splice an EXIF block into without re-encoding the picture.
META_EXTS = {".jpg", ".jpeg", ".jpe", ".jfif", ".webp"}


# Photographer, copyright and tags rarely change from one shoot to the next, so
# the metadata dialog can remember them. They live in the user's own settings
# (the registry on Windows), not in the code. Title and Description are
# deliberately not remembered - those belong to the shoot, and filling them in
# would paste over real captions.
SAVED_KEYS = ("author", "copyright", "tags")


def saved_details():
    s = QSettings(APP_NAME, APP_NAME)
    return {k: str(s.value(f"details/{k}", "") or "") for k in SAVED_KEYS}


def save_details(values):
    s = QSettings(APP_NAME, APP_NAME)
    for k in SAVED_KEYS:
        s.setValue(f"details/{k}", values.get(k, ""))
    s.sync()


def expand_tokens(text, position, total, photo):
    """{n}, {total} and {name} let one line of text number a whole set."""
    if not text:
        return text
    return (text.replace("{n}", str(position))
                .replace("{total}", str(total))
                .replace("{name}", photo.stem))


def resolve_meta(photo, position, total):
    return {k: expand_tokens(v, position, total, photo)
            for k, v in (photo.meta or {}).items() if v}


def _exif_dict_for(path, meta):
    """
    Existing EXIF with our fields laid over the top.

    Camera data - exposure, lens, capture time - is kept. If the file's own
    EXIF cannot be parsed we start clean rather than refusing to write.
    """
    try:
        exif = piexif.load(str(path))
    except Exception:
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    zeroth = exif.setdefault("0th", {})
    for key, _label, _help, xp_tag, byte_tag in META_FIELDS:
        value = meta.get(key)
        if not value:
            continue
        if xp_tag is not None:
            zeroth[xp_tag] = value.encode("utf-16le") + b"\x00\x00"
        if byte_tag is not None:
            zeroth[byte_tag] = value.encode("utf-8", "replace")
    return exif


def write_metadata(path, meta):
    """
    Writes `meta` into the file at `path`, which is always an exported copy.

    Returns True if it went in. A file whose EXIF block will not rebuild gets
    one more attempt with just our fields, because losing the camera data is
    better than losing the caption.
    """
    if piexif is None or not meta:
        return False
    if Path(path).suffix.lower() not in META_EXTS:
        return False
    merged = _exif_dict_for(path, meta)
    # A broken embedded thumbnail is the usual reason a rebuild fails, so the
    # second try drops it, and the last one keeps only the fields set here.
    without_thumb = dict(merged, **{"1st": {}, "thumbnail": None})
    for candidate in (merged, without_thumb, _exif_dict_for("", meta)):
        try:
            piexif.insert(piexif.dump(candidate), str(path))
            return True
        except Exception:
            continue
    return False


def fit_to_contents(dialog):
    """
    Makes a dialog at least as tall as its contents need at its current width.

    Wrapped labels only know their height once the width is known, and Qt sizes
    a dialog before that, so it opens too short and squashes the text boxes
    until their text is cut in half. This asks the layout again afterwards.
    """
    layout = dialog.layout()
    if layout is None or not layout.hasHeightForWidth():
        return
    need = layout.totalHeightForWidth(dialog.width())
    dialog.setMinimumHeight(need)
    if dialog.height() < need:
        dialog.resize(dialog.width(), need)


class MetadataDialog(QDialog):
    """One set of values, written to every photograph in the selection."""

    def __init__(self, photos, total=None, parent=None):
        super().__init__(parent)
        self.photos = photos
        self.total = total if total is not None else len(photos)
        self.values = None
        self.clearing = False
        self.setWindowTitle("Metadata")
        self.setModal(True)
        self.setMinimumWidth(560)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(13)

        title = QLabel("Metadata")
        title.setObjectName("dialog_title")
        root.addWidget(title)

        n = len(photos)
        head = QLabel(
            f"Applies to <b>{n} photograph{'s' if n != 1 else ''}</b>. "
            "It is written into the exported copies. Your originals are left "
            "exactly as they are."
        )
        head.setWordWrap(True)
        root.addWidget(head)

        form = QFormLayout()
        form.setSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self.edits = {}
        common = self._common_values()
        for key, label, help_text, _xp, _b in META_FIELDS:
            edit = QLineEdit()
            edit.setText(common.get(key) or "")
            if common.get(key) is None and any(p.meta.get(key) for p in photos):
                edit.setPlaceholderText("(these photographs differ, leave blank to keep)")
            else:
                edit.setPlaceholderText(help_text)
            edit.textChanged.connect(self._refresh)
            self.edits[key] = edit
            form.addRow(label, edit)
        root.addLayout(form)

        self._saved = saved_details()
        fill_row = QHBoxLayout()
        self.use_mine = QPushButton("Use my details")
        self.use_mine.setToolTip(
            "Fills in the photographer, copyright and tags you saved. "
            "Title and Description are left alone because they belong to the shoot."
        )
        self.use_mine.clicked.connect(self._fill_saved)
        self.remember = QPushButton("Save as my details")
        self.remember.setToolTip(
            "Remembers the photographer, copyright and tags in these boxes "
            "for next time."
        )
        self.remember.clicked.connect(self._remember)
        fill_row.addWidget(self.use_mine)
        fill_row.addWidget(self.remember)
        fill_row.addStretch(1)
        root.addLayout(fill_row)

        tokens = QLabel(
            "<span style='color:%s'>In Title and Description, "
            "<b>{n}</b> becomes the position in the sequence, <b>{total}</b> the "
            "number of photographs, and <b>{name}</b> the original filename.</span>"
            % TEXT_DIM
        )
        tokens.setWordWrap(True)
        root.addWidget(tokens)

        self.preview = QLabel()
        self.preview.setObjectName("preview")
        self.preview.setTextFormat(Qt.TextFormat.RichText)
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)

        note = QLabel(
            "A field left blank keeps whatever those photographs already have. "
            "Use <i>Clear metadata</i> to take values off."
        )
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {TEXT_DIM};")
        root.addWidget(note)

        self.warn = QLabel()
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        root.addWidget(self.warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Apply")
        ok.setObjectName("primary")
        clear = buttons.addButton("Clear metadata",
                                  QDialogButtonBox.ButtonRole.DestructiveRole)
        clear.clicked.connect(self._clear)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh()

    def _fill_saved(self):
        """Drops the saved details into the boxes, ready to review."""
        for key, value in self._saved.items():
            if value:
                self.edits[key].setText(value)
        self._refresh()

    def _remember(self):
        self._saved = {k: self.edits[k].text().strip() for k in SAVED_KEYS}
        save_details(self._saved)
        self._refresh()

    def _refresh_saved_buttons(self):
        here = {k: self.edits[k].text().strip() for k in SAVED_KEYS}
        stored = here == self._saved and any(here.values())
        self.use_mine.setEnabled(any(self._saved.values()) and not stored)
        self.remember.setText("Saved" if stored else "Save as my details")
        self.remember.setEnabled(any(here.values()) and not stored)

    def _common_values(self):
        """A value only fills the box if every selected photograph agrees."""
        common = {}
        for key in META_KEYS:
            values = {p.meta.get(key, "") for p in self.photos}
            common[key] = values.pop() if len(values) == 1 else None
        return common

    def _refresh(self):
        photo = self.photos[0]
        entered = {k: e.text() for k, e in self.edits.items()}
        merged = {**photo.meta, **{k: v for k, v in entered.items() if v}}
        resolved = resolve_meta(
            type("P", (), {"meta": merged, "stem": photo.stem})(), 1, self.total
        )
        rows = [f"<b>{label}</b>&nbsp; {resolved[key]}"
                for key, label, _h, _x, _b in META_FIELDS if resolved.get(key)]
        body = "<br>".join(rows) or f"<span style='color:{TEXT_DIM}'>nothing set</span>"
        self.preview.setText(
            f"<span style='color:{TEXT_DIM}'>On the first photograph</span><br>{body}"
        )

        skipped = [p for p in self.photos if p.suffix.lower() not in META_EXTS]
        if piexif is None:
            self.warn.setText("Metadata support is not installed in this build.")
            self.warn.show()
        elif skipped:
            self.warn.setText(
                f"{len(skipped)} of these are not JPEG or WebP and cannot carry "
                f"metadata. They will still export, without it."
            )
            self.warn.show()
        else:
            self.warn.hide()
        self._refresh_saved_buttons()
        if self.isVisible():
            fit_to_contents(self)

    def showEvent(self, event):
        super().showEvent(event)
        fit_to_contents(self)

    def _clear(self):
        for edit in self.edits.values():
            edit.clear()
        self.values = {k: "" for k in META_KEYS}
        self.clearing = True
        self.accept()

    def _accept(self):
        self.values = {k: e.text().strip() for k, e in self.edits.items()}
        self.accept()

# ---------------------------------------------------------------------------
#  Export
# ---------------------------------------------------------------------------

ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(text):
    """Trims a filename down to something Windows will actually accept."""
    text = ILLEGAL.sub("", text).strip().rstrip(".")
    return text or "photo"


def plan_names(photos, opts):
    """
    Works out the exported filename for every photograph, in grid order.

    Returns a list the same length as `photos`. Collisions get a numeric
    suffix, so no photograph is ever silently overwritten by another.
    """
    digits = opts["digits"]
    start = opts["start"]
    sep = opts["sep"]
    prefix = opts["prefix"].strip()
    seen = {}
    names = []
    for i, photo in enumerate(photos):
        parts = []
        if prefix:
            parts.append(prefix)
        if opts["numbered"]:
            parts.append(str(start + i).zfill(digits))
        if opts["keep_name"]:
            parts.append(photo.stem)
        base = safe_name(sep.join(parts) if parts else photo.stem)
        ext = photo.suffix.lower()
        name = base + ext
        key = name.lower()
        if key in seen:
            seen[key] += 1
            name = f"{base} ({seen[key]}){ext}"
        else:
            seen[key] = 1
        names.append(name)
    return names


class ExportWorker(QThread):
    progress = Signal(int, str)
    done = Signal(str, int, list)      # destination, written, missing
    failed = Signal(str)

    def __init__(self, photos, names, dest, as_zip, manifest, parent=None):
        super().__init__(parent)
        self.photos = photos
        self.names = names
        self.dest = Path(dest)
        self.as_zip = as_zip
        self.manifest = manifest
        self.tagged = 0
        self.untaggable = 0
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def _manifest_text(self, pairs):
        lines = [
            f"{APP_NAME} {APP_VERSION}",
            f"Exported {datetime.now().strftime('%d %B %Y, %H:%M')}",
            f"{len(pairs)} photographs, in this order:",
            "",
        ]
        width = len(str(len(pairs)))
        for i, (name, src) in enumerate(pairs, 1):
            lines.append(f"{str(i).rjust(width)}. {name}    (from {src})")
        return "\r\n".join(lines) + "\r\n"

    def run(self):
        try:
            if self.as_zip:
                self._run_zip()
            else:
                self._run_folder()
        except Exception as exc:                       # surfaced in the UI
            self.failed.emit(str(exc))

    def _meta_for(self, photo, position):
        return resolve_meta(photo, position, len(self.photos))

    def _run_zip(self):
        missing, pairs, written = [], [], 0
        tmp = self.dest.with_suffix(self.dest.suffix + ".part")
        try:
            # ZIP_STORED: JPEGs and PNGs are already compressed, so deflating
            # them costs minutes and saves almost nothing.
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
                for i, (photo, name) in enumerate(zip(self.photos, self.names)):
                    if self._cancel:
                        raise InterruptedError
                    if not photo.path.exists():
                        missing.append(photo.name)
                        continue
                    self.progress.emit(i, name)
                    meta = self._meta_for(photo, i + 1)
                    if meta:
                        # The metadata goes into a scratch copy so the original
                        # on disk is never opened for writing.
                        fd, scratch = tempfile.mkstemp(suffix=photo.suffix)
                        os.close(fd)
                        try:
                            shutil.copyfile(photo.path, scratch)
                            if write_metadata(scratch, meta):
                                self.tagged += 1
                            elif photo.suffix.lower() not in META_EXTS:
                                self.untaggable += 1
                            zf.write(scratch, arcname=name)
                        finally:
                            Path(scratch).unlink(missing_ok=True)
                    else:
                        zf.write(photo.path, arcname=name)
                    pairs.append((name, photo.name))
                    written += 1
                if self.manifest and pairs:
                    zf.writestr("order.txt", self._manifest_text(pairs))
        except InterruptedError:
            tmp.unlink(missing_ok=True)
            return
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        self.dest.unlink(missing_ok=True)
        tmp.replace(self.dest)
        self.done.emit(str(self.dest), written, missing)

    def _run_folder(self):
        missing, pairs, written = [], [], 0
        self.dest.mkdir(parents=True, exist_ok=True)
        for i, (photo, name) in enumerate(zip(self.photos, self.names)):
            if self._cancel:
                return
            if not photo.path.exists():
                missing.append(photo.name)
                continue
            self.progress.emit(i, name)
            target = self.dest / name
            shutil.copy2(photo.path, target)
            meta = self._meta_for(photo, i + 1)
            if meta:
                if write_metadata(target, meta):
                    self.tagged += 1
                elif photo.suffix.lower() not in META_EXTS:
                    self.untaggable += 1
            pairs.append((name, photo.name))
            written += 1
        if self.manifest and pairs:
            (self.dest / "order.txt").write_text(
                self._manifest_text(pairs), encoding="utf-8"
            )
        self.done.emit(str(self.dest), written, missing)


class ExportDialog(QDialog):
    """Naming and destination, with a live preview of the first few names."""

    SEPARATORS = [("Underscore  001_name", "_"),
                  ("Hyphen  001-name", "-"),
                  ("Space  001 name", " "),
                  ("Nothing  001name", "")]

    def __init__(self, photos, last_dir, parent=None):
        super().__init__(parent)
        self.photos = photos
        self.setWindowTitle("Export in this order")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.result_opts = None
        self._last_dir = last_dir

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(14)

        title = QLabel("Export")
        title.setObjectName("dialog_title")
        root.addWidget(title)

        head = QLabel(f"<b>{len(photos)} photographs</b> will be written in the "
                      f"order shown in the grid.")
        head.setWordWrap(True)
        root.addWidget(head)

        carrying = sum(1 for p in photos if getattr(p, "has_meta", False))
        if carrying:
            meta_note = QLabel(
                f"Metadata is set on {carrying} of them and will be written "
                f"into the exported copies."
            )
            meta_note.setWordWrap(True)
            meta_note.setStyleSheet(f"color: {TEXT_DIM};")
            root.addWidget(meta_note)

        # --- destination --------------------------------------------------
        dest_box = QVBoxLayout()
        dest_box.setSpacing(8)
        self.rb_zip = QRadioButton("A ZIP file")
        self.rb_zip.setChecked(True)
        self.rb_folder = QRadioButton("A folder of files")
        row = QHBoxLayout()
        row.addWidget(self.rb_zip)
        row.addWidget(self.rb_folder)
        row.addStretch(1)
        dest_box.addLayout(row)

        pick = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("Choose where to save...")
        browse = QPushButton("Browse...")
        browse.setObjectName("secondary")
        browse.clicked.connect(self._browse)
        pick.addWidget(self.dest_edit, 1)
        pick.addWidget(browse)
        dest_box.addLayout(pick)
        root.addLayout(dest_box)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("rule")
        root.addWidget(line)

        # --- naming -------------------------------------------------------
        form = QFormLayout()
        form.setSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.cb_number = QCheckBox("Number each file by its position")
        self.cb_number.setChecked(True)
        self.cb_keep = QCheckBox("Keep the original filename")
        self.cb_keep.setChecked(True)
        naming = QVBoxLayout()
        naming.addWidget(self.cb_number)
        naming.addWidget(self.cb_keep)
        form.addRow("Filenames", naming)

        self.prefix = QLineEdit()
        self.prefix.setPlaceholderText("optional, e.g. Smith-wedding")
        form.addRow("Prefix", self.prefix)

        self.sep = QComboBox()
        for label, _ in self.SEPARATORS:
            self.sep.addItem(label)
        form.addRow("Separator", self.sep)

        nums = QHBoxLayout()
        self.start = QSpinBox()
        self.start.setRange(0, 999999)
        self.start.setValue(1)
        self.digits = QSpinBox()
        self.digits.setRange(1, 6)
        self.digits.setValue(max(2, len(str(len(photos)))))
        nums.addWidget(QLabel("start at"))
        nums.addWidget(self.start)
        nums.addSpacing(16)
        nums.addWidget(QLabel("digits"))
        nums.addWidget(self.digits)
        nums.addStretch(1)
        form.addRow("Numbering", nums)

        self.cb_manifest = QCheckBox("Include order.txt listing the sequence")
        self.cb_manifest.setChecked(True)
        form.addRow("", self.cb_manifest)
        root.addLayout(form)

        self.preview = QLabel()
        self.preview.setObjectName("preview")
        self.preview.setTextFormat(Qt.TextFormat.RichText)
        self.preview.setWordWrap(True)
        root.addWidget(self.preview)

        self.warn = QLabel()
        self.warn.setObjectName("warn")
        self.warn.setWordWrap(True)
        self.warn.hide()
        root.addWidget(self.warn)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.ok_btn = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("Export")
        self.ok_btn.setObjectName("primary")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for w in (self.cb_number, self.cb_keep, self.cb_manifest):
            w.toggled.connect(self._refresh)
        self.prefix.textChanged.connect(self._refresh)
        self.sep.currentIndexChanged.connect(self._refresh)
        self.start.valueChanged.connect(self._refresh)
        self.digits.valueChanged.connect(self._refresh)
        self.rb_zip.toggled.connect(self._dest_kind_changed)
        self.dest_edit.textChanged.connect(self._refresh)
        self._refresh()

    # --- helpers ----------------------------------------------------------

    def options(self):
        return {
            "numbered": self.cb_number.isChecked(),
            "keep_name": self.cb_keep.isChecked(),
            "prefix": self.prefix.text(),
            "sep": self.SEPARATORS[self.sep.currentIndex()][1],
            "start": self.start.value(),
            "digits": self.digits.value(),
        }

    def _dest_kind_changed(self):
        text = self.dest_edit.text().strip()
        if text:
            p = Path(text)
            if self.rb_zip.isChecked() and p.suffix.lower() != ".zip":
                self.dest_edit.setText(str(p.with_suffix(".zip")))
            elif not self.rb_zip.isChecked() and p.suffix.lower() == ".zip":
                self.dest_edit.setText(str(p.with_suffix("")))
        self._refresh()

    def _browse(self):
        start_dir = self.dest_edit.text().strip() or self._last_dir
        if self.rb_zip.isChecked():
            suggested = str(Path(start_dir) / "photos-in-order.zip") \
                if Path(start_dir).is_dir() else start_dir
            path, _ = QFileDialog.getSaveFileName(
                self, "Save the ZIP as", suggested, "ZIP archive (*.zip)"
            )
        else:
            path = QFileDialog.getExistingDirectory(
                self, "Choose a folder to export into", start_dir
            )
        if path:
            self.dest_edit.setText(str(Path(path)))

    def _refresh(self):
        opts = self.options()
        self.sep.setEnabled(sum([opts["numbered"], opts["keep_name"],
                                 bool(opts["prefix"].strip())]) > 1)
        self.start.setEnabled(opts["numbered"])
        self.digits.setEnabled(opts["numbered"])

        sample = self.photos[:3]
        names = plan_names(sample, opts) if sample else []
        rows = "<br>".join(f"&nbsp;&nbsp;{n}" for n in names)
        more = "<br>&nbsp;&nbsp;..." if len(self.photos) > 3 else ""
        self.preview.setText(f"<span style='color:{TEXT_DIM}'>Preview</span>"
                             f"<br>{rows}{more}")

        problem = None
        if not opts["numbered"]:
            problem = ("Without numbers in the filenames, nothing outside this "
                       "tool knows the order. Explorer will sort them "
                       "alphabetically. order.txt will still record it.")
        dest = self.dest_edit.text().strip()
        if dest and not self.rb_zip.isChecked():
            p = Path(dest)
            if p.exists() and any(p.iterdir()):
                problem = (f"{p.name} already has files in it. Anything with a "
                           f"matching name will be overwritten.")
        if problem:
            self.warn.setText(problem)
            self.warn.show()
        else:
            self.warn.hide()
        self.ok_btn.setEnabled(bool(dest))
        if self.isVisible():
            fit_to_contents(self)

    def showEvent(self, event):
        super().showEvent(event)
        fit_to_contents(self)

    def _accept(self):
        dest = self.dest_edit.text().strip()
        if not dest:
            return
        p = Path(dest)
        if self.rb_zip.isChecked():
            if p.suffix.lower() != ".zip":
                p = p.with_suffix(".zip")
            if not p.parent.exists():
                QMessageBox.warning(self, APP_NAME,
                                    f"There is no folder called\n{p.parent}")
                return
            if p.exists():
                reply = QMessageBox.question(
                    self, APP_NAME,
                    f"{p.name} already exists. Replace it?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
        self.result_opts = {
            "dest": str(p),
            "as_zip": self.rb_zip.isChecked(),
            "manifest": self.cb_manifest.isChecked(),
            **self.options(),
        }
        self.accept()


# ---------------------------------------------------------------------------
#  Preview
# ---------------------------------------------------------------------------

class PreviewDialog(QDialog):
    """Big view of one photograph, with the arrow keys walking the sequence."""

    def __init__(self, grid, row, parent=None):
        super().__init__(parent)
        self.grid = grid
        self.row = row
        self.setWindowTitle("Preview")
        self.setModal(True)
        self.setObjectName("preview_dialog")
        screen = QApplication.primaryScreen().availableGeometry()
        self.resize(int(screen.width() * 0.78), int(screen.height() * 0.82))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(200, 200)
        layout.addWidget(self.image, 1)

        bar = QHBoxLayout()
        bar.setContentsMargins(14, 10, 14, 12)
        self.prev_btn = QPushButton("‹  Previous")
        self.prev_btn.setObjectName("secondary")
        self.next_btn = QPushButton("Next  ›")
        self.next_btn.setObjectName("secondary")
        self.caption = QLabel()
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        close = QPushButton("Close")
        close.setObjectName("secondary")
        self.prev_btn.clicked.connect(lambda: self.step(-1))
        self.next_btn.clicked.connect(lambda: self.step(1))
        close.clicked.connect(self.accept)
        bar.addWidget(self.prev_btn)
        bar.addWidget(self.caption, 1)
        bar.addWidget(self.next_btn)
        bar.addSpacing(12)
        bar.addWidget(close)
        layout.addLayout(bar)

        self._full = None
        self.load()

    def load(self):
        photo = self.grid.item(self.row).data(ROLE_PHOTO)
        self._full = None
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            with Image.open(photo.path) as img:
                img = ImageOps.exif_transpose(img)
                img.thumbnail((2200, 2200), Image.LANCZOS)
                img = img.convert("RGB")
                data = img.tobytes("raw", "RGB")
                qimg = QImage(data, img.width, img.height, img.width * 3,
                              QImage.Format.Format_RGB888).copy()
            self._full = QPixmap.fromImage(qimg)
        except Exception:
            self._full = None
        finally:
            QApplication.restoreOverrideCursor()
        self.caption.setText(
            f"{self.row + 1} of {self.grid.count()}   ·   {photo.name}"
        )
        self.prev_btn.setEnabled(self.row > 0)
        self.next_btn.setEnabled(self.row < self.grid.count() - 1)
        self.rescale()

    def rescale(self):
        if self._full is None:
            self.image.setText("This file could not be opened.")
            return
        area = self.image.size()
        self.image.setPixmap(self._full.scaled(
            area, Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        ))

    def step(self, delta):
        row = self.row + delta
        if 0 <= row < self.grid.count():
            self.row = row
            self.grid.setCurrentRow(row)
            self.load()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.rescale()

    def keyPressEvent(self, event):
        key = event.key()
        if key in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_Space):
            self.step(1)
        elif key in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self.step(-1)
        else:
            super().keyPressEvent(event)


# ---------------------------------------------------------------------------
#  Main window
# ---------------------------------------------------------------------------

def resource_path(name):
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).parent
    return Path(base) / name


def load_fonts():
    """
    Registers the bundled typefaces and reports what we actually got.

    Returns (serif, sans) family names - Cormorant Garamond and Jost when the
    files are present, otherwise Georgia and Segoe UI.
    """
    folder = resource_path("fonts")
    if folder.is_dir():
        for ttf in sorted(folder.glob("*.ttf")):
            QFontDatabase.addApplicationFont(str(ttf))
    have = set(QFontDatabase.families())
    return (SERIF if SERIF in have else "Georgia",
            SANS if SANS in have else "Segoe UI")


def reveal_in_explorer(path):
    """Opens Explorer with the file selected, without flashing up a console."""
    import subprocess
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(["explorer", f"/select,{path}"], creationflags=flags)
    except Exception:
        pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 820)
        self.setMinimumSize(760, 520)
        self.setAcceptDrops(True)
        self._last_dir = str(Path.home())
        self._list_path = None
        self._dirty = False
        self._worker = None

        icon = resource_path("icon.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_header())
        outer.addWidget(self._build_toolbar())

        stack = QWidget()
        stack_layout = QVBoxLayout(stack)
        stack_layout.setContentsMargins(10, 8, 10, 8)
        self.grid = PhotoGrid()
        stack_layout.addWidget(self.grid)
        outer.addWidget(stack, 1)

        self.empty = QLabel(
            "Drop photographs here\n\n"
            "or use Add photos above.\n"
            "Then drag them into the order you want.",
            self.grid,
        )
        self.empty.setObjectName("empty")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setFont(QFont(SERIF_FAMILY, 15))

        self.status = QLabel("No photographs yet")
        self.statusBar().addWidget(self.status)
        self.hint = QLabel("Drag a photograph to move it · double-click to preview")
        self.hint.setObjectName("hint")
        self.statusBar().addPermanentWidget(self.hint)

        self.grid.filesDropped.connect(self._on_files_dropped)
        self.grid.orderChanged.connect(self._on_changed)
        self.grid.countChanged.connect(self._refresh_status)
        self.grid.itemSelectionChanged.connect(self._refresh_status)
        self.grid.itemDoubleClicked.connect(self._preview)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._context_menu)

        self._build_menus()
        self._refresh_status()

    # --- chrome -----------------------------------------------------------

    def _build_header(self):
        """The wordmark across the top of the window."""
        bar = QWidget()
        bar.setObjectName("toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(16, 11, 16, 9)
        row.setSpacing(0)

        name = QLabel(APP_NAME)
        name.setObjectName("wordmark_name")
        row.addWidget(name)
        row.addStretch(1)

        version = QLabel(f"Version {APP_VERSION}")
        version.setObjectName("wordmark_sub")
        version.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom)
        row.addWidget(version)
        return bar

    def _build_toolbar(self):
        bar = QWidget()
        bar.setObjectName("toolbar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)

        add = QPushButton("Add photos")
        add.setObjectName("primary")
        add.clicked.connect(self.add_photos)
        folder = QPushButton("Add folder")
        folder.setObjectName("secondary")
        folder.clicked.connect(self.add_folder)
        row.addWidget(add)
        row.addWidget(folder)
        row.addWidget(self._sep())

        sort = QPushButton("Sort")
        sort.setObjectName("secondary")
        menu = QMenu(sort)
        menu.addAction("By filename", lambda: self._sort("name"))
        menu.addAction("By date taken", lambda: self._sort("taken"))
        menu.addAction("By date modified", lambda: self._sort("mtime"))
        menu.addSeparator()
        menu.addAction("Reverse current order", self._reverse)
        sort.setMenu(menu)
        row.addWidget(sort)

        self.meta_btn = QPushButton("Metadata")
        self.meta_btn.setObjectName("secondary")
        self.meta_btn.setToolTip(
            "Set the title, description, tags, photographer and copyright "
            "for every photograph you have selected"
        )
        self.meta_btn.clicked.connect(self.edit_metadata)
        row.addWidget(self.meta_btn)

        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("secondary")
        self.remove_btn.clicked.connect(self.remove_selected)
        row.addWidget(self.remove_btn)
        row.addWidget(self._sep())

        row.addWidget(QLabel("Size"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(96, 380)
        self.slider.setValue(168)
        self.slider.setFixedWidth(140)
        self.slider.valueChanged.connect(self.grid_resize)
        row.addWidget(self.slider)

        row.addStretch(1)
        self.export_btn = QPushButton("Export in this order")
        self.export_btn.setObjectName("primary")
        self.export_btn.clicked.connect(self.export)
        row.addWidget(self.export_btn)
        return bar

    def _sep(self):
        line = QFrame()
        line.setObjectName("vsep")
        line.setFrameShape(QFrame.Shape.VLine)
        line.setFixedHeight(22)
        return line

    def _build_menus(self):
        m = self.menuBar().addMenu("&File")
        act = m.addAction("Add photos...", self.add_photos)
        act.setShortcut(QKeySequence.StandardKey.Open)
        m.addAction("Add folder...", self.add_folder)
        m.addSeparator()
        a = m.addAction("Open saved list...", self.open_list)
        a.setShortcut("Ctrl+L")
        a = m.addAction("Save this list...", self.save_list)
        a.setShortcut(QKeySequence.StandardKey.Save)
        m.addSeparator()
        a = m.addAction("Export in this order...", self.export)
        a.setShortcut("Ctrl+E")
        m.addSeparator()
        m.addAction("Exit", self.close)

        e = self.menuBar().addMenu("&Edit")
        a = e.addAction("Select all", self.grid.selectAll)
        a.setShortcut(QKeySequence.StandardKey.SelectAll)
        a = e.addAction("Remove selected", self.remove_selected)
        a.setShortcut(QKeySequence.StandardKey.Delete)
        e.addSeparator()
        a = e.addAction("Metadata...", self.edit_metadata)
        a.setShortcut("Ctrl+M")
        e.addAction("Clear metadata", self.clear_metadata)
        e.addSeparator()
        e.addAction("Move to front", lambda: self._move_selection("front"))
        e.addAction("Move to back", lambda: self._move_selection("back"))
        e.addSeparator()
        e.addAction("Remove everything", self.clear_all)

        h = self.menuBar().addMenu("&Help")
        h.addAction("About", self.about)

    # --- adding -----------------------------------------------------------

    def add_photos(self):
        patterns = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add photographs", self._last_dir,
            f"Images ({patterns});;All files (*.*)",
        )
        if paths:
            self._last_dir = str(Path(paths[0]).parent)
            self._add(paths)

    def add_folder(self):
        path = QFileDialog.getExistingDirectory(
            self, "Add every photograph in a folder", self._last_dir
        )
        if path:
            self._last_dir = path
            self._add([path])

    def _add(self, paths, at=None):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            added, skipped = self.grid.add_paths(paths, at)
        finally:
            QApplication.restoreOverrideCursor()
        if added:
            self._dirty = True
        bits = [f"Added {added} photograph{'s' if added != 1 else ''}"]
        if skipped:
            bits.append(f"{skipped} already in the list or unreadable")
        if not added and not skipped:
            bits = ["Nothing there this tool can read"]
        self.statusBar().showMessage(" · ".join(bits), 6000)
        self._refresh_status()

    @Slot(list, int)
    def _on_files_dropped(self, paths, at):
        self._add(paths, at)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = [u.toLocalFile() for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            event.acceptProposedAction()
            self._add(paths)

    # --- editing ----------------------------------------------------------

    def remove_selected(self):
        n = self.grid.remove_selected()
        if n:
            self._dirty = True
            self.statusBar().showMessage(
                f"Removed {n} photograph{'s' if n != 1 else ''} from the list "
                f"(the files on disk are untouched)", 6000)

    def clear_all(self):
        if not self.grid.count():
            return
        reply = QMessageBox.question(
            self, APP_NAME,
            "Take every photograph out of the list?\n\n"
            "The files on your computer are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.grid.clear_all()
            self._dirty = False
            self._list_path = None

    def _move_selection(self, where):
        items = self.grid.selectedItems()
        if not items:
            return
        rows = sorted(self.grid.row(i) for i in items)
        moved = [self.grid.takeItem(r) for r in reversed(rows)][::-1]
        at = 0 if where == "front" else self.grid.count()
        for offset, it in enumerate(moved):
            self.grid.insertItem(at + offset, it)
            it.setSelected(True)
        self.grid.scrollToItem(moved[0])
        self.grid.orderChanged.emit()
        self.grid.viewport().update()

    def _meta_targets(self):
        """The selection, or everything when nothing is picked out."""
        items = self.grid.selectedItems()
        if not items:
            items = [self.grid.item(i) for i in range(self.grid.count())]
        return [i.data(ROLE_PHOTO) for i in items]

    def edit_metadata(self):
        photos = self._meta_targets()
        if not photos:
            QMessageBox.information(self, APP_NAME, "Add some photographs first.")
            return
        if piexif is None:
            QMessageBox.warning(
                self, APP_NAME,
                "This build cannot write metadata because the piexif package is "
                "missing. Rebuild with build.cmd to add it.",
            )
            return
        dlg = MetadataDialog(photos, self.grid.count(), self)
        if dlg.exec() != QDialog.DialogCode.Accepted or dlg.values is None:
            return
        if dlg.clearing:
            for photo in photos:
                photo.meta = {}
            what = "Metadata cleared on"
        else:
            for photo in photos:
                for key, value in dlg.values.items():
                    if value:
                        photo.meta[key] = value
            what = "Metadata set on"
        self._dirty = True
        self.grid.viewport().update()
        n = len(photos)
        self.statusBar().showMessage(
            f"{what} {n} photograph{'s' if n != 1 else ''}. "
            f"It is written when you export.", 7000)

    def clear_metadata(self):
        photos = self._meta_targets()
        carrying = [p for p in photos if p.has_meta]
        if not carrying:
            self.statusBar().showMessage("None of those carry metadata.", 4000)
            return
        reply = QMessageBox.question(
            self, APP_NAME,
            f"Take the metadata off {len(carrying)} photograph"
            f"{'s' if len(carrying) != 1 else ''}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        for photo in carrying:
            photo.meta = {}
        self._dirty = True
        self.grid.viewport().update()

    def _sort(self, field_name):
        if not self.grid.count():
            return
        if field_name == "taken":
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                for photo in self.grid.photos():
                    if not photo.taken_checked:
                        photo.taken = read_taken(photo.path) or photo.mtime
                        photo.taken_checked = True
            finally:
                QApplication.restoreOverrideCursor()
            key = lambda p: (p.taken, natural_key(p.name))
        elif field_name == "mtime":
            key = lambda p: (p.mtime, natural_key(p.name))
        else:
            key = lambda p: natural_key(p.name)
        self.grid.sort_photos(key)
        self.statusBar().showMessage("Sorted. Drag from here to fine-tune.", 5000)

    def _reverse(self):
        self.grid.reverse_order()

    def grid_resize(self, px):
        self.grid.set_cell_px(px)

    def _preview(self, item):
        PreviewDialog(self.grid, self.grid.row(item), self).exec()

    def _context_menu(self, pos):
        item = self.grid.itemAt(pos)
        menu = QMenu(self)
        if item is not None:
            menu.addAction("Preview", lambda: self._preview(item))
            menu.addSeparator()
            menu.addAction("Metadata...", self.edit_metadata)
            menu.addAction("Move to front", lambda: self._move_selection("front"))
            menu.addAction("Move to back", lambda: self._move_selection("back"))
            menu.addSeparator()
            menu.addAction("Show in Explorer", lambda: self._reveal(item))
            menu.addAction("Remove from list", self.remove_selected)
        else:
            menu.addAction("Add photos...", self.add_photos)
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _reveal(self, item):
        photo = item.data(ROLE_PHOTO)
        if photo.path.exists():
            reveal_in_explorer(photo.path)

    # --- lists ------------------------------------------------------------

    def save_list(self):
        if not self.grid.count():
            QMessageBox.information(self, APP_NAME, "There is nothing to save yet.")
            return
        suggested = self._list_path or str(Path(self._last_dir) / "photo-order.json")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save this list", suggested, "Photo Sequencer list (*.json)"
        )
        if not path:
            return
        data = {
            "app": APP_NAME,
            "version": 1,
            "saved": datetime.now().isoformat(timespec="seconds"),
            "photos": [
                {"path": str(p.path),
                 "meta": {k: v for k, v in p.meta.items() if v}}
                for p in self.grid.photos()
            ],
        }
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        self._list_path = path
        self._dirty = False
        self.statusBar().showMessage(f"Saved to {Path(path).name}", 6000)

    def open_list(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open a saved list", self._last_dir,
            "Photo Sequencer list (*.json);;All files (*.*)",
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            entries = [
                # Lists saved before metadata existed are plain path strings.
                {"path": e, "meta": {}} if isinstance(e, str) else e
                for e in data["photos"]
            ]
        except Exception as exc:
            QMessageBox.warning(self, APP_NAME, f"That file could not be read.\n\n{exc}")
            return

        self.grid.clear_all()
        missing = []
        for entry in entries:
            p = Path(entry["path"])
            if not p.exists():
                missing.append(p.name)
                continue
            added, _ = self.grid.add_paths([p])
            if added:
                last = self.grid.item(self.grid.count() - 1).data(ROLE_PHOTO)
                last.meta = dict(entry.get("meta") or {})
        self._list_path = path
        self._dirty = False
        self._refresh_status()
        if missing:
            QMessageBox.warning(
                self, APP_NAME,
                f"{len(missing)} photograph(s) in that list are no longer where "
                f"they were and have been left out:\n\n"
                + "\n".join(missing[:12])
                + ("\n..." if len(missing) > 12 else ""),
            )

    # --- export -----------------------------------------------------------

    def export(self):
        photos = self.grid.photos()
        if not photos:
            QMessageBox.information(
                self, APP_NAME, "Add some photographs first."
            )
            return
        dlg = ExportDialog(photos, self._last_dir, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_opts:
            return
        opts = dlg.result_opts
        names = plan_names(photos, opts)

        progress = QProgressDialog(
            "Exporting...", "Cancel", 0, len(photos), self
        )
        progress.setWindowTitle(APP_NAME)
        progress.setWindowModality(Qt.WindowModality.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)

        worker = ExportWorker(photos, names, opts["dest"], opts["as_zip"],
                              opts["manifest"], self)
        self._worker = worker

        def on_progress(i, name):
            progress.setValue(i)
            progress.setLabelText(f"{i + 1} of {len(photos)}\n{name}")

        def on_done(dest, written, missing):
            progress.close()
            self._worker = None
            self._dirty = False
            self._finished_message(dest, written, missing, opts["as_zip"],
                                   worker.tagged, worker.untaggable)

        def on_failed(message):
            progress.close()
            self._worker = None
            QMessageBox.critical(
                self, APP_NAME, f"The export could not be finished.\n\n{message}"
            )

        def on_finished():
            progress.close()
            if self._worker is worker:
                self._worker = None
                self.statusBar().showMessage("Export stopped. Nothing written.", 6000)

        worker.progress.connect(on_progress)
        worker.done.connect(on_done)
        worker.failed.connect(on_failed)
        worker.finished.connect(on_finished)
        progress.canceled.connect(worker.cancel)
        worker.start()

    def _finished_message(self, dest, written, missing, as_zip,
                          tagged=0, untaggable=0):
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Icon.Information)
        kind = "ZIP" if as_zip else "folder"
        box.setText(f"{written} photograph{'s' if written != 1 else ''} written "
                    f"to the {kind}, in your order.")
        detail = str(dest)
        if tagged:
            detail += f"\n\nMetadata written into {tagged} of them."
        if untaggable:
            detail += (f"\n{untaggable} could not carry metadata. Only "
                       f"JPEG and WebP can.")
        if missing:
            detail += (f"\n\nLeft out because the file is no longer there:\n"
                       + "\n".join(missing[:12])
                       + ("\n..." if len(missing) > 12 else ""))
        box.setInformativeText(detail)
        show = box.addButton("Show me", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Close", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() is show:
            target = Path(dest)
            if as_zip:
                reveal_in_explorer(target)
            else:
                os.startfile(str(target))

    # --- state ------------------------------------------------------------

    def _on_changed(self):
        self._dirty = True
        self._refresh_status()

    def _refresh_status(self):
        photos = self.grid.photos()
        n = len(photos)
        self.empty.setVisible(n == 0)
        if n == 0:
            self.empty.setGeometry(self.grid.rect())
            self.status.setText("No photographs yet")
        else:
            total = sum(p.size for p in photos)
            selected = len(self.grid.selectedItems())
            text = f"{n} photograph{'s' if n != 1 else ''}  ·  {human_size(total)}"
            if selected:
                text += f"  ·  {selected} selected"
            self.status.setText(text)
        has = n > 0
        self.export_btn.setEnabled(has)
        self.remove_btn.setEnabled(bool(self.grid.selectedItems()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.empty.isVisible():
            self.empty.setGeometry(self.grid.rect())

    def about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {APP_VERSION}<br><br>"
            "Drop photographs in, drag them into the order you want, then "
            "export the whole set in that exact order.<br><br>"
            "Nothing is uploaded anywhere and your original files are never "
            "moved, renamed or altered. Exporting writes copies.<br><br>"
            f"<span style='color:{MUTED}'>Set in Cormorant Garamond and Jost, "
            "both under the SIL Open Font License.</span>",
        )

    def closeEvent(self, event):
        if self._worker is not None and self._worker.isRunning():
            reply = QMessageBox.question(
                self, APP_NAME,
                "An export is still running. Stop it and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._worker.cancel()
            self._worker.wait(3000)
        if self._dirty and self.grid.count():
            reply = QMessageBox.question(
                self, APP_NAME,
                "Close without exporting?\n\n"
                "The order you have arranged will be lost unless you export it "
                "or save the list first.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


# ---------------------------------------------------------------------------
#  Look
# ---------------------------------------------------------------------------

CHECK_OFF = resource_path("check-off.png").as_posix()
CHECK_ON = resource_path("check-on.png").as_posix()
RADIO_OFF = resource_path("radio-off.png").as_posix()
RADIO_ON = resource_path("radio-on.png").as_posix()

def stylesheet():
    """The whole look, as one Qt style sheet."""
    return f"""
QMainWindow, QDialog {{ background: {PANEL}; }}
QWidget {{ color: {INK}; font-family: '{SANS_FAMILY}'; font-size: 13px; }}

QMenuBar {{ background: {PANEL}; border-bottom: 1px solid {LINE}; padding: 2px; }}
QMenuBar::item {{ padding: 5px 11px; border-radius: 4px; }}
QMenuBar::item:selected {{ background: {PAPER_3}; }}
QMenu {{ background: {PAPER}; border: 1px solid {LINE};
         border-radius: 4px; padding: 6px; }}
QMenu::item {{ padding: 7px 22px 7px 14px; border-radius: 3px; }}
QMenu::item:selected {{ background: {ACCENT}; color: {ON_DARK}; }}
QMenu::separator {{ height: 1px; background: {LINE}; margin: 5px 8px; }}

#toolbar {{ background: {PANEL}; border-bottom: 1px solid {LINE}; }}
#vsep {{ color: {LINE}; }}

/* Near-square corners and letterspaced uppercase labels. */
QPushButton {{
    background: {PAPER}; border: 1px solid {LINE}; border-radius: 3px;
    padding: 8px 15px; color: {INK_SOFT};
    font-size: 11px; letter-spacing: 1.4px; text-transform: uppercase;
}}
QPushButton:hover {{ background: {PAPER_2}; border-color: {ACCENT_2}; color: {INK}; }}
QPushButton:pressed {{ background: {PAPER_3}; }}
QPushButton:disabled {{ color: #B6ABA0; background: {PAPER}; border-color: #EFE7DC; }}
QPushButton#primary {{
    background: {ACCENT}; border: 1px solid {ACCENT}; color: {ON_DARK};
    font-weight: 500;
}}
QPushButton#primary:hover {{ background: #85684E; border-color: #85684E; color: {ON_DARK}; }}
QPushButton#primary:pressed {{ background: #745A44; }}
QPushButton#primary:disabled {{
    background: {PAPER_3}; border-color: {LINE}; color: #B6ABA0;
}}
QPushButton::menu-indicator {{ subcontrol-position: right center; right: 7px; }}

QListWidget {{ background: {BG}; border: none; }}

QLineEdit, QSpinBox, QComboBox {{
    background: {PAPER}; border: 1px solid {LINE}; border-radius: 3px;
    padding: 7px 9px; color: {INK}; selection-background-color: {ACCENT_2};
    selection-color: {INK};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{ border-color: {ACCENT}; }}
QComboBox QAbstractItemView {{
    background: {PAPER}; border: 1px solid {LINE}; color: {INK};
    selection-background-color: {ACCENT}; selection-color: {ON_DARK}; outline: none;
}}

QCheckBox, QRadioButton {{ spacing: 9px; padding: 2px; color: {INK_SOFT}; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 18px; height: 18px; }}
QCheckBox::indicator {{ image: url({CHECK_OFF}); }}
QCheckBox::indicator:checked {{ image: url({CHECK_ON}); }}
QRadioButton::indicator {{ image: url({RADIO_OFF}); }}
QRadioButton::indicator:checked {{ image: url({RADIO_ON}); }}
QCheckBox:disabled, QRadioButton:disabled {{ color: #B6ABA0; }}

QSlider::groove:horizontal {{ height: 2px; background: {LINE}; }}
QSlider::handle:horizontal {{
    background: {ACCENT}; width: 11px; height: 11px; margin: -5px 0;
    border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {INK_SOFT}; }}
QSlider::sub-page:horizontal {{ background: {ACCENT_2}; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 2px; }}
QScrollBar::handle:vertical {{
    background: #D6C8B7; border-radius: 5px; min-height: 40px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT_2}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QStatusBar {{ background: {PANEL}; border-top: 1px solid {LINE}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QLabel {{ color: {MUTED}; padding: 3px 7px; font-size: 12px; }}
#hint {{ color: #A2968B; }}

#wordmark_name {{
    font-family: '{SERIF_FAMILY}'; font-size: 21px; color: {INK};
    letter-spacing: 1.3px;
}}
#wordmark_sub {{
    font-size: 9px; letter-spacing: 2.6px; text-transform: uppercase;
    color: {MUTED};
}}
#empty {{ color: {MUTED}; font-size: 15px; line-height: 150%; }}
#dialog_title {{
    font-family: '{SERIF_FAMILY}'; font-size: 25px; color: {INK};
    letter-spacing: 0.4px;
}}
#preview {{
    background: {PAPER_2}; border: 1px solid {LINE}; border-radius: 3px;
    padding: 11px 13px; font-family: Consolas, monospace; font-size: 12px;
    color: {INK_SOFT};
}}
#warn {{ color: #A8703C; }}
#rule {{ color: {LINE}; }}
#preview_dialog {{ background: {INK}; }}
#preview_dialog QLabel {{ color: {PAPER}; }}

QProgressDialog {{ background: {PANEL}; }}
QProgressBar {{
    background: {PAPER_3}; border: none; border-radius: 2px;
    height: 4px; text-align: center;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 2px; }}

QToolTip {{
    background: {INK}; color: {PAPER}; border: none;
    padding: 7px 9px; font-size: 12px;
}}
"""


def main():
    if hasattr(Qt.ApplicationAttribute, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setStyle("Fusion")

    global SERIF_FAMILY, SANS_FAMILY
    SERIF_FAMILY, SANS_FAMILY = load_fonts()
    app.setFont(QFont(SANS_FAMILY, 10))
    app.setStyleSheet(stylesheet())

    icon = resource_path("icon.ico")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))

    window = MainWindow()
    window.show()

    # Photographs can be handed straight to the executable, e.g. by dropping a
    # folder onto it in Explorer.
    args = [a for a in sys.argv[1:] if Path(a).exists()]
    if args:
        window._add(args)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
