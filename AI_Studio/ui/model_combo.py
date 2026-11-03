"""模型下拉框填充（主窗口与设置页共用一份逻辑）。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QComboBox

from core.models import DEFAULT_MODEL, grouped_models, is_known

UNAVAILABLE_COLOR = QColor("#6C7280")


def fill_model_combo(combo: QComboBox, current_id: str | None = None) -> None:
    combo.blockSignals(True)
    combo.clear()
    for index, (_vendor, items) in enumerate(grouped_models()):
        if index:
            combo.insertSeparator(combo.count())
        for model in items:
            combo.addItem(model.display, model.id)
            if not model.available:
                combo.setItemData(
                    combo.count() - 1, UNAVAILABLE_COLOR, Qt.ItemDataRole.ForegroundRole
                )
    target = current_id if is_known(current_id) else DEFAULT_MODEL
    position = combo.findData(target)
    combo.setCurrentIndex(position if position >= 0 else 0)
    combo.blockSignals(False)


def current_model_id(combo: QComboBox) -> str:
    data = combo.currentData()
    return str(data) if data else DEFAULT_MODEL
