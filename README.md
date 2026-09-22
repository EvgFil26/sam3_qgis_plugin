# SAM3 QGIS Plugin — установка

Плагин запускает сегментацию SAM3 (`facebook/sam3`) прямо в QGIS: текстовый
промпт или клик/бокс на карте → векторный слой с полигонами в текущем
проекте. Инференс работает **на GPU (CUDA), если она есть, и автоматически
переключается на CPU, если её нет** — код плагина сам делает
`device = "cuda" if torch.cuda.is_available() else "cpu"`, но чтобы это
сработало, `torch` нужно поставить **правильной сборкой** (CPU-only или
CUDA) в интерпретатор, который использует сам QGIS — не в системный Python.

---

## 0. Требования

- QGIS 3.40+ (инструкции ниже — под Windows, путь к `python.exe` внутри
  QGIS может отличаться в зависимости от версии — поправь под свою).
- Если хочешь ускорение на GPU — видеокарта NVIDIA с установленным
  актуальным драйвером (сама CUDA toolkit ставить отдельно не нужно —
  `torch` тащит нужные CUDA-библиотеки внутри себя).

---

## 1. Скопируй папку плагина

Помести всю папку плагина (`sam3_plugin.py`, `sam3_dockwidget.py`,
`sam3_maptool.py`, `sam3_engine.py`, `sam3_task.py` и т.д.) в:

```
C:\Users\<Имя пользователя>\AppData\Roaming\QGIS\QGIS3\profiles\default\python\plugins\sam3_plugin
```

---

## 2. Установи зависимости в Python самого QGIS

QGIS использует **собственный** встроенный Python, отдельный от системного —
поэтому `pip install` в обычной командной строке ничего не даст: пакеты
нужно ставить строго через `python.exe`, лежащий внутри папки установки QGIS.

Найди свой путь — обычно похож на:
```
C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe
```
(версия `3.40.10` и `Python312` могут отличаться — посмотри, что реально
стоит на диске).

Дальше два варианта — **автоматический** (сам определит, есть ли GPU) и
**ручной** (если хочешь контролировать каждый шаг).

### Вариант А — автоматический скрипт (рекомендуется)

Сохрани как `install_deps.bat` рядом с папкой плагина, поправь
`QGIS_PYTHON` под свой путь и запусти двойным кликом (или через `cmd`):

```bat
@echo off
setlocal

REM ==== ПОПРАВЬ ПОД СВОЮ ВЕРСИЮ QGIS ====
set QGIS_PYTHON="C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe"

echo === Проверка наличия NVIDIA GPU ===
where nvidia-smi >nul 2>nul
if %ERRORLEVEL%==0 (
    echo GPU обнаружена — ставим CUDA-сборку PyTorch.
    set HAS_CUDA=1
) else (
    echo GPU не обнаружена (или нет драйвера NVIDIA) — ставим CPU-сборку.
    set HAS_CUDA=0
)

echo === Совместимые версии numpy/scipy/pydantic-core (проверено для этой связки пакетов) ===
%QGIS_PYTHON% -m pip install "numpy<2.3" "scipy==1.13.0" "pydantic-core==2.33.2"

if %HAS_CUDA%==1 (
    echo === Установка PyTorch (CUDA) ===
    REM Актуальный тег CUDA-сборки может меняться между релизами PyTorch.
    REM Если эта команда не найдёт пакет — зайди на https://pytorch.org/get-started/locally/,
    REM выбери свою версию CUDA и подставь index-url оттуда.
    %QGIS_PYTHON% -m pip install --force-reinstall torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
) else (
    echo === Установка PyTorch (CPU-only) ===
    %QGIS_PYTHON% -m pip install --force-reinstall torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
)

echo === Установка transformers и геопакетов ===
%QGIS_PYTHON% -m pip install transformers rasterio geopandas shapely

echo === Проверка ===
%QGIS_PYTHON% -c "import numpy, scipy, torch, transformers, rasterio, geopandas; print('numpy:', numpy.__version__); print('scipy:', scipy.__version__); print('torch:', torch.__version__); print('CUDA available:', torch.cuda.is_available()); print('GPU count:', torch.cuda.device_count())"

echo.
echo Готово. Перезапусти QGIS и включи плагин.
pause
```

