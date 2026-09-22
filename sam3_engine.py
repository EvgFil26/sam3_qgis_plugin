"""
sam3_engine.py — модель и логика инференса SAM3 для QGIS-плагина.

Единственные пакеты, которые реально нужно доустановить в python QGIS —
torch и transformers (чистый ML, без пересекающихся C-библиотек).
"""

import os
import torch
import numpy as np
from PIL import Image
# Убрано ограничение PIL на размер изображения (decompression bomb guard).
# Это наши собственные доверенные снимки (не файлы от посторонних
# пользователей), а мозаики Чукотки могут быть больше любого разумного
# фиксированного порога — проще снять ограничение полностью, чем
# гадать с очередным "магическим" числом.
Image.MAX_IMAGE_PIXELS = None

from osgeo import gdal, ogr, osr

from transformers import Sam3Processor, Sam3Model


TILING_THRESHOLD = 4000   # px — если снимок больше по любой стороне, включаем тайлинг
TILE_SIZE = 2048          # px
TILE_OVERLAP = 128        # px
DEDUP_IOU_THRESHOLD = 0.5
SIEVE_THRESHOLD_PX = 4    # px — связные "дырки"/вкрапления мельче этого сшиваются с соседями

# Драйвер запрашивается по имени один раз на модуль, а не на каждый
# вызов _sieve_mask()/_mask_to_polygons_pixel_space() — GetDriverByName
# сам по себе недорогой, но при сотнях объектов на снимок мелкие
# накладные расходы складываются.
_MEM_DRIVER = gdal.GetDriverByName("MEM")
_OGR_MEM_DRIVER = ogr.GetDriverByName("Memory")


class Sam3Engine:
    """Держит модель/процессор в памяти между запусками. Модель грузится
    один раз (при первом запуске или смене пути) и переиспользуется —
    загрузка занимает много времени, повторять её на каждый клик
    'Запустить' было бы неприемлемо медленно."""

    def __init__(self):
        self.model = None
        self.processor = None
        self.model_path = None
        self.device = "cuda" if torch.cuda.is_available() else "cpu"

    def ensure_loaded(self, model_path, log_cb=None):
        if self.model is not None and self.model_path == model_path:
            return  # уже загружено именно с этим путём

        if log_cb:
            log_cb(f"Загрузка модели SAM3 из {model_path} (device={self.device})...")

        self.model = Sam3Model.from_pretrained(model_path, local_files_only=True).to(self.device)
        self.processor = Sam3Processor.from_pretrained(model_path, local_files_only=True)
        self.model.eval()
        self.model_path = model_path

        if log_cb:
            log_cb("Модель загружена.")

    def run_segmentation(self, image, threshold, mask_threshold,
                          text_prompt=None, boxes_px=None, box_labels=None):
        if not text_prompt and not boxes_px:
            raise ValueError("Нужно задать text_prompt и/или boxes_px.")

        kwargs = {}
        if text_prompt:
            kwargs["text"] = text_prompt
        if boxes_px:
            kwargs["input_boxes"] = [boxes_px]
            kwargs["input_boxes_labels"] = [box_labels]

        inputs = self.processor(images=image, return_tensors="pt", **kwargs).to(self.device)
        with torch.no_grad():
            outputs = self.model(**inputs)

        results = self.processor.post_process_instance_segmentation(
            outputs,
            threshold=threshold,
            mask_threshold=mask_threshold,
            target_sizes=[(image.height, image.width)],
        )[0]

        masks_np = (
            results["masks"].cpu().numpy()
            if hasattr(results["masks"], "cpu")
            else np.asarray(results["masks"])
        )
        scores = results["scores"]

        extra_props = {}
        if text_prompt:
            extra_props["prompt"] = text_prompt
        if boxes_px:
            extra_props["boxes_px"] = str(boxes_px)
            extra_props["box_labels"] = str(box_labels)

        return masks_np, scores, extra_props


def overlay_masks(image, masks_np, mask_threshold=0.5):
    """Наложение масок на снимок — идентично overlay_masks() из скрипта."""
    import matplotlib

    image = image.convert("RGBA")
    masks_bin = (masks_np > mask_threshold).astype(np.uint8) * 255

    n_masks = masks_bin.shape[0]
    cmap = matplotlib.colormaps.get_cmap("rainbow").resampled(max(n_masks, 1))
    colors = [tuple(int(c * 255) for c in cmap(i)[:3]) for i in range(n_masks)]

    for mask, color in zip(masks_bin, colors):
        mask_image = Image.fromarray(mask)
        overlay = Image.new("RGBA", image.size, color + (0,))
        alpha = mask_image.point(lambda v: int(v * 0.5))
        overlay.putalpha(alpha)
        image = Image.alpha_composite(image, overlay)

    return image


