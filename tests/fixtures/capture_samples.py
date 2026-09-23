"""Capture raw EKT API responses: uv run python tests/fixtures/capture_samples.py."""

import os
import re
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

API_URL = "https://ekt.kz/api/products"
FIXTURES_DIR = Path(__file__).resolve().parent
PROJECT_DIR = FIXTURES_DIR.parent.parent


def capture(client: httpx.Client, url: str, params: dict, filename: str) -> object:
    response = client.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    (FIXTURES_DIR / filename).write_bytes(response.content)
    print(f"Saved tests/fixtures/{filename}", flush=True)
    return payload


def main() -> int:
    try:
        # Existing environment variables take precedence over both .env files.
        load_dotenv(PROJECT_DIR / ".env", override=False)
        load_dotenv(PROJECT_DIR.parent / ".env", override=False)
        user = os.environ.get("EKT_API_USER")
        password = os.environ.get("EKT_API_PASSWORD")
        if not user or not password:
            raise ValueError("Set EKT_API_USER and EKT_API_PASSWORD in the environment or .env.")

        with httpx.Client(auth=httpx.BasicAuth(user, password), timeout=15.0) as client:
            page = capture(client, API_URL, {"page": 1}, "api_products_page1.json")
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise ValueError("Expected a JSON object with an 'items' list; inspect the saved page.")
            if len(page["items"]) < 3:
                raise ValueError("The first page contains fewer than 3 products.")

            for item in page["items"][:3]:
                product_id = item.get("id") if isinstance(item, dict) else None
                if type(product_id) not in (int, str) or not re.fullmatch(
                    r"[A-Za-z0-9_-]+", str(product_id)
                ):
                    raise ValueError("Expected a product ID usable in a fixture filename.")
                capture(
                    client,
                    f"{API_URL}/detail",
                    {"id": product_id},
                    f"api_product_detail_{product_id}.json",
                )
    except httpx.HTTPStatusError as error:
        print(f"API returned HTTP {error.response.status_code}.", file=sys.stderr)
        return 1
    except httpx.RequestError:
        print("Could not reach the API; check the network connection.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"Capture failed: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Could not read .env or write fixtures; check file permissions.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
