#!/usr/bin/env python3
"""USPS Informed Delivery MCP Server — read-only access to mail pieces and packages."""

import time
from pathlib import Path

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image

load_dotenv()

mcp = FastMCP("usps")

CACHE_MAX_AGE = 6 * 60 * 60  # 6 hours in seconds
_cache: dict = {}


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and (time.time() - entry["ts"]) < CACHE_MAX_AGE:
        return entry["data"]
    return None


def _cache_set(key: str, data) -> None:
    _cache[key] = {"ts": time.time(), "data": data}


@mcp.tool()
async def get_mail_pieces() -> list[dict]:
    """
    Get recent mail pieces from USPS Informed Delivery (last 7 days).

    Returns a list of mail pieces with fields:
      - account: which USPS account
      - delivery_date: date delivered (YYYY-MM-DD)
      - id: unique mail piece ID
      - piece_type: type of mail piece
      - tracking: IMB barcode
      - image_url: URL of the envelope scan image
      - local_image_path: local file path if image has been downloaded
    """
    cached = _cache_get("mail_pieces")
    if cached is not None:
        return cached
    from usps_client import get_mail_pieces as _get
    result = await _get()
    _cache_set("mail_pieces", result)
    return result


@mcp.tool()
async def get_packages() -> list[dict]:
    """
    Get packages currently being tracked via USPS Informed Delivery.

    Returns a list of packages with tracking number, status, estimated
    delivery date, and shipper name.
    """
    cached = _cache_get("packages")
    if cached is not None:
        return cached
    from usps_client import get_packages as _get
    result = await _get()
    _cache_set("packages", result)
    return result


@mcp.tool()
async def get_mail_piece_image(piece_id: str) -> Image:
    """
    Get the envelope scan image for a specific mail piece.

    Use get_mail_pieces() first to get piece IDs, then call this tool
    for each piece you want to view. The image shows the front of the
    envelope/mailer as scanned by USPS.

    Args:
        piece_id: The mail piece ID from get_mail_pieces()
    """
    pieces = await get_mail_pieces()

    piece = next((p for p in pieces if p.get("id") == piece_id), None)
    if piece is None:
        raise ValueError(f"Mail piece '{piece_id}' not found. Call get_mail_pieces() first.")

    local_path = piece.get("local_image_path")
    if local_path and Path(local_path).exists():
        return Image(data=Path(local_path).read_bytes(), format="jpeg")

    raise ValueError(
        f"Image not yet downloaded for piece '{piece_id}'. "
        "Trigger a cache refresh by calling reset_cache() then get_mail_pieces()."
    )


@mcp.tool()
async def reset_cache() -> str:
    """
    Clear all cached USPS data, forcing fresh data to be fetched on the next request.
    """
    _cache.clear()
    return "Cache cleared."


if __name__ == "__main__":
    mcp.run()
