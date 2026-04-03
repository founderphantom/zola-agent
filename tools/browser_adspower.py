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
import subprocess
import threading
import time
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
# WSL2 → Windows CDP bridge
# ---------------------------------------------------------------------------

def _start_cdp_proxy(port: int) -> Optional[subprocess.Popen]:
    """Spawn a Windows-side TCP relay that bridges 0.0.0.0:PORT → 127.0.0.1:PORT.

    Problem: AdsPower Chrome binds CDP to Windows 127.0.0.1:PORT only.
    From WSL2, 127.0.0.1 is the Linux VM loopback — not Windows.
    172.22.0.1 is the Windows WSL adapter, but Chrome is NOT listening there.

    Solution: powershell.exe called from WSL2 runs on Windows as the current
    user (no admin required for user-space ports >1024).  It binds a TCP
    listener on 0.0.0.0:PORT on Windows, which accepts the connection arriving
    on 172.22.0.1:PORT from WSL2 and relays it to 127.0.0.1:PORT where Chrome
    is actually listening.
    """
    api_host = urlparse(_get_api_url()).hostname
    if not api_host or api_host in ("127.0.0.1", "localhost"):
        return None  # Running natively on Windows — no bridge needed

    # Bind to api_host specifically (e.g. 172.22.0.1), NOT 0.0.0.0.
    # Chrome already holds 127.0.0.1:PORT — binding Any would conflict.
    # Binding to a different IP on the same port is allowed.
    ps = (
        f"$l=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse('{api_host}'),{port});"
        "try{$l.Start()}catch{exit};"
        "while($true){"
        "$c=$l.AcceptTcpClient();"
        f"$t=New-Object Net.Sockets.TcpClient('127.0.0.1',{port});"
        "$cs=$c.GetStream();$ts=$t.GetStream();"
        "[void][Threading.Tasks.Task]::Run([Action]{try{$cs.CopyTo($ts)}catch{}});"
        "[void][Threading.Tasks.Task]::Run([Action]{try{$ts.CopyTo($cs)}catch{}})}"
    )
    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-Command", ps],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.5)  # let the listener bind before browser-use connects
        logger.info("CDP proxy started on Windows port %d → 127.0.0.1:%d (pid %d)",
                    port, port, proc.pid)
        return proc
    except FileNotFoundError:
        logger.error("powershell.exe not found — is this running in WSL2?")
        return None
    except Exception as e:
        logger.warning("CDP proxy failed for port %d: %s", port, e)
        return None


# ---------------------------------------------------------------------------
# AdsPower Local API helpers
# ---------------------------------------------------------------------------

def _api_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = _get_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _start_profile(profile_id: str) -> tuple[str, Optional[subprocess.Popen]]:
    """Start an AdsPower profile via V2 API.

    Returns (ws_url, proxy_proc) where ws_url has 127.0.0.1 rewritten to the
    Windows host IP and proxy_proc is a PowerShell TCP relay on Windows that
    forwards 0.0.0.0:PORT → 127.0.0.1:PORT, or None when no bridge is needed.
    """
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
    port = urlparse(ws_url).port
    proxy_proc = _start_cdp_proxy(port) if port else None

    # Rewrite 127.0.0.1 → Windows host IP so browser-use reaches the proxy.
    # The proxy (PowerShell) listens on 0.0.0.0:PORT on Windows and forwards
    # to 127.0.0.1:PORT where Chrome is actually running.
    api_host = urlparse(_get_api_url()).hostname or "127.0.0.1"
    rewritten = ws_url.replace("127.0.0.1", api_host).replace("localhost", api_host)
    if rewritten != ws_url:
        logger.info("CDP URL rewritten: %s → %s", ws_url, rewritten)

    logger.info("AdsPower profile %s started, CDP port: %s", profile_id, port or "?")
    return rewritten, proxy_proc


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
    """Emergency cleanup at exit — stop all AdsPower profiles and socat proxies."""
    with _sessions_lock:
        for name, session in list(_active_sessions.items()):
            try:
                proxy = session.get("proxy_proc")
                if proxy:
                    proxy.terminate()
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

