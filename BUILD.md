
## Firmware Build

Requirements:

- Python 3
- PlatformIO

Install PlatformIO if needed:

```bash
pip install platformio
```

Build controller firmware:

```bash
python -m platformio run -e controller_upload
```

Build satellite firmware:

```bash
python -m platformio run -e satellite_upload
```

Flash controller over USB:

```bash
python -m platformio run -e controller_upload -t upload
```

Flash satellite over USB:

```bash
python -m platformio run -e satellite_upload -t upload
```

### Flash over OTA


```bash
python pc_logger\ota_satellite.py --port COM6 --node-id 1 --firmware .pio\build\satellite_upload\firmware.bin
```
```bash
python pc_logger\ota_satellite.py --port COM6 --node-id 2 --firmware .pio\build\satellite_upload\firmware.bin
```
If PlatformIO does not auto-detect the correct board, pass a local upload port with `--upload-port`, for example `--upload-port COM6`.

## Desktop App Build

The Windows desktop logger lives in `Temp_and_HumidityLogger/` and is packaged with PyInstaller.

Create the app virtual environment and install dependencies:

```powershell
cd Temp_and_HumidityLogger
python -m venv venv
.\venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Run from source:

```powershell
python -m temp_humidity_logger.main
```

Build the executable:

```powershell
.\build_exe.ps1 -Clean
```

Batch wrapper:

```bash
./Temp_and_HumidityLogger/build_exe.bat -Clean
```

Output:

- `Temp_and_HumidityLogger\dist\TempHumidityLogger\TempHumidityLogger.exe`
- bundled libraries under `Temp_and_HumidityLogger\dist\TempHumidityLogger\libraries\`

The package is built as `--onedir`, not `--onefile`. Packaged runtime data is stored in `%LOCALAPPDATA%\TempHumidityLogger`, so rebuilds do not overwrite local `config.ini`, `logger.db`, or CSV exports.

Sign the packaged executable with a Windows code-signing certificate:

```powershell
.\build_exe.ps1 -Clean -Sign -CertificatePath "C:\secure\codesign.pfx" -CertificatePassword "pfx-password"
```

Alternatively, use a certificate already installed in the Windows certificate store:

```powershell
.\build_exe.ps1 -Clean -Sign -CertificateThumbprint "0123456789ABCDEF0123456789ABCDEF01234567"
```

The signing step requires `signtool.exe` from the Windows SDK. If it is not on `PATH`, pass `-SignToolPath`.

## Automated Checks

Run app tests, UI smoke tests, firmware static checks, and Python compile checks:

```bash
python run_tests.py --suite fast
```

Run all checks, including PlatformIO firmware builds:

```bash
python run_tests.py --suite all
```

See `TESTING.md` for suite filters and coverage details.

## PC Tools

### Controller Terminal

The controller serial helper is in `pc_logger/controller_terminal.py`.

Run:

```bash
python pc_logger\controller_terminal.py --port COM6
```

It opens the serial port, pushes current PC time to the controller, and provides interactive command entry.

Rename one or more satellites without staying in the terminal:

```bash
python pc_logger\controller_terminal.py --port COM6 --rename 3 "Boiler Room"
python pc_logger\controller_terminal.py --port COM6 --rename 3 "Boiler Room" --rename 4 "Outdoor Rack"
```

Interactive helper commands:

- `/sync` resend the current PC time with `TIME SET`
- `/rename <nodeId> <name>` sanitize the name and send `RENAME`
- `/quit` close the terminal

Any other line is sent directly to the controller command parser.

### Satellite OTA

The OTA helper is in `pc_logger/ota_satellite.py`.

Build satellite firmware first, then upload through the controller:

```bash
python -m platformio run -e satellite_upload

```
```bash
python pc_logger\ota_satellite.py --port COM6 --node-id 1 --firmware .pio\build\satellite_upload\firmware.bin
```
```bash
python pc_logger\ota_satellite.py --port COM6 --node-id 2 --firmware .pio\build\satellite_upload\firmware.bin
```
Flash controller over USB:

```bash
python -m platformio run -e controller_upload -t upload
```

Run OTA one satellite at a time.

Optional arguments:

- `--baud 460800` override the controller serial baud rate

The OTA helper automatically enables streaming and uses `OTA PREP <nodeId> ON|OFF` to put the target satellite into OTA-accept mode. In that mode the controller pushes settings that mark the node OTA-ready, force sleep off, switch to a quiet OTA report interval, and suppress normal reading delivery until OTA prep is turned off.

Manual OTA preparation from the controller terminal:

```text
OTA PREP 1 ON
OTA STATUS
OTA BEGIN 1 <size> <crc32hex>
...
OTA END
OTA PREP 1 OFF
```

`OTA PREP` is the high-level command intended for real OTA uploads. `OTA READY` is still available as a lower-level flag toggle, but it does not apply the full quiet-mode profile by itself.

## Controller Commands

Available controller commands:

- `HELP` print the controller command list
- `NODES` print the current node snapshot
- `BIND` open the bind window for pairing new satellites
- `BIND OFF` close the bind window
- `STREAM ON` enable JSON reading/config event output over serial
- `STREAM OFF` disable streaming output
- `SETINT <nodeId> <ms>` set one node report interval in milliseconds
- `SETINT ALL <ms>` set the report interval for every known node
- `SETSAMPLE <nodeId> <hz>` set one node sample rate in Hz (`1` to `5`)
- `SETSAMPLE ALL <hz>` set the sample rate for every known node (`1` to `5`)
- `SLEEP <nodeId> ON|OFF` enable or disable light sleep for one node
- `SLEEP ALL ON|OFF` enable or disable light sleep for every known node
- `SETTOFF <nodeId> <tempOffsetC>` store and apply a temperature offset
- `HEATER <nodeId> ON` enable the SHT85 heater for one node
- `HEATER <nodeId> OFF` disable the SHT85 heater for one node
- `RENAME <nodeId> <name>` rename a node with a sanitized ASCII name
- `TIME STATUS` print controller clock status and ISO timestamp
- `TIME SET <unixSeconds>` set the controller clock from a Unix timestamp
- `OTA PREP <nodeId> ON|OFF` enable or disable OTA preparation mode for one node; this applies the OTA-ready flag, forces sleep off, uses the quiet OTA interval, and pauses normal reading delivery until prep is disabled
- `OTA READY <nodeId> ON|OFF` mark a node ready or not ready for OTA transfer without applying the full OTA prep profile
- `OTA BEGIN <nodeId> <size> <crc32hex>` begin or resume an OTA session
- `OTA CHUNK <offset> <hex>` send one OTA data chunk
- `OTA END` finish the active OTA session
- `OTA STATUS` print the active OTA state, including whether OTA prep is active for the current target
- `OTA ABORT` cancel the active OTA session
