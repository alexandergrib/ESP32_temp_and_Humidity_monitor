"""Application entry point."""

import os
import sys
import tkinter as tk

if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))
    from temp_humidity_logger.app import ArduinoLoggerApp
else:
    from .app import ArduinoLoggerApp


def main():
    root = tk.Tk()
    app = ArduinoLoggerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
