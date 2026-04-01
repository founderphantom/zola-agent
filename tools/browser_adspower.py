#!/usr/bin/env python3
"""
AdsPower Browser Adapter

Integrates AdsPower's Local API with hermes-agent for multi-account
anti-detect browser automation.  Each AdsPower profile has pre-configured
cookies, proxies, and fingerprints — this module launches profiles via the
API and connects browser-use (https://github.com/browser-use/browser-use)
over CDP for autonomous page interaction.

Architecture:
    hermes-agent (Kimi K2.5) → adspower_browse tool
        → AdsPower V2 API (start profile) → ws:// CDP URL
        → browser-use Browser(cdp_url=...) + Agent(task=..., llm=...)
        → AdsPower V1 API (stop profile)

Environment Variables:
    ADSPOWER_API_URL:  AdsPower Local API base URL
                       (default: http://127.0.0.1:50325)
    ADSPOWER_API_KEY:  Optional API key for AdsPower auth
    BROWSER_USE_LLM_MODEL:  LLM model for browser-use Agent
                            (default: moonshotai/kimi-k2.5)
    OPENROUTER_API_KEY:  API key for the LLM provider

Config File:
    ~/.hermes/adspower_accounts.json  — maps account names to profile IDs
"""

import asyncio
import atexit
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import requests

from tools.registry import registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------

_DEFAULT_API_URL = "http://172.22.0.1:50326"


def _get_api_url() -> str:
    return os.getenv("ADSPOWER_API_URL", _DEFAULT_API_URL).rstrip("/")


def _get_api_key() -> Optional[str]:
    return os.getenv("ADSPOWER_API_KEY")


def _get_hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


def _load_accounts() -> list:
    """Load account list from ~/.hermes/adspower_accounts.json."""
    config_path = _get_hermes_home() / "adspower_accounts.json"
    if not config_path.exists():
        return []
    try:
        data = json.loads(config_path.read_text())
        return data.get("accounts", [])
    except Exception as e:
        logger.error("Failed to read adspower_accounts.json: %s", e)
        return []


# ---------------------------------------------------------------------------
# URL rewriting (WSL2 → Windows host)
# ---------------------------------------------------------------------------

def _rewrite_ws_url(ws_url: str) -> str:
    """Replace 127.0.0.1/localhost in AdsPower's WS URL with the API host.

    AdsPower returns ws://127.0.0.1:XXXXX/devtools/browser/UUID but from
    WSL2, 127.0.0.1 means the Linux VM itself.  We swap in the host from
    ADSPOWER_API_URL (the Windows host gateway IP) so CDP connects correctly.
    """
    api_host = urlparse(_get_api_url()).hostname
    if not api_host or api_host in ("127.0.0.1", "localhost"):
        return ws_url  # No rewriting needed when running on same host
    rewritten = ws_url.replace("127.0.0.1", api_host).replace("localhost", api_host)
    if rewritten != ws_url:
        logger.info("Rewrote WebSocket URL: %s → %s", ws_url, rewritten)
    return rewritten


# ---------------------------------------------------------------------------
# AdsPower Local API helpers
# ---------------------------------------------------------------------------

