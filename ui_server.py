from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from dotenv import dotenv_values

from main import LIVE_CONFIRMATION, load_config


ROOT = Path(__file__).resolve().parent
UI_ROOT = ROOT / "ui"
DATA_ROOT = ROOT / "data"
SETTINGS_PATH = DATA_ROOT / "ui_settings.json"
GENERATED_CONFIG_PATH = DATA_ROOT / "ui_config.yaml"
MAX_LOG_LINES = 800
EARLIEST_HOUR = 8
LATEST_HOUR = 22


class SettingsError(ValueError):
    pass


def _group_label(group: dict[str, Any]) -> str:
    slots = group.get("slots", [])
    if len(slots) == 2:
        return f"{slots[0].split('-')[0]}–{slots[1].split('-')[1]}"
    return " / ".join(str(slot) for slot in slots)


def _custom_group(slots: list[str]) -> dict[str, Any]:
    start_hour = int(slots[0].split(":", 1)[0])
    end_hour = int(slots[-1].split("-", 1)[1].split(":", 1)[0])
    return {
        "name": f"custom_{start_hour:02d}_{end_hour:02d}",
        "label": f"{start_hour:02d}:00–{end_hour:02d}:00",
        "slots": slots,
        "enabled": True,
        "custom": True,
    }


def _validate_custom_slots(raw: Any) -> list[str]:
    if not isinstance(raw, list) or len(raw) != 2:
        raise SettingsError("自定义时间必须是连续两小时")
    for start_hour in range(EARLIEST_HOUR, LATEST_HOUR - 1):
        expected = [
            f"{start_hour:02d}:00-{start_hour + 1:02d}:00",
            f"{start_hour + 1:02d}:00-{start_hour + 2:02d}:00",
        ]
        if raw == expected:
            return expected
    raise SettingsError("自定义时间必须在 08:00–22:00 内且连续两小时")


def default_settings() -> dict[str, Any]:
    config = load_config()
    groups = sorted(config["booking"]["time_groups"], key=lambda item: item["priority"])
    return {
        "release_delay_seconds": 70,
        "wake_enabled": False,
        "venue_priority": [int(item) for item in config["booking"]["venue_priority"]],
        "time_groups": [
            {
                "name": str(group["name"]),
                "label": _group_label(group),
                "slots": list(group["slots"]),
                "enabled": True,
                "custom": False,
            }
            for group in groups
        ],
    }


