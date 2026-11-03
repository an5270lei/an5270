import sys
import traceback
from pathlib import Path

# 允许从任意工作目录启动：把项目根加入模块搜索路径。
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui.main_window import MainWindow  # noqa: E402


def _install_excepthook():
    """未捕获异常打到控制台，避免界面直接消失且没有任何线索。"""

    def hook(exc_type, exc_value, exc_tb):
        traceback.print_exception(exc_type, exc_value, exc_tb)

    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AI Studio")
    app.setStyle("Fusion")
    _install_excepthook()

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
