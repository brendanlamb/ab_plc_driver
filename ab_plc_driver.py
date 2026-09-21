"""
Ethernet/IP driver for reading tag data from Allen-Bradley ControlLogix /
CompactLogix PLCs (Logix5000 family), built on pycomm3.

Usage:
    from ab_plc_driver import PLCDriver

    driver = PLCDriver("192.168.1.10")
    with driver:
        value = driver.read_tag("Line1_Speed")
        values = driver.read_tags(["Line1_Speed", "Tank_Level"])

    # Continuous polling with a callback
    driver = PLCDriver("192.168.1.10")
    driver.poll(["Line1_Speed", "Tank_Level"], interval=1.0, callback=my_handler)
"""

from __future__ import annotations

import csv
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from pycomm3 import LogixDriver
from pycomm3.exceptions import CommError, PycommError

logger = logging.getLogger("ab_plc_driver")


@dataclass
class TagReading:
    tag: str
    value: object
    type: Optional[str]
    error: Optional[str]
    timestamp: datetime

    @property
    def ok(self) -> bool:
        return self.error is None

    def as_row(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "tag": self.tag,
            "value": self.value,
            "type": self.type,
            "error": self.error,
        }


class PLCConnectionError(RuntimeError):
    """Raised when the driver cannot establish or maintain a PLC connection."""


class PLCDriver:
    """
    Wraps pycomm3.LogixDriver with reconnect handling, batch reads, and a
    background polling loop. One instance targets one PLC (by IP or
    IP/slot path, e.g. "192.168.1.10" or "192.168.1.10/1" for a CompactLogix
    behind a chassis slot).
    """

    def __init__(
        self,
        ip_address: str,
        *,
        connect_timeout: float = 5.0,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
    ):
        self.ip_address = ip_address
        self.connect_timeout = connect_timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

        self._driver: Optional[LogixDriver] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        with self._lock:
            if self._driver is not None and self._driver.connected:
                return
            last_exc: Optional[Exception] = None
            for attempt in range(1, self.max_retries + 1):
                try:
                    driver = LogixDriver(self.ip_address)
                    driver.open()
                    if not driver.connected:
                        raise PLCConnectionError(f"Failed to open connection to {self.ip_address}")
                    self._driver = driver
                    logger.info("Connected to PLC at %s", self.ip_address)
                    return
                except (CommError, PycommError, OSError) as exc:
                    last_exc = exc
                    logger.warning(
                        "Connect attempt %d/%d to %s failed: %s",
                        attempt, self.max_retries, self.ip_address, exc,
                    )
                    if attempt < self.max_retries:
                        time.sleep(self.retry_backoff * attempt)
            raise PLCConnectionError(
                f"Could not connect to PLC at {self.ip_address} after {self.max_retries} attempts"
            ) from last_exc

    def disconnect(self) -> None:
        with self._lock:
            if self._driver is not None:
                try:
                    self._driver.close()
                finally:
                    self._driver = None
                    logger.info("Disconnected from PLC at %s", self.ip_address)

    def __enter__(self) -> "PLCDriver":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop_poll()
        self.disconnect()

    @property
    def connected(self) -> bool:
        return self._driver is not None and self._driver.connected

    def _ensure_connected(self) -> LogixDriver:
        if not self.connected:
            self.connect()
        return self._driver  # type: ignore[return-value]

    # -- reads ------------------------------------------------------------

    def read_tag(self, tag: str) -> TagReading:
        return self.read_tags([tag])[0]

    def read_tags(self, tags: Iterable[str]) -> list[TagReading]:
        """
        Read one or more tags in a single request where possible.
        Reconnects once and retries on a communication failure.
        """
        tags = list(tags)
        now = lambda: datetime.now(timezone.utc)

        for attempt in (1, 2):
            try:
                driver = self._ensure_connected()
                results = driver.read(*tags)
                if not isinstance(results, list):
                    results = [results]

                readings = []
                for tag, result in zip(tags, results):
                    ts = now()
                    if result.error:
                        readings.append(TagReading(tag, None, None, result.error, ts))
                    else:
                        readings.append(TagReading(tag, result.value, result.type, None, ts))
                return readings
            except (CommError, PycommError, PLCConnectionError, OSError) as exc:
                logger.warning("Read failed (attempt %d) for %s: %s", attempt, tags, exc)
                self.disconnect()
                if attempt == 2:
                    ts = now()
                    return [TagReading(t, None, None, str(exc), ts) for t in tags]
        return []  # unreachable

    def list_tags(self) -> list[str]:
        """Return the names of all tags (controller- and program-scope) on the PLC."""
        driver = self._ensure_connected()
        return sorted(driver.tags.keys())

    # -- polling ------------------------------------------------------------

    def poll(
        self,
        tags: Iterable[str],
        interval: float,
        callback: Callable[[list[TagReading]], None],
        *,
        blocking: bool = True,
    ) -> None:
        """
        Read `tags` every `interval` seconds and invoke `callback` with the
        list of TagReadings. If blocking=False, runs in a background thread
        and returns immediately; call stop_poll() to end it.
        """
        tags = list(tags)
        self._stop_event.clear()

        def _loop():
            while not self._stop_event.is_set():
                start = time.monotonic()
                try:
                    readings = self.read_tags(tags)
                    callback(readings)
                except Exception:
                    logger.exception("Poll callback raised an exception")
                elapsed = time.monotonic() - start
                self._stop_event.wait(max(0.0, interval - elapsed))

        if blocking:
            _loop()
        else:
            self._poll_thread = threading.Thread(target=_loop, daemon=True)
            self._poll_thread.start()

    def stop_poll(self) -> None:
        self._stop_event.set()
        if self._poll_thread is not None:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None


