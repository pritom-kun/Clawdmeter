#!/usr/bin/env python3
"""Claude Usage Tracker Daemon (BLE) — cross-platform host daemon.

Polls Claude API rate-limit headers and writes a JSON payload to the
ESP32 "Clawdmeter" peripheral over a custom GATT service. Uses
bleak (CoreBluetooth on macOS, BlueZ on Linux, WinRT on Windows).
"""

import asyncio
import calendar
import datetime
import getpass
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from bleak import BleakClient
from bleak.exc import BleakError

DEVICE_NAME = "Clawdmeter"
SERVICE_UUID = "4c41555a-4465-7669-6365-000000000001"
RX_CHAR_UUID = "4c41555a-4465-7669-6365-000000000002"
REQ_CHAR_UUID = "4c41555a-4465-7669-6365-000000000004"

POLL_INTERVAL = 60
TICK = 5
CONNECT_TIMEOUT = 20.0

# macOS: token lives in Keychain (service "Claude Code-credentials").
# Linux/Windows: each config dir keeps its own ~/.claude/.credentials.json.
KEYCHAIN_SERVICE = "Claude Code-credentials"  # darwin only — read via `security find-generic-password`
DEFAULT_CONFIG_DIR = Path.home() / ".claude"
SAVED_ADDR_FILE = Path.home() / ".config" / "claude-usage-monitor" / "ble-address"
CONFIG_FILE = Path.home() / ".config" / "claude-usage-monitor" / "config"

API_URL = "https://api.anthropic.com/v1/messages"
API_HEADERS_TEMPLATE = {
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
    "User-Agent": "claude-code/2.1.5",
}
API_BODY = {
    "model": "claude-haiku-4-5-20251001",
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "hi"}],
}

OAUTH_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # public Claude Code OAuth client id
TOKEN_REFRESH_SKEW_SECONDS = 300  # refresh if token expires within 5 minutes
_MIN_REFRESH_INTERVAL = 30        # clock-skew floor — never refresh more than once per 30s
_last_refresh_at: float = 0.0
_refresh_backoff_seconds: float = _MIN_REFRESH_INTERVAL  # extended after 429 etc.


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _redirect_windows_logs(log_dir_arg: str | None) -> None:
    """When the Scheduled Task launches us via pythonw.exe there is no
    console, so `sys.stdout`/`sys.stderr` are None and the task can't do a
    `>>` redirect. Reopen them onto log files ourselves. The log directory is
    passed as argv[1] by install-win.ps1 ($LogDir); we fall back to the same
    %LOCALAPPDATA%\\Clawdmeter\\logs convention the installer uses. No-op on
    every other platform / when a real console is attached.

    Once entered (pythonw, stdout is None) we must NEVER leave stdout/stderr
    as None: a later log() would raise AttributeError and crash the daemon
    with no window and no log to explain it. If the real log files can't be
    opened, fall back to os.devnull so the daemon keeps running.
    """
    if sys.platform != "win32" or sys.stdout is not None:
        return
    out_target = err_target = os.devnull
    try:
        if log_dir_arg and log_dir_arg.strip():
            log_dir = Path(log_dir_arg)
        else:
            base = os.environ.get("LOCALAPPDATA")
            log_dir = Path(base) / "Clawdmeter" / "logs" if base else None
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
            out_target = log_dir / "claude-usage-daemon.out.log"
            err_target = log_dir / "claude-usage-daemon.err.log"
    except OSError:
        pass  # couldn't prepare the log dir — fall back to devnull below
    # append (matches the old cmd.exe `>>`), line-buffered, UTF-8
    try:
        sys.stdout = open(out_target, "a", encoding="utf-8", buffering=1)
        sys.stderr = open(err_target, "a", encoding="utf-8", buffering=1)
    except OSError:
        # Last resort: keep the streams non-None so log() can't crash us.
        try:
            sys.stdout = open(os.devnull, "a")
            sys.stderr = open(os.devnull, "a")
        except OSError:
            pass


def _extract_access_token(blob: str) -> str | None:
    """Pull the accessToken out of a credentials blob.

    Claude Code stores credentials as a JSON object; the blob may also be
    nested ({"claudeAiOauth": {"accessToken": "..."}}). Fall back to a
    regex match so unexpected shapes still work, and finally treat the
    blob as a raw token if nothing else matches.
    """
    blob = blob.strip()
    if not blob:
        return None
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        # direct: {"accessToken": "..."}
        if isinstance(data.get("accessToken"), str):
            return data["accessToken"]
        # nested: {"claudeAiOauth": {"accessToken": "..."}}
        for v in data.values():
            if isinstance(v, dict) and isinstance(v.get("accessToken"), str):
                return v["accessToken"]
    m = re.search(r'"accessToken"\s*:\s*"([^"]+)"', blob)
    if m:
        return m.group(1)
    # Raw token (no JSON wrapper) — must look plausible (sk-ant-... etc.)
    if re.fullmatch(r"[A-Za-z0-9_\-.~+/=]{20,}", blob):
        return blob
    return None


