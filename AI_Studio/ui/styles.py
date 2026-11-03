"""界面样式：一套结构 + 两套配色（深色 / 浅色）。

用 ``string.Template`` 的 ``$变量`` 占位，CSS 里没有 ``$``，因此不必像
``str.format`` 那样转义满屏的花括号。新增配色只要在 PALETTES 里补一组即可。
"""

from string import Template

THEME_DARK = "dark"
THEME_LIGHT = "light"

# 切换按钮上显示的是"目标模式"，所以深色时提示可切到白天。
THEME_LABELS = {THEME_DARK: "白天", THEME_LIGHT: "黑夜"}

PALETTES = {
    THEME_DARK: {
        "bg": "#111318",
        "sidebar": "#191C23",
        "border": "#2B2F39",
        "border_soft": "#292D36",
        "text": "#E8EAF0",
        "text_strong": "#F5F6FA",
        "text_dim": "#858C9B",
        "section": "#9299A8",
        "role": "#9DA8BC",
        "muted": "#858C9B",
        "bubble_user": "#252B36",
        "bubble_ai": "#191D25",
        "bubble_border": "#232833",
        "msg_text": "#E7EAF0",
        "btn_bg": "#292E39",
        "btn_hover": "#353B49",
        "btn_text": "#E8EAF0",
        "primary_bg": "#303746",
        "primary_hover": "#3B4558",
        "send_bg": "#D9E4FF",
        "send_hover": "#C5D6FF",
        "send_text": "#172033",
        "stop_bg": "#C7503F",
        "stop_hover": "#DA6250",
        "stop_disabled_bg": "#5A3B39",
        "stop_disabled_text": "#C9B7B4",
        "input_bg": "#1B2029",
        "input_border": "#343B49",
        "input_text": "#F0F2F6",
        "input_focus": "#647DAF",
        "selection": "#526B9D",
        "list_text": "#C9CED8",
        "list_hover": "#252A34",
        "list_sel": "#303746",
        "list_sel_text": "#FFFFFF",
        "scroll": "#3A414E",
        "tag_text": "#7C8798",
        "tag_border": "#333A48",
        "tab_bg": "#191C23",
        "tab_sel": "#262C38",
        "tab_text": "#9DA8BC",
        "dialog_bg": "#111318",
        "combo_bg": "#1B2029",
        "combo_view_sel": "#303746",
        "combo_view_sel_text": "#FFFFFF",
        "check_text": "#C9CED8",
        "page_title": "#F2F4F8",
        "disabled_text": "#5D6472",
    },
    THEME_LIGHT: {
        "bg": "#F7F8FA",
        "sidebar": "#FFFFFF",
        "border": "#E4E7EC",
        "border_soft": "#E9ECF1",
        "text": "#1F2329",
        "text_strong": "#101418",
        "text_dim": "#6B7280",
        "section": "#6B7280",
        "role": "#6B7280",
        "muted": "#6B7280",
        "bubble_user": "#E8EFFC",
        "bubble_ai": "#FFFFFF",
        "bubble_border": "#E3E8F0",
        "msg_text": "#24292F",
        "btn_bg": "#F1F3F6",
        "btn_hover": "#E4E8EF",
        "btn_text": "#1F2329",
        "primary_bg": "#EDF1F7",
        "primary_hover": "#E0E6F0",
        "send_bg": "#2F6FEB",
        "send_hover": "#1F5FE0",
        "send_text": "#FFFFFF",
        "stop_bg": "#D1453B",
        "stop_hover": "#E0554A",
        "stop_disabled_bg": "#F2D9D6",
        "stop_disabled_text": "#B08A86",
        "input_bg": "#FFFFFF",
        "input_border": "#D8DDE6",
        "input_text": "#1F2329",
        "input_focus": "#2F6FEB",
        "selection": "#BBD1FF",
        "list_text": "#3A4250",
        "list_hover": "#F2F4F8",
        "list_sel": "#E8EFFC",
        "list_sel_text": "#12233F",
        "scroll": "#C7CEDA",
        "tag_text": "#8A93A3",
        "tag_border": "#E1E5EC",
        "tab_bg": "#F1F3F6",
        "tab_sel": "#FFFFFF",
        "tab_text": "#6B7280",
        "dialog_bg": "#FFFFFF",
        "combo_bg": "#FFFFFF",
        "combo_view_sel": "#E8EFFC",
        "combo_view_sel_text": "#12233F",
        "check_text": "#3A4250",
        "page_title": "#101418",
        "disabled_text": "#9AA1AC",
    },
}

