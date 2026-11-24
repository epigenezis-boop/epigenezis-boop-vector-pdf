THEMES = {
    'dark': ('#151522', '#25293b', '#303950', '#515773', '#fff0d2', '#b8c6cc', '#434c64', '#171825'),
    'light': ('#edf1f6', '#f8fbff', '#ffffff', '#c6d0df', '#17283e', '#536881', '#dceaff', '#dbe2ec'),
    'solar': ('#ffead0', '#ffefda', '#fff9ee', '#c9a486', '#442a36', '#705253', '#f9d6ad', '#edcfae'),
}
STRIPES = ('#ec3e46', '#ff702d', '#ffad32', '#ffe8b4')


def stylesheet(theme):
    base, panel, raised, border, text, muted, select, _ = THEMES[theme]
    return f"""
    QMainWindow, QDialog, QTabWidget::pane {{ background: {base}; color: {text}; }}
    QWidget {{ color: {text}; font-size: 13px; }}
    QLabel#brand {{ font-size: 19px; letter-spacing: 0.5px; }}
    QLabel#subhead {{ color: {muted}; font-size: 12px; }}
    QLabel#emptyTitle {{ font-size: 25px; font-weight: 700; }}
    QLabel#panelHeading {{ font-size: 15px; font-weight: 700; }}
    QLabel#panelNote {{ color: {muted}; }}
    QWidget#side, QWidget#toolbar, QWidget#status, QDockWidget,
    QMenuBar, QMenu {{ background: {panel}; }}
    QWidget#toolbar, QWidget#status {{ border-bottom: 1px solid {border}; }}
    QWidget#side {{ border: 1px solid {border}; }}
    QPushButton, QToolButton, QComboBox, QDoubleSpinBox, QLineEdit {{
        background: {raised}; border: 1px solid {border}; border-radius: 6px; padding: 6px 9px;
    }}
    QPushButton:hover, QToolButton:hover, QMenu::item:selected {{ background: {select}; }}
    QPushButton#primary, QToolButton#primary {{ background: #ffe8b4; border-color: #ffad32; color: #34263f; }}
    QPushButton#primary:hover, QToolButton#primary:hover {{ background: #fff0c8; }}
    QPushButton:disabled, QToolButton:disabled {{ color: {muted}; background: {panel}; }}
    QToolButton:checked, QPushButton:checked {{ background: {select}; border-color: #ff8e32; }}
    QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus {{ border-color: #ff8e32; }}
    QTabBar::tab {{
        background: {panel}; color: {muted}; padding: 8px 12px; border-bottom: 2px solid {border};
    }}
    QTabBar::tab:selected {{ background: {raised}; color: {text}; border-bottom-color: #ff8e32; }}
    QSplitter::handle {{ background-color: {border}; }}
    QListWidget {{ background: {base}; border: 1px solid {border}; border-radius: 6px; outline: 0; }}
    QListWidget::item {{ padding: 5px; border-bottom: 1px solid {border}; }}
    QListWidget::item:selected {{ background: {select}; color: {text}; }}
    QScrollBar:vertical {{ background: {panel}; width: 9px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {border}; min-height: 28px; border-radius: 4px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    """
