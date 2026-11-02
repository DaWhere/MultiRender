"""MultiRender – parallel rendering of QGIS print layouts."""


def classFactory(iface):  # noqa: N802 (name required by QGIS)
    from .plugin import MultiRenderPlugin

    return MultiRenderPlugin(iface)
