"""Render the editable vector icon into a multi-resolution Windows ICO."""
from pathlib import Path

from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main():
    assets = Path(__file__).resolve().parents[1] / "assets"
    image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    QSvgRenderer(str(assets / "voxlate.svg")).render(painter)
    painter.end()
    image.save(str(assets / "voxlate.png"))
    with Image.open(assets / "voxlate.png") as source:
        source.save(assets / "voxlate.ico", sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])


if __name__ == "__main__":
    main()