def _read_token_keychain() -> str | None:
    try:
        out = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                getpass.getuser(),
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.CalledProcessError as e:
        log(f"Keychain read failed (rc={e.returncode}): {e.stderr.strip()}")
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        log(f"Keychain access error: {e}")
        return None
    return _extract_access_token(out.stdout)


# `claude setup-token` prints a 1-year OAuth token but doesn't persist it.
# We accept it from either the documented env var or a file we write during
# install (for Scheduled Tasks, where env var propagation is unreliable).
LONG_LIVED_TOKEN_FILE = Path.home() / ".claude" / ".clawdmeter-oauth-token"


def _read_long_lived_token() -> str | None:
    env_tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if isinstance(env_tok, str) and env_tok.strip():
        return env_tok.strip()
    try:
        # utf-8-sig auto-strips a UTF-8 BOM if the installer wrote one
        # (PowerShell 5.1's `Set-Content -Encoding utf8` does — preserving
        # ﻿ would prepend it to the Bearer header and fail auth).
        raw = LONG_LIVED_TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeDecodeError) as e:
        log(f"Long-lived token file unreadable: {e}")
        return None
    return raw or None


def read_config_dirs() -> list[Path]:
    """Claude config dirs to poll, from the `config_dirs` option (comma list).

    Defaults to [~/.claude] so existing single-plan setups are unchanged. ~ is
    expanded. Mirrors the Linux bash daemon's read_config_dirs.
    """
    raw = ""
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "config_dirs":
                    raw = val.strip()
    except OSError:
        pass
    if not raw:
        return [DEFAULT_CONFIG_DIR]
    dirs = [Path(p.strip()).expanduser() for p in raw.split(",") if p.strip()]
    return dirs or [DEFAULT_CONFIG_DIR]


def read_token_for(config_dir: Path) -> str | None:
    """Read the OAuth token for one config dir.

    Linux/Windows: each dir keeps its own ``<dir>/.credentials.json``. macOS:
    the default install stores the token in Keychain with no file, so for the
    default dir we fall back to Keychain when no file is present — preserving
    existing single-plan macOS behavior. Additional macOS dirs are read from
    their files; a work plan whose token lives only in the single Keychain
    entry can't be told apart there (documented follow-up).

    The default dir also honors a long-lived OAuth token (env var or a file
    the installer writes) as a global override — `claude setup-token` issues
    a token that doesn't expire for a year, useful on a host with no
    interactive `claude` session to keep .credentials.json refreshed (e.g. a
    Windows Scheduled Task). This function stays synchronous and does no
    network I/O; poll_active_payload separately retries a failed poll with an
    OAuth-refreshed token (see get_valid_token) for non-macOS dirs.
    """
    if config_dir == DEFAULT_CONFIG_DIR:
        ll_token = _read_long_lived_token()
        if ll_token:
            return ll_token
    cred = config_dir / ".credentials.json"
    try:
        if cred.exists():
            return _extract_access_token(cred.read_text())
    except OSError as e:
        log(f"Error reading credentials in {config_dir}: {e}")
    if sys.platform == "darwin" and config_dir == DEFAULT_CONFIG_DIR:
        return _read_token_keychain()
    return None


def _read_credentials_blob(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(raw)
    except OSError as e:
        log(f"Error reading credentials file: {e}")
        return None
    except json.JSONDecodeError as e:
        log(f"Error parsing credentials file: {e}")
        return None


def _token_is_fresh(creds: dict) -> bool:
    try:
        expires_at = creds["claudeAiOauth"]["expiresAt"]
        return expires_at > time.time() * 1000 + TOKEN_REFRESH_SKEW_SECONDS * 1000
    except (KeyError, TypeError):
        return False


async def _refresh_token(refresh_token: str) -> dict | None:
    global _refresh_backoff_seconds
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "claude-code/2.1.5",
    }
    body = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": OAUTH_CLIENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(OAUTH_TOKEN_URL, headers=headers, json=body)
    except httpx.HTTPError as e:
        log(f"OAuth refresh request failed: {e}")
        _refresh_backoff_seconds = float(_MIN_REFRESH_INTERVAL)
        return None
    if resp.status_code < 200 or resp.status_code >= 300:
        body_text = resp.text[:200]
        log(f"OAuth refresh HTTP {resp.status_code}: {body_text}")
        if resp.status_code == 429 or "cloudflare" in resp.text.lower():
            log("OAuth refresh rate-limited; backing off 5 minutes")
            _refresh_backoff_seconds = 300.0
        else:
            _refresh_backoff_seconds = float(_MIN_REFRESH_INTERVAL)
        return None
    data = resp.json()
    if not isinstance(data, dict):
        log(f"OAuth refresh returned unexpected type {type(data).__name__}")
        return None
    _refresh_backoff_seconds = float(_MIN_REFRESH_INTERVAL)  # reset on success
    return data