_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: object) -> object:
    """
    Neutralize spreadsheet formula injection: if a cell's text starts with a
    character Excel/Sheets treats as a formula prefix, prepend a single quote
    so it's opened as literal text instead of evaluated.
    """
    if isinstance(value, str) and value.startswith(_FORMULA_LEAD_CHARS):
        return "'" + value
    return value


def csv_logger(csv_path: str | Path) -> Callable[[list[TagReading]], None]:
    """
    Build a poll() callback that appends each batch of readings to a CSV
    file, writing the header once if the file doesn't already exist.
    """
    path = Path(csv_path)
    fieldnames = ["timestamp", "tag", "value", "type", "error"]

    def _callback(readings: list[TagReading]) -> None:
        is_new = not path.exists()
        with path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if is_new:
                writer.writeheader()
            for r in readings:
                row = {k: _csv_safe(v) for k, v in r.as_row().items()}
                writer.writerow(row)

    return _callback


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Read tags from an Allen-Bradley Logix5000 PLC")
    parser.add_argument("ip", help="PLC IP address, e.g. 192.168.1.10 (or 192.168.1.10/1 for a slot path)")
    parser.add_argument("tags", nargs="*", help="Tag names to read (omit to list all controller tags)")
    parser.add_argument("--poll", type=float, default=None, help="Poll interval in seconds (repeats until Ctrl+C)")
    parser.add_argument("--csv", type=str, default=None, help="Log readings to this CSV file (use with --poll)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                         format="%(asctime)s %(levelname)s %(message)s")

    driver = PLCDriver(args.ip)
    with driver:
        if not args.tags:
            for t in driver.list_tags():
                print(t)
        elif args.poll:
            callback = csv_logger(args.csv) if args.csv else None

            def _print_and_log(readings: list[TagReading]):
                for r in readings:
                    print(f"{r.timestamp.isoformat()}  {r.tag} = {r.value!r}" + (f"  ERROR: {r.error}" if r.error else ""))
                if callback:
                    callback(readings)

            try:
                driver.poll(args.tags, args.poll, _print_and_log)
            except KeyboardInterrupt:
                driver.stop_poll()
        else:
            for r in driver.read_tags(args.tags):
                print(f"{r.tag} = {r.value!r}" + (f"  ERROR: {r.error}" if r.error else ""))
