# Temp and Humidity Logger

Desktop logger for the ESP32 temperature monitor project.

## What It Does

- Connects to Arduino and ESP controller serial ports
- Displays live temperature and humidity values
- Maintains separate temperature and humidity charts
- Stores sessions in SQLite
- Exports CSV data
- Supports per-channel naming, colors, recording enable/disable, and calibration
- Supports marker annotations and session reload
- Handles ESP satellite presence using the configured report interval instead of a fixed timeout
- Smooths satellite chart data using a time window to reduce jagged 0.1 C/minute fluctuations
- Builds into a Windows desktop app with PyInstaller
- Shows the app version in the title bar and App info dialog

## Requirements

- Windows with Python 3 and `tkinter`
- Serial access to the controller hardware

Python packages:

```bash
pip install -r requirements.txt
```

## Run From Source

```bash
python -m temp_humidity_logger.main
```

The app implementation is split into the `temp_humidity_logger` package:

- `main.py` - application entry point
- `app.py` - main UI orchestration
- `config_store.py` - config and runtime settings
- `database.py` - SQLite sessions, markers, and CSV export
- `esp_controller.py` - ESP controller and satellite presence logic
- `serial_io.py` - serial connection, packet parsing, and polling
- `channels.py` - channel table, legend, and editor UI
- `layout.py` - docked/floating terminal and marker layout
- `sessions.py` - session browser and reload UI
- `graph_interaction.py` - graph zoom, pan, scrollbar, and markers
- `calibration.py` - calibration logic and manager UI
- `esp_events.py` - ESP JSON parsing helpers
- `smoothing.py` - chart smoothing helpers
- `version.py` - app name/version constants

## Recommended Setup

```bash
python -m venv venv
venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
python -m temp_humidity_logger.main
```

## Build Executable

PowerShell:

```powershell
.\build_exe.ps1
```

Batch wrapper:

```bat
build_exe.bat
```

Output:

- `dist\TempHumidityLogger\TempHumidityLogger.exe`
- bundled dependencies under `dist\TempHumidityLogger\libraries\`

The build uses `--onedir`, not `--onefile`.

Packaged runtime data is stored outside the build output in:

```text
%LOCALAPPDATA%\TempHumidityLogger
```

That avoids rebuilds deleting `config.ini`, `logger.db`, or exported CSV files.

## Tests

From the repository root:

```powershell
.\Temp_and_HumidityLogger\venv\Scripts\python.exe run_tests.py --suite fast
```

Run the full suite, including firmware builds:

```powershell
.\Temp_and_HumidityLogger\venv\Scripts\python.exe run_tests.py --suite all
```

See `..\TESTING.md` for details.

## Runtime Files

Common runtime artifacts:

- `config.ini`
- `logger.db`
- `data_temperature.csv`
- `data_humidity.csv`

When running the packaged `.exe`, these files live under `%LOCALAPPDATA%\TempHumidityLogger`.
When running from source, they live in `Temp_and_HumidityLogger\`.

Generated build output:

- `build\`
- `dist\`
- `__pycache__\`

## Performance Notes

- The chart renderer keeps at most one plotted sample per second per channel in memory.
- Visible chart data is downsampled to `Max render points` before plotting.
- Downsampled render data is cached per channel, so pan/zoom and redraws do not repeatedly rebuild the same 500k-point histories.
- Very large session loads may still spend noticeable time reading and reconstructing historical data from SQLite before the first draw.

If you need smoother interaction on slower machines, lower `Max render points` in the application settings.

## Troubleshooting

### App starts but charts feel heavy

- Reduce `Max render points`
- Shorten plot history if you do not need a full week
- Load smaller sessions when testing

### Build fails because `tkinter` is missing

Install Python with Tcl/Tk support and recreate the virtual environment.

### COM port is busy

Close serial terminals, previous logger instances, or OTA tools using the same port.

## Relevant Files

- `temp_humidity_logger\` - modular application package
- `temp_humidity_logger\main.py` - source and PyInstaller entry point
- `build_exe.ps1` - primary Windows packaging script
- `TempHumidityLogger.spec` - PyInstaller spec
- `config.ini` - local runtime configuration
- `requirements.txt` - Python dependencies
