from nicegui import ui
from ui.layout import main_layout
from .plugins_ui import render_plugins_page


def register_plugin_routes():  # name must match the caller exactly
    # Installing plugins runs arbitrary code in-process — gate on the manage
    # permission so ordinary authenticated users cannot reach plugin install.
    @ui.page('/plugins')
    @main_layout('Plugins', permission='feature:plugins.manage')
    def plugins_page():
        render_plugins_page()