async def _run_browser_task(
    cdp_url: str, task: str, max_steps: int,
    file_paths: Optional[list] = None,
) -> dict:
    """Connect browser-use to an AdsPower CDP endpoint and run a task."""
    from browser_use import Agent, Browser, Tools, ActionResult, BrowserSession
    from browser_use.llm.openrouter.chat import ChatOpenRouter

    llm = ChatOpenRouter(
        model=os.getenv("BROWSER_USE_LLM_MODEL", "moonshotai/kimi-k2.5"),
        api_key=os.getenv("OPENROUTER_API_KEY"),
    )

    # -- Custom tools for handling lazy-loaded / infinite-scroll pages ------
    # CDP synthesized scroll gestures often fail to fire the JS
    # IntersectionObserver / scroll events that lazy-loading pages rely on.
    # This action uses page.evaluate(window.scrollTo) which triggers those
    # events properly, then waits for new content between each scroll.
    tools = Tools()

    @tools.action(
        'Scroll the entire page to load all lazy-loaded content. '
        'Call this before extracting data from pages that may have '
        'more results below the fold.'
    )
    async def scroll_to_load_all(browser_session: BrowserSession) -> ActionResult:
        page = await browser_session.must_get_current_page()
        last_height = await page.evaluate("document.body.scrollHeight")
        scroll_count = 0
        while scroll_count < 50:
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(2000)
            new_height = await page.evaluate("document.body.scrollHeight")
            if new_height == last_height:
                break
            last_height = new_height
            scroll_count += 1
        await page.evaluate("window.scrollTo(0, 0)")
        return ActionResult(
            extracted_content=(
                f'Scrolled {scroll_count} times to load all content. '
                f'Page height: {last_height}px.'
            )
        )

    # browser-use 0.12.5 has a built-in upload_file action that dispatches
    # UploadFileEvent via CDP (DOM.setFileInputFiles).  It needs:
    #   1. available_file_paths passed directly to Agent()
    #   2. Windows paths (Chrome runs on Windows, reads files from its FS)
    #
    # Because we connect via cdp_url, browser_session.is_local == False,
    # so the built-in upload_file skips the os.path.exists() check that
    # would fail for Windows paths on WSL.
    effective_task = task
    if file_paths:
        file_list = "\n".join(f"  - {p}" for p in file_paths)
        effective_task = (
            f"{task}\n\n"
            f"IMPORTANT: To upload photos, use the upload_file action with "
            f"the element index and file path. Click the photo/file upload "
            f"area first, then call upload_file for each file below:\n"
            f"{file_list}"
        )

    # Append a lazy-load hint so the LLM knows to scroll first
    effective_task += (
        "\n\nNOTE: If the page may contain more results than initially "
        "visible (lazy-loaded / infinite-scroll content), call the "
        "scroll_to_load_all action BEFORE extracting any data."
    )

    browser = Browser(cdp_url=cdp_url)
    agent = Agent(
        task=effective_task,
        llm=llm,
        browser=browser,
        available_file_paths=file_paths or None,
        tools=tools,
    )
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

def _handle_sync_accounts(args: dict, **kw) -> str:
    """Fetch all profiles from AdsPower API and write adspower_accounts.json."""
    api_url = _get_api_url()
    page = 1
    all_profiles = []

    try:
        while True:
            resp = requests.get(
                f"{api_url}/api/v1/user/list",
                params={"page": page, "page_size": 100},
                headers=_api_headers(),
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") != 0:
                return json.dumps({
                    "success": False,
                    "error": f"AdsPower API error: {data.get('msg', 'unknown')}",
                })

            profiles = data.get("data", {}).get("list", [])
            if not profiles:
                break

            for p in profiles:
                all_profiles.append({
                    "name": p.get("name", p.get("serial_number", "unnamed")),
                    "profile_id": p.get("user_id", ""),
                    "description": p.get("remark", ""),
                })

            page_count = data.get("data", {}).get("page_count", 1)
            if page >= page_count:
                break
            page += 1

    except requests.ConnectionError:
        return json.dumps({
            "success": False,
            "error": (
                f"Cannot reach AdsPower at {api_url}. "
                "Ensure AdsPower is running and ADSPOWER_API_URL is correct."
            ),
        })
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)})

    if not all_profiles:
        return json.dumps({
            "success": False,
            "error": "No profiles found in AdsPower.",
        })

    config_path = _get_hermes_home() / "adspower_accounts.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"accounts": all_profiles}, indent=2))
    logger.info("Synced %d profiles to %s", len(all_profiles), config_path)

    return json.dumps({
        "success": True,
        "synced": len(all_profiles),
        "accounts": all_profiles,
        "config_path": str(config_path),
    })


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
    file_paths = args.get("file_paths", [])

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
            ws_url, proxy_proc = _start_profile(profile_id)
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
                "proxy_proc": proxy_proc,
            }

    # Run browser-use Agent
    try:
        result = _run_async(_run_browser_task(
            ws_url, task, max_steps, file_paths=file_paths or None,
        ))
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
        proxy = session.get("proxy_proc")
        if proxy:
            proxy.terminate()
        _stop_profile(session["profile_id"])
        closed.append(name)

    if not closed:
        return json.dumps({
            "success": True,
            "message": "No active sessions to close.",
            "closed": [],
        })
    return json.dumps({"success": True, "closed": closed})


def _detect_windows_downloads() -> Path:
    """Detect the Windows Downloads folder accessible from WSL2."""
    mnt_users = Path("/mnt/c/Users")
    if mnt_users.exists():
        for entry in mnt_users.iterdir():
            if entry.is_dir() and entry.name not in ("Public", "Default", "Default User", "All Users"):
                downloads = entry / "Downloads" / "listing_photos"
                return downloads
    # Fallback to hermes cache
    return _get_hermes_home() / "cache" / "listing_photos"