def _persist_credentials(creds: dict, path: Path) -> bool:
    tmp = path.parent / (path.name + ".tmp." + str(os.getpid()))
    try:
        tmp.write_text(json.dumps(creds, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError as e:
        log(f"Error persisting credentials: {e}")
        return False
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


async def get_valid_token(config_dir: Path, force_refresh: bool = False) -> str | None:
    """Read (refreshing if needed) the OAuth token for one config dir's
    .credentials.json. Used as a recovery path when a poll fails outright —
    read_token_for() stays a cheap synchronous file read for the common case.
    """
    global _last_refresh_at
    cred_path = config_dir / ".credentials.json"
    creds = _read_credentials_blob(cred_path)
    if not creds:
        return None
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        log(f"{cred_path} missing claudeAiOauth block")
        return None

    current_access = oauth.get("accessToken")

    if not force_refresh and _token_is_fresh(creds):
        return current_access if isinstance(current_access, str) else None

    # Backoff floor: don't hammer the OAuth endpoint after a recent attempt.
    now = time.time()
    backoff = _refresh_backoff_seconds
    if now - _last_refresh_at < backoff:
        # If the token is actually still usable, return it (this is the
        # force_refresh-with-fresh-token case). Otherwise signal "no token"
        # so the caller skips the poll instead of spamming /v1/messages
        # with an expired token.
        if _token_is_fresh(creds) and isinstance(current_access, str):
            return current_access
        remaining = int(backoff - (now - _last_refresh_at))
        log(f"OAuth refresh recently failed; backing off (~{remaining}s remaining)")
        return None

    refresh_tok = oauth.get("refreshToken")
    if not isinstance(refresh_tok, str) or not refresh_tok:
        log(f"no refreshToken in {cred_path}; please re-run `claude` to authenticate")
        return None

    resp = await _refresh_token(refresh_tok)
    _last_refresh_at = time.time()
    if not resp:
        log("token refresh failed; please re-run `claude` to authenticate")
        return None

    new_access = resp.get("access_token")
    if not isinstance(new_access, str):
        log(f"refresh response missing access_token: keys={list(resp.keys())}")
        return None

    oauth["accessToken"] = new_access
    new_refresh = resp.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh:
        oauth["refreshToken"] = new_refresh
    # RFC 6749 lets refresh_token grant responses omit expires_in (token inherits
    # the original lifetime); fall back to 1 h so _token_is_fresh doesn't loop us
    # straight back into another refresh on the next poll.
    expires_in = resp.get("expires_in")
    if not isinstance(expires_in, (int, float)):
        expires_in = 3600
    oauth["expiresAt"] = int((time.time() + float(expires_in)) * 1000)
    creds["claudeAiOauth"] = oauth

    if not _persist_credentials(creds, cred_path):
        log("failed to persist refreshed credentials; using token in memory only")
    log("OAuth token refreshed successfully")
    return new_access


def load_cached_address() -> str | None:
    if not SAVED_ADDR_FILE.exists():
        return None
    addr = SAVED_ADDR_FILE.read_text().strip()
    # Accept both Linux MAC (AA:BB:CC:DD:EE:FF) and macOS CoreBluetooth UUID
    # (E621E1F8-C36C-495A-93FC-0C247A3E6E5F).
    if re.fullmatch(r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}", addr) or re.fullmatch(
        r"[0-9A-Fa-f]{8}-(?:[0-9A-Fa-f]{4}-){3}[0-9A-Fa-f]{12}", addr
    ):
        return addr
    log("Cached address malformed, discarding")
    SAVED_ADDR_FILE.unlink(missing_ok=True)
    return None


# --- macOS: recover a device the OS already holds as an HID keyboard --------
#
# The firmware advertises as a BLE HID keyboard so its buttons type into the
# Mac. macOS auto-connects to that HID, and CoreBluetooth then EXCLUDES the
# peripheral from BleakScanner.discover() results (already-connected devices
# never appear in scans). bleak's connect-by-address path also scans
# internally, so a cached address can't help either. The documented escape
# hatch is retrieveConnectedPeripheralsWithServices_, which returns
# peripherals the system is already connected to. We wrap the result in a
# BLEDevice carrying the live (peripheral, manager) details so BleakClient
# connects to it directly without scanning. CoreBluetooth shares the single
# physical link, so this rides the existing HID connection — the keyboard
# keeps working.
_cb_manager = None  # reused CentralManagerDelegate (CoreBluetooth)


async def _get_cb_manager():
    """Lazily create and ready a shared CoreBluetooth central manager."""
    global _cb_manager
    if _cb_manager is None:
        from bleak.backends.corebluetooth.CentralManagerDelegate import (
            CentralManagerDelegate,
        )

        mgr = CentralManagerDelegate()
        await mgr.wait_until_ready()  # raises if Bluetooth is unauthorized/off
        _cb_manager = mgr
    return _cb_manager


async def retrieve_connected_macos(skip_addr: str | None = None):
    """Return a BLEDevice for a system-connected 'Clawdmeter', or None.

    Two-step lookup, strongest signal first:

    1. Peripherals connected under our CUSTOM service UUID. Membership in
       that service is unambiguous (no other device exposes it), so we accept
       by service alone — the peripheral's name can be None on macOS.
    2. Fall back to the generic HID service 0x1812, but ONLY trust a
       peripheral whose name matches DEVICE_NAME. 0x1812 also matches
       unrelated keyboards/mice, so picking blindly here could grab the
       wrong device.

    ``skip_addr`` skips a peripheral whose UUID just failed to connect, so a
    stale CoreBluetooth handle can't trap us into never trying a fresh scan.
    """
    from CoreBluetooth import CBUUID
    from bleak.backends.device import BLEDevice

    try:
        manager = await _get_cb_manager()
    except Exception as e:  # BleakBluetoothNotAvailableError etc.
        log(f"CoreBluetooth unavailable: {e}")
        return None

    cm = manager.central_manager

    def _wrap(p):
        addr = p.identifier().UUIDString()
        log(f"Found system-connected peripheral: {p.name()!r} [{addr}]")
        return BLEDevice(addr, p.name(), (p, manager))

    def _ok(p) -> bool:
        return not (skip_addr and p.identifier().UUIDString() == skip_addr)

    # 1. Custom service — accept by service membership alone.
    custom = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_(SERVICE_UUID)]
    )
    for p in custom or []:
        if _ok(p):
            return _wrap(p)

    # 2. Generic HID service — require an exact name match.
    hid = cm.retrieveConnectedPeripheralsWithServices_(
        [CBUUID.UUIDWithString_("1812")]
    )
    for p in hid or []:
        if _ok(p) and p.name() == DEVICE_NAME:
            return _wrap(p)

    return None


