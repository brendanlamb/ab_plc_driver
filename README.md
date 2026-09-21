# AB PLC Reader

EtherNet/IP driver + desktop GUI for reading tag data from Allen-Bradley
ControlLogix / CompactLogix (Logix5000 family) PLCs, built on
[pycomm3](https://github.com/ottowayi/pycomm3).

## Security note

EtherNet/IP (CIP) has no built-in authentication or encryption — anyone on
the same network segment as a PLC can read and write its tags, with or
without this tool. Only use this on a trusted, segmented OT network, not
across an untrusted or public network.

## Install

```bash
pip install -r requirements.txt
```

## Use as a library

```python
from ab_plc_driver import PLCDriver

driver = PLCDriver("192.168.1.10")
with driver:
    value = driver.read_tag("Line1_Speed")
    values = driver.read_tags(["Line1_Speed", "Tank_Level"])
```

## Run the GUI

```bash
python ab_plc_gui.py
```

Connect to a PLC, browse/check the tags you want to watch, start monitoring,
and optionally log to CSV. The Trend tab plots live numeric tags with
user-settable axis ranges and per-tag colors.

## Run the CLI

```bash
python ab_plc_driver.py <ip> [tags...] [--poll SECONDS] [--csv path]
```

## Build a standalone exe (Windows)

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name AB_PLC_Reader ab_plc_gui.py
```

The exe is unsigned, so Windows SmartScreen will warn on first run —
"More info" → "Run anyway".
