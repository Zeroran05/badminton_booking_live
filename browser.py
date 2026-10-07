from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import BrowserContext, Playwright, Request, Response


SENSITIVE_KEYS = re.compile(
    r"(token|cookie|authorization|password|passwd|secret|ticket|session|credential)",
    re.I,
)


class NetworkObserver:
    def __init__(self, logger: logging.Logger, output: Path) -> None:
        self.logger = logger
        self.output = output
        self.events: list[dict[str, Any]] = []

    @staticmethod
    def _parameter_names(request: Request) -> list[str]:
        names = {key for key, _ in parse_qsl(urlsplit(request.url).query)}
        body = request.post_data or ""
        if body:
            try:
                data = json.loads(body)
                if isinstance(data, dict):
                    names.update(str(k) for k in data)
            except (ValueError, TypeError):
                names.update(key for key, _ in parse_qsl(body))
        return sorted(n for n in names if not SENSITIVE_KEYS.search(n))

    def on_request(self, request: Request) -> None:
        if request.resource_type not in {"xhr", "fetch"}:
            return
        if urlsplit(request.url).hostname == "ids.hit.edu.cn":
            return
        event = {
            "kind": "request",
            "method": request.method,
            "url": request.url.split("?", 1)[0].split("#", 1)[0],
            "resource_type": request.resource_type,
            "parameter_names": self._parameter_names(request),
        }
        self.events.append(event)
        self.logger.info(
            "Network %s %s params=%s",
            request.method,
            event["url"],
            event["parameter_names"],
        )

    async def on_response(self, response: Response) -> None:
        if response.request.resource_type not in {"xhr", "fetch"}:
            return
        if urlsplit(response.url).hostname == "ids.hit.edu.cn":
            return
        event: dict[str, Any] = {
            "kind": "response",
            "status": response.status,
            "url": response.url.split("?", 1)[0].split("#", 1)[0],
        }
        content_type = (await response.all_headers()).get("content-type", "")
        if "json" in content_type:
            try:
                payload = await response.json()
                event["json_structure"] = _json_structure(payload)
            except Exception:
                event["json_structure"] = "unreadable"
        self.events.append(event)

    def save(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_text(
            json.dumps(self.events, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _json_structure(value: Any, depth: int = 0) -> Any:
    if depth >= 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {
            str(k): _json_structure(v, depth + 1)
            for k, v in list(value.items())[:30]
            if not SENSITIVE_KEYS.search(str(k))
        }
    if isinstance(value, list):
        return [(_json_structure(value[0], depth + 1) if value else "empty")]
    return type(value).__name__


async def create_browser(
    playwright: Playwright, config: dict[str, Any], root: Path, logger: logging.Logger
) -> BrowserContext:
    browser_cfg = config["browser"]
    profile = (root / browser_cfg["profile_dir"]).resolve()
    profile.mkdir(parents=True, exist_ok=True)
    logger.info("Starting Microsoft Edge")
    logger.info("Using persistent profile: %s", profile)
    return await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel=browser_cfg.get("channel", "msedge"),
        headless=bool(browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 500)),
        viewport={"width": 1440, "height": 1000},
        accept_downloads=False,
    )


async def install_dry_run_network_guard(
    context: BrowserContext, config: dict[str, Any], logger: logging.Logger
) -> None:
    allow_patterns = [
        re.compile(item) for item in config.get("safety", {}).get("read_only_post_url_patterns", [])
    ]

    async def guard(route: Any, request: Request) -> None:
        if request.method in {"GET", "HEAD", "OPTIONS"}:
            await route.continue_()
            return
        if request.method == "POST" and any(p.search(request.url) for p in allow_patterns):
            await route.continue_()
            return
        logger.warning("DRY_RUN_BLOCKED %s %s", request.method, request.url.split("?", 1)[0])
        await route.abort("blockedbyclient")

    await context.route("**/*", guard)
    logger.info("Dry-run network guard enabled: state-changing requests are blocked")