# ------------------------------------------------------------------------- #
# Геометрия на GDAL/OGR — замена rasterio.features.shapes()/shapely
# ------------------------------------------------------------------------- #

def _pixel_to_map(px, py, geotransform):
    """Формула из документации GDAL: пиксель/строка -> координаты карты."""
    gt = geotransform
    x = gt[0] + px * gt[1] + py * gt[2]
    y = gt[3] + px * gt[4] + py * gt[5]
    return x, y


def _transform_geom_pixels_to_map(geom, geotransform):
    """Пересобирает Polygon-геометрию (в пиксельных координатах, как её
    отдаёт gdal.Polygonize) в координаты карты, кольцо за кольцом —
    так, чтобы сохранялись и дырки (holes), если они есть."""
    new_geom = ogr.Geometry(ogr.wkbPolygon)
    for i in range(geom.GetGeometryCount()):
        ring = geom.GetGeometryRef(i)
        new_ring = ogr.Geometry(ogr.wkbLinearRing)
        for j in range(ring.GetPointCount()):
            px, py, _ = ring.GetPoint(j)
            x, y = _pixel_to_map(px, py, geotransform)
            new_ring.AddPoint(x, y)
        new_geom.AddGeometry(new_ring)
    return new_geom


def _sieve_mask(mask_uint8, threshold_px):
    """Убирает мелкие 'дырки' и шумовые вкрапления из бинарной маски ДО
    векторизации — через gdal.SieveFilter. Связные области (что дырки
    внутри объекта, что мелкие ложные вкрапления рядом с ним) мельче
    threshold_px пикселей сливаются с крупнейшим соседом.

    Это не уменьшает количество найденных ОБЪЕКТОВ (дырки — это
    внутренние кольца геометрии одного полигона, не отдельные фичи), а
    уменьшает сложность геометрии каждого объекта — меньше колец/вершин
    в WKT, быстрее дедупликация и рендер в QGIS."""
    if threshold_px <= 0:
        return mask_uint8

    height, width = mask_uint8.shape
    ds = _MEM_DRIVER.Create("", width, height, 1, gdal.GDT_Byte)
    band = ds.GetRasterBand(1)
    band.WriteArray(mask_uint8)

    gdal.SieveFilter(band, None, band, threshold_px, connectedness=4)

    return band.ReadAsArray()


def _mask_to_polygons_pixel_space(mask_uint8):
    """Полигонизирует бинарную маску (0/1) через gdal.Polygonize —
    прямой аналог того, что rasterio.features.shapes() делает под
    капотом (там тоже вызывается GDALPolygonize). Возвращает список
    ogr.Geometry в ПИКСЕЛЬНЫХ координатах маски (без учёта геопривязки —
    её накладываем отдельно в _transform_geom_pixels_to_map)."""
    height, width = mask_uint8.shape

    src_ds = _MEM_DRIVER.Create("", width, height, 1, gdal.GDT_Byte)
    src_band = src_ds.GetRasterBand(1)
    src_band.WriteArray(mask_uint8)

    ogr_ds = _OGR_MEM_DRIVER.CreateDataSource("mask_polygons")
    ogr_layer = ogr_ds.CreateLayer("mask", geom_type=ogr.wkbPolygon)
    ogr_layer.CreateField(ogr.FieldDefn("value", ogr.OFTInteger))

    # mask=None -> полигонизируем все пиксели (включая фон 0), затем ниже
    # оставляем только полигоны со значением 1 (сама маска).
    gdal.Polygonize(src_band, None, ogr_layer, 0, [], callback=None)

    polygons = []
    ogr_layer.ResetReading()
    for feature in ogr_layer:
        if feature.GetField("value") == 1:
            polygons.append(feature.GetGeometryRef().Clone())

    return polygons