def _api_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = _get_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _start_profile(profile_id: str) -> str:
    """Start an AdsPower profile via V2 API, return rewritten WebSocket URL."""
    api_url = _get_api_url()
    resp = requests.post(
        f"{api_url}/api/v2/browser-profile/start",
        headers=_api_headers(),
        json={
            "profile_id": profile_id,
            "last_opened_tabs": "0",
            "proxy_detection": "0",
            "cdp_mask": "1",
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(
            f"AdsPower start failed for profile {profile_id}: "
            f"{data.get('msg', 'unknown error')}"
        )

    ws_url = data["data"]["ws"]["puppeteer"]
    logger.info("AdsPower profile %s started, CDP port: %s",
                profile_id, data["data"].get("debug_port", "?"))
    return _rewrite_ws_url(ws_url)


def _stop_profile(profile_id: str) -> bool:
    """Stop an AdsPower profile via V1 API."""
    api_url = _get_api_url()
    try:
        resp = requests.get(
            f"{api_url}/api/v1/browser/stop",
            params={"user_id": profile_id},
            headers=_api_headers(),
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            logger.info("AdsPower profile %s stopped", profile_id)
            return True
        logger.warning("AdsPower stop returned code %s: %s",
                       data.get("code"), data.get("msg"))
        return False
    except Exception as e:
        logger.error("Failed to stop AdsPower profile %s: %s", profile_id, e)
        return False


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

_active_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()


def _cleanup_all_sessions():
    """Emergency cleanup at exit — stop all AdsPower profiles."""
    with _sessions_lock:
        for name, session in list(_active_sessions.items()):
            try:
                _stop_profile(session["profile_id"])
            except Exception:
                pass
        _active_sessions.clear()


atexit.register(_cleanup_all_sessions)


def _resolve_account(account_name: Optional[str]) -> Dict[str, Any]:
    """Get session for account_name, or the sole active session."""
    with _sessions_lock:
        if account_name and account_name in _active_sessions:
            return _active_sessions[account_name]
        if not account_name and len(_active_sessions) == 1:
            return next(iter(_active_sessions.values()))
    raise KeyError(
        f"No active session for '{account_name or '(none)'}'. "
        "Call adspower_browse to launch a profile first."
    )


# ---------------------------------------------------------------------------
# browser-use integration
# ---------------------------------------------------------------------------

async def _run_browser_task(cdp_url: str, task: str, max_steps: int) -> dict:
    """Connect browser-use to an AdsPower CDP endpoint and run a task."""
    from browser_use import Agent, Browser
    from browser_use.llm.openrouter.chat import ChatOpenRouter

    llm = ChatOpenRouter(
        model=os.getenv("BROWSER_USE_LLM_MODEL", "moonshotai/kimi-k2.5"),
    )

    browser = Browser(cdp_url=cdp_url)
    agent = Agent(task=task, llm=llm, browser=browser)
    result = await agent.run(max_steps=max_steps)

    extracted = ""
    steps = 0
    if hasattr(result, "extracted_content"):
        content = result.extracted_content()
        extracted = "\n".join(content) if isinstance(content, list) else str(content)
    if hasattr(result, "model_actions"):
        steps = len(result.model_actions())

    return {"extracted_content": extracted, "steps": steps}


def _run_async(coro):
    """Run an async coroutine from a sync context."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

def _handle_list_accounts(args: dict, **kw) -> str:
    accounts = _load_accounts()
    if not accounts:
        config_path = _get_hermes_home() / "adspower_accounts.json"
        return json.dumps({
            "success": False,
            "error": (
                f"No accounts configured. Create {config_path} with format: "
                '{"accounts": [{"name": "Account 1", "profile_id": "xxx", '
                '"description": "..."}]}'
            ),
        })

    result = []
    with _sessions_lock:
        for acct in accounts:
            result.append({
                "name": acct.get("name", "unnamed"),
                "profile_id": acct.get("profile_id", ""),
                "description": acct.get("description", ""),
                "active": acct.get("name", "") in _active_sessions,
            })

    return json.dumps({"success": True, "accounts": result})


def _handle_browse(args: dict, **kw) -> str:
    account_name = args.get("account_name", "")
    task = args.get("task", "")
    max_steps = args.get("max_steps", 50)

    if not account_name:
        return json.dumps({"success": False, "error": "account_name is required"})
    if not task:
        return json.dumps({"success": False, "error": "task is required"})

    # Look up profile_id from config
    accounts = _load_accounts()
    profile_id = None
    for acct in accounts:
        if acct.get("name") == account_name:
            profile_id = acct.get("profile_id")
            break
    if not profile_id:
        names = [a.get("name") for a in accounts]
        return json.dumps({
            "success": False,
            "error": f"Account '{account_name}' not found. Available: {names}",
        })

    # Start profile (or reuse existing session)
    with _sessions_lock:
        session = _active_sessions.get(account_name)

    ws_url = None
    if session:
        ws_url = session.get("ws_url")
        logger.info("Reusing existing session for %s", account_name)
    else:
        try:
            ws_url = _start_profile(profile_id)
        except requests.ConnectionError:
            return json.dumps({
                "success": False,
                "error": (
                    f"Cannot reach AdsPower at {_get_api_url()}. "
                    "Ensure AdsPower is running on the Windows host and "
                    "ADSPOWER_API_URL is correct."
                ),
            })
        except Exception as e:
            return json.dumps({"success": False, "error": str(e)})

        with _sessions_lock:
            _active_sessions[account_name] = {
                "profile_id": profile_id,
                "ws_url": ws_url,
            }

    # Run browser-use Agent
    try:
        result = _run_async(_run_browser_task(ws_url, task, max_steps))
        return json.dumps({
            "success": True,
            "account": account_name,
            "task": task,
            "result": result.get("extracted_content", ""),
            "steps_taken": result.get("steps", 0),
        })
    except Exception as e:
        logger.exception("browser-use task failed for %s: %s", account_name, e)
        return json.dumps({
            "success": False,
            "error": f"Browser task failed: {type(e).__name__}: {e}",
        })


def _handle_close(args: dict, **kw) -> str:
    account_name = args.get("account_name")
    closed = []

    with _sessions_lock:
        if account_name:
            targets = {account_name: _active_sessions.pop(account_name, None)}
        else:
            targets = dict(_active_sessions)
            _active_sessions.clear()

    for name, session in targets.items():
        if session is None:
            continue
        _stop_profile(session["profile_id"])
        closed.append(name)

    if not closed:
        return json.dumps({
            "success": True,
            "message": "No active sessions to close.",
            "closed": [],
        })
    return json.dumps({"success": True, "closed": closed})


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

ADSPOWER_LIST_ACCOUNTS_SCHEMA = {
    "name": "adspower_list_accounts",
    "description": (
        "List available AdsPower browser profiles for Facebook Marketplace "
        "automation. Shows account names, profile IDs, and whether each has "
        "an active browser session."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

ADSPOWER_BROWSE_SCHEMA = {
    "name": "adspower_browse",
    "description": (
        "Launch an AdsPower anti-detect browser profile and run a browser "
        "automation task. The task is executed autonomously by a browser "
        "agent that navigates pages, clicks, types, and fills forms. "
        "The profile's cookies, proxy, and fingerprint are pre-configured "
        "in AdsPower — do NOT modify them. Always call adspower_close when done."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_name": {
                "type": "string",
                "description": (
                    "Account name from the config "
                    "(use adspower_list_accounts to see available names)"
                ),
            },
            "task": {
                "type": "string",
                "description": (
                    "Natural language task for the browser agent, e.g. "
                    "'Navigate to Facebook Marketplace and create a new "
                    "listing for a 2BR apartment at $1500/month with these "
                    "photos: ...'"
                ),
            },
            "max_steps": {
                "type": "integer",
                "description": "Maximum browser actions (default: 50)",
                "default": 50,
            },
        },
        "required": ["account_name", "task"],
    },
}

ADSPOWER_CLOSE_SCHEMA = {
    "name": "adspower_close",
    "description": (
        "Close an AdsPower browser profile session. Stops the browser via "
        "AdsPower API to free resources on the Windows host. "
        "Closes all active sessions if no account_name is specified."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "account_name": {
                "type": "string",
                "description": (
                    "Account name to close. Omit to close all active sessions."
                ),
            },
        },
        "required": [],
    },
}

# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def check_adspower_requirements() -> bool:
    """Return True when AdsPower tools should be available.

    Requires ADSPOWER_API_URL to be set (or uses default) and the accounts
    config file to exist.  browser-use + langchain-openai are checked at
    call time, not registration time, so they don't block other tools.
    """
    config_path = _get_hermes_home() / "adspower_accounts.json"
    return config_path.exists()


# ---------------------------------------------------------------------------
# Registry registration
# ---------------------------------------------------------------------------

registry.register(
    name="adspower_list_accounts",
    toolset="adspower",
    schema=ADSPOWER_LIST_ACCOUNTS_SCHEMA,
    handler=_handle_list_accounts,
    check_fn=check_adspower_requirements,
    requires_env=[],
    is_async=False,
    description="List AdsPower browser profiles",
    emoji="📋",
)

registry.register(
    name="adspower_browse",
    toolset="adspower",
    schema=ADSPOWER_BROWSE_SCHEMA,
    handler=_handle_browse,
    check_fn=check_adspower_requirements,
    requires_env=["OPENROUTER_API_KEY"],
    is_async=False,
    description="Run browser task on AdsPower profile",
    emoji="🌐",
)

registry.register(
    name="adspower_close",
    toolset="adspower",
    schema=ADSPOWER_CLOSE_SCHEMA,
    handler=_handle_close,
    check_fn=check_adspower_requirements,
    requires_env=[],
    is_async=False,
    description="Close AdsPower browser session",
    emoji="🛑",
)
