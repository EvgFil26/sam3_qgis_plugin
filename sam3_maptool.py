from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtGui import QColor
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import (
    QgsWkbTypes, QgsPointXY, QgsRectangle,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsProject,
)


class Sam3BoxMapTool(QgsMapTool):
    """Рисование боксов (позитив/негатив) прямо на холсте QGIS.

    Логика — прямой аналог pick_boxes_interactive() из скрипта:
    тянем мышью прямоугольник, помечаем его как позитивный/негативный,
    можно нарисовать несколько в разных местах (в т.ч. после
    панорамирования/зума штатными средствами QGIS — их отдельно
    реализовывать не нужно, это уже умеет QgsMapCanvas).

    Боксы отдаются наружу в двух видах:
      - `box_px` — пиксельные координаты растрового слоя (нужны модели,
        та же система координат, которую ожидает
        run_segmentation()/run_segmentation_tiled() в sam3_engine.py);
      - `map_coords_str` — читаемая строка для UI/лога: координаты в
        текущей СК проекта + геодезические (градусы/минуты) в скобках.

    Управление с клавиатуры (когда холст в фокусе):
      p / n   — переключить метку (позитив/негатив)
      z       — отменить последний бокс
      r       — сбросить все боксы
      ПКМ / Esc — закончить выбор
    """

    boxAdded = pyqtSignal(list, int, str)  # box_px [x1,y1,x2,y2], label (1/0), map_coords_str
    boxesCleared = pyqtSignal()
    boxRemoved = pyqtSignal()
    finished = pyqtSignal()

    def __init__(self, canvas, raster_layer):
        super().__init__(canvas)
        self.canvas = canvas
        self.raster_layer = raster_layer

        self.dragging = False
        self.start_point = None
        self.current_label = 1  # 1 = позитив, 0 = негатив

        self.boxes_px = []
        self.box_labels = []
        self.confirmed_bands = []  # постоянные QgsRubberBand подтверждённых боксов

        # временный rubber band — прямоугольник, который тянем мышью прямо сейчас
        self.drag_band = QgsRubberBand(canvas, QgsWkbTypes.PolygonGeometry)
        self.drag_band.setWidth(2)

    # ------------------------------------------------------------------ #
    # Публичный API — дёргается из dock-виджета
    # ------------------------------------------------------------------ #
    def set_label(self, label):
        self.current_label = label

    def undo_last(self):
        if not self.boxes_px:
            return
        self.boxes_px.pop()
        self.box_labels.pop()
        band = self.confirmed_bands.pop()
        self.canvas.scene().removeItem(band)
        self.boxRemoved.emit()

    def clear_all(self):
        self.boxes_px = []
        self.box_labels = []
        for band in self.confirmed_bands:
            self.canvas.scene().removeItem(band)
        self.confirmed_bands = []
        self.boxesCleared.emit()

    # ------------------------------------------------------------------ #
    # События холста
    # ------------------------------------------------------------------ #
    def canvasPressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.start_point = self.toMapCoordinates(event.pos())
            self.drag_band.reset(QgsWkbTypes.PolygonGeometry)

    def canvasMoveEvent(self, event):
        if self.dragging and self.start_point is not None:
            end_point = self.toMapCoordinates(event.pos())
            color = QColor(0, 255, 0, 80) if self.current_label == 1 else QColor(255, 0, 0, 80)
            self.drag_band.setColor(color)
            self._set_band_geometry(self.drag_band, self.start_point, end_point)

    def canvasReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.dragging:
            self.dragging = False
            end_point = self.toMapCoordinates(event.pos())
            self.drag_band.reset(QgsWkbTypes.PolygonGeometry)
            self._finalize_box(self.start_point, end_point)
        elif event.button() == Qt.RightButton:
            self.finished.emit()  # ПКМ — закончить выбор

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_P:
            self.set_label(1)
        elif event.key() == Qt.Key_N:
            self.set_label(0)
        elif event.key() == Qt.Key_Z:
            self.undo_last()
        elif event.key() == Qt.Key_R:
            self.clear_all()
        elif event.key() == Qt.Key_Escape:
            self.finished.emit()

    # ------------------------------------------------------------------ #
    # Внутренняя механика
    # ------------------------------------------------------------------ #
    def _set_band_geometry(self, band, p1, p2):
        rect = QgsRectangle(p1, p2)
        points = [
            QgsPointXY(rect.xMinimum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMinimum()),
            QgsPointXY(rect.xMaximum(), rect.yMaximum()),
            QgsPointXY(rect.xMinimum(), rect.yMaximum()),
        ]
        band.reset(QgsWkbTypes.PolygonGeometry)
        for pt in points:
            band.addPoint(pt, False)
        band.closePoints()

    def _map_point_to_pixel(self, map_point):
        """Переводит точку из CRS проекта в пиксельные координаты растра.
        Аналог map_point_to_pixel()/rowcol() из исходного скрипта, но
        через API QgsRasterLayer вместо rasterio."""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        raster_crs = self.raster_layer.crs()

        if canvas_crs != raster_crs:
            ctx = QgsProject.instance().transformContext()
            xform = QgsCoordinateTransform(canvas_crs, raster_crs, ctx)
            map_point = xform.transform(map_point)

        extent = self.raster_layer.extent()
        px_x = (map_point.x() - extent.xMinimum()) / self.raster_layer.rasterUnitsPerPixelX()
        # Y инвертируется: строки растра считаются сверху вниз,
        # а координата Y карты растёт снизу вверх.
        px_y = (extent.yMaximum() - map_point.y()) / self.raster_layer.rasterUnitsPerPixelY()
        return px_x, px_y

    def _format_dm(self, value, positive_hemisphere, negative_hemisphere):
        """Десятичные градусы -> строка вида 59°02.345'N."""
        hemisphere = positive_hemisphere if value >= 0 else negative_hemisphere
        value = abs(value)
        degrees = int(value)
        minutes = (value - degrees) * 60
        return f"{degrees}\u00b0{minutes:.3f}'{hemisphere}"

    def _format_map_box(self, p1, p2):
        """Читаемая строка для лога: координаты в СК проекта (как они
        сейчас отображаются на холсте) плюс геодезические координаты
        центра бокса в градусах/минутах в скобках — для быстрой сверки
        "на глаз", независимо от того, в какой проекции работает проект."""
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        rect = QgsRectangle(p1, p2)

        crs_label = canvas_crs.authid() or canvas_crs.description()
        map_str = (
            f"X: {rect.xMinimum():.2f}..{rect.xMaximum():.2f}, "
            f"Y: {rect.yMinimum():.2f}..{rect.yMaximum():.2f} [{crs_label}]"
        )

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        center = rect.center()
        if canvas_crs != wgs84:
            try:
                ctx = QgsProject.instance().transformContext()
                xform = QgsCoordinateTransform(canvas_crs, wgs84, ctx)
                center = xform.transform(center)
            except Exception:
                return map_str  # не удалось перевести в геодезические — обойдёмся без них

        lat_str = self._format_dm(center.y(), "N", "S")
        lon_str = self._format_dm(center.x(), "E", "W")
        return f"{map_str} (центр ~ {lat_str}, {lon_str})"

    def _finalize_box(self, p1, p2):
        px1, py1 = self._map_point_to_pixel(p1)
        px2, py2 = self._map_point_to_pixel(p2)

        box_px = [
            int(min(px1, px2)), int(min(py1, py2)),
            int(max(px1, px2)), int(max(py1, py2)),
        ]
        if box_px[2] <= box_px[0] or box_px[3] <= box_px[1]:
            return  # вырожденный бокс (клик без протяжки) — игнорируем

        self.boxes_px.append(box_px)
        self.box_labels.append(self.current_label)

        band = QgsRubberBand(self.canvas, QgsWkbTypes.PolygonGeometry)
        band.setWidth(2)
        color = QColor(0, 255, 0, 120) if self.current_label == 1 else QColor(255, 0, 0, 120)
        band.setColor(color)
        self._set_band_geometry(band, p1, p2)
        self.confirmed_bands.append(band)

        map_coords_str = self._format_map_box(p1, p2)
        self.boxAdded.emit(box_px, self.current_label, map_coords_str)