def _wsl_to_windows_path(wsl_path: str) -> str:
    """Convert a /mnt/c/... WSL path to a C:\\... Windows path."""
    if wsl_path.startswith("/mnt/"):
        parts = wsl_path.split("/")
        drive = parts[2].upper()
        rest = "\\".join(parts[3:])
        return f"{drive}:\\{rest}"
    return wsl_path


def _windows_to_wsl_path(win_path: str) -> str:
    """Convert a C:\\... Windows path to a /mnt/c/... WSL path."""
    normalized = win_path.replace("\\", "/")
    if len(normalized) >= 2 and normalized[1] == ":":
        drive = normalized[0].lower()
        rest = normalized[2:].lstrip("/")
        return f"/mnt/{drive}/{rest}"
    return win_path


def _handle_download_photos(args: dict, **kw) -> str:
    """Download listing photos from URLs to a Windows-accessible directory."""
    urls = args.get("urls", [])
    listing_id = args.get("listing_id", "listing")

    if not urls:
        return json.dumps({"success": False, "error": "No URLs provided"})

    dest_dir = _detect_windows_downloads()
    dest_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    failed = []

    from tools.vision_tools import _download_image

    for i, url in enumerate(urls, 1):
        fname = f"{listing_id}_photo_{i}.jpg"
        dest = dest_dir / fname
        try:
            _run_async(_download_image(url, dest))
            win_path = _wsl_to_windows_path(str(dest))
            downloaded.append({"file": fname, "wsl_path": str(dest), "windows_path": win_path})
        except Exception as e:
            failed.append({"url": url, "error": str(e)[:200]})
            logger.warning("Photo download failed for %s: %s", url[:80], e)

    return json.dumps({
        "success": len(downloaded) > 0,
        "downloaded": len(downloaded),
        "failed": len(failed),
        "file_paths": [d["windows_path"] for d in downloaded],
        "wsl_paths": [d["wsl_path"] for d in downloaded],
        "details": downloaded,
        "errors": failed,
        "download_dir": str(dest_dir),
    })


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

ADSPOWER_SYNC_SCHEMA = {
    "name": "adspower_sync",
    "description": (
        "Fetch all browser profiles from the AdsPower API and save them to "
        "~/.hermes/adspower_accounts.json. Run this to populate or refresh "
        "the account list from AdsPower."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}

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
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "List of local file paths (Windows paths) the browser "
                    "agent can upload to file inputs. Use paths returned by "
                    "adspower_download_photos."
                ),
            },
        },
        "required": ["account_name", "task"],
    },
}

ADSPOWER_DOWNLOAD_PHOTOS_SCHEMA = {
    "name": "adspower_download_photos",
    "description": (
        "Download listing photos from URLs to a Windows-accessible directory. "
        "Use this after extracting photo URLs from a portal or Kijiji listing "
        "via adspower_browse. Returns Windows file paths that can be passed "
        "to adspower_browse via the file_paths parameter for uploading to "
        "Facebook Marketplace."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of image URLs to download",
            },
            "listing_id": {
                "type": "string",
                "description": (
                    "Identifier for the listing, used in filenames "
                    "(e.g., 'listing_1', 'listing_2')"
                ),
            },
        },
        "required": ["urls", "listing_id"],
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

def _check_adspower_api() -> bool:
    """Return True when AdsPower API env is configured (for sync tool)."""
    return bool(_get_api_key() or os.getenv("ADSPOWER_API_URL"))


def _check_adspower_accounts() -> bool:
    """Return True when accounts config exists (for browse/list/close tools)."""
    config_path = _get_hermes_home() / "adspower_accounts.json"
    return config_path.exists()


# ---------------------------------------------------------------------------
# Registry registration
# ---------------------------------------------------------------------------

registry.register(
    name="adspower_sync",
    toolset="adspower",
    schema=ADSPOWER_SYNC_SCHEMA,
    handler=_handle_sync_accounts,
    check_fn=_check_adspower_api,
    requires_env=[],
    is_async=False,
    description="Sync profiles from AdsPower API",
    emoji="🔄",
)

registry.register(
    name="adspower_list_accounts",
    toolset="adspower",
    schema=ADSPOWER_LIST_ACCOUNTS_SCHEMA,
    handler=_handle_list_accounts,
    check_fn=_check_adspower_accounts,
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
    check_fn=_check_adspower_accounts,
    requires_env=["OPENROUTER_API_KEY"],
    is_async=False,
    description="Run browser task on AdsPower profile",
    emoji="🌐",
)

registry.register(
    name="adspower_download_photos",
    toolset="adspower",
    schema=ADSPOWER_DOWNLOAD_PHOTOS_SCHEMA,
    handler=_handle_download_photos,
    check_fn=_check_adspower_api,
    requires_env=[],
    is_async=False,
    description="Download listing photos to Windows directory",
    emoji="📸",
)

registry.register(
    name="adspower_close",
    toolset="adspower",
    schema=ADSPOWER_CLOSE_SCHEMA,
    handler=_handle_close,
    check_fn=_check_adspower_accounts,
    requires_env=[],
    is_async=False,
    description="Close AdsPower browser session",
    emoji="🛑",
)