def validate_settings(raw: Any) -> dict[str, Any]:
    defaults = default_settings()
    if not isinstance(raw, dict):
        raise SettingsError("设置格式不正确")

    try:
        delay = float(raw.get("release_delay_seconds", defaults["release_delay_seconds"]))
    except (TypeError, ValueError) as exc:
        raise SettingsError("延时必须是数字") from exc
    if not 0 <= delay <= 600:
        raise SettingsError("延时必须在 0 到 600 秒之间")

    default_venues = defaults["venue_priority"]
    venues_raw = raw.get("venue_priority", default_venues)
    try:
        venues = [int(item) for item in venues_raw]
    except (TypeError, ValueError) as exc:
        raise SettingsError("场地优先级格式不正确") from exc
    if len(venues) != len(default_venues) or set(venues) != set(default_venues):
        raise SettingsError("每个场地必须且只能出现一次")

    defaults_by_name = {group["name"]: group for group in defaults["time_groups"]}
    groups_raw = raw.get("time_groups", defaults["time_groups"])
    if not isinstance(groups_raw, list):
        raise SettingsError("时间段格式不正确")
    if not 1 <= len(groups_raw) <= 20 or not all(isinstance(item, dict) for item in groups_raw):
        raise SettingsError("时间段数量或格式不正确")
    names = [item.get("name") for item in groups_raw]
    if not set(defaults_by_name).issubset(names):
        raise SettingsError("时间段列表不完整")

    groups = []
    used_names: set[str] = set()
    used_slots: set[tuple[str, ...]] = set()
    for item in groups_raw:
        name = item.get("name")
        if name in defaults_by_name:
            group = {**defaults_by_name[name], "enabled": bool(item.get("enabled", True))}
        else:
            slots = _validate_custom_slots(item.get("slots"))
            group = {**_custom_group(slots), "enabled": bool(item.get("enabled", True))}
        slot_key = tuple(group["slots"])
        if group["name"] in used_names or slot_key in used_slots:
            raise SettingsError("同一个时间窗口不能重复添加")
        used_names.add(group["name"])
        used_slots.add(slot_key)
        groups.append(group)
    if not any(group["enabled"] for group in groups):
        raise SettingsError("请至少选择一个时间段")

    return {
        "release_delay_seconds": int(delay) if delay.is_integer() else delay,
        "wake_enabled": bool(raw.get("wake_enabled", defaults["wake_enabled"])),
        "venue_priority": venues,
        "time_groups": groups,
    }


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return default_settings()
    try:
        return validate_settings(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, SettingsError):
        return default_settings()


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    validated = validate_settings(settings)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(validated, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(SETTINGS_PATH)
    return validated


def write_effective_config(settings: dict[str, Any]) -> Path:
    config = copy.deepcopy(load_config())
    base_groups = {
        group["name"]: group for group in config["booking"]["time_groups"]
    }
    selected_groups = []
    for item in settings["time_groups"]:
        if not item["enabled"]:
            continue
        if item["name"] in base_groups:
            group = copy.deepcopy(base_groups[item["name"]])
        else:
            group = {"name": item["name"], "slots": list(item["slots"])}
        group["priority"] = len(selected_groups) + 1
        selected_groups.append(group)
    config["booking"]["venue_priority"] = settings["venue_priority"]
    config["booking"]["time_groups"] = selected_groups

    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = GENERATED_CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    temporary.replace(GENERATED_CONFIG_PATH)
    return GENERATED_CONFIG_PATH


def credentials_status() -> dict[str, bool]:
    values = dotenv_values(ROOT / ".env")
    return {
        "phone": bool(str(values.get("CONTACT_PHONE") or "").strip()),
        "username": bool(str(values.get("PORTAL_USERNAME") or "").strip()),
        "password": bool(str(values.get("PORTAL_PASSWORD") or "")),
    }


def configure_macos_wake(enabled: bool) -> None:
    if sys.platform != "darwin":
        raise SettingsError("自动唤醒设置目前仅支持 macOS")
    pmset_command = (
        "/usr/bin/pmset repeat wakeorpoweron MTWRFSU 07:57:00"
        if enabled
        else "/usr/bin/pmset repeat cancel"
    )
    apple_script = (
        'do shell script "' + pmset_command + '" with administrator privileges'
    )
    result = subprocess.run(
        ["/usr/bin/osascript", "-e", apple_script],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "用户取消或系统拒绝了操作").strip()
        raise SettingsError(f"自动唤醒设置未生效：{message}")


def build_command(settings: dict[str, Any], live: bool) -> list[str]:
    config_path = write_effective_config(settings)
    command = [
        sys.executable,
        str(ROOT / "main.py"),
        "--config",
        str(config_path),
        "--release-delay-seconds",
        str(settings["release_delay_seconds"]),
    ]
    if live:
        command.append("--live")
    else:
        command.extend(["--run-now", "--once"])

    if sys.platform == "darwin" and shutil.which("caffeinate"):
        return ["caffeinate", "-is", *command]
    if sys.platform.startswith("linux") and shutil.which("systemd-inhibit"):
        return [
            "systemd-inhibit",
            "--what=sleep",
            "--why=Badminton booking",
            *command,
        ]
    return command


class ProcessManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._logs: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_LINES)
        self._next_log_id = 1
        self._started_at: str | None = None
        self._mode: str | None = None
        self._return_code: int | None = None

    def _append_log(self, line: str) -> None:
        with self._lock:
            self._logs.append({"id": self._next_log_id, "line": line.rstrip()})
            self._next_log_id += 1

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self._append_log(line)
        return_code = process.wait()
        with self._lock:
            self._return_code = return_code
        self._append_log(f"[UI] 预约程序已结束，退出码：{return_code}")

    def start(self, settings: dict[str, Any], live: bool) -> dict[str, Any]:
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise SettingsError("已有一个预约任务正在运行")

        command = build_command(settings, live)
        environment = os.environ.copy()
        if live:
            environment["LIVE_BOOKING_CONFIRM"] = LIVE_CONFIRMATION
        popen_kwargs: dict[str, Any] = {
            "cwd": ROOT,
            "env": environment,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "text": True,
            "bufsize": 1,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        process = subprocess.Popen(command, **popen_kwargs)
        with self._lock:
            self._process = process
            self._logs.clear()
            self._next_log_id = 1
            self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._mode = "live" if live else "dry_run"
            self._return_code = None
        self._append_log(
            "[UI] 已开启真实预约任务" if live else "[UI] 已开启安全测试，不会提交预约"
        )
        threading.Thread(target=self._read_output, args=(process,), daemon=True).start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            raise SettingsError("当前没有正在运行的任务")
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        self._append_log("[UI] 已发送停止指令")
        return self.status()

    def status(self, since: int = 0) -> dict[str, Any]:
        with self._lock:
            process = self._process
            running = process is not None and process.poll() is None
            logs = [item for item in self._logs if item["id"] > since]
            return {
                "running": running,
                "pid": process.pid if running and process is not None else None,
                "mode": self._mode,
                "started_at": self._started_at,
                "return_code": self._return_code,
                "logs": logs,
                "last_log_id": self._next_log_id - 1,
            }

    def close(self) -> None:
        with self._lock:
            process = self._process
        if process is not None and process.poll() is None:
            try:
                if os.name == "nt":
                    process.terminate()
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass


MANAGER = ProcessManager()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "BadmintonDashboard/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise SettingsError("请求长度无效") from exc
        if length > 64 * 1024:
            raise SettingsError("请求过大")
        try:
            value = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as exc:
            raise SettingsError("请求内容不是有效 JSON") from exc
        if not isinstance(value, dict):
            raise SettingsError("请求格式不正确")
        return value

    def _static(self, requested_path: str) -> None:
        relative = "index.html" if requested_path == "/" else requested_path.lstrip("/")
        candidate = (UI_ROOT / relative).resolve()
        if UI_ROOT.resolve() not in candidate.parents and candidate != UI_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_types = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
        }
        body = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_types.get(candidate.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/config":
            creds = credentials_status()
            self._json(
                {
                    "settings": load_settings(),
                    "platform": sys.platform,
                    "wake_supported": sys.platform == "darwin",
                    "credentials_ready": all(creds.values()),
                    "credential_fields": creds,
                }
            )
            return
        if parsed.path == "/api/status":
            since = 0
            try:
                if parsed.query.startswith("since="):
                    since = int(parsed.query.split("=", 1)[1])
            except ValueError:
                pass
            self._json(MANAGER.status(since))
            return
        self._static(parsed.path)

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            if self.path == "/api/settings":
                self._json({"settings": save_settings(body.get("settings", body))})
                return
            if self.path == "/api/wake":
                settings = validate_settings(body.get("settings", {}))
                enabled = bool(settings["wake_enabled"])
                configure_macos_wake(enabled)
                settings = save_settings(settings)
                self._json(
                    {
                        "settings": settings,
                        "message": (
                            "已设置每天 07:57 自动唤醒"
                            if enabled
                            else "已取消 macOS 重复唤醒计划"
                        ),
                    }
                )
                return
            if self.path == "/api/start":
                live = bool(body.get("live", True))
                if live and body.get("confirmed") is not True:
                    raise SettingsError("请先确认这会创建真实预约")
                settings = save_settings(body.get("settings", {}))
                if live and not all(credentials_status().values()):
                    raise SettingsError(".env 中的手机号、账号或密码尚未填写完整")
                self._json(MANAGER.start(settings, live), HTTPStatus.CREATED)
                return
            if self.path == "/api/stop":
                self._json(MANAGER.stop())
                return
            self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
        except SettingsError as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._json({"error": f"操作失败：{exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local badminton booking dashboard")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true", help="Do not open the browser automatically")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), DashboardHandler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"预约控制台已启动：{url}", flush=True)
    print("请保持此终端窗口开启；按 Control+C 关闭。", flush=True)
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MANAGER.close()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