async def discover_target(skip_addr: str | None = None):
    """Return a connectable target, or None.

    The daemon only ever targets the device this system already holds — it
    never scans for a nearby device by name, so it can't grab a stranger's or
    the wrong nearby unit. On macOS that's the system-connected peripheral (the
    firmware advertises as an HID keyboard, so once paired the OS auto-connects
    and holds it — HID-grabbed devices are invisible to scans anyway). On other
    platforms (Linux, Windows) it's a previously-pinned address in the cache
    file. If the device isn't held/pinned, we log and wait rather than
    scanning. ``skip_addr`` skips a peripheral whose handle just failed to
    connect.
    """
    if sys.platform == "darwin":
        dev = await retrieve_connected_macos(skip_addr=skip_addr)
        if dev is None:
            log("Device not held by OS; waiting (not scanning by name)")
        return dev

    address = load_cached_address()
    if not address:
        log("No pinned address cached; waiting (not scanning by name)")
    return address


def read_chime_setting() -> str:
    """Read the `chime` option from the config file. One of: off|on.

    Defaults to "off" (the device stays silent) so existing setups are
    unaffected until the user opts in.
    """
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "chime":
                    val = val.strip().lower()
                    if val in ("off", "on"):
                        return val
    except OSError:
        pass
    return "off"


def read_clock_setting() -> str:
    """Read the `clock` option from the config file. One of: off|auto|12|24.

    Defaults to "off" (no clock; the device keeps showing "Usage") so existing
    setups are unaffected until the user opts in.
    """
    try:
        if CONFIG_FILE.exists():
            for line in CONFIG_FILE.read_text().splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                if key.strip().lower() == "clock":
                    val = val.strip().lower()
                    if val in ("off", "auto", "12", "24"):
                        return val
    except OSError:
        pass
    return "off"


def add_chime_field(payload: dict) -> None:
    """Add "c":1 to the payload when the config opts in, so the firmware may
    sound the session-reset chime. Omitted entirely when chime is off."""
    if read_chime_setting() == "on":
        payload["c"] = 1