STYLE_TEMPLATE = Template("""
QMainWindow, QWidget {
    background: $bg;
    color: $text;
    font-family: "Microsoft YaHei UI", "Segoe UI";
    font-size: 14px;
}
/* 文字控件一律透明：底色由气泡/面板自己负责，否则会在气泡里再盖一层色块。 */
QLabel { background: transparent; }
#sidebar {
    background: $sidebar;
    border-right: 1px solid $border;
}
#brand {
    color: $text_strong;
    font-size: 22px;
    font-weight: 700;
    padding: 4px 2px 12px 2px;
}
#sectionLabel {
    color: $section;
    font-size: 12px;
    font-weight: 600;
    padding: 8px 2px 0 2px;
}
#muted {
    color: $muted;
    font-size: 12px;
}
QPushButton {
    border: none;
    border-radius: 9px;
    padding: 10px 14px;
    color: $btn_text;
    background: $btn_bg;
}
QPushButton:hover { background: $btn_hover; }
#newChatButton {
    background: $primary_bg;
    text-align: left;
    padding: 12px 14px;
    font-weight: 600;
}
#newChatButton:hover { background: $primary_hover; }
#sendButton {
    background: $send_bg;
    color: $send_text;
    font-weight: 700;
    padding: 11px 18px;
}
#sendButton:hover { background: $send_hover; }
#secondaryButton {
    background: $primary_bg;
    padding: 8px 16px;
}
QListWidget {
    background: transparent;
    border: none;
    outline: none;
}
QListWidget::item {
    color: $list_text;
    padding: 11px 10px;
    border-radius: 8px;
    margin: 2px 0;
}
QListWidget::item:hover { background: $list_hover; }
QListWidget::item:selected {
    background: $list_sel;
    color: $list_sel_text;
}
#topbar {
    background: $bg;
    border-bottom: 1px solid $border_soft;
}
#pageTitle {
    color: $page_title;
    font-size: 16px;
    font-weight: 650;
}
#chatPanel { background: $bg; }
#chatScroll { background: transparent; }
#userBubble, #assistantBubble {
    border-radius: 12px;
    border: 1px solid $bubble_border;
}
#userBubble { background: $bubble_user; }
#assistantBubble { background: $bubble_ai; }
#messageRole {
    color: $role;
    font-size: 11px;
    font-weight: 700;
}
#messageText {
    color: $msg_text;
    font-size: 14px;
}
#inputPanel {
    background: $bg;
    border-top: 1px solid $border_soft;
}
#messageInput {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 12px;
    padding: 12px;
    color: $input_text;
    selection-background-color: $selection;
}
#messageInput:focus { border: 1px solid $input_focus; }
QScrollBar:vertical {
    background: transparent;
    width: 9px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: $scroll;
    border-radius: 4px;
    min-height: 24px;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QLineEdit {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 7px;
    padding: 8px;
    color: $input_text;
    selection-background-color: $selection;
}
QLineEdit:focus { border: 1px solid $input_focus; }

/* ---------- 模型选择 ---------- */
#modelCombo {
    background: $combo_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 6px 10px;
    color: $text;
}
#modelCombo:hover { border: 1px solid $input_focus; }
#sizeCombo {
    background: $combo_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 6px 2px 6px 9px;
    font-size: 12px;
    color: $text;
}
#sizeCombo:hover { border: 1px solid $input_focus; }
QComboBox QAbstractItemView {
    background: $combo_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 4px;
    color: $text;
    outline: none;
    selection-background-color: $combo_view_sel;
    selection-color: $combo_view_sel_text;
}
QComboBox QAbstractItemView::item {
    padding: 6px 8px;
    border-radius: 5px;
    min-height: 18px;
}
#modelCombo QAbstractItemView {
    background: $combo_bg;
    border: 1px solid $input_border;
    border-radius: 8px;
    padding: 4px;
    color: $text;
    outline: none;
    selection-background-color: $combo_view_sel;
    selection-color: $combo_view_sel_text;
}
#modelCombo QAbstractItemView::item {
    padding: 6px 8px;
    border-radius: 5px;
    min-height: 18px;
}

/* ---------- 停止按钮 ---------- */
#stopButton {
    background: $stop_bg;
    color: #FFFFFF;
    font-weight: 700;
    padding: 11px 18px;
}
#stopButton:hover { background: $stop_hover; }
#stopButton:disabled {
    background: $stop_disabled_bg;
    color: $stop_disabled_text;
}

/* ---------- 气泡上的模型标签 ---------- */
#modelTag {
    color: $tag_text;
    font-size: 11px;
    padding: 1px 6px;
    border: 1px solid $tag_border;
    border-radius: 5px;
}

/* ---------- 输入区附件缩略图 ---------- */
#attachmentChip {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 7px;
    padding: 2px;
}

/* ---------- 设置窗口 ---------- */
QDialog { background: $dialog_bg; }
QTabWidget::pane {
    border: 1px solid $border;
    border-radius: 8px;
    top: -1px;
    background: $dialog_bg;
}
QTabBar::tab {
    background: $tab_bg;
    color: $tab_text;
    padding: 8px 18px;
    border: 1px solid $border;
    border-bottom: none;
    border-top-left-radius: 8px;
    border-top-right-radius: 8px;
    margin-right: 2px;
}
QTabBar::tab:selected { background: $tab_sel; color: $text_strong; }
QTabBar::tab:hover { color: $text; }
QSpinBox, QComboBox {
    background: $input_bg;
    border: 1px solid $input_border;
    border-radius: 7px;
    padding: 6px 8px;
    color: $input_text;
    selection-background-color: $selection;
}
QSpinBox:focus, QComboBox:focus { border: 1px solid $input_focus; }
QCheckBox { color: $check_text; spacing: 6px; }
QDialogButtonBox QPushButton {
    background: $btn_bg;
    color: $btn_text;
    min-width: 76px;
    padding: 9px 16px;
}
QDialogButtonBox QPushButton:hover { background: $btn_hover; }
QToolTip {
    background: $input_bg;
    color: $input_text;
    border: 1px solid $input_border;
    padding: 4px 8px;
}
""")


