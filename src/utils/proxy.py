"""
Proxy configuration helpers for ms-claude.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional


def resolve_upstream_proxy_url(config: Any) -> str:
    """Resolve the explicit upstream proxy URL for ms-claude.

    Priority:
    1. proxy.upstream_proxy_url in config
    2. MS_CLAUDE_UPSTREAM_PROXY_URL env var
    """
    proxy_url = ""
    if config is not None:
        try:
            proxy_url = config.get("proxy.upstream_proxy_url", "") or ""
        except Exception:
            proxy_url = ""

    if proxy_url:
        return proxy_url

    return os.environ.get("MS_CLAUDE_UPSTREAM_PROXY_URL", "") or ""


def apply_upstream_proxy(session: Any, proxy_url: str) -> None:
    """Apply an explicit upstream proxy to a requests session."""
    session.trust_env = False
    if proxy_url:
        session.proxies.update({"http": proxy_url, "https": proxy_url})


def build_script_env(base_env: Dict[str, str], proxy_url: Optional[str] = None) -> Dict[str, str]:
    """Build a clean environment for the model fetch script.

    We intentionally do not inherit HTTP(S)_PROXY from the shell so that
    ms-claude does not follow cc-switch's provider changes.
    """
    env = dict(base_env)
    for key in [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ]:
        env.pop(key, None)

    if proxy_url:
        env["MS_CLAUDE_UPSTREAM_PROXY_URL"] = proxy_url

    return env
