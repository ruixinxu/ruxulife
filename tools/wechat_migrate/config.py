"""Load and validate config.json for the WeChat migration tool."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    token: str
    cookie: str
    fakeid: str
    user_agent: str
    request_delay_seconds: float
    default_categories: list[str]
    default_tags: list[str]

    @staticmethod
    def load(path: Path) -> "Config":
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found at {path}. "
                f"Copy config.example.json to config.json and fill in your "
                f"token + cookie (see README.md)."
            )
        data = json.loads(path.read_text(encoding="utf-8"))

        token = (data.get("token") or "").strip()
        cookie = (data.get("cookie") or "").strip()
        if not token or token.startswith("PASTE_"):
            raise ValueError(
                "config.json is missing a real 'token' value. See README.md "
                "section 'Capturing the cookie and token'."
            )
        if not cookie or cookie.startswith("PASTE_"):
            raise ValueError(
                "config.json is missing a real 'cookie' value. See README.md "
                "section 'Capturing the cookie and token'."
            )

        return Config(
            token=token,
            cookie=cookie,
            fakeid=(data.get("fakeid") or "").strip(),
            user_agent=data.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            request_delay_seconds=float(data.get("request_delay_seconds", 4)),
            default_categories=list(data.get("default_categories") or ["wechat"]),
            default_tags=list(data.get("default_tags") or ["wechat"]),
        )
