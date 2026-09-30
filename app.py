"""
Grocery Price Comparator  v7
- Catalog mode: pre-define products with direct URLs per store.
  Multiple URLs per store → cheapest is shown.
- Dynamic search: Metro + Super C (Chromium), Maxi + Provigo (Firefox profile copy).
"""

import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
import webbrowser
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from urllib.parse import quote_plus, urlparse, parse_qs

import requests as _requests

from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)
# Re-read templates from disk on every request so edits to templates/index.html
# take effect on a browser refresh, without needing to restart the app.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

CATALOG_PATH = os.path.join(os.path.dirname(__file__), "catalog.json")
METRO_COOKIES_PATH = os.path.join(os.path.dirname(__file__), "metro_cookies.json")
METRO_PROFILE_DIR  = os.path.join(os.path.dirname(__file__), "metro_profile")
METRO_PROFILE_RUN  = os.path.join(os.path.dirname(__file__), "metro_profile_run")

# Real Chrome debug-mode settings (subprocess Chrome, no automation flags → bypasses CF)
_CHROME_DEBUG_PORT = 9223
_chrome_proc: "subprocess.Popen | None" = None
_chrome_proc_lock = threading.Lock()

# CDP worker thread — owns ALL Playwright objects (Playwright's sync API is
# greenlet-bound; sharing objects across threads raises "Cannot switch to a
# different thread").  Callers submit (url, banner, result_queue) and wait.
from queue import Queue as _Queue
_cdp_task_queue: "_Queue | None" = None      # initialised by worker on startup
_cdp_worker_thread: "threading.Thread | None" = None
_cdp_worker_lock = threading.Lock()

_DEFAULT_STORE_IDS = {
    "maxi":    "8632",
    "provigo": "7288",
}

def _load_metro_cookies() -> list[dict]:
    """Load saved Metro/SuperC cookies from disk (set via metro-store-setup)."""
    try:
        if os.path.exists(METRO_COOKIES_PATH):
            with open(METRO_COOKIES_PATH) as f:
                return json.load(f)
    except Exception:
        pass
    return []

def _get_chrome_metro_cookies() -> list[dict]:
    """Read Metro/SuperC cookies directly from Chrome — the most reliable source
    because it uses the exact session the user already has with their store set."""
    try:
        import browser_cookie3
        results = []
        for domain in ("metro.ca", "superc.ca"):
            jar = browser_cookie3.chrome(domain_name=domain)
            for c in jar:
                results.append({
                    "name": c.name,
                    "value": c.value,
                    "domain": c.domain if c.domain.startswith(".") else f".{c.domain}",
                    "path": c.path or "/",
                })
        return results
    except Exception:
        return []

def _save_metro_cookies(cookies: list[dict]):
    try:
        with open(METRO_COOKIES_PATH, "w") as f:
            json.dump(cookies, f, indent=2)
    except Exception:
        pass

def _copy_metro_profile() -> str:
    """Return a persistent Metro run-copy that survives between scrape sessions.

    Using a persistent copy (metro_profile_run/) instead of a fresh temp dir
    means Cloudflare cf_clearance cookies are retained across runs.  The user
    only needs to complete the checkbox challenge once; subsequent scrapes reuse
    the stored clearance cookie automatically.

    metro_profile/ is the source of truth for store selection.
    metro_profile_run/ is the live working copy used by Playwright.

    If metro_profile_run/ doesn't exist yet, seed it from metro_profile/.
    """
    run_dir = METRO_PROFILE_RUN
    if not os.path.isdir(run_dir):
        os.makedirs(run_dir, exist_ok=True)
        if os.path.isdir(METRO_PROFILE_DIR):
            shutil.copytree(
                METRO_PROFILE_DIR, run_dir,
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(
                    "Cache", "Cache_Data", "Code Cache", "GPUCache",
                    "Crashpad", "CrashpadMetrics*", "Safe Browsing",
                ),
            )
    # Remove Chromium singleton lock files so the copy can be opened
    for lock_name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            os.unlink(os.path.join(run_dir, lock_name))
        except Exception:
            pass
    return run_dir

# ================================================================
# Real Chrome subprocess + CDP (bypasses Cloudflare managed challenge)
# ================================================================

def _find_chrome_exe() -> str | None:
    """Find the Chrome executable on Windows."""
    paths = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return None

