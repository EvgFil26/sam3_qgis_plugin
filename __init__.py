def classFactory(iface):
    from .sam3_plugin import Sam3Plugin
    return Sam3Plugin(iface)