def detect_hour_format() -> int:
    """Best-effort 12h/24h detection for the host. Returns 12 or 24 (default 24)."""
    # macOS: the explicit System Settings toggle lives in NSGlobalDomain.
    for key, result in (("AppleICUForce24HourTime", 24), ("AppleICUForce12HourTime", 12)):
        try:
            out = subprocess.run(["defaults", "read", "-g", key],
                                 capture_output=True, text=True, timeout=3)
            if out.stdout.strip() == "1":
                return result
        except (OSError, subprocess.SubprocessError):
            pass
    # Fallback to the C locale's time format (may be C/24h under launchd).
    try:
        import locale
        locale.setlocale(locale.LC_TIME, "")
        fmt = locale.nl_langinfo(locale.T_FMT)
        if "%p" in fmt or "%r" in fmt or "%I" in fmt:
            return 12
    except (ImportError, locale.Error, AttributeError):
        pass
    return 24


def add_clock_fields(payload: dict) -> None:
    """Add wall-clock fields to the payload when the config opts in.

    "t"  = local wall-clock epoch (UTC epoch shifted by the tz offset) so the
           device can show the time without an RTC.
    "tf" = 12 or 24, the hour format the device should render.
    """
    clock = read_clock_setting()
    if clock == "off":
        return
    tf = 24 if clock == "24" else 12 if clock == "12" else detect_hour_format()
    payload["t"] = int(time.time()) + time.localtime().tm_gmtoff
    payload["tf"] = tf


async def poll_api(token: str) -> dict | None:
    headers = dict(API_HEADERS_TEMPLATE)
    headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=20.0) as http:
            resp = await http.post(API_URL, headers=headers, json=API_BODY)
    except httpx.HTTPError as e:
        log(f"API call failed: {e}")
        return None
    if resp.status_code >= 400:
        log(f"API HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    def hdr(name: str, default: str = "0") -> str:
        return resp.headers.get(name, default)

    now = time.time()

    def reset_minutes(reset_ts: str) -> int:
        try:
            r = float(reset_ts)
        except ValueError:
            return 0
        mins = (r - now) / 60.0
        return int(round(mins)) if mins > 0 else 0

    def pct(util: str) -> int:
        try:
            return int(round(float(util) * 100))
        except ValueError:
            return 0

    # Pro/Max accounts expose 5h/7d windows; Enterprise/overage use a single
    # spending-limit model reported via overage-utilization.
    if resp.headers.get("anthropic-ratelimit-unified-5h-utilization"):
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-5h-utilization")),
            "sr": reset_minutes(hdr("anthropic-ratelimit-unified-5h-reset")),
            "w": pct(hdr("anthropic-ratelimit-unified-7d-utilization")),
            "wr": reset_minutes(hdr("anthropic-ratelimit-unified-7d-reset")),
            "st": hdr("anthropic-ratelimit-unified-5h-status", "unknown"),
            "acct": "pro",
            "ok": True,
        }
    else:
        reset_ts = hdr("anthropic-ratelimit-unified-overage-reset")
        payload = {
            "s": pct(hdr("anthropic-ratelimit-unified-overage-utilization")),
            "sr": reset_minutes(reset_ts),
            "w": 0,
            "wr": 0,
            "st": hdr("anthropic-ratelimit-unified-status", "unknown"),
            "acct": "ent",
            **_billing_period_info(now, reset_ts),
            "ok": True,
        }
    add_chime_field(payload)   # adds "c":1 iff the config opts in
    add_clock_fields(payload)   # adds "t" + "tf" iff the config opts in
    return payload


def _billing_period_info(now: float, reset_ts: str) -> dict:
    """Fraction of billing period elapsed (tp, 0-100) and period length in days (pd).

    Billing periods are assumed calendar-monthly: period_end is the reset
    timestamp, period_start is the same day/time one calendar month earlier.

    The rate-limit headers expose only the reset timestamp, not the period
    length, so the monthly window is an assumption — but a documented one:
    Enterprise spend-limit `period` "the only value today is monthly"
    (Claude Enterprise Admin API reference). The doc notes period is an open
    string that may gain other values later; revisit this if so.
    """
    try:
        period_end = float(reset_ts)
    except ValueError:
        return {"tp": 0, "pd": 30}
    if period_end <= 0:
        # reset_ts defaults to "0" when the overage-reset header is absent.
        # fromtimestamp(0) is 1970; stepping a month back lands in 1969, and
        # datetime.timestamp() raises OSError for pre-1970 dates on Windows.
        # Benign on macOS/Linux, but guard here too to keep the daemons parallel.
        return {"tp": 0, "pd": 30}
    dt_end = datetime.datetime.fromtimestamp(period_end)
    prev_month = dt_end.month - 1 or 12
    prev_year = dt_end.year if dt_end.month > 1 else dt_end.year - 1
    prev_day = min(dt_end.day, calendar.monthrange(prev_year, prev_month)[1])
    dt_start = dt_end.replace(year=prev_year, month=prev_month, day=prev_day)
    period_start = dt_start.timestamp()
    period_len = period_end - period_start
    if period_len <= 0:
        return {"tp": 0, "pd": 30}
    pct_val = (now - period_start) / period_len * 100
    total_days = int(round(period_len / 86400))
    rd = f"{dt_end.strftime('%b')} {dt_end.day}"
    return {
        "tp": max(0, min(100, int(round(pct_val)))),
        "pd": total_days,
        "rd": rd,
    }