Если у тебя есть GPU, но скрипт всё равно поставил CPU-сборку (или
наоборот) — значит `nvidia-smi` не нашёлся в `PATH`; можно вручную
переставить `HAS_CUDA` на `1` в скрипте и перезапустить.

### Вариант Б — вручную, по шагам

**Без GPU (CPU-only):**
```
"C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe" -m pip install "numpy<2.3" "scipy==1.13.0" "pydantic-core==2.33.2"
"C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe" -m pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu
"C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe" -m pip install transformers rasterio geopandas shapely
```

**С GPU (CUDA):** сначала проверь версию драйвера/CUDA командой
`nvidia-smi` в `cmd`, зайди на
[pytorch.org/get-started/locally](https://pytorch.org/get-started/locally/),
выбери Windows → Pip → Python → свою версию CUDA (например 12.1 / 12.4 /
12.6 / 12.8 — актуальный список смотри на сайте, он меняется от релиза к
релизу PyTorch) и используй `index-url` со страницы вместо примера ниже:
```
"C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe" -m pip install "numpy<2.3" "scipy==1.13.0" "pydantic-core==2.33.2"
"C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe" -m pip install --force-reinstall torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
"C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe" -m pip install transformers rasterio geopandas shapely
```

> Не пытайся угадать точный номер сборки (`+cu121`, `+cu128` и т.п.)
> заранее — версии `torch`/`torchvision`/`torchaudio` должны совпадать
> между собой и с тегом CUDA, а актуальный набор меняется от релиза к
> релизу. Правильный источник истины — селектор на pytorch.org, а не
> запомненная команда из старого гайда.

---

## 3. Проверка перед запуском QGIS

```
"C:\Program Files\QGIS 3.40.10\apps\Python312\python.exe" -c "import torch, transformers; print('torch', torch.__version__); print('transformers', transformers.__version__); print('CUDA available:', torch.cuda.is_available())"
```

Ожидаемо:
- на машине **без** GPU — `CUDA available: False`, это нормально: плагин
  сам переключится на CPU-инференс (просто медленнее);
- на машине **с** GPU и правильно поставленным CUDA-сборкой торча —
  `CUDA available: True`.

Если строка про CUDA не совпадает с ожиданием (например, есть GPU, но
`False`) — почти всегда причина в том, что `torch` был поставлен
CPU-сборкой поверх системы с GPU, либо версия CUDA-тега не совпала с
драйвером. Переустанови `torch` через `--force-reinstall` с правильным
`index-url` (шаг 2, вариант Б).

---

## 4. Модель SAM3

Плагин ожидает локально скачанные веса `facebook/sam3` (используется
`local_files_only=True`, интернет во время работы плагина не нужен).
Путь к папке с моделью задаётся в настройках плагина внутри QGIS.

---

## 5. Включение плагина

1. Перезапусти QGIS.
2. `Плагины → Управление и установка плагинов → Установленные` — найди
   плагин в списке, поставь галочку.
3. Если плагин не появился в списке — проверь, что папка лежит именно в
   `...\profiles\default\python\plugins\` (а не на уровень выше/ниже), и
   что в консоли Python QGIS (`Плагины → Консоль Python`) команда
   `import torch` отрабатывает без ошибок.

---

## Известные проблемы

- **`You are using a model of type sam3_video to instantiate sam3_tracker`**
  — предупреждение от `transformers` при загрузке чекпоинта трекера, само
  по себе не ошибка; если после него всё грузится и инференс отрабатывает
  — можно игнорировать.
- **GeoPackage не открывается в ArcGIS Pro** — известная проблема
  совместимости; при необходимости экспортируй результат в Shapefile.
- **Конфликты версий `numpy`/`scipy`/`pydantic-core`** — если после
  установки `transformers` что-то из этого само обновилось и всё
  перестало работать, переустанови зафиксированные версии из шага 2
  (`numpy<2.3`, `scipy==1.13.0`, `pydantic-core==2.33.2`) поверх уже
  стоящих пакетов.
