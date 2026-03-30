import asyncio
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright, Page

DASHBOARD_URL = "https://informeddelivery.usps.com/portal/dashboard"
LOGIN_URL = (
    "https://reg.usps.com/entreg/LoginAction_input"
    "?app=Phoenix&appURL=https://informeddelivery.usps.com/portal/dashboard"
)

CACHE_TTL_SECONDS = 30 * 60  # 30 minutes

_cache: dict = {
    "mail_pieces": None,
    "packages": None,
    "fetched_at": None,
}
_lock = asyncio.Lock()


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _get_accounts() -> list[dict]:
    accounts_file = os.environ.get("USPS_ACCOUNTS_FILE", "").strip()
    if accounts_file:
        try:
            accounts = json.loads(Path(accounts_file).read_text())
            if isinstance(accounts, list):
                return accounts
        except Exception as e:
            _log(f"[usps] Failed to load USPS_ACCOUNTS_FILE '{accounts_file}': {e}")

    raw = os.environ.get("USPS_ACCOUNTS", "").strip()
    if raw:
        try:
            accounts = json.loads(raw)
            if isinstance(accounts, list):
                return accounts
        except Exception as e:
            _log(f"[usps] Failed to parse USPS_ACCOUNTS: {e}")

    username = os.environ.get("USPS_USERNAME", "")
    password = os.environ.get("USPS_PASSWORD", "")
    if username and password:
        return [{"username": username, "password": password}]

    raise ValueError("No USPS accounts configured.")


def _cache_valid() -> bool:
    if _cache["fetched_at"] is None:
        return False
    age = (datetime.now(timezone.utc) - _cache["fetched_at"]).total_seconds()
    return age < CACHE_TTL_SECONDS


async def _wait_load(page: Page, timeout: int = 8000) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except Exception:
        pass


async def _is_on_dashboard(page: Page) -> bool:
    # Parse only the path, not query params (which contain redirect URLs)
    url = page.url.lower()
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return (
            parsed.netloc == "informeddelivery.usps.com"
            and parsed.path.startswith("/portal")
        )
    except Exception:
        return False


async def _login(page: Page, username: str, password: str) -> None:
    _log(f"[usps] Navigating to login page...")
    await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
    await _wait_load(page)

    if await _is_on_dashboard(page):
        _log("[usps] Already logged in.")
        return

    try:
        await page.wait_for_selector('input[name="username"]', timeout=10000)
        await page.fill('input[name="username"]', username)
        await page.fill('input[name="password"]', password)
        _log(f"[usps] Credentials filled, clicking Sign In...")
        await page.click('button#btn-submit', timeout=5000)
    except Exception as e:
        _log(f"[usps] Login form error: {e}")
        raise

    try:
        await page.wait_for_url(
            lambda url: "reg.usps.com" not in url,
            timeout=20000,
        )
    except Exception:
        pass

    await _wait_load(page)

    if await _is_on_dashboard(page):
        _log("[usps] Login successful.")
        return

    # MFA required
    current_url = page.url.lower()
    if "reg.usps.com" in current_url or "verify" in current_url or "twofactor" in current_url:
        _log(f"[usps] MFA required for {username} — waiting for completion (up to 2 min)...")
        for _ in range(120):
            if page.is_closed():
                break
            if await _is_on_dashboard(page):
                _log("[usps] MFA completed.")
                return
            await asyncio.sleep(1)

    if not await _is_on_dashboard(page):
        raise RuntimeError(f"Login failed — ended on: {page.url}")


def _extract_image_url(image_obj: dict) -> str | None:
    if not image_obj:
        return None
    links = image_obj.get("links", [])
    for link in links:
        href = link.get("href")
        if href:
            return href
    return image_obj.get("imageName")


def _parse_mail_pieces(responses: list[dict], label: str) -> list[dict]:
    """Parse list of per-day API responses from mail-pieces/search."""
    result = []
    for resp in responses:
        date = resp.get("date", "")
        for item in resp.get("mailpieces", []):
            piece_id = item.get("mailpieceID")
            # Skip placeholder "no mail" entries
            if piece_id == "singleBlueBox":
                continue
            result.append({
                "account": label,
                "id": piece_id,
                "delivery_date": date,
                "sender": item.get("senderName") or item.get("MID"),
                "image_url": _extract_image_url(item.get("image")),
                "piece_type": item.get("pieceType"),
                "tracking": item.get("IMB"),
            })
    return result


