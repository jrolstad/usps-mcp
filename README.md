# USPS Informed Delivery MCP

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io) server that provides read-only access to [USPS Informed Delivery](https://informeddelivery.usps.com) data — mail pieces, packages, and envelope scan images — for use with AI assistants like Claude.

## Features

- View recent mail pieces delivered in the last 7 days
- Track packages currently in transit
- Retrieve scanned envelope images of incoming mail
- Support multiple USPS accounts simultaneously

## Tools

| Tool | Description |
|------|-------------|
| `get_mail_pieces` | Returns recent mail with delivery date, tracking barcode, sender, and image path |
| `get_packages` | Returns in-transit packages with tracking number, status, and estimated delivery |
| `get_mail_piece_image` | Returns the scanned envelope image (JPEG) for a given mail piece ID |
| `reset_cache` | Clears cached data and forces a fresh fetch on the next call |

## Requirements

- Python 3.7+
- Valid [USPS Informed Delivery](https://informeddelivery.usps.com) account

## Installation

```bash
pip install -r requirements.txt
playwright install chromium
```

## Configuration

Copy `.env.example` to `.env` and set your credentials.

### Multiple accounts (recommended)

```env
USPS_ACCOUNTS=[{"username":"user1@email.com","password":"pass1","label":"Alice"},{"username":"user2@email.com","password":"pass2","label":"Bob"}]
```

### Accounts from a file

```env
USPS_ACCOUNTS_FILE=/path/to/accounts.json
```

The JSON file should be an array of account objects (see `accounts.json.example`).

### Single account

```env
USPS_USERNAME=your_usps_username_or_email
USPS_PASSWORD=your_usps_password
```

## Running

```bash
python server.py
```

On first run the server will open a headless Chromium browser, log in to USPS, and cache the results. Subsequent calls within the cache window (30 minutes client / 6 hours server) return cached data.

## Integrating with Claude Desktop

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "usps": {
      "command": "python",
      "args": ["/path/to/usps-mcp/server.py"],
      "env": {
        "USPS_USERNAME": "your_email@example.com",
        "USPS_PASSWORD": "your_password"
      }
    }
  }
}
```

## Notes

- The server uses browser automation to authenticate with USPS — credentials are never sent anywhere except directly to USPS.
- If your account has MFA enabled, the browser will wait up to 120 seconds for you to complete the challenge.
- When using multiple accounts, a 10-second delay is added between logins to avoid rate limiting.
- Envelope images are cached in the system temp directory under `usps_images/`.