class PlanSelector:
    """Decide which config dir's plan is "active" across polls.

    "Active" = the plan whose session % rose most recently (recent API activity).
    A rise stamps a monotonic poll counter, so the choice is sticky and a window
    reset (a drop to 0) isn't mistaken for use. Before any rise is seen (startup)
    the highest current session % wins. Mirrors the Linux bash daemon.
    """

    def __init__(self) -> None:
        self.prev_s: dict[Path, int] = {}
        self.last_active: dict[Path, int] = {}
        self.seq = 0

    def choose(self, sessions: dict[Path, int]) -> Path:
        """Update state from this cycle's {dir: session_pct} and return the active dir."""
        self.seq += 1
        for d, s in sessions.items():
            if d in self.prev_s and s > self.prev_s[d]:
                self.last_active[d] = self.seq
            self.prev_s[d] = s
        # Most recent activity wins; ties (and the startup case) break by highest %.
        return max(sessions, key=lambda d: (self.last_active.get(d, 0), sessions[d]))


# Module-level so the active-plan state survives reconnects.
_SELECTOR = PlanSelector()


async def poll_active_payload(selector: PlanSelector = _SELECTOR) -> dict | None:
    """Poll every configured config dir and return the active plan's payload.

    Returns None when no dir yields a usable payload this cycle. A single
    configured dir (the default) collapses to exactly the old single-poll path.
    """
    dirs = read_config_dirs()
    payloads: dict[Path, dict] = {}
    sessions: dict[Path, int] = {}
    for d in dirs:
        token = read_token_for(d)
        if not token:
            log(f"No token in {d}; skipping")
            continue
        payload = await poll_api(token)
        if (
            payload is None
            and sys.platform != "darwin"
            and not (d == DEFAULT_CONFIG_DIR and _read_long_lived_token())
        ):
            # Covers 401s from an expired token on hosts with no interactive
            # `claude` session to refresh it (e.g. a Windows Scheduled Task).
            # get_valid_token's backoff prevents hammering the OAuth endpoint
            # if this keeps failing. Not attempted for a long-lived token —
            # there's no refresh token behind a `claude setup-token` grant.
            refreshed = await get_valid_token(d, force_refresh=True)
            if refreshed and refreshed != token:
                payload = await poll_api(refreshed)
        if payload is not None:
            payloads[d] = payload
            sessions[d] = int(payload.get("s", 0) or 0)
    if not payloads:
        return None
    active = selector.choose(sessions)
    if len(dirs) > 1:
        log(f"Active plan: {active} (s={sessions[active]})")
    return payloads[active]


class Session:
    def __init__(self, client: BleakClient) -> None:
        self.client = client
        self.refresh_requested = asyncio.Event()

    def _on_refresh(self, _char, _data: bytearray) -> None:
        log("Refresh requested by device")
        self.refresh_requested.set()

    async def setup_refresh_subscription(self) -> None:
        # start_notify awaits CoreBluetooth's CCCD-write confirmation, which
        # never arrives if the peripheral doesn't ACK the subscribe (a
        # half-open link after the OS auto-connects the HID). Unbounded, that
        # await wedges the whole daemon between "Connected" and the first poll
        # — the device then shows nothing until a manual restart. Bound it: the
        # subscription is only an optional device-initiated refresh nudge (we
        # poll every POLL_INTERVAL regardless), so on timeout we proceed.
        try:
            await asyncio.wait_for(
                self.client.start_notify(REQ_CHAR_UUID, self._on_refresh),
                timeout=10,
            )
        except (BleakError, ValueError) as e:
            log(f"Refresh subscription unavailable: {e}")
        except asyncio.TimeoutError:
            log("Refresh subscription timed out; polling without it")

    async def write_payload(self, payload: dict) -> bool:
        data = json.dumps(payload, separators=(",", ":")).encode()
        log(f"Sending: {data.decode()}")
        # WriteWithoutResponse (response=False) has no ATT-level flow control on
        # WinRT: after the first packet the rest are silently dropped with no
        # error raised, so the device freezes on the first value while the
        # daemon keeps "sending". Use an acknowledged write on Windows so each
        # payload is confirmed end-to-end (and a genuine failure now surfaces as
        # "Write failed" instead of vanishing). macOS/Linux keep the cheaper
        # no-response write, which is reliable on those stacks.
        try:
            await self.client.write_gatt_char(
                RX_CHAR_UUID, data, response=sys.platform == "win32"
            )
            return True
        except BleakError as e:
            log(f"Write failed: {e}")
            return False