def masks_to_features(masks_np, scores, geotransform, mask_threshold=0.5,
                       extra_props=None, sieve_threshold_px=SIEVE_THRESHOLD_PX,
                       id_offset=0):
    """Аналог masks_to_gdf() из скрипта: маски -> список словарей с
    WKT-геометрией в координатах карты (без geopandas/shapely). Перед
    векторизацией каждая маска дополнительно чистится через
    _sieve_mask() — см. её докстринг.

    id_offset сдвигает нумерацию object_id — это НЕ локальный индекс
    внутри одного вызова (иначе при тайлинге, где функция вызывается
    отдельно на каждый тайл, id начинался бы заново с 0 в каждом тайле,
    и в итоговой таблице оказывалась бы куча объектов с одинаковым
    object_id, хотя они никак не связаны). Вызывающий код (см.
    run_segmentation_tiled) должен передавать растущее смещение, чтобы
    id был уникален в рамках всего прогона.

    Если один и тот же обнаруженный экземпляр распался на несколько
    несвязных частей маски (полигонизация вернула больше одного
    полигона на одну i-ю маску) — эти части намеренно получают ОДИН и
    тот же object_id, это не баг: они действительно одна и та же
    находка модели, просто с разорванной геометрией."""
    features = []
    for i, mask in enumerate(masks_np):
        mask_uint8 = (mask > mask_threshold).astype(np.uint8)
        mask_uint8 = _sieve_mask(mask_uint8, sieve_threshold_px)
        polygons_px = _mask_to_polygons_pixel_space(mask_uint8)
        for geom_px in polygons_px:
            geom_map = _transform_geom_pixels_to_map(geom_px, geotransform)
            props = {
                "object_id": id_offset + i,
                "score": float(scores[i]),
                "geometry_wkt": geom_map.ExportToWkt(),
            }
            if extra_props:
                props.update(extra_props)
            features.append(props)
    return features


def generate_tile_windows(img_w, img_h, tile_size=TILE_SIZE, overlap=TILE_OVERLAP):
    step = max(tile_size - overlap, 1)
    windows = []

    y0 = 0
    while True:
        y1 = min(y0 + tile_size, img_h)
        x0 = 0
        while True:
            x1 = min(x0 + tile_size, img_w)
            windows.append((x0, y0, x1, y1))
            if x1 >= img_w:
                break
            x0 += step
        if y1 >= img_h:
            break
        y0 += step

    return windows


def deduplicate_overlapping(features, iou_threshold=DEDUP_IOU_THRESHOLD):
    """То же самое, что в скрипте, но на OGR-геометриях вместо
    geopandas.sindex/shapely. O(n^2) по парам — приемлемо, т.к. типичное
    количество найденных объектов — десятки-сотни, не миллионы. Перед
    дорогими Intersects/Intersection/Union — дешёвая проверка по
    bounding box: большинство пар вообще не пересекаются, и это можно
    понять на порядок быстрее, чем гонять полную геометрию через GEOS."""
    if len(features) <= 1:
        return features

    features = sorted(features, key=lambda f: f["score"], reverse=True)
    geoms = [ogr.CreateGeometryFromWkt(f["geometry_wkt"]) for f in features]
    envelopes = [g.GetEnvelope() for g in geoms]  # (minX, maxX, minY, maxY)

    discarded = set()
    keep_idx = []
    for i in range(len(geoms)):
        if i in discarded:
            continue
        keep_idx.append(i)
        ei = envelopes[i]
        for j in range(i + 1, len(geoms)):
            if j in discarded:
                continue
            ej = envelopes[j]
            if ei[1] < ej[0] or ej[1] < ei[0] or ei[3] < ej[2] or ej[3] < ei[2]:
                continue  # bounding box'ы не пересекаются -> геометрии точно не пересекаются
            if not geoms[i].Intersects(geoms[j]):
                continue
            inter_area = geoms[i].Intersection(geoms[j]).GetArea()
            union_area = geoms[i].Union(geoms[j]).GetArea()
            iou = inter_area / union_area if union_area > 0 else 0
            if iou > iou_threshold:
                discarded.add(j)

    return [features[i] for i in keep_idx]


def filter_by_min_area(features, min_area):
    """Отбрасывает полигоны с площадью меньше min_area. Площадь считается
    в квадратных единицах СК растра — метры² для проекций (типично для
    большинства рабочих CRS), градусы² для географических CRS (для них
    число становится малоинтуитивным — если снимок в EPSG:4326, лучше
    сначала прикинуть площадь объекта в тех же единицах, что покажет
    сам QGIS в свойствах слоя после первого прогона без фильтра)."""
    if min_area <= 0:
        return features
    kept = []
    for f in features:
        geom = ogr.CreateGeometryFromWkt(f["geometry_wkt"])
        if geom.GetArea() >= min_area:
            kept.append(f)
    return kept


