# Testing

The repository uses stdlib `unittest` so tests do not require pytest.

## Fast Local Checks

Runs Python compile checks, app unit/functionality tests, Tk UI smoke test, and firmware static contract tests:

```powershell
.\Temp_and_HumidityLogger\venv\Scripts\python.exe run_tests.py --suite fast
```

## Full Local Checks

Adds PlatformIO firmware builds for controller and satellite:

```powershell
.\Temp_and_HumidityLogger\venv\Scripts\python.exe run_tests.py --suite all
```

The PlatformIO tests automatically fall back to `python -m platformio` or the repository `.venv` if PlatformIO is not installed in the logger app venv.

## Suite Filters

```powershell
.\Temp_and_HumidityLogger\venv\Scripts\python.exe run_tests.py --suite app
.\Temp_and_HumidityLogger\venv\Scripts\python.exe run_tests.py --suite firmware
```

## Coverage Scope

Automated coverage currently includes:

- App logic: smoothing, ESP event parsing, calibration, runtime setting sanitising, interval parsing, serial JSON extraction.
- App functionality: SQLite session/readings/markers, CSV export, ESP presence/interval edge cases, `nan` sensor readings, valid satellite readings.
- UI: Tk app startup/close smoke test using an isolated temporary runtime directory.
- Firmware: controller/satellite PlatformIO builds and static checks for logger-facing event/command/protocol contracts.

Hardware-in-loop tests are not automated yet. Those require real controller/satellite boards or a serial/ESP-NOW simulator.
