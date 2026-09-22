from qgis.core import QgsTask, QgsMessageLog, Qgis

from .sam3_engine import (
    run_pipeline, TILING_THRESHOLD, TILE_SIZE, TILE_OVERLAP,
    DEDUP_IOU_THRESHOLD, SIEVE_THRESHOLD_PX,
)


MESSAGE_CATEGORY = "SAM3"


class Sam3SegmentationTask(QgsTask):
    """Оборачивает run_pipeline() в QgsTask, чтобы инференс (загрузка
    модели + forward pass, потенциально по многим тайлам) шёл в фоновом
    потоке и не подвешивал интерфейс QGIS. Прогресс и ошибки идут через
    QgsMessageLog — их видно в Log Messages Panel (вкладка 'SAM3')."""

    def __init__(self, description, engine, model_path, raster_path,
                 threshold, mask_threshold, text_prompt, boxes_px, box_labels,
                 tiling_threshold=TILING_THRESHOLD, tile_size=TILE_SIZE, tile_overlap=TILE_OVERLAP,
                 dedup_iou_threshold=DEDUP_IOU_THRESHOLD, sieve_threshold_px=SIEVE_THRESHOLD_PX,
                 min_area=0.0, output_gpkg_path=None):
        super().__init__(description, QgsTask.CanCancel)
        self.engine = engine
        self.model_path = model_path
        self.raster_path = raster_path
        self.threshold = threshold
        self.mask_threshold = mask_threshold
        self.text_prompt = text_prompt
        self.boxes_px = boxes_px
        self.box_labels = box_labels

        self.tiling_threshold = tiling_threshold
        self.tile_size = tile_size
        self.tile_overlap = tile_overlap
        self.dedup_iou_threshold = dedup_iou_threshold
        self.sieve_threshold_px = sieve_threshold_px
        self.min_area = min_area
        self.output_gpkg_path = output_gpkg_path

        self.features = None
        self.overlay_image = None
        self.crs_wkt = None
        self.error = None

    def run(self):
        try:
            def log(msg):
                QgsMessageLog.logMessage(msg, MESSAGE_CATEGORY, Qgis.Info)

            def progress(i, total):
                if self.isCanceled():
                    return
                self.setProgress(int(100 * i / total))

            self.engine.ensure_loaded(self.model_path, log_cb=log)

            self.features, self.overlay_image, self.crs_wkt = run_pipeline(
                self.engine, self.raster_path, self.threshold, self.mask_threshold,
                text_prompt=self.text_prompt, boxes_px=self.boxes_px, box_labels=self.box_labels,
                tiling_threshold=self.tiling_threshold, tile_size=self.tile_size,
                tile_overlap=self.tile_overlap, dedup_iou_threshold=self.dedup_iou_threshold,
                sieve_threshold_px=self.sieve_threshold_px,
                min_area=self.min_area, output_gpkg_path=self.output_gpkg_path,
                log_cb=log, progress_cb=progress,
            )
            return True

        except Exception as exc:
            self.error = str(exc)
            QgsMessageLog.logMessage(f"Ошибка задачи SAM3: {exc}", MESSAGE_CATEGORY, Qgis.Critical)
            return False