def write_gpkg(features, crs_wkt, output_path, layer_name="sam3_results"):
    """Пишет фичи напрямую в GeoPackage на диске через OGR — без
    промежуточного memory-слоя QGIS. Если файл уже существует —
    перезаписывает его целиком (аналог 'Overwrite' при экспорте)."""
    driver = ogr.GetDriverByName("GPKG")

    if os.path.exists(output_path):
        driver.DeleteDataSource(output_path)

    ds = driver.CreateDataSource(output_path)

    srs = None
    if crs_wkt:
        srs = osr.SpatialReference()
        srs.ImportFromWkt(crs_wkt)

    layer = ds.CreateLayer(layer_name, srs=srs, geom_type=ogr.wkbPolygon)
    layer.CreateField(ogr.FieldDefn("object_id", ogr.OFTInteger))
    layer.CreateField(ogr.FieldDefn("score", ogr.OFTReal))
    prompt_field = ogr.FieldDefn("prompt", ogr.OFTString)
    prompt_field.SetWidth(254)
    layer.CreateField(prompt_field)

    layer_defn = layer.GetLayerDefn()
    for f in features:
        feat = ogr.Feature(layer_defn)
        feat.SetGeometry(ogr.CreateGeometryFromWkt(f["geometry_wkt"]))
        feat.SetField("object_id", f.get("object_id", 0))
        feat.SetField("score", f.get("score", 0.0))
        feat.SetField("prompt", f.get("prompt", "") or "")
        layer.CreateFeature(feat)
        feat = None

    ds = None  # закрыть датасет -> сбросить на диск


def run_segmentation_tiled(
    engine, image, geotransform, threshold, mask_threshold,
    text_prompt=None, boxes_px=None, box_labels=None,
    tile_size=TILE_SIZE, overlap=TILE_OVERLAP,
    sieve_threshold_px=SIEVE_THRESHOLD_PX,
    log_cb=None, progress_cb=None,
):
    windows = generate_tile_windows(image.width, image.height, tile_size, overlap)
    if log_cb:
        log_cb(f"Снимок разбит на {len(windows)} тайлов ({tile_size}px, перекрытие {overlap}px).")

    full_overlay = image.convert("RGBA").copy()
    all_features = []
    next_object_id = 0  # сквозной счётчик через ВСЕ тайлы — см. докстринг masks_to_features

    for i, (x0, y0, x1, y1) in enumerate(windows, start=1):
        if progress_cb:
            progress_cb(i, len(windows))

        tile_img = image.crop((x0, y0, x1, y1))

        tile_boxes, tile_labels = None, None
        if boxes_px:
            tile_boxes, tile_labels = [], []
            for box, label in zip(boxes_px, box_labels):
                bx0, by0, bx1, by1 = box
                ix0, iy0 = max(bx0, x0), max(by0, y0)
                ix1, iy1 = min(bx1, x1), min(by1, y1)
                if ix1 > ix0 and iy1 > iy0:
                    tile_boxes.append([ix0 - x0, iy0 - y0, ix1 - x0, iy1 - y0])
                    tile_labels.append(label)

        if not text_prompt and not tile_boxes:
            continue

        masks_np, scores, extra_props = engine.run_segmentation(
            tile_img, threshold, mask_threshold,
            text_prompt=text_prompt,
            boxes_px=tile_boxes if tile_boxes else None,
            box_labels=tile_labels if tile_boxes else None,
        )

        if len(masks_np) == 0:
            continue

        # Локальный geotransform тайла = origin сдвигается в пиксель
        # (x0, y0) полного снимка, шаг/поворот пикселя не меняются.
        tile_origin_x, tile_origin_y = _pixel_to_map(x0, y0, geotransform)
        tile_gt = (
            tile_origin_x, geotransform[1], geotransform[2],
            tile_origin_y, geotransform[4], geotransform[5],
        )
        all_features.extend(
            masks_to_features(
                masks_np, scores, tile_gt, mask_threshold, extra_props,
                sieve_threshold_px=sieve_threshold_px,
                id_offset=next_object_id,
            )
        )
        next_object_id += len(masks_np)

        tile_overlay = overlay_masks(tile_img, masks_np, mask_threshold)
        full_overlay.paste(tile_overlay, (x0, y0), tile_overlay)

    # Примечание: дедупликация по IoU здесь НЕ выполняется — она вынесена
    # в run_pipeline() и применяется одинаково что к тайловому, что к
    # обычному пути. Дубли одного и того же объекта могут возникать не
    # только на стыках тайлов (для чего изначально писалась эта функция),
    # но и внутри одного прохода модели — поэтому дедуп должен быть
    # безусловным, а не только тайловым шагом.
    return all_features, full_overlay


