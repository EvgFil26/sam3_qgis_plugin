from qgis.PyQt.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QFormLayout, QHBoxLayout,
    QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox, QPushButton, QPlainTextEdit,
    QLabel, QRadioButton, QButtonGroup, QFileDialog, QGroupBox,
)
from qgis.PyQt.QtCore import Qt, QMetaType
from qgis.gui import QgsMapLayerComboBox
from qgis.core import (
    QgsMapLayerProxyModel, QgsApplication, QgsVectorLayer, QgsFeature,
    QgsField, QgsGeometry, QgsCoordinateReferenceSystem, QgsProject,
)

from .sam3_maptool import Sam3BoxMapTool
from .sam3_task import Sam3SegmentationTask
from .sam3_engine import TILING_THRESHOLD, TILE_SIZE, TILE_OVERLAP, DEDUP_IOU_THRESHOLD, SIEVE_THRESHOLD_PX


class Sam3DockWidget(QDockWidget):
    """Панель плагина: выбор снимка, режима (текст / боксы / текст+боксы),
    параметры threshold/mask_threshold, выделение боксов прямо на холсте,
    запуск сегментации в фоне и добавление результата в проект."""

    def __init__(self, canvas, engine, parent=None):
        super().__init__("SAM3 Segmentation", parent)
        self.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)

        self.canvas = canvas
        self.engine = engine
        self.map_tool = None
        self.previous_map_tool = None
        self.task = None
        self._last_gpkg_path = None

        # Состояние выбранных боксов — синхронизируется с map tool через сигналы
        self.boxes_px = []
        self.box_labels = []

        content = QWidget()
        layout = QVBoxLayout(content)

        form = QFormLayout()

        # --- Модель ------------------------------------------------------
        model_row = QHBoxLayout()
        self.model_path_edit = QLineEdit()
        self.model_path_edit.setPlaceholderText(r"C:\Nogames\pract\fedor\models")
        model_row.addWidget(self.model_path_edit)
        self.model_browse_btn = QPushButton("...")
        self.model_browse_btn.setMaximumWidth(30)
        self.model_browse_btn.clicked.connect(self._on_browse_model)
        model_row.addWidget(self.model_browse_btn)
        form.addRow("Путь к модели:", model_row)

        # --- Слой-подложка -------------------------------------------------
        self.layer_combo = QgsMapLayerComboBox()
        self.layer_combo.setFilters(QgsMapLayerProxyModel.RasterLayer)
        form.addRow("Снимок:", self.layer_combo)

        # --- Режим -----------------------------------------------------------
        self.mode_combo = QComboBox()
        self.mode_combo.addItems([
            "1 - Текстовый промпт",
            "2 - Боксы на карте",
            "3 - Текст + боксы",
        ])
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        form.addRow("Режим:", self.mode_combo)

        self.text_edit = QLineEdit()
        self.text_edit.setPlaceholderText("например: agricultural field")
        form.addRow("Текстовый промпт:", self.text_edit)

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0.0, 1.0)
        self.threshold_spin.setSingleStep(0.05)
        self.threshold_spin.setValue(0.3)
        form.addRow("Threshold:", self.threshold_spin)

        self.mask_threshold_spin = QDoubleSpinBox()
        self.mask_threshold_spin.setRange(0.0, 1.0)
        self.mask_threshold_spin.setSingleStep(0.05)
        self.mask_threshold_spin.setValue(0.5)
        form.addRow("Mask threshold:", self.mask_threshold_spin)

        # --- Путь сохранения результата ----------------------------------
        gpkg_row = QHBoxLayout()
        self.gpkg_path_edit = QLineEdit()
        self.gpkg_path_edit.setPlaceholderText(
            "не задано — результат добавится как временный (memory) слой"
        )
        gpkg_row.addWidget(self.gpkg_path_edit)
        self.gpkg_browse_btn = QPushButton("...")
        self.gpkg_browse_btn.setMaximumWidth(30)
        self.gpkg_browse_btn.clicked.connect(self._on_browse_gpkg)
        gpkg_row.addWidget(self.gpkg_browse_btn)
        form.addRow("Сохранить в .gpkg:", gpkg_row)

        layout.addLayout(form)

        # --- Дополнительные параметры (тайлинг, дедуп, мин. площадь) ----
        advanced_group = QGroupBox("Дополнительные параметры")
        advanced_form = QFormLayout()

        self.tiling_threshold_spin = QSpinBox()
        self.tiling_threshold_spin.setRange(256, 100000)
        self.tiling_threshold_spin.setSingleStep(256)
        self.tiling_threshold_spin.setValue(TILING_THRESHOLD)
        self.tiling_threshold_spin.setSuffix(" px")
        advanced_form.addRow("Порог тайлинга:", self.tiling_threshold_spin)

        self.tile_size_spin = QSpinBox()
        self.tile_size_spin.setRange(128, 100000)
        self.tile_size_spin.setSingleStep(128)
        self.tile_size_spin.setValue(TILE_SIZE)
        self.tile_size_spin.setSuffix(" px")
        advanced_form.addRow("Размер тайла:", self.tile_size_spin)

        self.tile_overlap_spin = QSpinBox()
        self.tile_overlap_spin.setRange(0, 4096)
        self.tile_overlap_spin.setSingleStep(32)
        self.tile_overlap_spin.setValue(TILE_OVERLAP)
        self.tile_overlap_spin.setSuffix(" px")
        advanced_form.addRow("Перекрытие тайлов:", self.tile_overlap_spin)

        self.dedup_iou_spin = QDoubleSpinBox()
        self.dedup_iou_spin.setRange(0.0, 1.0)
        self.dedup_iou_spin.setSingleStep(0.05)
        self.dedup_iou_spin.setValue(DEDUP_IOU_THRESHOLD)
        self.dedup_iou_spin.setToolTip(
            "Порог IoU для схлопывания дублей на стыках тайлов.\n"
            "Ниже — агрессивнее (риск слить два соседних объекта в один).\n"
            "Выше — консервативнее (риск оставить настоящий дубль на стыке)."
        )
        advanced_form.addRow("IoU дедупликации:", self.dedup_iou_spin)

        self.sieve_threshold_spin = QSpinBox()
        self.sieve_threshold_spin.setRange(0, 100000)
        self.sieve_threshold_spin.setValue(SIEVE_THRESHOLD_PX)
        self.sieve_threshold_spin.setSuffix(" px")
        self.sieve_threshold_spin.setToolTip(
            "Связные дырки/вкрапления в маске мельче этого размера (в пикселях)\n"
            "сшиваются с соседями ДО векторизации (gdal.SieveFilter) — убирает\n"
            "шум бинаризации (мелкие 'дырки', которые на самом деле не дырки),\n"
            "не меняя число найденных объектов. 0 — фильтр выключен.\n\n"
            "ВНИМАНИЕ: если весь объект меньше этого порога и со всех сторон\n"
            "окружён фоном — он сольётся с фоном и ИСЧЕЗНЕТ целиком, а не\n"
            "просто потеряет дырки. Держи значение заметно меньше размера\n"
            "самого маленького нужного объекта."
        )
        advanced_form.addRow("Фильтр шумов маски:", self.sieve_threshold_spin)

        self.min_area_spin = QDoubleSpinBox()
        self.min_area_spin.setRange(0.0, 1e12)
        self.min_area_spin.setDecimals(2)
        self.min_area_spin.setSingleStep(10.0)
        self.min_area_spin.setValue(0.0)
        self.min_area_spin.setToolTip(
            "Минимальная площадь объекта в квадратных единицах СК растра\n"
            "(м² для метрических проекций, град² для географических CRS).\n"
            "0 — фильтр выключен."
        )
        advanced_form.addRow("Мин. площадь объекта:", self.min_area_spin)

        advanced_group.setLayout(advanced_form)
        layout.addWidget(advanced_group)

        # --- Метка бокса (позитив/негатив) ------------------------------
        label_row = QHBoxLayout()
        label_row.addWidget(QLabel("Метка бокса:"))
        self.positive_radio = QRadioButton("Позитив (p)")
        self.negative_radio = QRadioButton("Негатив (n)")
        self.positive_radio.setChecked(True)
        label_group = QButtonGroup(self)
        label_group.addButton(self.positive_radio)
        label_group.addButton(self.negative_radio)
        self.positive_radio.toggled.connect(self._on_label_toggled)
        label_row.addWidget(self.positive_radio)
        label_row.addWidget(self.negative_radio)
        layout.addLayout(label_row)

        # --- Управление боксами на карте --------------------------------
        boxes_row = QHBoxLayout()
        self.pick_boxes_btn = QPushButton("Выделить боксы на карте")
        self.pick_boxes_btn.setCheckable(True)
        self.pick_boxes_btn.toggled.connect(self._on_pick_boxes_toggled)
        boxes_row.addWidget(self.pick_boxes_btn)

        self.undo_btn = QPushButton("Отменить последний")
        self.undo_btn.clicked.connect(self._on_undo_clicked)
        boxes_row.addWidget(self.undo_btn)

        self.clear_btn = QPushButton("Очистить все")
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        boxes_row.addWidget(self.clear_btn)
        layout.addLayout(boxes_row)

        self.boxes_label = QLabel("Боксов выбрано: 0")
        layout.addWidget(self.boxes_label)

        # --- Запуск ------------------------------------------------------
        self.run_btn = QPushButton("Запустить сегментацию")
        self.run_btn.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_btn)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)

        self.setWidget(content)

        self._on_mode_changed(self.mode_combo.currentIndex())

    # ---------------------------------------------------------------------- #
    # UI-реакции
    # ---------------------------------------------------------------------- #
    def _on_mode_changed(self, index):
        mode = index + 1
        self.text_edit.setEnabled(mode in (1, 3))       # текст нужен в 1 и 3
        boxes_needed = mode in (2, 3)
        self.pick_boxes_btn.setEnabled(boxes_needed)
        self.undo_btn.setEnabled(boxes_needed)
        self.clear_btn.setEnabled(boxes_needed)

    def _on_browse_model(self):
        path = QFileDialog.getExistingDirectory(self, "Выбери папку с моделью SAM3")
        if path:
            self.model_path_edit.setText(path)

    def _on_browse_gpkg(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Сохранить результат как", "", "GeoPackage (*.gpkg)"
        )
        if path:
            if not path.lower().endswith(".gpkg"):
                path += ".gpkg"
            self.gpkg_path_edit.setText(path)

    def _on_label_toggled(self, checked):
        label = 1 if self.positive_radio.isChecked() else 0
        if self.map_tool is not None:
            self.map_tool.set_label(label)

    def _on_pick_boxes_toggled(self, checked):
        if checked:
            self._activate_map_tool()
        else:
            self._deactivate_map_tool()

    def _on_undo_clicked(self):
        if self.map_tool is not None:
            self.map_tool.undo_last()

    def _on_clear_clicked(self):
        if self.map_tool is not None:
            self.map_tool.clear_all()
        else:
            self.boxes_px = []
            self.box_labels = []
            self._refresh_boxes_label()

    # ---------------------------------------------------------------------- #
    # Запуск сегментации
    # ---------------------------------------------------------------------- #
    def _on_run_clicked(self):
        raster_layer = self.layer_combo.currentLayer()
        if raster_layer is None:
            self.log.appendPlainText("Выбери снимок в поле 'Снимок'.")
            return

        model_path = self.model_path_edit.text().strip()
        if not model_path:
            self.log.appendPlainText("Укажи путь к модели SAM3.")
            return

        mode = self.mode_combo.currentIndex() + 1
        text_prompt = self.text_edit.text().strip() or None
        threshold = self.threshold_spin.value()
        mask_threshold = self.mask_threshold_spin.value()

        boxes_px = self.boxes_px if mode in (2, 3) else None
        box_labels = self.box_labels if mode in (2, 3) else None

        if mode in (2, 3) and not boxes_px:
            self.log.appendPlainText("В этом режиме нужно выделить хотя бы один бокс на карте.")
            return
        if mode in (1, 3) and not text_prompt:
            self.log.appendPlainText("В этом режиме нужно ввести текстовый промпт.")
            return

        raster_path = raster_layer.source()

        tiling_threshold = self.tiling_threshold_spin.value()
        tile_size = self.tile_size_spin.value()
        tile_overlap = self.tile_overlap_spin.value()
        dedup_iou_threshold = self.dedup_iou_spin.value()
        sieve_threshold_px = self.sieve_threshold_spin.value()
        min_area = self.min_area_spin.value()
        gpkg_path = self.gpkg_path_edit.text().strip() or None
        self._last_gpkg_path = gpkg_path

        self.run_btn.setEnabled(False)
        self.log.appendPlainText(f"Запуск сегментации ({raster_path})...")

        self.task = Sam3SegmentationTask(
            "SAM3 сегментация", self.engine, model_path, raster_path,
            threshold, mask_threshold, text_prompt, boxes_px, box_labels,
            tiling_threshold=tiling_threshold, tile_size=tile_size, tile_overlap=tile_overlap,
            dedup_iou_threshold=dedup_iou_threshold, sieve_threshold_px=sieve_threshold_px,
            min_area=min_area, output_gpkg_path=gpkg_path,
        )
        self.task.taskCompleted.connect(self._on_task_completed)
        self.task.taskTerminated.connect(self._on_task_terminated)
        QgsApplication.taskManager().addTask(self.task)

    def _on_task_completed(self):
        self.run_btn.setEnabled(True)

        if self.task.error:
            self.log.appendPlainText(f"Ошибка: {self.task.error}")
            return

        n = len(self.task.features) if self.task.features else 0
        self.log.appendPlainText(f"Готово: найдено {n} объектов.")

        if self._last_gpkg_path:
            self._add_gpkg_layer(self._last_gpkg_path)
        else:
            self._add_result_layer(self.task.features, self.task.crs_wkt)

    def _add_gpkg_layer(self, path):
        """Грузит уже записанный на диск GeoPackage (см. write_gpkg() в
        sam3_engine.py — запись туда идёт ещё внутри фоновой задачи) —
        быстрее, чем строить memory-слой и потом вручную сохранять его
        через 'Make Permanent', и сразу даёт постоянный слой на диске."""
        layer = QgsVectorLayer(f"{path}|layername=sam3_results", "SAM3 results", "ogr")
        if not layer.isValid():
            self.log.appendPlainText(f"Не удалось открыть сохранённый слой: {path}")
            return
        QgsProject.instance().addMapLayer(layer)
        self.log.appendPlainText(f"Слой 'SAM3 results' загружен из {path} ({layer.featureCount()} объектов).")

    def _on_task_terminated(self):
        self.run_btn.setEnabled(True)
        if self.task and self.task.error:
            self.log.appendPlainText(f"Задача завершилась с ошибкой: {self.task.error}")
        else:
            self.log.appendPlainText("Задача прервана.")

    def _add_result_layer(self, features, crs_wkt):
        if not features:
            self.log.appendPlainText("Объекты не найдены — попробуй снизить threshold или изменить промпт/боксы.")
            return

        crs = QgsCoordinateReferenceSystem.fromWkt(crs_wkt) if crs_wkt else QgsProject.instance().crs()
        crs_id = crs.authid() if crs.isValid() and crs.authid() else "EPSG:4326"

        layer = QgsVectorLayer(f"Polygon?crs={crs_id}", "SAM3 results", "memory")
        provider = layer.dataProvider()
        provider.addAttributes([
            QgsField("object_id", QMetaType.Type.Int),
            QgsField("score", QMetaType.Type.Double),
            QgsField("prompt", QMetaType.Type.QString),
        ])
        layer.updateFields()

        qgs_features = []
        for f in features:
            feat = QgsFeature(layer.fields())
            feat.setGeometry(QgsGeometry.fromWkt(f["geometry_wkt"]))
            feat.setAttribute("object_id", f.get("object_id"))
            feat.setAttribute("score", f.get("score"))
            feat.setAttribute("prompt", f.get("prompt", ""))
            qgs_features.append(feat)

        provider.addFeatures(qgs_features)
        layer.updateExtents()

        QgsProject.instance().addMapLayer(layer)
        self.log.appendPlainText(f"Слой 'SAM3 results' добавлен в проект ({len(qgs_features)} объектов).")

    # ---------------------------------------------------------------------- #
    # Управление map tool
    # ---------------------------------------------------------------------- #
    def _activate_map_tool(self):
        raster_layer = self.layer_combo.currentLayer()
        if raster_layer is None:
            self.log.appendPlainText("Сначала выбери снимок в поле 'Снимок' — растровый слой не выбран.")
            self.pick_boxes_btn.setChecked(False)
            return

        self.previous_map_tool = self.canvas.mapTool()

        self.map_tool = Sam3BoxMapTool(self.canvas, raster_layer)
        self.map_tool.set_label(1 if self.positive_radio.isChecked() else 0)
        self.map_tool.boxAdded.connect(self._on_box_added)
        self.map_tool.boxRemoved.connect(self._on_box_removed)
        self.map_tool.boxesCleared.connect(self._on_boxes_cleared)
        self.map_tool.finished.connect(lambda: self.pick_boxes_btn.setChecked(False))

        # переносим уже накопленные боксы в новый инструмент (повторная
        # активация не должна начинать с нуля)
        self.map_tool.boxes_px = list(self.boxes_px)
        self.map_tool.box_labels = list(self.box_labels)

        self.canvas.setMapTool(self.map_tool)
        self.log.appendPlainText(
            "Рисование боксов включено: тяните ЛКМ на карте. "
            "p/n — метка, z — отменить, r — сбросить, ПКМ/Esc — закончить."
        )

    def _deactivate_map_tool(self):
        if self.map_tool is not None:
            self.boxes_px = list(self.map_tool.boxes_px)
            self.box_labels = list(self.map_tool.box_labels)

        if self.previous_map_tool is not None:
            self.canvas.setMapTool(self.previous_map_tool)
        else:
            self.canvas.unsetMapTool(self.map_tool)

        self.map_tool = None
        self.log.appendPlainText("Рисование боксов выключено.")
        self._refresh_boxes_label()

    def _on_box_added(self, box_px, label, map_coords_str):
        self.boxes_px.append(box_px)
        self.box_labels.append(label)
        label_str = "позитивный" if label == 1 else "негативный"
        self.log.appendPlainText(
            f"Бокс #{len(self.boxes_px)} ({label_str}): {map_coords_str} | px: {box_px}"
        )
        self._refresh_boxes_label()

    def _on_box_removed(self):
        if self.map_tool is not None:
            self.boxes_px = list(self.map_tool.boxes_px)
            self.box_labels = list(self.map_tool.box_labels)
        self.log.appendPlainText("Последний бокс отменён.")
        self._refresh_boxes_label()

    def _on_boxes_cleared(self):
        self.boxes_px = []
        self.box_labels = []
        self.log.appendPlainText("Все боксы сброшены.")
        self._refresh_boxes_label()

    def _refresh_boxes_label(self):
        self.boxes_label.setText(f"Боксов выбрано: {len(self.boxes_px)}")

    # ---------------------------------------------------------------------- #
    def closeEvent(self, event):
        if self.pick_boxes_btn.isChecked():
            self.pick_boxes_btn.setChecked(False)
        super().closeEvent(event)
