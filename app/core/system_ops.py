"""Host-level operations for the Fleet Commander Pi (git update, time/NTP)."""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Repo root = parent of the `app/` package, overridable for atypical installs.
_DEFAULT_REPO = Path(__file__).resolve().parents[2]
REPO_DIR = os.environ.get("FLEET_REPO_DIR", str(_DEFAULT_REPO))
SERVICE_NAME = os.environ.get("FLEET_SERVICE_NAME", "fleetcontrol")

# systemd units often set PATH to only the venv; host tools live under /usr/bin.
_SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


def _host_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    current = env.get("PATH", "")
    parts = [p for p in (_SYSTEM_PATH.split(":") + current.split(":")) if p]
    # Preserve order, drop duplicates
    seen = set()
    ordered: List[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    env["PATH"] = ":".join(ordered)
    if extra:
        env.update(extra)
    return env


def _which(name: str, fallback: Optional[str] = None) -> Optional[str]:
    path = _host_env().get("PATH", _SYSTEM_PATH)
    found = shutil.which(name, path=path)
    if found:
        return found
    if fallback and os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def _needs_sudo() -> bool:
    try:
        return os.geteuid() != 0
    except AttributeError:
        return True


def _privileged_prefix() -> List[str]:
    """Empty when root; otherwise ``sudo -n`` (never hang on a password prompt)."""
    if not _needs_sudo():
        return []
    sudo_bin = _which("sudo", "/usr/bin/sudo")
    if not sudo_bin:
        raise FileNotFoundError(
            "sudo is not available on PATH. The Fleet Commander service PATH "
            "must include /usr/bin (see setup.sh), or run the service as root."
        )
    return [sudo_bin, "-n"]


def _run_host(
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: Optional[float] = 60,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(args),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env or _host_env(),
        check=check,
    )


def _format_called_process_error(exc: subprocess.CalledProcessError) -> str:
    stderr = (exc.stderr or "").strip()
    stdout = (exc.stdout or "").strip()
    detail = stderr or stdout or str(exc)
    cmd = " ".join(str(c) for c in (exc.cmd or []))
    low = detail.lower()
    if "password is required" in low or "a password is required" in low or "no askpass" in low:
        return (
            "Passwordless sudo is required for timedatectl/date/systemctl. "
            "Grant the fleet service user NOPASSWD for those commands, or run "
            f"the service as root.\n\n{detail}"
        )
    return f"OS command failed ({cmd}): {detail}"


def run_git_update() -> Dict[str, Any]:
    """
    Run ``git pull --ff-only`` in the Fleet Commander install directory.

    Returns the same shape as a module ``POST /api/update`` result:
    ``result``, ``code``, ``message``, ``changed``, ``can_force``.
    """
    git_bin = _which("git", "/usr/bin/git")
    if not git_bin:
        return {
            "result": False,
            "code": "git_unavailable",
            "message": (
                "Git is not available on this Fleet Commander. The fleetcontrol "
                "systemd unit often sets PATH to only the venv — include /usr/bin "
                "(see setup.sh), then restart the service."
            ),
            "changed": False,
            "can_force": False,
        }

    env = _host_env(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
        }
    )

    try:
        result = _run_host(
            [
                git_bin,
                "-c",
                f"safe.directory={REPO_DIR}",
                "-C",
                REPO_DIR,
                "pull",
                "--ff-only",
            ],
            check=False,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {
            "result": False,
            "code": "timeout",
            "message": (
                "The update timed out after 2 minutes. This usually means a slow "
                "or dropped internet connection. Please try again."
            ),
            "changed": False,
            "can_force": False,
        }
    except FileNotFoundError:
        return {
            "result": False,
            "code": "git_unavailable",
            "message": (
                "Git is not available on this Fleet Commander. Update the "
                "fleetcontrol service PATH to include /usr/bin (see setup.sh), "
                "then restart the service."
            ),
            "changed": False,
            "can_force": False,
        }

    out = (result.stdout or "").strip()
    err = (result.stderr or "").strip()
    combined = "\n".join(part for part in (out, err) if part).strip()
    low = combined.lower()

    if result.returncode == 0:
        if "already up to date" in low or "already up-to-date" in low:
            return {
                "result": True,
                "code": "up_to_date",
                "message": "You are already running the latest version. No update was needed.",
                "changed": False,
                "can_force": False,
            }
        summary = out or "Changes were pulled from the remote repository."
        return {
            "result": True,
            "code": "updated",
            "message": (
                "The latest code has been pulled. The Fleet Commander service "
                f"will restart to load the new version.\n\n{summary}"
            ),
            "changed": True,
            "can_force": False,
        }

    if any(
        marker in low
        for marker in (
            "authentication failed",
            "permission denied (publickey)",
            "permission denied (keyboard-interactive)",
            "invalid username or password",
            "terminal prompts disabled",
            "could not read username",
            "access denied",
            "403 forbidden",
            "error: 403",
            "authentication required",
        )
    ):
        return {
            "result": False,
            "code": "authentication_error",
            "message": (
                "Git authentication failed. This Fleet Commander could not log "
                "in to the remote. Check SSH keys or HTTPS credentials."
            ),
            "changed": False,
            "can_force": False,
        }

    if any(
        marker in low
        for marker in (
            "could not resolve host",
            "unable to access",
            "connection timed out",
            "network is unreachable",
            "temporary failure in name resolution",
            "failed to connect",
            "connection refused",
            "could not read from remote repository",
        )
    ):
        return {
            "result": False,
            "code": "network_error",
            "message": (
                "This Fleet Commander cannot reach the Git remote. Check the "
                "network connection and try again."
            ),
            "changed": False,
            "can_force": False,
        }

    if any(
        marker in low
        for marker in (
            "permission denied",
            "operation not permitted",
            "read-only file system",
            "insufficient permission",
        )
    ):
        return {
            "result": False,
            "code": "permission_error",
            "message": (
                "The Git working tree is not writable by the Fleet Commander "
                "service. Check ownership of the install directory."
            ),
            "changed": False,
            "can_force": False,
        }

    if any(
        marker in low
        for marker in (
            "not a git repository",
            "no such remote",
            "would be overwritten",
            "local changes",
            "not possible to fast-forward",
            "diverging",
            "non-fast-forward",
            "unmerged",
            "needs merge",
            "failed to push some refs",
            "fetch first",
            "couldn't find remote ref",
            "does not appear to be a git repository",
        )
    ):
        return {
            "result": False,
            "code": "repository_error",
            "message": (
                "A safe git pull is not possible. This install has local changes, "
                "a diverged branch, or a misconfigured remote. Update the "
                f"repository manually.\n\n{combined}"
            ),
            "changed": False,
            "can_force": False,
        }

    return {
        "result": False,
        "code": "git_error",
        "message": combined or "Git reported an unexpected error while updating.",
        "changed": False,
        "can_force": False,
    }


def schedule_service_restart(service: Optional[str] = None) -> None:
    """Restart the fleet systemd unit after the HTTP response can flush."""
    name = service or SERVICE_NAME
    systemctl = _which("systemctl", "/usr/bin/systemctl") or "systemctl"
    try:
        prefix = _privileged_prefix()
    except FileNotFoundError:
        prefix = ["sudo", "-n"]
    # Delayed so the HTTP response can flush first.
    cmd = f"(sleep 1; {' '.join(prefix)} {systemctl} restart {name}) &"
    subprocess.Popen(cmd, shell=True, env=_host_env())


def apply_system_time_config(
    mode: str,
    date_str: Optional[str] = None,
    timezone: Optional[str] = None,
    ntp_server: Optional[str] = None,
) -> Tuple[bool, str]:
    """Apply timezone / NTP / manual clock on the commander host via timedatectl."""
    try:
        prefix = _privileged_prefix()
        timedatectl = _which("timedatectl", "/usr/bin/timedatectl")
        date_bin = _which("date", "/usr/bin/date")
        sed_bin = _which("sed", "/usr/bin/sed")
        systemctl = _which("systemctl", "/usr/bin/systemctl")

        missing = [
            name
            for name, path in (
                ("timedatectl", timedatectl),
                ("date", date_bin),
                ("sed", sed_bin),
                ("systemctl", systemctl),
            )
            if path is None
        ]
        if missing:
            return (
                False,
                (
                    f"Missing system tools on PATH: {', '.join(missing)}. "
                    "Update the fleetcontrol systemd PATH to include /usr/bin "
                    "(see setup.sh), then restart the service."
                ),
            )

        if timezone:
            _run_host([*prefix, timedatectl, "set-timezone", timezone], check=True)
            if "TZ" in os.environ:
                del os.environ["TZ"]
            time.tzset()

        if mode == "network":
            target_server = ntp_server if ntp_server else "pool.ntp.org"

            try:
                client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                client.settimeout(2.0)
                client.sendto(b"\x1b" + 47 * b"\0", (target_server, 123))
                client.recvfrom(1024)
                client.close()
            except OSError as exc:
                if "client" in locals():
                    client.close()
                return (
                    False,
                    f"Cannot reach NTP server on UDP port 123: {target_server} ({exc})",
                )

            if ntp_server:
                config_line = f"NTP={ntp_server}"
                _run_host(
                    [
                        *prefix,
                        sed_bin,
                        "-i",
                        f"s/^#*NTP=.*/{config_line}/",
                        "/etc/systemd/timesyncd.conf",
                    ],
                    check=True,
                )

            _run_host([*prefix, timedatectl, "set-ntp", "true"], check=True)
            _run_host([*prefix, systemctl, "restart", "systemd-timesyncd"], check=True)

        elif mode == "manual" and date_str:
            _run_host([*prefix, timedatectl, "set-ntp", "false"], check=True)
            _run_host([*prefix, date_bin, "-s", date_str], check=True)
        else:
            return False, "Invalid time mode or missing date for manual mode."

        return True, "Time configuration applied successfully."

    except FileNotFoundError as e:
        return False, str(e)
    except PermissionError as e:
        return False, f"Permission denied while changing system time: {e}"
    except subprocess.TimeoutExpired:
        return False, "Timed out while applying time configuration."
    except subprocess.CalledProcessError as e:
        return False, _format_called_process_error(e)


def _read_timesyncd_ntp_server() -> str:
    conf_path = "/etc/systemd/timesyncd.conf"
    try:
        with open(conf_path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("NTP=") and not stripped.startswith("#"):
                    value = stripped.split("=", 1)[1].strip()
                    if value:
                        return value.split()[0]
    except OSError:
        pass
    return "pool.ntp.org"


def _timedatectl_show() -> Dict[str, str]:
    timedatectl = _which("timedatectl", "/usr/bin/timedatectl")
    if not timedatectl:
        return {}
    try:
        result = _run_host([timedatectl, "show"], check=False, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return {}

    props: Dict[str, str] = {}
    if result.returncode != 0:
        return props
    for line in (result.stdout or "").splitlines():
        if "=" in line:
            key, val = line.split("=", 1)
            props[key.strip()] = val.strip()
    return props


def get_commander_time_status() -> Dict[str, Any]:
    """Snapshot of the commander host clock / NTP configuration."""
    props = _timedatectl_show()
    ntp_raw = props.get("NTP", props.get("NetworkTimeProtocol", ""))
    ntp_enabled = ntp_raw.lower() in ("yes", "true", "1", "ntp")

    # Fallback: StatusText / older timedatectl
    if not ntp_raw:
        timedatectl = _which("timedatectl", "/usr/bin/timedatectl")
        if timedatectl:
            try:
                status = _run_host([timedatectl, "status"], check=False, timeout=5)
                text = status.stdout or ""
                match = re.search(r"NTP service:\s*(\w+)", text, re.I)
                if match:
                    ntp_enabled = match.group(1).lower() == "active"
                tz_match = re.search(r"Time zone:\s*(\S+)", text)
                if tz_match and "Timezone" not in props:
                    props["Timezone"] = tz_match.group(1)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass

    timezone = props.get("Timezone") or time.tzname[0] or "UTC"
    return {
        "local_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": timezone,
        "ntp_enabled": ntp_enabled,
        "ntp_server": _read_timesyncd_ntp_server(),
        "mode": "network" if ntp_enabled else "manual",
    }


def _list_host_addresses() -> List[Dict[str, str]]:
    """Return non-loopback IPv4 addresses with interface names when possible."""
    addresses: List[Dict[str, str]] = []
    try:
        result = _run_host(
            ["ip", "-4", "-o", "addr", "show"],
            check=False,
            timeout=5,
        )
        for line in (result.stdout or "").splitlines():
            # e.g. "2: eth0    inet 10.0.0.5/24 ..."
            parts = line.split()
            if len(parts) < 4 or parts[2] != "inet":
                continue
            iface = parts[1].rstrip(":")
            cidr = parts[3]
            ip = cidr.split("/", 1)[0]
            if ip.startswith("127."):
                continue
            addresses.append({"interface": iface, "address": ip, "cidr": cidr})
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass

    if addresses:
        return addresses

    # Fallback: hostname -I
    try:
        result = _run_host(["hostname", "-I"], check=False, timeout=5)
        for ip in (result.stdout or "").split():
            if ip and not ip.startswith("127."):
                addresses.append({"interface": "?", "address": ip, "cidr": ip})
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        pass
    return addresses


def get_commander_network_status() -> Dict[str, Any]:
    """Discovery subnet, host addresses, and a coarse install-mode hint."""
    from app.core import state as fleet_state

    addresses = _list_host_addresses()
    subnet = fleet_state.TARGET_SUBNET_BASE
    has_ap_ip = any(
        a.get("address") == "192.168.50.1" or a.get("interface") == "wlan0"
        for a in addresses
    )
    if subnet == "192.168.50" or any(a.get("address") == "192.168.50.1" for a in addresses):
        mode_hint = "hotspot"
    elif has_ap_ip and subnet.startswith("192.168.50"):
        mode_hint = "hotspot"
    elif addresses:
        mode_hint = "consumer"
    else:
        mode_hint = "unknown"

    return {
        "subnet": subnet,
        "addresses": addresses,
        "mode_hint": mode_hint,
        "runtime_env_path": str(fleet_state.RUNTIME_ENV_PATH),
        "persisted_in_file": fleet_state.has_persisted_subnet(),
    }