def build_stylesheet(theme: str = THEME_DARK) -> str:
    palette = PALETTES.get(theme) or PALETTES[THEME_DARK]
    return STYLE_TEMPLATE.substitute(**palette)


def build_palette(theme: str = THEME_DARK):
    """给 Qt 自绘的部分（下拉箭头、滚动条、提示框）配同一套颜色。

    不设 palette 的话，浅色主题下这些原生部件仍会按默认深色调绘制，
    会出现下拉框右侧那道突兀的黑边。
    """
    from PySide6.QtGui import QColor, QPalette

    colors = PALETTES.get(theme) or PALETTES[THEME_DARK]
    qp = QPalette()
    qp.setColor(QPalette.ColorRole.Window, QColor(colors["bg"]))
    qp.setColor(QPalette.ColorRole.WindowText, QColor(colors["text"]))
    qp.setColor(QPalette.ColorRole.Base, QColor(colors["input_bg"]))
    qp.setColor(QPalette.ColorRole.AlternateBase, QColor(colors["sidebar"]))
    qp.setColor(QPalette.ColorRole.Text, QColor(colors["input_text"]))
    qp.setColor(QPalette.ColorRole.Button, QColor(colors["btn_bg"]))
    qp.setColor(QPalette.ColorRole.ButtonText, QColor(colors["btn_text"]))
    qp.setColor(QPalette.ColorRole.Highlight, QColor(colors["selection"]))
    qp.setColor(QPalette.ColorRole.HighlightedText, QColor(colors["text_strong"]))
    qp.setColor(QPalette.ColorRole.ToolTipBase, QColor(colors["input_bg"]))
    qp.setColor(QPalette.ColorRole.ToolTipText, QColor(colors["input_text"]))
    qp.setColor(QPalette.ColorRole.PlaceholderText, QColor(colors["text_dim"]))
    qp.setColor(QPalette.ColorRole.Link, QColor(colors["send_bg"]))
    qp.setColor(QPalette.ColorRole.BrightText, QColor("#FFFFFF"))
    for role in (
        QPalette.ColorRole.WindowText,
        QPalette.ColorRole.Text,
        QPalette.ColorRole.ButtonText,
    ):
        qp.setColor(QPalette.ColorGroup.Disabled, role, QColor(colors["disabled_text"]))
    return qp


def normalize_theme(theme: str | None) -> str:
    return THEME_LIGHT if theme == THEME_LIGHT else THEME_DARK


# 兼容旧引用（默认深色）。
APP_STYLE = build_stylesheet(THEME_DARK)