def _is_encryption_error(exc: BaseException) -> bool:
    """True if a connect error is a macOS bonding/encryption mismatch.

    macOS reports a stale bond as CBErrorDomain Code=15 ("Failed to encrypt
    the connection..."). Match on the message text so we don't depend on how
    bleak wraps the underlying CoreBluetooth error.
    """
    s = str(exc).lower()
    return "code=15" in s or "encrypt" in s


# blueutil talks to Bluetooth via IOBluetooth, which on recent macOS needs its
# OWN Bluetooth TCC grant (separate from the daemon's CoreBluetooth grant).
# Without it, blueutil *hangs* instead of erroring — so every call is bounded
# by a timeout and a hang is reported as a permission problem, not a crash.
BLUEUTIL_TIMEOUT = 8


def _blueutil(*args: str) -> str | None:
    """Run `blueutil <args>`, returning stdout, or None on failure/timeout.

    A timeout almost always means blueutil lacks Bluetooth permission (it
    blocks rather than failing), so we surface that cause explicitly.
    """
    try:
        return subprocess.run(
            ["blueutil", *args],
            capture_output=True, text=True,
            timeout=BLUEUTIL_TIMEOUT, check=True,
        ).stdout
    except subprocess.TimeoutExpired:
        log(f"blueutil {' '.join(args)} timed out — it likely lacks Bluetooth "
            "permission. Grant it under System Settings > Privacy & Security > "
            "Bluetooth (run `blueutil --paired` once from Terminal to prompt).")
        return None
    except (subprocess.SubprocessError, OSError) as e:
        log(f"blueutil {' '.join(args)} failed: {e}")
        return None


def unpair_macos() -> bool:
    """Forget a stale macOS bond for DEVICE_NAME so the device can re-pair.

    A Code=15 "failed to encrypt" connect error means macOS holds bonding
    keys that no longer match the ESP32's (e.g. after a firmware reflash or
    the on-device bond-clear gesture). The firmware pairs "just works" (no
    MITM), so once the stale bond is gone the next connect re-bonds silently
    with no GUI prompt.

    CoreBluetooth exposes no unpair API, so we shell out to `blueutil`. The
    daemon only knows the peripheral's CoreBluetooth UUID, not the BD_ADDR
    that blueutil needs, so we map by name via `blueutil --paired`. Returns
    True if a bond was removed. Mirrors the Linux daemon's `bluetoothctl
    remove` self-heal.
    """
    if not shutil.which("blueutil"):
        log("Stale bond detected but `blueutil` is not installed; cannot "
            "auto-recover. Run `brew install blueutil`, or forget "
            f"'{DEVICE_NAME}' in System Settings > Bluetooth and reconnect.")
        return False

    out = _blueutil("--paired")
    if out is None:
        return False

    # Each line looks like:
    #   address: 28-84-85-55-5c-3d, ... name: "Clawdmeter", ...
    addr = None
    for line in out.splitlines():
        if f'name: "{DEVICE_NAME}"' in line:
            m = re.search(r"address:\s*([0-9a-fA-F:-]+)", line)
            if m:
                addr = m.group(1)
                break
    if not addr:
        log(f"No paired '{DEVICE_NAME}' found to unpair (already forgotten?)")
        return False

    if _blueutil("--unpair", addr) is None:
        return False
    log(f"Unpaired stale bond for '{DEVICE_NAME}' [{addr}]; re-pairing on "
        "next connect")
    return True


