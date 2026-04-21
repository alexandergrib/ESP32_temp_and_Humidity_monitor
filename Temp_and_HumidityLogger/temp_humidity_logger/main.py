"""Application entry point."""

import tkinter as tk

from .app import ArduinoLoggerApp


def main():
    root = tk.Tk()
    app = ArduinoLoggerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