def _parse_packages(data: dict, label: str) -> list[dict]:
    """Parse packages/search response."""
    if not isinstance(data, dict):
        return []
    result = []
    for item in data.get("inboundPackages", []):
        delivery_info = item.get("deliveryInfo", {})
        result.append({
            "account": label,
            "tracking_number": item.get("trackingNumber"),
            "status": delivery_info.get("text1") or item.get("eventTimestamp"),
            "estimated_delivery": delivery_info.get("deliveryDate"),
            "shipper": item.get("shipperName"),
        })
    return result


async def _fetch_account(username: str, password: str, label: str) -> tuple[list, list]:
    """Login to one USPS account, capture mail + packages via API intercept."""
    mail_responses: list[dict] = []
    packages_data: dict = {}
    access_token: list[str] = []  # mutable container for nonlocal capture

    async def on_response(resp):
        url = resp.url
        if "verified.usps.com" in url and "/access_token" in url:
            try:
                data = await resp.json()
                t = data.get("access_token")
                if t:
                    access_token.clear()
                    access_token.append(t)
            except Exception:
                pass
            return
        if "apis-id.usps.com" not in url:
            return
        try:
            data = await resp.json()
            _log(f"[usps:{label}] API: {url.split('?')[0]}")
            if "mail-pieces/search" in url:
                mail_responses.append(data)
            elif "packages/search" in url:
                packages_data.update(data)
        except Exception:
            pass

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        page.on("response", on_response)

        try:
            await _login(page, username, password)

            # Wait for the dashboard URL (not just callback)
            try:
                await page.wait_for_url(
                    lambda url: "portal/dashboard" in url,
                    timeout=15000,
                )
            except Exception:
                pass

            # Wait for React app to initialize and fire API calls
            await _wait_load(page, timeout=10000)
            await asyncio.sleep(4)

            mail = _parse_mail_pieces(mail_responses, label)
            packages = _parse_packages(packages_data, label)

            # Download images in the same authenticated session
            token = access_token[0] if access_token else None
            if token:
                await _download_images(context, token, mail, label)
            else:
                _log(f"[usps:{label}] No access token captured — images unavailable.")

            _log(f"[usps:{label}] {len(mail)} mail pieces, {len(packages)} packages.")
            return mail, packages

        except Exception as e:
            _log(f"[usps:{label}] ERROR: {e}\n{traceback.format_exc()}")
            raise
        finally:
            await browser.close()


async def _download_images(context, token: str, mail_pieces: list[dict], label: str) -> None:
    """Download mail piece images using the OAuth token and save paths into each piece."""
    import pathlib
    import tempfile
    img_dir = pathlib.Path(tempfile.gettempdir()) / "usps_images"
    img_dir.mkdir(exist_ok=True)

    headers = {"Authorization": f"Bearer {token}"}
    for piece in mail_pieces:
        img_url = piece.get("image_url")
        if not img_url:
            continue
        fname = f"{label.replace(' ', '_')}_{piece['delivery_date']}_{piece['id'][:8]}.jpg"
        fpath = img_dir / fname
        if fpath.exists():
            piece["local_image_path"] = str(fpath)
            continue
        try:
            resp = await context.request.get(img_url, headers=headers)
            if resp.ok:
                fpath.write_bytes(await resp.body())
                piece["local_image_path"] = str(fpath)
                _log(f"[usps:{label}] Image saved: {fpath.name}")
            else:
                _log(f"[usps:{label}] Image HTTP {resp.status}: {img_url}")
        except Exception as e:
            _log(f"[usps:{label}] Image error: {e}")


async def _refresh_cache() -> None:
    accounts = _get_accounts()
    _log(f"[usps] Refreshing cache for {len(accounts)} account(s) sequentially...")

    all_mail: list = []
    all_packages: list = []
    for i, acct in enumerate(accounts):
        if i > 0:
            _log(f"[usps] Waiting 10s before next account to avoid rate limiting...")
            await asyncio.sleep(10)
        username = acct.get("username", "")
        password = acct.get("password", "")
        label = acct.get("label") or username
        try:
            mail, packages = await _fetch_account(username, password, label)
            all_mail.extend(mail)
            all_packages.extend(packages)
        except Exception as e:
            _log(f"[usps] Account '{label}' failed: {e}")

    _cache["mail_pieces"] = all_mail
    _cache["packages"] = all_packages
    _cache["fetched_at"] = datetime.now(timezone.utc)
    _log(f"[usps] Cache populated: {len(all_mail)} mail pieces, {len(all_packages)} packages total.")


async def _ensure_cache() -> None:
    async with _lock:
        if not _cache_valid():
            await _refresh_cache()


async def get_mail_pieces() -> list[dict]:
    await _ensure_cache()
    return _cache["mail_pieces"] or []


async def get_packages() -> list[dict]:
    await _ensure_cache()
    return _cache["packages"] or []