async def connect_and_run(target, stop_event: asyncio.Event, once: bool = False) -> bool:
    """Connect to a target and poll until disconnected or stopped.

    ``target`` is either an address string (Linux) or a BLEDevice carrying
    live CoreBluetooth details (macOS). Returns True if the connection was
    used successfully (so the caller keeps the cached address), False if the
    connection failed and the cache should be invalidated.
    """
    display = target if isinstance(target, str) else target.address
    log(f"Connecting to {display}...")
    # Windows/WinRT needs two nudges that CoreBluetooth and BlueZ don't:
    #
    #  1. pair=True -- WinRT does not auto-bond when an encrypted characteristic
    #     is first accessed. The firmware's custom service requires bonding
    #     (NimBLE setSecurityAuth bond=true), so an unbonded host can't resolve
    #     the characteristics at all. Idempotent: Bleak skips it when bonded.
    #
    #  2. use_cached_services=False -- once bonded, WinRT caches the GATT table
    #     per device, and that cache can go stale (the characteristics drop out
    #     of it) while the bond + service node stay in Windows' device tree.
    #     That makes start_notify/write_gatt_char fail with "Characteristic ...
    #     was not found" even though the device is fully paired and the service
    #     is enumerated. Forcing uncached discovery re-reads the live table from
    #     the device on every connect instead of trusting the cache.
    #
    # Both are gated to Windows: pair() is unavailable on macOS, BlueZ bonds
    # implicitly, and neither platform needs the cache override.
    if sys.platform == "win32":
        client = BleakClient(target, pair=True, winrt={"use_cached_services": False})
    else:
        client = BleakClient(target)
    try:
        # Bound the connect the same way #84 bounded the refresh subscribe.
        # On macOS the OS auto-connects the firmware's HID link, so
        # CoreBluetooth can hand us a half-open peripheral whose GATT connect
        # handshake never completes. BleakClient's own timeout governs
        # discovery, not connectPeripheral, so an unbounded await here wedges
        # the single-threaded daemon forever at "Connecting..." (observed ~13h,
        # device stuck on stale data). wait_for raises TimeoutError, which the
        # handler below already treats as a connection failure -> drop the
        # cached address and rescan.
        await asyncio.wait_for(client.connect(), timeout=CONNECT_TIMEOUT)
    except (BleakError, asyncio.TimeoutError) as e:
        log(f"Connection failed: {e}")
        if sys.platform == "darwin" and _is_encryption_error(e):
            log("Encryption failed — likely a stale macOS bond; self-healing")
            unpair_macos()
        return False

    if not client.is_connected:
        log("Connection failed (no error but not connected)")
        return False

    log("Connected")
    session = Session(client)
    await session.setup_refresh_subscription()

    last_poll = 0.0
    used_successfully = False
    try:
        while client.is_connected and not stop_event.is_set():
            now = time.time()
            elapsed = now - last_poll
            if session.refresh_requested.is_set() or elapsed >= POLL_INTERVAL:
                session.refresh_requested.clear()
                payload = await poll_active_payload()
                if payload is None:
                    log("No usable config dir this cycle")
                elif await session.write_payload(payload):
                    last_poll = time.time()
                    used_successfully = True
                    if once:
                        break

            try:
                await asyncio.wait_for(session.refresh_requested.wait(), timeout=TICK)
            except asyncio.TimeoutError:
                pass
    finally:
        try:
            await client.disconnect()
        except BleakError:
            pass

    log("Device disconnected" if not stop_event.is_set() else "Stopping")
    return used_successfully


async def main(once: bool = False) -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _stop(*_args: object) -> None:
        log("Daemon stopping")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    log("=== Claude Usage Tracker Daemon (BLE) ===")
    log(f"Poll interval: {POLL_INTERVAL}s")
    if once:
        log("Priming run: will exit after the first usage update is sent")

    backoff = 1
    skip_addr: str | None = None  # macOS: a peripheral to skip for one cycle
    while not stop_event.is_set():
        # Apply any pending skip exactly once, then clear it so the next
        # cycle re-tries retrieveConnected (the device may have recovered).
        target = await discover_target(skip_addr=skip_addr)
        skip_addr = None
        if not target:
            log(f"Device not found, retrying in {backoff}s...")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
            continue

        addr = target if isinstance(target, str) else target.address
        ok = await connect_and_run(target, stop_event, once=once)
        if once and ok:
            log("First usage update sent -- priming complete; the background task takes over now")
            return
        if not ok:
            if sys.platform == "darwin":
                # No string cache to drop; instead skip this stale handle on
                # the next retrieveConnected so the scan fallback is reachable.
                skip_addr = addr
            else:
                log("Invalidating cached address")
                SAVED_ADDR_FILE.unlink(missing_ok=True)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 60)
        else:
            backoff = 1


if __name__ == "__main__":
    run_once = "--once" in sys.argv[1:]
    # First non-flag positional arg is the log dir (passed by the installer
    # to the windowless background task). Flags start with "-".
    log_dir_arg = next((a for a in sys.argv[1:] if not a.startswith("-")), None)
    _redirect_windows_logs(log_dir_arg)
    try:
        asyncio.run(main(once=run_once))
    except KeyboardInterrupt:
        sys.exit(0)
