from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import Qt

from .sam3_dockwidget import Sam3DockWidget
from .sam3_engine import Sam3Engine


class Sam3Plugin:
    """Главный класс плагина. QGIS создаёт один экземпляр при старте
    (через classFactory в __init__.py) и вызывает:
      - initGui()  — когда плагин включается (один раз при старте QGIS
                     или при ручном включении в Plugin Manager);
      - unload()   — когда плагин выключается или QGIS закрывается.
    """

    def __init__(self, iface):
        # iface — это "пульт управления" QGIS: доступ к холсту (canvas),
        # слоям проекта, главному окну и т.д. Сохраняем его — он
        # понадобится почти везде дальше.
        self.iface = iface
        self.action = None
        self.dock_widget = None  # создаётся лениво, при первом клике

        # Engine живёт на уровне плагина (не dock widget'а), чтобы модель
        # не перезагружалась заново при каждом закрытии/открытии панели —
        # только при первом запуске сегментации или смене пути к модели.
        self.engine = Sam3Engine()

    def initGui(self):
        # Шаг 1: используем встроенную иконку Qt/QGIS, чтобы не
        # возиться с resources.qrc — свою иконку добавим позже.
        icon = QIcon(":/images/themes/default/mActionAddLayer.svg")
        self.action = QAction(icon, "SAM3 Segmentation", self.iface.mainWindow())
        self.action.triggered.connect(self.run)

        # Добавляем кнопку и на панель инструментов, и в меню "Плагины"
        self.iface.addToolBarIcon(self.action)
        self.iface.addPluginToMenu("&SAM3 Segmentation", self.action)

    def unload(self):
        self.iface.removeToolBarIcon(self.action)
        self.iface.removePluginMenu("&SAM3 Segmentation", self.action)

        if self.dock_widget is not None:
            self.iface.removeDockWidget(self.dock_widget)
            self.dock_widget.deleteLater()
            self.dock_widget = None

    def run(self):
        # Шаг 4: клик по кнопке открывает (или скрывает, если уже
        # открыта) панель с параметрами сегментации, запуском в фоне
        # через QgsTask и добавлением результата в проект.
        if self.dock_widget is None:
            self.dock_widget = Sam3DockWidget(self.iface.mapCanvas(), self.engine, self.iface.mainWindow())
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dock_widget)
        else:
            self.dock_widget.setVisible(not self.dock_widget.isVisible())