def run_pipeline(
    engine, raster_path, threshold, mask_threshold,
    text_prompt=None, boxes_px=None, box_labels=None,
    tiling_threshold=TILING_THRESHOLD, tile_size=TILE_SIZE, tile_overlap=TILE_OVERLAP,
    dedup_iou_threshold=DEDUP_IOU_THRESHOLD,
    sieve_threshold_px=SIEVE_THRESHOLD_PX,
    min_area=0.0,
    output_gpkg_path=None,
    log_cb=None, progress_cb=None,
):
    """Точка входа, которую дёргает Sam3SegmentationTask (см. sam3_task.py).
    Открывает растр через GDAL (по тому же пути, что использует сам QGIS
    для этого слоя), решает — тайлить или нет (по tiling_threshold),
    гоняет модель, фильтрует по минимальной площади, при необходимости
    сразу пишет результат в GeoPackage на диске, и возвращает список фич
    (WKT + атрибуты), PNG-оверлей и WKT исходной CRS."""
    ds = gdal.Open(raster_path, gdal.GA_ReadOnly)
    if ds is None:
        raise RuntimeError(f"Не удалось открыть растр через GDAL: {raster_path}")

    geotransform = ds.GetGeoTransform()
    crs_wkt = ds.GetProjection() or None

    arr = ds.ReadAsArray()
    ds = None  # закрываем датасет сразу после чтения

    if arr.ndim == 2:
        # одноканальный снимок — дублируем в псевдо-RGB
        arr = np.stack([arr] * 3, axis=0)

    img_rgb = np.transpose(arr[:3], (1, 2, 0)).astype(np.uint8)
    image = Image.fromarray(img_rgb)

    needs_tiling = max(image.width, image.height) > tiling_threshold

    if needs_tiling:
        if log_cb:
            log_cb(
                f"Снимок {image.width}x{image.height}px больше порога "
                f"{tiling_threshold}px — включаю тайлинг (тайл {tile_size}px, "
                f"перекрытие {tile_overlap}px)."
            )
        features, overlay_image = run_segmentation_tiled(
            engine, image, geotransform, threshold, mask_threshold,
            text_prompt=text_prompt, boxes_px=boxes_px, box_labels=box_labels,
            tile_size=tile_size, overlap=tile_overlap,
            sieve_threshold_px=sieve_threshold_px,
            log_cb=log_cb, progress_cb=progress_cb,
        )
    else:
        masks_np, scores, extra_props = engine.run_segmentation(
            image, threshold, mask_threshold,
            text_prompt=text_prompt, boxes_px=boxes_px, box_labels=box_labels,
        )
        features = masks_to_features(
            masks_np, scores, geotransform, mask_threshold, extra_props,
            sieve_threshold_px=sieve_threshold_px,
        )
        overlay_image = overlay_masks(image, masks_np, mask_threshold)

    # Дедупликация по IoU — безусловно, для ОБОИХ путей. Дубли одного и
    # того же объекта (несколько раз с чуть разным контуром) возникают не
    # только на стыках тайлов, но и внутри одного прохода модели, поэтому
    # раньше (когда дедуп жил только внутри run_segmentation_tiled) такие
    # дубли на нетайловых снимках не отлавливались вообще.
    before = len(features)
    features = deduplicate_overlapping(features, iou_threshold=dedup_iou_threshold)
    if log_cb:
        log_cb(f"Объектов до дедупликации: {before}, после: {len(features)} (IoU порог: {dedup_iou_threshold})")

    if min_area > 0:
        before = len(features)
        features = filter_by_min_area(features, min_area)
        if log_cb:
            log_cb(f"Фильтр по минимальной площади (>= {min_area}): {before} -> {len(features)}")

    if output_gpkg_path and features:
        write_gpkg(features, crs_wkt, output_gpkg_path)
        if log_cb:
            log_cb(f"Результат записан напрямую в {output_gpkg_path}")

    return features, overlay_image, crs_wkt