def _chrome_debug_ready(port: int = None, timeout: float = 15.0) -> bool:
    """Poll Chrome's debug endpoint until it responds."""
    if port is None:
        port = _CHROME_DEBUG_PORT
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = _requests.get(f"http://127.0.0.1:{port}/json/version", timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False

def _shutdown_chrome():
    """Close Chrome after a comparison job finishes.

    Sends a shutdown signal to the CDP worker, which closes Chrome gracefully
    via browser.close() (CDP Browser.close command).  The OS-level kill is a
    fallback for when the CDP path isn't available."""
    global _chrome_proc
    # Signal the CDP worker to close Chrome via browser.close() and then exit
    if _cdp_task_queue is not None:
        try: _cdp_task_queue.put(None)
        except Exception: pass
        # Give the worker a moment to send Browser.close before we resort to OS kill
        time.sleep(1.5)
    # Fallback: if the debug port is still up, Chrome didn't close on its own.
    # Kill the EXACT debug instance by matching its unique --remote-debugging-port
    # on the command line. This is reliable regardless of which page/tab it is on,
    # unlike a window-title filter, which races with the about:blank navigation
    # (the Loblaw product pages are slow, so the tab is often still on the product
    # URL when shutdown runs — the old title filter then missed and Chrome leaked).
    if _chrome_debug_ready(_CHROME_DEBUG_PORT, timeout=1.0):
        print("  [chrome-cdp] CDP close didn't work, force-killing debug Chrome")
        ps = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
            "Where-Object { $_.CommandLine -like '*remote-debugging-port="
            + str(_CHROME_DEBUG_PORT) + "*' } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=15)
        except Exception:
            pass
        # Last-resort: original window-title filter, only if it's still up.
        if _chrome_debug_ready(_CHROME_DEBUG_PORT, timeout=1.0):
            try:
                subprocess.run(
                    ["taskkill", "/F", "/IM", "chrome.exe",
                     "/FI", "WINDOWTITLE eq about:blank*"],
                    capture_output=True
                )
            except Exception:
                pass
    with _chrome_proc_lock:
        _chrome_proc = None
    print("  [chrome-cdp] Chrome shutdown complete")


def _ensure_debug_chrome() -> bool:
    """Ensure a Chrome instance is listening on _CHROME_DEBUG_PORT.

    KEY INSIGHT — Windows chrome.exe is a launcher: the process we Popen exits
    quickly after spawning the real browser process under a different PID.  We
    therefore check the *debug port* first and treat a live port as proof Chrome
    is healthy, regardless of whether our tracked PID is still running.

    Chrome is launched WITHOUT --enable-automation so navigator.webdriver is
    undefined, bypassing Cloudflare's managed challenge detection."""
    global _chrome_proc
    with _chrome_proc_lock:
        # Port-first check: if Chrome (under any PID) is on the port, we're good.
        if _chrome_debug_ready(_CHROME_DEBUG_PORT, timeout=1.5):
            return True

        # Nothing on the port — kill our stale tracked process if still alive.
        if _chrome_proc is not None and _chrome_proc.poll() is None:
            print(f"  [chrome-cdp] Debug port gone; killing tracked pid={_chrome_proc.pid}")
            try: _chrome_proc.kill()
            except Exception: pass
        _chrome_proc = None

        exe = _find_chrome_exe()
        if not exe:
            print("  [chrome-cdp] Chrome executable not found")
            return False
        run_dir = _copy_metro_profile()
        args = [
            exe,
            f"--remote-debugging-port={_CHROME_DEBUG_PORT}",
            f"--user-data-dir={run_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--mute-audio",
            "--start-minimized",
            # IMPORTANT: no --enable-automation, no --headless → real browser behavior
            # NOTE: --disable-background-networking intentionally omitted — it blocks
            # QUIC/HTTP3 on some domains (e.g. superc.ca) causing ERR_TIMED_OUT
        ]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _chrome_proc = proc
            print(f"  [chrome-cdp] Chrome starting (pid={proc.pid}, port={_CHROME_DEBUG_PORT})...")
        except Exception as e:
            print(f"  [chrome-cdp] Failed to start Chrome: {e}")
            return False

        # Wait for the port — Chrome may re-spawn under a new PID on Windows.
        if _chrome_debug_ready(_CHROME_DEBUG_PORT, timeout=20.0):
            print("  [chrome-cdp] Chrome ready")
            return True
        print("  [chrome-cdp] Chrome debug port not ready in time")
        try: _chrome_proc.kill()
        except Exception: pass
        _chrome_proc = None
        return False

def _cdp_extract_loblaw(page, url: str, banner: str, PWT) -> "dict | None":
    """Loblaw (Maxi/Provigo) via the real-Chrome CDP page: navigate to the exact
    product URL and capture the product-facade JSON the page fetches for itself.
    Real Chrome passes Cloudflare, so this succeeds where headless Firefox/Chromium
    (blocked by CF) and the bare requests() API call (404 / no session) both fail."""
    m = re.search(r"/p/(\d+)", url)
    code = m.group(1) if m else "unknown"
    captured = {}
    def on_response(response):
        try:
            if captured: return
            if response.status != 200: return
            if "json" not in response.headers.get("content-type", ""): return
            ru = response.url
            if not any(k in ru for k in ("product-facade", "/products/", "product_details")): return
            if code != "unknown" and code not in ru: return
            data = response.json()
            if isinstance(data, dict):
                captured.update(data)
        except Exception:
            pass
    page.on("response", on_response)
    title = ""
    try:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except PWT:
            print(f"  [{banner}] cdp loblaw goto timeout")
        # Wait out any Cloudflare interstitial (real Chrome usually clears it fast).
        steps = 0
        while ("just a moment" in page.title().lower() or "please wait" in page.title().lower()):
            steps += 1
            if steps > 15:
                break
            time.sleep(2)
        # Wait up to ~20 s for the product-facade JSON response to arrive.
        for _ in range(40):
            if captured:
                break
            page.wait_for_timeout(500)
        try: title = page.title()
        except Exception: pass
    finally:
        try: page.remove_listener("response", on_response)
        except Exception: pass
    result = _parse_loblaw_api(captured, url) if captured else None
    try:
        _dir = os.path.join(os.path.dirname(__file__), "debug_screenshots")
        os.makedirs(_dir, exist_ok=True)
        with open(os.path.join(_dir, f"{banner}_{code}_cdp.txt"), "w") as _f:
            _f.write(f"url={url}\ntitle={title!r}\ncaptured_json={bool(captured)}\n"
                     f"parsed={'OK $'+str(result['price']) if result else 'None'}\n")
    except Exception:
        pass
    return result

def _cdp_navigate_and_extract(page, url: str, banner: str, PWT) -> "dict | None":
    """Navigate *page* to *url* and extract a price dict.
    Called exclusively from the CDP worker thread — never call from elsewhere."""
    if STORES.get(banner, {}).get("type") == "loblaw":
        return _cdp_extract_loblaw(page, url, banner, PWT)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except PWT:
        print(f"  [{banner}] cdp goto timeout")
        return None

    print(f"  [{banner}] cdp loaded: {page.url[:80]!r}")
    print(f"  [{banner}] cdp title: {page.title()!r}")

    # Wait for CF challenge to resolve (real Chrome usually passes immediately)
    wait_steps = 0
    while ("just a moment" in page.title().lower()
           or "please wait" in page.title().lower()):
        wait_steps += 1
        if wait_steps > 15:
            print(f"  [{banner}] CF challenge timed out after 30 s")
            return None
        time.sleep(2)
    if wait_steps:
        print(f"  [{banner}] CF resolved after {wait_steps * 2}s")

    # If Metro redirected to a category page (no /p/ in URL), skip
    if "/p/" not in page.url:
        print(f"  [{banner}] redirected to category page, skipping: {page.url[:80]}")
        return None

    # Wait for price selector
    try:
        page.wait_for_selector(
            ".price-update, .pricing__item, h1.product-name, h1[class*='product']",
            timeout=15_000,
        )
        print(f"  [{banner}] price selector found")
    except PWT:
        print(f"  [{banner}] price selector timeout")

    # Dismiss cookie consent if present
    try:
        page.wait_for_selector(
            "#onetrust-reject-all-handler, button[id*='reject'], button[id*='refuse']",
            timeout=3_000,
        )
        btn = page.query_selector("#onetrust-reject-all-handler")
        if btn:
            btn.click()
            page.wait_for_timeout(500)
    except PWT:
        pass

    # Give Metro's price API time to replace the $9.99 placeholder
    page.wait_for_timeout(5_000)

    result = _extract_price_from_page(page, url, banner)
    if result is None:
        try:
            ss_dir = os.path.join(os.path.dirname(__file__), "debug_screenshots")
            os.makedirs(ss_dir, exist_ok=True)
            m2 = re.search(r"/p/([^/?#]+)", url)
            code = m2.group(1) if m2 else "unknown"
            page.screenshot(
                path=os.path.join(ss_dir, f"{banner}_{code}_no_price.png"),
                full_page=False,
            )
            print(f"  [{banner}] screenshot saved to debug_screenshots/")
        except Exception:
            pass
    return result


def _cdp_worker():
    """Dedicated thread that owns ALL Playwright objects.

    Playwright's sync API is greenlet-bound: you cannot share Browser/Context/Page
    objects across threads.  This worker serialises all Metro/Super C fetches so
    those objects are always accessed from the same thread.

    Protocol: callers put (url, banner, result_queue) on _cdp_task_queue and
    block on result_queue.get(timeout=120).  The worker puts the result dict
    (or None) onto result_queue when done.
    """
    global _cdp_task_queue
    from queue import Queue, Empty
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWT
    except ImportError:
        print("  [chrome-cdp] playwright not installed — Metro/Super C unavailable")
        return

    q: Queue = Queue()
    _cdp_task_queue = q   # signal _start_cdp_worker that we are ready

    try:
        with sync_playwright() as p:
            browser = None

            while True:
                try:
                    task = q.get(timeout=300)   # exit after 5 min idle
                except Empty:
                    print("  [chrome-cdp] CDP worker idle timeout, exiting")
                    break

                if task is None:                # explicit shutdown signal
                    # Close Chrome gracefully via CDP before exiting
                    if browser is not None:
                        try:
                            browser.close()
                            print("  [chrome-cdp] Chrome closed via CDP")
                        except Exception:
                            pass
                    break

                url, banner, result_q = task
                try:
                    if not _ensure_debug_chrome():
                        result_q.put(None)
                        continue

                    # (Re)connect playwright to Chrome when needed
                    try:
                        if browser is None or not browser.is_connected():
                            browser = p.chromium.connect_over_cdp(
                                f"http://127.0.0.1:{_CHROME_DEBUG_PORT}"
                            )
                            print("  [chrome-cdp] playwright connected to Chrome")
                    except Exception as e:
                        print(f"  [chrome-cdp] connect_over_cdp failed: {e}")
                        browser = None
                        result_q.put(None)
                        continue

                    ctx = (browser.contexts[0]
                           if browser.contexts else browser.new_context())

                    # Reuse an existing page regardless of current URL — keeps Chrome at 1 tab.
                    # Filtering for blank URLs is unreliable when about:blank nav fails.
                    pages = ctx.pages
                    page = pages[0] if pages else ctx.new_page()

                    try:
                        result = _cdp_navigate_and_extract(page, url, banner, PWT)
                    finally:
                        try:
                            page.goto("about:blank", wait_until="commit", timeout=5_000)
                        except Exception:
                            pass  # Leave tab as-is — closing it would exit Chrome on Windows

                    result_q.put(result)

                except Exception as e:
                    print(f"  [{banner}] cdp worker error: {e}")
                    import traceback; traceback.print_exc()
                    result_q.put(None)

    finally:
        _cdp_task_queue = None
        print("  [chrome-cdp] CDP worker thread exiting")


def _start_cdp_worker():
    """Ensure the CDP worker thread is alive. Safe to call from any thread.
    ALL callers wait for the worker to publish its queue — not just the one
    that started it (fixing a race where threads 2-N returned early)."""
    global _cdp_worker_thread
    with _cdp_worker_lock:
        if _cdp_worker_thread is None or not _cdp_worker_thread.is_alive():
            t = threading.Thread(target=_cdp_worker, daemon=True, name="cdp-worker")
            t.start()
            _cdp_worker_thread = t
    # Every caller waits up to 10 s for the worker to publish _cdp_task_queue
    deadline = time.time() + 10
    while _cdp_task_queue is None and time.time() < deadline:
        time.sleep(0.05)


def _fetch_metro_price_via_cdp(url: str, banner: str) -> "dict | None":
    """Submit a Metro/Super C URL to the CDP worker thread and block for the result."""
    from queue import Queue
    _start_cdp_worker()
    if _cdp_task_queue is None:
        print(f"  [{banner}] cdp: worker failed to start")
        return None
    result_q: Queue = Queue()
    _cdp_task_queue.put((url, banner, result_q))
    try:
        return result_q.get(timeout=300)
    except Exception:
        print(f"  [{banner}] cdp: timed out waiting for worker result")
        return None

# ================================================================
# Chrome profile cookie reader (for Metro direct fetch)
# ================================================================

def _get_chrome_profile_aes_key(profile_dir: str):
    """Return the AES-256 key used to encrypt cookies in a Chrome profile.
    Requires Windows (DPAPI) + pywin32.  Returns None on any failure."""
    import base64, json
    # Local State sits in the user-data-dir (same level as Default/)
    for candidate in [
        os.path.join(profile_dir, "Local State"),
        os.path.join(os.path.dirname(profile_dir), "Local State"),
    ]:
        if os.path.exists(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    ls = json.load(f)
                enc_key_b64 = ls["os_crypt"]["encrypted_key"]
                enc_key = base64.b64decode(enc_key_b64)[5:]  # strip "DPAPI" prefix
                import win32crypt
                return win32crypt.CryptUnprotectData(enc_key, None, None, None, 0)[1]
            except ImportError:
                print("  [metro-direct] pywin32 not found — run: python -m pip install pywin32")
                return None
            except Exception as e:
                print(f"  [metro-direct] cookie key decryption failed: {e}")
                return None
    print(f"  [metro-direct] Local State not found in {profile_dir}")
    return None

def _decrypt_chrome_cookie_value(key: bytes, enc_val: bytes) -> str | None:
    """Decrypt a Chrome cookie value.
    Supports v10/v11 (AES-256-GCM, Chrome <127) and v20 (App-Bound Encryption, Chrome 127+)."""
    try:
        prefix = enc_val[:3]
        if prefix in (b"v10", b"v11"):
            # Standard AES-256-GCM: 3-byte prefix + 12-byte nonce + ciphertext
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = enc_val[3:15]
            ciphertext = enc_val[15:]
            return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8", errors="replace")
        if prefix == b"v20":
            # Chrome 127+ App-Bound Encryption: 3-byte prefix + 32-byte app-key hash
            # + 12-byte nonce + ciphertext. The AES key is the same DPAPI-derived key.
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            nonce = enc_val[35:47]
            ciphertext = enc_val[47:]
            return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8", errors="replace")
        # Pre-v80 DPAPI fallback (no prefix)
        if enc_val:
            import win32crypt
            return win32crypt.CryptUnprotectData(enc_val, None, None, None, 0)[1].decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [metro-direct] cookie decrypt failed (prefix={enc_val[:3]!r}): {e}")
    return None

def get_chrome_profile_cookies(profile_dir: str, domain: str) -> dict:
    """Return {name: value} dict of cookies for *domain* from a Chrome profile dir."""
    key = _get_chrome_profile_aes_key(profile_dir)
    if key is None:
        return {}
    for cookies_path in [
        os.path.join(profile_dir, "Default", "Network", "Cookies"),
        os.path.join(profile_dir, "Network", "Cookies"),
        os.path.join(profile_dir, "Default", "Cookies"),
    ]:
        if os.path.exists(cookies_path):
            break
    else:
        return {}
    # Use a unique suffix per call to avoid race conditions with concurrent scrapes
    import uuid
    _uid = uuid.uuid4().hex
    tmp     = cookies_path + f"_cffi_{_uid}"
    tmp_wal = tmp + "-wal"
    tmp_shm = tmp + "-shm"
    conn = None
    try:
        shutil.copy2(cookies_path, tmp)
        # Chrome uses WAL mode — copy WAL+SHM too so SQLite sees pending writes.
        # These files may be locked if Chrome is running; failure is non-fatal.
        wal_copied = False
        for ext in ("-wal", "-shm"):
            src = cookies_path + ext
            dst = tmp + ext
            if os.path.exists(src):
                try:
                    shutil.copy2(src, dst)
                    wal_copied = True
                except OSError:
                    pass  # Chrome has WAL locked — fall through to immutable open
        try:
            conn = sqlite3.connect(tmp)
            conn.execute("SELECT 1")  # verify it's readable
        except Exception:
            # WAL present in DB header but WAL file missing/locked.
            # Open as immutable to read the checkpointed (main DB) state only.
            try:
                conn = sqlite3.connect(f"file:{tmp}?immutable=1&mode=ro", uri=True)
            except Exception as e2:
                print(f"  [metro-direct] cookies DB error for {domain}: {e2}")
                return {}
        rows = conn.execute(
            "SELECT name, encrypted_value FROM cookies WHERE host_key LIKE ?",
            (f"%{domain}%",),
        ).fetchall()
        conn.close()
        conn = None
    except Exception as e:
        print(f"  [metro-direct] cookies DB error for {domain}: {e}")
        return {}
    finally:
        if conn:
            try: conn.close()
            except Exception: pass
        for f in (tmp, tmp_wal, tmp_shm):
            try:
                if os.path.exists(f):
                    os.unlink(f)
            except Exception:
                pass
    result = {}
    for name, enc_val in rows:
        val = _decrypt_chrome_cookie_value(key, enc_val)
        if val is not None:
            result[name] = val
    return result


def _fetch_metro_price_direct(url: str, banner: str = "metro") -> dict | None:
    """Fetch a Metro or Super C product page using curl_cffi (Chrome TLS fingerprint)
    + cf_clearance cookie from metro_profile_run.  Parses price from HTML.
    Returns None if unavailable or if price looks like the $9.99 placeholder."""
    try:
        from curl_cffi import requests as cffi_requests
        from bs4 import BeautifulSoup
    except ImportError as e:
        print(f"  [metro-direct] missing package: {e} — install with: python -m pip install curl_cffi beautifulsoup4 lxml")
        return None

    domain = f"{banner}.ca"
    # Try metro_profile_run first, then the user's real Chrome profile as fallback.
    # The cf_clearance in metro_profile_run may be tainted by Playwright sessions;
    # the real Chrome profile has clearance obtained by the genuine browser.
    _REAL_CHROME_PROFILE = os.path.expandvars(
        r"%LOCALAPPDATA%\Google\Chrome\User Data\Default"
    )
    cookies = get_chrome_profile_cookies(METRO_PROFILE_RUN, domain)
    if not cookies.get("cf_clearance") and os.path.exists(_REAL_CHROME_PROFILE):
        print(f"  [metro-direct] no cf_clearance in metro_profile_run — trying real Chrome profile")
        cookies = get_chrome_profile_cookies(_REAL_CHROME_PROFILE, domain)
    print(f"  [metro-direct] {banner}: {len(cookies)} cookies, cf_clearance={'yes' if cookies.get('cf_clearance') else 'NO'}")
    if not cookies.get("cf_clearance"):
        print(f"  [metro-direct] no cf_clearance anywhere — falling back to browser")
        return None  # No Cloudflare clearance — request will be challenged

    headers = {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
    }
    # Send only cf_clearance — skipping stale short-lived CF cookies (__cf_bm
    # expires in 30 min; if stale it causes a 400 validation failure)
    send_cookies = {"cf_clearance": cookies["cf_clearance"]}
    print(f"  [metro-direct] {banner} → sending only cf_clearance")
    # Try newest impersonation first (closest to user's real Chrome), fall back to older
    last_err = None
    for impersonate in ("chrome136", "chrome131", "chrome124"):
        try:
            resp = cffi_requests.get(
                url, impersonate=impersonate,
                cookies=send_cookies, headers=headers, timeout=15,
                allow_redirects=True,
            )
            print(f"  [metro-direct] {banner} HTTP {resp.status_code} (impersonate={impersonate})")
            if resp.status_code == 200:
                break
            print(f"  [metro-direct] {banner} response headers: {dict(resp.headers)}")
            print(f"  [metro-direct] {banner} body: {resp.text[:300]!r}")
            last_err = resp.status_code
        except Exception as e:
            print(f"  [metro-direct] {banner} request error ({impersonate}): {e}")
            last_err = e
    else:
        return None  # All impersonations failed (for-else: loop never broke)
    if resp.status_code != 200:
        return None

    try:
        soup = BeautifulSoup(resp.text, "lxml")
    except Exception as e:
        print(f"  [metro-direct] parse error: {e}")
        return None

    # --- price ---
    price_val = None
    # Try sale price first (lowest), then regular
    for sel in [".pricing__sale-price", "span.price-update", ".pi--prices"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(" ", strip=True)
            m = re.search(r"\$\s*(\d+\.\d{2})", txt)
            if m:
                price_val = float(m.group(1))
                if price_val != 9.99:  # skip placeholder
                    break
    if not price_val or price_val == 9.99:
        return None

    # --- unit/comparison price ---
    unit_str = None
    unit_el = soup.select_one(".pricing__secondary-price, [class*='unit-price'], [class*='secondary-price']")
    if unit_el:
        unit_txt = unit_el.get_text(" ", strip=True)
        m = re.search(r"\$\s*(\d+\.\d{2})\s*/\s*([\w]+)", unit_txt)
        if m:
            unit_str = f"${m.group(1)} /{m.group(2)}"

    # --- name / brand / size ---
    h1 = soup.select_one("h1.pi--title, h1.product-name, h1")
    name = h1.get_text(strip=True) if h1 else ""
    brand_el = soup.select_one(".pi--brand, [class*='brand']")
    brand = brand_el.get_text(strip=True) if brand_el else ""
    size_el = soup.select_one(".pi--weight-pkg, [class*='weight-pkg'], [class*='package']")
    size = size_el.get_text(strip=True) if size_el else ""

    return _make_result(
        name=name, brand=brand, price=price_val,
        unit_str=unit_str, size=size, url=url, image="",
    )


_LOBLAW_API_HEADERS_CACHE: dict = {}
_LOBLAW_API_HEADERS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "loblaw_api_headers.json"
)

def _load_cached_api_headers():
    try:
        if os.path.exists(_LOBLAW_API_HEADERS_FILE):
            with open(_LOBLAW_API_HEADERS_FILE, "r") as f:
                _LOBLAW_API_HEADERS_CACHE.update(json.load(f))
    except Exception:
        pass

def _save_api_headers(headers: dict):
    skip = {"host", "content-length", "connection", "accept-encoding", "cookie", "user-agent"}
    keep = {k: v for k, v in headers.items() if k.lower() not in skip}
    if keep:
        _LOBLAW_API_HEADERS_CACHE.update(keep)
        try:
            with open(_LOBLAW_API_HEADERS_FILE, "w") as f:
                json.dump(_LOBLAW_API_HEADERS_CACHE, f, indent=2)
        except Exception:
            pass

_load_cached_api_headers()

STORES = {
    "maxi":    {"label": "Maxi",    "url": "https://www.maxi.ca/en/search",
                "param": "search-bar", "type": "loblaw"},
    "provigo": {"label": "Provigo", "url": "https://www.provigo.ca/en/search",
                "param": "search-bar", "type": "loblaw"},
    "metro":   {"label": "Metro",   "url": "https://www.metro.ca/en/online-grocery/search",
                "param": "filter",    "type": "metro"},
    "superc":  {"label": "Super C", "url": "https://www.superc.ca/en/search",
                "param": "filter",    "type": "metro"},
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ================================================================
# Catalog persistence
# ================================================================

def load_catalog() -> dict:
    if os.path.exists(CATALOG_PATH):
        try:
            with open(CATALOG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"items": []}

def save_catalog(catalog: dict):
    with open(CATALOG_PATH, "w", encoding="utf-8") as f:
        json.dump(catalog, f, ensure_ascii=False, indent=2)

# ================================================================
# Produce / weight detection
# ================================================================

_WEIGHT_KEYWORDS = {
    "apple","pear","orange","lemon","lime","grapefruit","banana","grape",
    "strawberry","blueberry","raspberry","blackberry","cherry","peach",
    "plum","nectarine","apricot","mango","pineapple","watermelon","melon",
    "cantaloupe","kiwi","avocado","fig","pomegranate","papaya",
    "carrot","potato","onion","garlic","tomato","cucumber","pepper",
    "zucchini","squash","broccoli","cauliflower","cabbage","lettuce",
    "spinach","kale","arugula","celery","asparagus","corn","mushroom",
    "eggplant","radish","turnip","beet","leek","fennel","parsnip","yam",
    "sweet potato","bok choy","rapini",
    "ground beef","ground pork","ground turkey","ground chicken",
    "chicken breast","chicken thigh","chicken drumstick",
    "steak","pork chop","lamb chop","veal",
    "salmon","tilapia","trout","cod","halibut","shrimp",
    "deli","cold cut","prosciutto","salami",
}

# Products that contain weight-keyword fruit/veg names as flavours or ingredients
# (e.g. "Strawberry Yogurt", "Blueberry Muffin") should NOT be treated as
# sold-by-weight items.
_NON_WEIGHT_OVERRIDES = {
    "yogurt","yogourt","milk","cream","butter","cheese","ice cream","sorbet",
    "juice","smoothie","jam","jelly","preserve","puree","sauce","dressing",
    "muffin","cake","cookie","cracker","cereal","granola","bar","chip",
    "tea","coffee","drink","beverage","syrup","spread","vinegar",
}

def is_weight_item(term: str) -> bool:
    t = term.lower()
    if any(kw in t for kw in _NON_WEIGHT_OVERRIDES):
        return False
    return any(kw in t for kw in _WEIGHT_KEYWORDS)

# ================================================================
# Price helpers
# ================================================================

def _parse_price(text: str):
    if not text:
        return None
    m = re.search(r"\$?\s*(\d{1,3}(?:[.,]\d{3})*[.,]\d{2})", str(text))
    if not m:
        return None
    raw = m.group(1).replace(",", ".")
    parts = raw.rsplit(".", 1)
    if len(parts) == 2 and len(parts[1]) == 2:
        try:
            return float(parts[0].replace(".", "") + "." + parts[1])
        except ValueError:
            return None
    return None

def _extract_unit_price(text: str):
    if not text:
        return None, "fixed"
    # Some stores (e.g. Loblaw/Provigo) print the unit price with an explicit
    # quantity of 1 between the slash and the unit — "$5.77 / 1lb", "$12.72 / 1kg".
    # The optional (?:\d+\s*)? tolerates that leading quantity so those parse as
    # per-lb/per-kg instead of falling back to the total ("fixed") price.
    for pattern, kind in [
        (r"\$?\s*(\d+[.,]\d{2})\s*/\s*(?:\d+\s*)?(?:lb|lbs|pound|livre)\b", "per_lb"),
        (r"\$?\s*(\d+[.,]\d{2})\s*/\s*(?:\d+\s*)?(?:kg|kilo)\b",            "per_kg"),
        (r"\$?\s*(\d+[.,]\d{2})\s*/\s*100\s*g\b",                           "per_100g"),
        (r"\$?\s*(\d+[.,]\d{2})\s*/\s*100\s*ml\b",                          "per_100ml"),
    ]:
        m = re.search(pattern, text, re.I)
        if m:
            try:
                return float(m.group(1).replace(",", ".")), kind
            except ValueError:
                pass
    return None, "fixed"

def _make_result(name, brand, price, unit_str, size, url, image) -> dict:
    up, pt = _extract_unit_price(unit_str or "")
    if pt == "per_kg" and up is not None:
        up = round(up / 2.2046, 2)
        pt = "per_lb"
    # Compare by per-unit price whenever one is available (per-lb/kg AND
    # per-100g/100ml), so a larger bottle that's cheaper per ml wins over a
    # smaller one with a lower sticker price. Falls back to total price only
    # when no unit price was parsed. The template guards against mixing units
    # across stores within a row.
    comp = up if up is not None else price
    return {
        "name":             name or "",
        "brand":            brand or "",
        "price":            round(float(price), 2) if price is not None else None,
        "unit_price":       unit_str,
        "price_type":       pt,
        "comparison_price": round(float(comp), 2) if comp is not None else None,
        "size":             size or "",
        "url":              url or "",
        "image":            image or "",
    }

# ================================================================
# Firefox helpers
# ================================================================

def _get_firefox_profile_dir() -> str | None:
    ff_dir = os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")
    if not os.path.isdir(ff_dir):
        return None
    best, best_t = None, 0
    for name in os.listdir(ff_dir):
        db = os.path.join(ff_dir, name, "cookies.sqlite")
        if os.path.exists(db):
            t = os.path.getmtime(db)
            if t > best_t:
                best_t, best = t, os.path.join(ff_dir, name)
    return best

def _copy_firefox_auth_files(src: str, dst: str) -> int:
    files = [
        "cookies.sqlite", "cookies.sqlite-wal", "cookies.sqlite-shm",
        "webappsstore.sqlite", "webappsstore.sqlite-wal", "webappsstore.sqlite-shm",
        "key4.db", "cert9.db", "sessionstore.jsonlz4",
    ]
    copied = 0
    for fname in files:
        src_f = os.path.join(src, fname)
        if os.path.exists(src_f):
            try:
                shutil.copy2(src_f, os.path.join(dst, fname))
                copied += 1
            except Exception:
                pass
    for lock in ["lock", ".parentlock", "parent.lock"]:
        lf = os.path.join(dst, lock)
        if os.path.exists(lf):
            try:
                os.unlink(lf)
            except Exception:
                pass
    return copied

def get_firefox_local_storage(domain: str) -> dict:
    ff_dir = os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")
    if not os.path.isdir(ff_dir):
        return {}
    best, best_t = None, 0
    for name in os.listdir(ff_dir):
        db = os.path.join(ff_dir, name, "webappsstore.sqlite")
        if os.path.exists(db):
            t = os.path.getmtime(db)
            if t > best_t:
                best_t, best = t, db
    if not best:
        return {}
    tmp = tempfile.mktemp(suffix=".sqlite")
    try:
        shutil.copy2(best, tmp)
        conn = sqlite3.connect(tmp)
        reversed_domain = ".".join(reversed(domain.split(".")))
        rows = conn.execute(
            "SELECT key, value FROM webappsstore2 WHERE scope LIKE ?",
            (f"%{reversed_domain}%",),
        ).fetchall()
        conn.close()
        return {k: v for k, v in rows}
    except Exception:
        return {}
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

def get_firefox_cookies(host_contains: str) -> list[dict]:
    ff_dir = os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")
    if not os.path.isdir(ff_dir):
        return []
    best, best_t = None, 0
    for name in os.listdir(ff_dir):
        db = os.path.join(ff_dir, name, "cookies.sqlite")
        if os.path.exists(db):
            t = os.path.getmtime(db)
            if t > best_t:
                best_t, best = t, db
    if not best:
        return []
    tmp = tempfile.mktemp(suffix=".sqlite")
    try:
        shutil.copy2(best, tmp)
        conn = sqlite3.connect(tmp)
        rows = conn.execute(
            "SELECT name, value, host, path, expiry, isSecure, isHttpOnly "
            "FROM moz_cookies WHERE host LIKE ?",
            (f"%{host_contains}%",),
        ).fetchall()
        conn.close()
    except Exception:
        return []
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    cookies = []
    for name, value, host, path, expiry, secure, httponly in rows:
        cookies.append({
            "name": name, "value": value, "domain": host, "path": path,
            "expires": expiry if expiry > 0 else -1,
            "secure": bool(secure), "httpOnly": bool(httponly), "sameSite": "None",
        })
    return cookies

# ================================================================
# Browser context factory
# ================================================================

def _make_browser_context(p, store_type: str):
    ff_profile_copy = None
    browser = None
    if store_type == "loblaw":
        src = _get_firefox_profile_dir()
        if src:
            ff_profile_copy = tempfile.mkdtemp(prefix="ff_profile_")
            if _copy_firefox_auth_files(src, ff_profile_copy) == 0:
                shutil.rmtree(ff_profile_copy, ignore_errors=True)
                ff_profile_copy = None
    if store_type == "loblaw" and ff_profile_copy:
        try:
            ctx = p.firefox.launch_persistent_context(
                ff_profile_copy, headless=True, locale="en-CA",
                viewport={"width": 1280, "height": 900},
            )
            return ctx, browser, ff_profile_copy
        except Exception:
            # Firefox browser binary not installed / failed to launch. Drop the
            # Firefox auth profile and fall through to a plain Chromium context —
            # the public search page still renders prices for most items.
            shutil.rmtree(ff_profile_copy, ignore_errors=True)
            ff_profile_copy = None
    if store_type == "metro":
        # patchright's patched Chromium runs headless without CF bot detection.
        # The persistent profile retains the user's selected Metro store.
        metro_copy = _copy_metro_profile()
        ff_profile_copy = metro_copy   # reuse cleanup slot
        ctx = p.chromium.launch_persistent_context(
            metro_copy, headless=False,   # DEBUG: watch what patchright does in the browser
            args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
            user_agent=_UA, locale="en-CA",
            viewport={"width": 1280, "height": 900},
        )
    else:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=_UA, locale="en-CA",
            viewport={"width": 1280, "height": 900},
        )
    return ctx, browser, ff_profile_copy

def _close_browser(ctx, browser, ff_profile_copy):
    try:
        ctx.close()
    except Exception:
        pass
    if browser:
        try:
            browser.close()
        except Exception:
            pass
    if ff_profile_copy and ff_profile_copy != METRO_PROFILE_RUN:
        # Don't delete the Metro run-copy — it retains cf_clearance cookies
        shutil.rmtree(ff_profile_copy, ignore_errors=True)

# ================================================================
# Price extraction from a loaded page
# ================================================================

_LOBLAW_JS = r"""
() => {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
            const data = JSON.parse(s.textContent);
            const nodes = Array.isArray(data) ? data : [data];
            for (const n of nodes) {
                if (n['@type'] === 'Product') {
                    const offer = Array.isArray(n.offers) ? n.offers[0] : n.offers;
                    if (offer?.price != null) return {
                        source: 'ld', name: n.name || '',
                        price: String(offer.price), unitPrice: null,
                        image: Array.isArray(n.image) ? n.image[0] : (n.image || '')
                    };
                }
            }
        } catch(e) {}
    }
    const grabPrice = t => { const m = String(t).match(/\$\s*(\d+\.\d{2})/); return m ? '$' + m[1] : null; };
    for (const sel of [
        '[data-testid="sale-price"]', '[data-testid="regular-price"]',
        '[data-testid="non-member-price"]', '[data-testid="product-price"]',
        '[data-testid="price"]',
    ]) {
        const el = document.querySelector(sel);
        if (el) {
            const p = grabPrice(el.textContent);
            if (p) return { source: 'testid', name: document.querySelector('h1')?.textContent?.trim() || '', price: p, unitPrice: null, image: null };
        }
    }
    const h1 = document.querySelector('h1');
    const prices = [], unitPrices = [];
    for (const el of document.querySelectorAll('p,span,div,h2,h3,strong,b')) {
        const txt = el.textContent.trim();
        if (txt.length > 40) continue;
        const p = grabPrice(txt);
        if (p && p !== '$0.00') prices.push(p);
        if (/\$[\d.]+\s*\//.test(txt)) unitPrices.push(txt.trim());
    }
    return { source: 'scan', name: h1?.textContent?.trim() || null, price: prices[0] || null, unitPrice: unitPrices[0] || null, image: null };
}
"""

# JS for Metro/Super C: scan for prices including "N for $X.XX" multi-buy deals
_METRO_SCAN_JS = r"""
() => {
    const h1 = document.querySelector('h1');
    let dealPerUnit = null, dealLabel = null, regularPrice = null, unitStr = null;
    for (const el of document.querySelectorAll('p,span,div,h2,h3,strong,b')) {
        if (el.closest('#onetrust-consent-sdk, [id*="cookie"], [class*="cookie"], [id*="consent"]')) continue;
        const txt = el.textContent.trim();
        if (txt.length > 200 || txt.length < 2) continue;
        if (dealPerUnit == null) {
            const mm = txt.match(/\b(\d+)\s*(?:\/|for)\s*\$\s*(\d+\.\d{2})\b/i);
            if (mm) {
                const n = parseInt(mm[1]), total = parseFloat(mm[2]);
                if (n >= 2 && n <= 10 && total > 0) {
                    dealPerUnit = Math.round(total / n * 100) / 100;
                    dealLabel = n + ' for $' + total.toFixed(2);
                }
            }
        }
        if (regularPrice == null) {
            const rm = txt.match(/^\$\s*(\d+\.\d{2})\s*(?:ea\.?|each|avg\.?)?$/i);
            if (rm) { const v = parseFloat(rm[1]); if (v > 0) regularPrice = v; }
        }
        if (!unitStr && /\$[\d.]+\s*\/\s*\d/.test(txt) && txt.length < 40) unitStr = txt;
    }
    const price = dealPerUnit != null ? dealPerUnit : regularPrice;
    const unit  = dealPerUnit != null ? dealLabel   : unitStr;
    return price != null ? { name: h1?.textContent?.trim() || null, price: price.toFixed(2), unit: unit || null } : null;
}
"""

# JS for Metro/Super C JSON-LD extraction (avoids triple-quote issues by using a module-level constant)
_METRO_LD_JS = r"""
() => {
    for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
            const d = JSON.parse(s.textContent);
            const nodes = Array.isArray(d) ? d : [d];
            for (const n of nodes) {
                if (n['@type'] === 'Product') {
                    const o = Array.isArray(n.offers) ? n.offers[0] : n.offers;
                    if (o && o.price != null) return {name: n.name||'', price: String(o.price)};
                }
            }
        } catch(e) {}
    }
    return null;
}
"""


def _extract_price_from_page(page, url: str, banner: str) -> dict | None:
    cfg        = STORES[banner]
    store_type = cfg["type"]

    if store_type == "loblaw":
        try:
            raw = page.evaluate(_LOBLAW_JS)
        except Exception:
            raw = None
        if raw and raw.get("price"):
            price = _parse_price(raw["price"])
            if price is not None:
                return _make_result(
                    name=raw.get("name") or "", brand="", price=price,
                    unit_str=raw.get("unitPrice"), size="",
                    url=url, image=raw.get("image") or "",
                )

    else:  # metro / superc
        # Metro's page has a placeholder price ($9.99) in the initial HTML that
        # the client-side JS replaces with the real store price after an API call.
        # We wait 5 s for that update (see fetch_price_from_url), then extract.

        # Key insight: Metro renders the real price in its own elements — NOT in
        # .price-update (which keeps the placeholder).  The correct price IS in
        # the DOM but in the product-detail section near the h1.
        #
        # Strategy: scan every visible element within 600 px below the product
        # title (h1).  This captures the sale/regular price while excluding the
        # "Frequently Bought Together" carousel that starts ~800–1000 px below.
        # We take the minimum standalone price found (sale < regular).
        # Unit prices (e.g. "$1.32 /100g") are excluded by requiring no "/" after.
        _METRO_PRICE_JS = r"""
        () => {
            const h1 = document.querySelector('h1.product-name, h1');
            if (!h1) return null;
            const h1Top = h1.getBoundingClientRect().top;

            // Detect variable-weight items: Metro shows "avg." in the price area
            // (e.g. "$19.82 avg. ea." for chicken sold by weight).
            // Fixed-weight items (butter, milk, yogurt) never show "avg.".
            let isVariableWeight = false;
            for (const el of document.querySelectorAll('span,div,p,strong,b')) {
                if (el.children.length > 3) continue;
                const rect = el.getBoundingClientRect();
                if (rect.top < h1Top || rect.top > h1Top + 400) continue;
                if ((el.innerText || '').toLowerCase().includes('avg')) {
                    isVariableWeight = true;
                    break;
                }
            }

            // Scan the product area for a per-weight unit price (/100g, /kg, /lb, /100ml).
            // We don't rely on .pricing__secondary-price because it may still show a
            // stale placeholder (e.g. "$29.99 /un.") when Metro's JS hasn't updated it yet.
            let unitTxt = null;
            const perWeightRe = /\$\s*\d+\.\d{2}\s*\/\s*(100\s*g|100\s*ml|kg|kilo|lb|lbs|pound)\b/i;
            for (const el of document.querySelectorAll('span,div,p,strong,b,td')) {
                if (el.closest('#onetrust-consent-sdk,[id*="cookie"],[id*="consent"]')) continue;
                const rect = el.getBoundingClientRect();
                if (rect.top < h1Top - 20 || rect.top > h1Top + 500) continue;
                const txt = (el.innerText || '').trim();
                if (txt && txt.length < 50 && perWeightRe.test(txt)) {
                    unitTxt = txt;
                    break;
                }
            }

            if (isVariableWeight) {
                // For variable-weight items, scan the product area for /lb (or /kg)
                // prices directly in the DOM text.  We can't rely on
                // .pricing__secondary-price because it may still show the $9.99
                // placeholder unit, while the real per-lb line is elsewhere.
                // Collect all /lb values, deduplicate, take the minimum (= sale price).
                const lbPrices = new Set();
                const kgPrices = new Set();
                for (const el of document.querySelectorAll('span,div,p,strong,b,td')) {
                    if (el.closest('#onetrust-consent-sdk,[id*="cookie"],[id*="consent"]')) continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.top < h1Top - 20 || rect.top > h1Top + 600) continue;
                    const txt = (el.innerText || '').trim();
                    if (!txt || txt.length > 80) continue;
                    for (const m of txt.matchAll(/\$\s*(\d+\.\d{2})\s*\/\s*(?:lb|lbs)\b/gi))
                        lbPrices.add(parseFloat(m[1]));
                    for (const m of txt.matchAll(/\$\s*(\d+\.\d{2})\s*\/\s*kg\b/gi))
                        kgPrices.add(parseFloat(m[1]));
                }
                if (lbPrices.size) {
                    const price = Math.min(...lbPrices);
                    return { name: h1.textContent.trim(), price: price.toFixed(2), unit: '$' + price.toFixed(2) + ' /lb' };
                }
                if (kgPrices.size) {
                    const price = Math.min(...kgPrices);
                    return { name: h1.textContent.trim(), price: price.toFixed(2), unit: '$' + price.toFixed(2) + ' /kg' };
                }
            }

            // Fixed-weight item: scan for standalone prices within 600 px of h1.
            // Take the minimum (sale price < regular price < placeholder $9.99).
            const prices = [];
            for (const el of document.querySelectorAll('span,div,p,strong,b')) {
                if (el.closest('#onetrust-consent-sdk,[id*="cookie"],[class*="cookie"],[id*="consent"]')) continue;
                if (el.children.length > 2) continue;
                const rect = el.getBoundingClientRect();
                if (rect.top < h1Top || rect.top > h1Top + 600) continue;
                const txt = (el.innerText || '').trim();
                if (!txt || txt.length < 3 || txt.length > 25) continue;
                const m = txt.match(/^\$\s*(\d+\.\d{2})\s*(?:ea\.?|each|chacun\.?|un\.?)?$/i);
                if (m) {
                    const v = parseFloat(m[1]);
                    if (v > 0.01) prices.push(v);
                }
            }
            if (!prices.length) return null;
            return {
                name: h1.textContent.trim(),
                price: Math.min(...prices).toFixed(2),
                unit: unitTxt,
            };
        }
        """
        try:
            scan = page.evaluate(_METRO_PRICE_JS)
        except Exception:
            scan = None
        if scan and scan.get("price"):
            price = _parse_price(scan["price"])
            if price is not None:
                return _make_result(
                    name=scan.get("name") or "", brand="", price=price,
                    unit_str=scan.get("unit"), size="", url=url, image="",
                )

        # 2. Text scan fallback — handles "N for $X.XX" multi-buy deals.
        try:
            scan = page.evaluate(_METRO_SCAN_JS)
        except Exception:
            scan = None
        if scan and scan.get("price"):
            price = _parse_price(scan["price"])
            if price is not None:
                return _make_result(
                    name=scan.get("name") or "", brand="", price=price,
                    unit_str=scan.get("unit"), size="", url=url, image="",
                )

    return None


# ================================================================
# Fetch price from a direct product URL
# ================================================================

def _intercept_loblaw_price(page, product_url: str) -> dict | None:
    m = re.search(r"/p/(\d+)", product_url)
    product_code = m.group(1) if m else None
    captured = {}
    def on_response(response):
        try:
            if captured: return
            if response.status != 200: return
            if "json" not in response.headers.get("content-type", ""): return
            resp_url = response.url
            if not any(k in resp_url for k in ("product-facade", "/products/", "product_details")): return
            if product_code and product_code not in resp_url: return
            data = response.json()
            if isinstance(data, dict):
                captured.update(data)
        except Exception:
            pass
    page.on("response", on_response)
    return captured

def _parse_loblaw_api(data: dict, url: str) -> dict | None:
    offers = data.get("offers") or []
    offer  = offers[0] if offers else {}
    price_obj  = offer.get("price") or {}
    was_obj    = offer.get("wasPrice") or {}
    comp_list  = offer.get("comparisonPrices") or []
    comp       = comp_list[0] if comp_list else {}
    price_val = price_obj.get("value") or was_obj.get("value")
    if not price_val or float(price_val) <= 0:
        return None
    unit_str = None
    if comp.get("value") and comp.get("unit"):
        qty = comp.get("quantity") or 1
        qty_str = f"{qty} " if qty and int(qty) != 1 else ""
        unit_str = f"${float(comp['value']):.2f} / {qty_str}{comp['unit']}"
    return _make_result(
        name=data.get("name") or "", brand=data.get("brand") or "",
        price=float(price_val), unit_str=unit_str,
        size=data.get("packageSize") or "", url=url, image="",
    )

def _fetch_loblaw_price_direct(product_url: str, banner: str) -> dict | None:
    m = re.search(r"/p/([^/?#]+)", product_url)
    if not m:
        return None
    product_code = m.group(1)
    qs = parse_qs(urlparse(product_url).query)
    store_id = (qs.get("storeId") or [""])[0]
    if not store_id:
        for c in get_firefox_cookies(f"{banner}.ca"):
            if c["name"] == "last_selected_store":
                store_id = c["value"]
                break
    if not store_id:
        store_id = _DEFAULT_STORE_IDS.get(banner, "")
    if not store_id:
        return None
    date_str = datetime.now().strftime("%d%m%Y")
    api_url = (
        f"https://api.pcexpress.ca/pcx-bff/api/v1/products/{product_code}"
        f"?lang=en&date={date_str}&pickupType=STORE&storeId={store_id}&banner={banner}"
    )
    # Extract only the essential auth cookies — don't send all Firefox cookies
    # (bulk cookies cause HTTP 431 Request Header Fields Too Large)
    _ESSENTIAL_COOKIES = {"AccessToken", "SMSESSION", "pcx-bff-session", "pcx_session"}
    cookie_jar = {}
    for domain in ("pcexpress.ca", f"{banner}.ca"):
        for c in get_firefox_cookies(domain):
            if c["name"] in _ESSENTIAL_COOKIES:
                cookie_jar[c["name"]] = c["value"]
    bearer_token = None
    all_cookies = []
    for domain in (f"{banner}.ca", "pcexpress.ca"):
        all_cookies.extend(get_firefox_cookies(domain))
    for c in all_cookies:
        if c["name"] == "AccessToken":
            bearer_token = c["value"]
            break
    if not bearer_token:
        for domain in (f"{banner}.ca", "pcexpress.ca"):
            ls = get_firefox_local_storage(domain)
            for key in ("access_token", "id_token", "accessToken", "AccessToken"):
                if key in ls:
                    bearer_token = ls[key]
                    break
            if bearer_token:
                break
    # Build a minimal whitelist of headers — don't copy all cached headers,
    # which bloat the request and cause HTTP 431.
    _cached = _LOBLAW_API_HEADERS_CACHE or {}
    headers = {
        "User-Agent": _UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-CA,en;q=0.9",
        "Referer": f"https://www.{banner}.ca/",
        "Origin": f"https://www.{banner}.ca",
        "site-banner": banner,
        "x-apikey": _cached.get("x-apikey", "C1xujSegT5j3ap3yexJjqhOfELwGKYvz"),
        "x-loblaw-tenant-id": _cached.get("x-loblaw-tenant-id", "ONLINE_GROCERIES"),
        "x-application-type": _cached.get("x-application-type", "Web"),
        "business-user-agent": _cached.get("business-user-agent", "PCXWEB"),
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    def _log_api(label: str, body: str = ""):
        try:
            _diag_dir = os.path.join(os.path.dirname(__file__), "debug_screenshots")
            os.makedirs(_diag_dir, exist_ok=True)
            with open(os.path.join(_diag_dir, f"{banner}_{product_code}_api_diag.txt"), "w") as _f:
                _f.write(f"URL: {api_url}\n{label}\n{body}")
        except Exception:
            pass
    try:
        resp = _requests.get(api_url, cookies=cookie_jar, headers=headers, timeout=15)
        if resp.status_code == 401:
            _log_api(f"Status: 401 (auth expired)\nHeaders sent: Authorization={'Bearer ***' if bearer_token else 'None'}")
            return None
        if resp.status_code == 404:
            _log_api(f"Status: 404 (product not found at storeId={store_id})")
            return None
        if resp.status_code != 200:
            _log_api(f"Status: {resp.status_code}\nBody: {resp.text[:500]}")
            return None
        data = resp.json()
    except Exception as _exc:
        _log_api(f"Exception: {_exc}")
        return None
    result = _parse_loblaw_api(data, product_url)
    if result is None:
        import json as _json
        _log_api(f"Status: 200 — parse returned None\n\n", _json.dumps(data, indent=2)[:8000])
    return result

def _loblaw_search_terms(product_url: str, banner: str) -> list[str]:
    """Build candidate search terms for a Loblaw product URL: the original
    search-bar query if the catalog captured one, then the URL slug words."""
    terms = []
    qs = parse_qs(urlparse(product_url).query)
    param = STORES[banner].get("param", "search-bar")
    sb = (qs.get(param) or qs.get("search-bar") or [""])[0].strip()
    if sb:
        terms.append(sb)
    m = re.search(r"/([^/]+)/p/", product_url)      # slug segment before /p/
    if m:
        words = [w for w in m.group(1).split("-") if len(w) >= 2 and not w.isdigit()]
        if words:
            terms.append(" ".join(words[:6]))
    seen, out = set(), []
    for t in terms:
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out

# JS run in-page to scrape search-result tiles. Keyed by product code so the
# caller can match the exact SKU (not just the cheapest/first result).
_LOBLAW_SEARCH_JS = r"""
() => {
    const out = {};
    document.querySelectorAll('a[href*="/p/"]').forEach(a => {
        const m = a.getAttribute('href').match(/\/p\/([^\/?#]+)/);
        if (!m) return;
        const code = m[1];
        let card = a;
        for (let i = 0; i < 7 && card; i++) {
            card = card.parentElement;
            if (card && /\$\d+\.\d{2}/.test(card.innerText)) break;
        }
        if (!card) return;
        const txt = card.innerText;
        const price = (txt.match(/\$\s?(\d+\.\d{2})/) || [])[1] || null;
        const unit  = (txt.match(/\$\s?[\d.]+\s*\/\s*[\d]*\s*(?:100\s?(?:g|ml)|kg|ml|g|l|ea)/i) || [])[0] || null;
        const nameEl  = card.querySelector('[data-testid="product-title"]');
        const brandEl = card.querySelector('[data-testid="product-brand"]');
        const sizeEl  = card.querySelector('[data-testid="product-package-size"]');
        if (code && price && !out[code]) out[code] = {
            code, price, unit,
            name:  nameEl  ? nameEl.textContent.trim()  : "",
            brand: brandEl ? brandEl.textContent.trim() : "",
            size:  sizeEl  ? sizeEl.textContent.trim()  : "",
            link:  a.getAttribute('href'),
        };
    });
    return out;
}
"""

def _fetch_loblaw_price_via_search(product_url: str, banner: str) -> dict | None:
    """Fallback for SKUs that 404 on the product-detail API even though they are
    in stock (observed for multi-buy / promo "offer" items). The search/listing
    page still renders their price, so search by name and match the result tile
    whose link carries the exact product code."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWT
    m = re.search(r"/p/([^/?#]+)", product_url)
    if not m:
        return None
    code  = m.group(1)
    cfg   = STORES[banner]
    base  = re.match(r"https?://[^/]+", cfg["url"]).group(0)
    terms = _loblaw_search_terms(product_url, banner)

    # The app usually runs under pythonw (no console), so print() diagnostics are
    # lost. Write a small diagnostic file instead so we can see exactly why the
    # browser fallback did or didn't produce a price.
    _fb_log = []
    def _flush_fb(outcome):
        try:
            _dir = os.path.join(os.path.dirname(__file__), "debug_screenshots")
            os.makedirs(_dir, exist_ok=True)
            with open(os.path.join(_dir, f"{banner}_{code}_fallback.txt"), "w") as _f:
                _f.write(f"code={code}  banner={banner}\nterms={terms}\noutcome={outcome}\n"
                         + "\n".join(_fb_log))
        except Exception:
            pass

    if not terms:
        _flush_fb("no_search_terms")
        return None
    try:
        with sync_playwright() as p:
            ctx, browser, ff_profile_copy = _make_browser_context(p, cfg["type"])
            _fb_log.append("browser context created OK")
            page = ctx.new_page()
            try:
                for term in terms:
                    full_url = f"{cfg['url']}?{cfg['param']}={quote_plus(term)}"
                    try:
                        page.goto(full_url, wait_until="domcontentloaded", timeout=30_000)
                        page.wait_for_selector('a[href*="/p/"]', timeout=20_000)
                    except PWT:
                        _fb_log.append(f"term={term!r}: timed out waiting for search results")
                        continue
                    page.wait_for_timeout(3000)     # let SSR/lazy prices settle
                    data = page.evaluate(_LOBLAW_SEARCH_JS) or {}
                    _fb_log.append(f"term={term!r}: {len(data)} product tiles, "
                                   f"target_code_present={code in data}")
                    hit  = data.get(code)
                    if hit and hit.get("price"):
                        href = hit.get("link") or ""
                        if href.startswith("/"):
                            href = base + href
                        _flush_fb(f"matched {code} @ ${hit['price']}")
                        return _make_result(
                            name=hit.get("name") or "", brand=hit.get("brand") or "",
                            price=float(hit["price"]), unit_str=hit.get("unit"),
                            size=hit.get("size") or "", url=href or product_url, image="",
                        )
                _flush_fb("code_not_found_on_search_page")
                return None
            finally:
                _close_browser(ctx, browser, ff_profile_copy)
    except Exception as _exc:
        _fb_log.append(f"EXCEPTION: {type(_exc).__name__}: {_exc}")
        _flush_fb("exception")
        return None

def _fetch_loblaw_price_via_browser(product_url: str, banner: str) -> dict | None:
    """Load the EXACT product URL in a real browser and capture the product JSON
    the page fetches for itself. This succeeds when our bare requests() call to the
    same API 404s, because the page carries a valid anonymous PC Express session
    (cookies + tokens) that the standalone request lacks. Preferred over the
    search fallback since we already know the exact product URL."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWT
    cfg  = STORES[banner]
    m    = re.search(r"/p/(\d+)", product_url)
    code = m.group(1) if m else "unknown"
    _log = []
    def _flush(outcome):
        try:
            _dir = os.path.join(os.path.dirname(__file__), "debug_screenshots")
            os.makedirs(_dir, exist_ok=True)
            with open(os.path.join(_dir, f"{banner}_{code}_browser.txt"), "w") as _f:
                _f.write(f"url={product_url}\noutcome={outcome}\n" + "\n".join(_log))
        except Exception:
            pass
    try:
        with sync_playwright() as p:
            ctx, browser, prof = _make_browser_context(p, cfg["type"])
            _log.append("browser context created OK")
            page = ctx.new_page()
            try:
                captured = _intercept_loblaw_price(page, product_url)
                try:
                    page.goto(product_url, wait_until="domcontentloaded", timeout=30_000)
                except PWT:
                    _log.append("goto timed out (page still may have fired API calls)")
                # Wait up to ~15 s for the product-facade JSON response to arrive.
                for _ in range(30):
                    if captured:
                        break
                    page.wait_for_timeout(500)
                if captured:
                    result = _parse_loblaw_api(captured, product_url)
                    _flush("captured JSON, parsed=" + ("OK $" + str(result["price"]) if result else "None"))
                    return result
                _log.append("no product JSON captured within timeout")
                _flush("no_json_captured")
                return None
            finally:
                _close_browser(ctx, browser, prof)
    except Exception as _exc:
        _log.append(f"EXCEPTION: {type(_exc).__name__}: {_exc}")
        _flush("exception")
        return None

def fetch_price_from_url(url: str, banner: str) -> dict | None:
    store_type = STORES[banner]["type"]
    print(f"  [{banner}] fetch start")

    # --- Loblaw (Maxi / Provigo): direct API, no browser needed ---
    if store_type == "loblaw":
        result = _fetch_loblaw_price_direct(url, banner)
        print(f"  [{banner}] loblaw API → {'OK $' + str(result['price']) if result else 'None'}")
        return result

    # --- Metro / Super C: serialised by the CDP worker thread queue ---
    return _fetch_metro_price_via_cdp(url, banner)

def search_store_by_urls(urls: list[str], banner: str) -> list | dict:
    import time
    if not urls:
        return {"error": "No products linked for this store"}
    results = []
    for i, url in enumerate(urls):
        if i > 0:
            time.sleep(0.8)
        r = fetch_price_from_url(url, banner)
        for _retry in range(2):
            if r is not None:
                break
            time.sleep(2)   # short retry for transient errors (a 404 won't become 200)
            r = fetch_price_from_url(url, banner)
        # Fallback: some in-stock SKUs (variable-weight _KG / promo offers) 404 on
        # the Loblaw product-detail API even though the product page renders fine.
        # 1) Load the exact product URL in the REAL Chrome (CDP worker) and capture
        #    the price JSON the page fetches — real Chrome passes Cloudflare, which
        #    blocks headless Firefox/Chromium (confirmed via *_browser/_fallback logs).
        # 2/3) Headless browser + listing-page search as last-resort backups.
        # Only runs after the fast path is exhausted, so normal items are unaffected.
        if r is None and STORES[banner]["type"] == "loblaw" and playwright_available():
            r = _fetch_metro_price_via_cdp(url, banner)   # real Chrome, intercept product JSON
            if r is None:
                r = _fetch_loblaw_price_via_browser(url, banner)
            if r is None:
                r = _fetch_loblaw_price_via_search(url, banner)
        if r:
            results.append(r)
    if not results:
        return {"error": f"Could not load prices from {len(urls)} linked product(s)"}
    results.sort(key=lambda r: r.get("comparison_price") or r.get("price") or float("inf"))
    return results

# ================================================================
# Playwright dynamic search
# ================================================================

def playwright_available() -> bool:
    try:
        import playwright  # noqa
        return True
    except ImportError:
        return False

def search_store_playwright(term: str, banner: str) -> list | dict:
    from playwright.sync_api import sync_playwright, TimeoutError as PWT
    cfg        = STORES[banner]
    url        = cfg["url"]
    base       = re.match(r"https?://[^/]+", url).group(0)
    store_type = cfg["type"]
    with sync_playwright() as p:
        ctx, browser, ff_profile_copy = _make_browser_context(p, store_type)
        page = ctx.new_page()
        try:
            full_url = f"{url}?{cfg['param']}={quote_plus(term)}"
            page.goto(full_url, wait_until="domcontentloaded", timeout=30_000)
            if store_type == "metro":
                # Dismiss cookie banner so page renders real prices
                try:
                    page.wait_for_selector("#onetrust-reject-all-handler", timeout=5_000)
                    btn = page.query_selector("#onetrust-reject-all-handler")
                    if btn:
                        btn.click()
                        page.wait_for_timeout(800)
                except PWT:
                    pass
                try:
                    page.wait_for_selector(".tile-product", timeout=30_000)
                except PWT:
                    return {"error": f"{cfg['label']}: products did not load (30 s timeout)"}
                results = []
                for card in page.query_selector_all(".tile-product")[:8]:
                    name  = card.query_selector(".head__title").inner_text().strip() if card.query_selector(".head__title") else ""
                    brand = card.query_selector(".head__brand").inner_text().strip() if card.query_selector(".head__brand") else ""
                    size  = card.query_selector(".head__unit-details").inner_text().strip() if card.query_selector(".head__unit-details") else ""
                    price_el = card.query_selector(".price-update")
                    unit_el  = card.query_selector(".pricing__secondary-price")
                    link_el  = card.query_selector("a.product-details-link")
                    price_txt = price_el.inner_text().strip() if price_el else ""
                    unit_txt  = unit_el.inner_text().strip()  if unit_el  else ""
                    href      = link_el.get_attribute("href") if link_el  else ""
                    if href and href.startswith("/"):
                        href = base + href
                    price = _parse_price(price_txt)
                    if name and price is not None:
                        results.append(_make_result(name=name, brand=brand, price=price, unit_str=unit_txt or None, size=size, url=href or full_url, image=""))
                # Metro often shows sponsored/featured items first that don't match
                # the search term. Filter to results whose name contains at least one
                # meaningful word from the search query, then sort cheapest first.
                search_words = [w.lower() for w in re.split(r'\W+', term) if len(w) >= 3]
                if search_words:
                    relevant = [r for r in results if any(w in (r.get("name") or "").lower() for w in search_words)]
                    if relevant:
                        results = relevant
                results.sort(key=lambda r: r.get("comparison_price") or r.get("price") or float("inf"))
                return results or {"error": f"No products parsed from {cfg['label']}"}
            else:
                try:
                    page.wait_for_selector('[data-testid="product-tile"]', timeout=30_000)
                except PWT:
                    return {"error": f"{cfg['label']}: products did not load — try using the Catalog tab with direct URLs instead"}
                raw = page.evaluate(r"""
                () => {
                    const tiles = [...document.querySelectorAll('[data-testid="product-tile"]')].slice(0, 8);
                    return tiles.map(tile => {
                        const leaves = [];
                        const walk = el => {
                            if (el.children.length === 0 && el.textContent.trim()) leaves.push(el.textContent.trim());
                            else for (const ch of el.children) walk(ch);
                        };
                        walk(tile);
                        const prices   = leaves.filter(t => /^\$\d+\.\d{2}\b/.test(t) && t.length < 30);
                        const unitP    = leaves.filter(t => /\$[\d.]+\s*\//.test(t));
                        const nonPrice = leaves.filter(t => !t.startsWith('$') && !/^\d/.test(t) && t.length > 3 && t.length < 100 && !['ea.', 'each', 'Save', 'avg.'].some(x => t.includes(x)));
                        const link = tile.querySelector('a')?.getAttribute('href') || '';
                        return { price: prices[0]||null, unitPrice: unitP[0]||null, name: nonPrice[0]||null, brand: nonPrice[1]||null, link };
                    });
                }
                """)
                results = []
                for item in (raw or []):
                    price = _parse_price(item.get("price"))
                    if item.get("name") and price is not None:
                        href = item.get("link", "")
                        if href.startswith("/"):
                            href = base + href
                        results.append(_make_result(name=item["name"], brand=item.get("brand") or "", price=price, unit_str=item.get("unitPrice"), size="", url=href or full_url, image=""))
                return results or {"error": f"No products parsed from {cfg['label']}"}
        except Exception as e:
            return {"error": f"{cfg['label']}: {e}"}
        finally:
            _close_browser(ctx, browser, ff_profile_copy)

def search_store(term: str, banner: str) -> list | dict:
    if not playwright_available():
        return {"error": "Playwright not installed — run: pip install playwright && playwright install chromium firefox"}
    return search_store_playwright(term, banner)

# ================================================================
# SSE helper
# ================================================================

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"

# ================================================================
# Flask routes — catalog CRUD
# ================================================================

@app.route("/api/catalog", methods=["GET"])
def api_catalog_get():
    return jsonify(load_catalog())

@app.route("/api/catalog/items", methods=["POST"])
def api_catalog_add():
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    catalog = load_catalog()
    item = {"id": str(uuid.uuid4()), "name": name, "urls": {b: [] for b in STORES}}
    catalog["items"].append(item)
    save_catalog(catalog)
    return jsonify(item), 201

@app.route("/api/catalog/items/<item_id>", methods=["PUT"])
def api_catalog_update(item_id):
    data = request.get_json() or {}
    catalog = load_catalog()
    for item in catalog["items"]:
        if item["id"] == item_id:
            if "name" in data:
                item["name"] = data["name"].strip()
            if "urls" in data:
                for b in STORES:
                    if b in data["urls"]:
                        item["urls"][b] = [u for u in data["urls"][b] if u.strip()]
            save_catalog(catalog)
            return jsonify(item)
    return jsonify({"error": "not found"}), 404

@app.route("/api/catalog/items/<item_id>", methods=["DELETE"])
def api_catalog_delete(item_id):
    catalog = load_catalog()
    catalog["items"] = [it for it in catalog["items"] if it["id"] != item_id]
    save_catalog(catalog)
    return jsonify({"ok": True})

# ================================================================
# Flask routes — catalog compare (SSE)
# ================================================================

@app.route("/api/catalog/compare", methods=["POST"])
def api_catalog_compare():
    threading.Thread(target=_refresh_access_token, daemon=True).start()
    data     = request.get_json() or {}
    item_ids = data.get("ids")
    catalog  = load_catalog()
    items    = catalog["items"]
    if item_ids is not None:
        items = [it for it in items if it["id"] in item_ids]
    if not items:
        return jsonify({"error": "no items"}), 400

    def generate():
        from queue import Queue, Empty
        total = len(items)
        q = Queue()

        def process_item(idx, item):
            q.put(("searching", idx, item["name"], None))
            store_results = {}
            item_urls = item.get("urls", {})
            with ThreadPoolExecutor(max_workers=4) as ex:
                fmap = {ex.submit(search_store_by_urls, item_urls.get(b, []), b): b for b in STORES}
                for fut in as_completed(fmap):
                    banner = fmap[fut]
                    try:
                        store_results[banner] = fut.result(timeout=360)
                    except Exception as e:
                        store_results[banner] = {"error": str(e)}
                    q.put(("store_done", idx, item["name"], banner))
            q.put(("item_done", idx, item["name"], store_results, item.get("id")))

        item_pool = ThreadPoolExecutor(max_workers=3)
        for idx, item in enumerate(items):
            item_pool.submit(process_item, idx, item)
        item_pool.shutdown(wait=False)

        try:
            done = 0
            while done < total:
                try:
                    msg = q.get(timeout=180)
                    kind, idx, name = msg[0], msg[1], msg[2]
                    msg_data = msg[3] if len(msg) > 3 else None
                    msg_extra = msg[4] if len(msg) > 4 else None
                except Empty:
                    break
                if kind == "searching":
                    yield _sse({"type": "searching", "item": name, "index": idx, "total": total})
                elif kind == "store_done":
                    yield _sse({"type": "store_done", "item": name, "store": msg_data})
                elif kind == "item_done":
                    done += 1
                    yield _sse({"type": "item_done", "item": name, "id": msg_extra,
                                "index": idx, "total": total,
                                "is_weight_item": is_weight_item(name), "results": msg_data})
            yield _sse({"type": "all_done", "total": total})
        finally:
            _shutdown_chrome()

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ================================================================
# Flask routes — single-store retry (SSE)
# ================================================================

@app.route("/api/catalog/retry", methods=["POST"])
def api_catalog_retry():
    data    = request.get_json() or {}
    item_id = data.get("item_id")
    store   = data.get("store")
    if not item_id or not store or store not in STORES:
        return jsonify({"error": "item_id and valid store required"}), 400
    catalog = load_catalog()
    item = next((it for it in catalog["items"] if it["id"] == item_id), None)
    if not item:
        return jsonify({"error": "item not found"}), 404

    def generate():
        try:
            urls   = (item.get("urls") or {}).get(store, [])
            result = search_store_by_urls(urls, store)
        except Exception as e:
            result = {"error": f"Unexpected error: {e}"}
        yield _sse({"type": "retry_done", "item": item["name"], "id": item_id,
                    "store": store, "result": result, "is_weight_item": is_weight_item(item["name"])})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ================================================================
# Flask routes — dynamic search (SSE)
# ================================================================

@app.route("/")
def index():
    return render_template("index.html", playwright_ok=playwright_available())

@app.route("/api/compare", methods=["POST"])
def api_compare():
    threading.Thread(target=_refresh_access_token, daemon=True).start()
    data  = request.get_json() or {}
    items = data.get("items") or []
    if not items:
        return jsonify({"error": "no items"}), 400

    def generate():
        total = len(items)
        for idx, item in enumerate(items):
            yield _sse({"type": "searching", "item": item, "index": idx, "total": total})
            store_results = {}
            with ThreadPoolExecutor(max_workers=4) as ex:
                fmap = {ex.submit(search_store, item, b): b for b in STORES}
                for fut in as_completed(fmap):
                    banner = fmap[fut]
                    try:
                        store_results[banner] = fut.result(timeout=45)
                    except Exception as e:
                        store_results[banner] = {"error": str(e)}
                    yield _sse({"type": "store_done", "item": item, "store": banner})
            yield _sse({"type": "item_done", "item": item, "index": idx, "total": total,
                        "is_weight_item": is_weight_item(item), "results": store_results})
        yield _sse({"type": "all_done", "total": total})

    return Response(stream_with_context(generate()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ================================================================
# Token warmup
# ================================================================

_warmup_status = {"state": "idle", "message": ""}

def _refresh_access_token():
    for banner in ("maxi", "provigo"):
        for c in get_firefox_cookies(f"{banner}.ca"):
            if c["name"] == "AccessToken" and c.get("value"):
                _LOBLAW_API_HEADERS_CACHE["authorization"] = f"Bearer {c['value']}"
                try:
                    with open(_LOBLAW_API_HEADERS_FILE, "w") as f:
                        json.dump(_LOBLAW_API_HEADERS_CACHE, f, indent=2)
                except Exception:
                    pass
                return True
    return False

def _test_api_headers(store_id: str = "8632") -> bool:
    if not _LOBLAW_API_HEADERS_CACHE:
        return False
    test_url = (f"https://api.pcexpress.ca/pcx-bff/api/v1/products/dummy_EA"
                f"?lang=en&date={datetime.now().strftime('%d%m%Y')}&pickupType=STORE&storeId={store_id}&banner=maxi")
    headers = dict(_LOBLAW_API_HEADERS_CACHE)
    headers["User-Agent"] = _UA
    try:
        resp = _requests.get(test_url, headers=headers, timeout=8)
        return resp.status_code in (200, 404)
    except Exception:
        return False

def _do_browser_warmup() -> bool:
    catalog = load_catalog()
    warmup_url = None
    for item in catalog.get("items", []):
        urls = (item.get("urls") or {}).get("maxi", [])
        if urls:
            warmup_url = urls[0]
            break
    if not warmup_url:
        return False
    print(f"  Token warmup: browser run on {warmup_url[:60]}")
    try:
        return fetch_price_from_url(warmup_url, "maxi") is not None
    except Exception:
        return False

def _warmup_worker():
    global _warmup_status
    _warmup_status = {"state": "running", "message": "Refreshing auth tokens..."}
    _refresh_access_token()
    _warmup_status = {"state": "running", "message": "Validating API connection..."}
    if _test_api_headers():
        _warmup_status = {"state": "ready", "message": "API tokens valid"}
        print("  Token warmup: cached headers valid")
        return
    _warmup_status = {"state": "running", "message": "Refreshing API credentials..."}
    print("  Token warmup: cached headers expired, running browser warmup...")
    ok = _do_browser_warmup()
    if ok:
        _warmup_status = {"state": "ready", "message": "API tokens refreshed"}
        print("  Token warmup: complete")
    else:
        _warmup_status = {"state": "warn", "message": "Could not refresh — add a Maxi URL to your catalog, or run Compare once"}
        print("  Token warmup: browser run failed")

@app.route("/api/warmup-status")
def api_warmup_status():
    return jsonify(_warmup_status)

@app.route("/api/warmup", methods=["POST"])
def api_warmup():
    if _warmup_status.get("state") != "running":
        threading.Thread(target=_warmup_worker, daemon=True).start()
    return jsonify({"ok": True})

# ================================================================
# Metro store setup — launches a visible browser so you can select
# your Metro store, then saves the cookies for headless scraping.
# ================================================================

_metro_setup_status = {"state": "idle", "message": ""}

async def _metro_setup_async():
    """Open a persistent Chromium profile (visible window) so the user can select
    their Metro store. Everything — cookies AND localStorage — is saved automatically
    to METRO_PROFILE_DIR by Playwright's persistent context.  Subsequent headless
    scrapes launch the same profile, so Metro sees the exact same 'browser'.
    """
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        from playwright.async_api import async_playwright

    # Terminate any running debug Chrome to avoid profile lock conflict
    global _chrome_proc
    with _chrome_proc_lock:
        if _chrome_proc is not None and _chrome_proc.poll() is None:
            try:
                _chrome_proc.terminate()
                _chrome_proc.wait(timeout=5)
            except Exception:
                pass
            _chrome_proc = None
    # (CDP worker will reconnect automatically when Chrome restarts)

    _copy_metro_profile()

    verified_price = None
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            METRO_PROFILE_RUN, headless=False,
            args=["--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
            user_agent=_UA, locale="en-CA",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto("https://www.metro.ca/en/online-grocery",
                        wait_until="domcontentloaded", timeout=30_000)
        try:
            await ctx.wait_for_event("close", timeout=300_000)
        except Exception:
            pass

        # Verify store selection by checking avocado price
        try:
            vpage = await ctx.new_page()
            await vpage.goto(
                "https://www.metro.ca/en/online-grocery/aisles/fruits-vegetables"
                "/fruits/avocadoes-exotic-fruits/avocado/p/4770",
                wait_until="domcontentloaded", timeout=30_000,
            )
            await vpage.wait_for_timeout(8_000)
            price_el = await vpage.query_selector(".price-update")
            if price_el:
                txt = await price_el.inner_text()
                m = re.search(r"\$\s*(\d+\.\d{2})", txt)
                if m:
                    verified_price = f"${m.group(1)}"
        except Exception:
            pass

        try:
            await ctx.close()
        except Exception:
            pass

    return verified_price


def _metro_setup_worker():
    import asyncio
    global _metro_setup_status
    _metro_setup_status = {
        "state": "running",
        "message": "Opening Metro in a browser window — select your store, then close the window.",
    }
    try:
        verified_price = asyncio.run(_metro_setup_async())
        if verified_price and verified_price != "$9.99":
            _metro_setup_status = {
                "state": "done",
                "message": f"Store saved! Avocado price verified: {verified_price}. Prices should now be correct.",
            }
        elif verified_price == "$9.99":
            _metro_setup_status = {
                "state": "warn",
                "message": "Profile saved but avocado still shows $9.99 — your store may not have been set. Try again and make sure to click 'Set my store'.",
            }
        else:
            _metro_setup_status = {
                "state": "done",
                "message": "Profile saved. Run a price scan to confirm prices are correct.",
            }
    except Exception as e:
        _metro_setup_status = {"state": "error", "message": f"Error: {e}"}

@app.route("/api/metro-setup-status")
def api_metro_setup_status():
    return jsonify(_metro_setup_status)

@app.route("/api/metro-setup", methods=["POST"])
def api_metro_setup():
    if _metro_setup_status.get("state") == "running":
        return jsonify({"ok": False, "message": "Setup already running — check the browser window."})
    threading.Thread(target=_metro_setup_worker, daemon=True).start()
    return jsonify({"ok": True})

if __name__ == "__main__":
    def _open():
        import time, subprocess
        time.sleep(1.2)
        ff_paths = [r"C:\Program Files\Mozilla Firefox\firefox.exe", r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"]
        for ff in ff_paths:
            if os.path.exists(ff):
                try:
                    subprocess.Popen([ff, "http://127.0.0.1:5000"])
                    return
                except Exception:
                    pass
        webbrowser.open("http://127.0.0.1:5000")
    threading.Thread(target=_open, daemon=True).start()
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
