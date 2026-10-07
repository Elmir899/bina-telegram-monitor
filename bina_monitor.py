import os
import re
import json
import time
import logging
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ===== SETTINGS =====
BINA_URL = "https://bina.az/baki/28-may"
MAX_PRICE = 140_000
CHECK_EVERY_SECONDS = 300  # 5 minutes
STATE_FILE = Path("seen.json")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
    )
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)


def load_seen():
    if not STATE_FILE.exists():
        return set()
    try:
        return set(json.loads(STATE_FILE.read_text(encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen):
    # Keep the file small.
    values = list(seen)[-3000:]
    STATE_FILE.write_text(
        json.dumps(values, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def telegram_send(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    r = session.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )
    r.raise_for_status()


def money_number(text):
    # Handles 140 000, 140,000, 140.000 and plain numbers.
    m = re.search(r"(\d[\d\s.,]*)\s*(?:AZN|₼|манат)?", text, re.I)
    if not m:
        return None
    raw = re.sub(r"[^\d]", "", m.group(1))
    return int(raw) if raw else None


def extract_item_links(html):
    soup = BeautifulSoup(html, "html.parser")
    links = set()

    for a in soup.select('a[href*="/items/"]'):
        href = a.get("href")
        if not href:
            continue
        m = re.search(r"/items/(\d+)", href)
        if m:
            links.add(urljoin("https://bina.az", f"/items/{m.group(1)}"))

    return sorted(links)


def parse_item(url):
    r = session.get(url, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = " ".join(soup.stripped_strings)

    # Hard filters. We deliberately require the exact Bina.az category wording.
    if "Satılır" not in text:
        return None
    if "1 otaqlı" not in text:
        return None
    if "Yeni tikili" not in text:
        return None
    if "28 May m." not in text:
        return None

    price = None
    # Try the page's structured/meta information first.
    for selector in [
        'meta[itemprop="price"]',
        'meta[property="product:price:amount"]',
    ]:
        tag = soup.select_one(selector)
        if tag and tag.get("content"):
            try:
                price = int(float(tag["content"]))
                break
            except Exception:
                pass

    if price is None:
        # Look around AZN occurrences, preferring a normal sale price.
        for candidate in re.findall(r"[\d\s.,]{2,}\s*(?:AZN|₼)", text, re.I):
            n = money_number(candidate)
            if n and n >= 10_000:
                price = n
                break

    if price is None or price > MAX_PRICE:
        return None

    title = soup.title.get_text(" ", strip=True) if soup.title else "Yeni elan"

    # Extract common facts from the page text.
    area = None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*m²", text)
    if m:
        area = m.group(1).replace(",", ".") + " m²"

    floor = None
    # Bina.az commonly shows e.g. 6 / 16 mərtəbə.
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*mərtəbə", text)
    if m:
        floor = f"{m.group(1)}/{m.group(2)}"

    # Try to extract the listing number.
    item_id = re.search(r"/items/(\d+)", url)
    item_id = item_id.group(1) if item_id else url

    return {
        "id": item_id,
        "url": url,
        "title": title,
        "price": price,
        "area": area or "—",
        "floor": floor or "—",
    }


def check_once(seen):
    found = set()

    # First page plus a few pages so a newly added item is not missed
    # if VIP/featured listings occupy the first positions.
    for page in range(1, 6):
        page_url = BINA_URL if page == 1 else f"{BINA_URL}?page={page}"
        try:
            r = session.get(page_url, timeout=25)
            r.raise_for_status()
            links = extract_item_links(r.text)
        except Exception as e:
            logging.warning("Could not read %s: %s", page_url, e)
            continue

        for link in links:
            item_id = re.search(r"/items/(\d+)", link)
            if not item_id:
                continue
            item_id = item_id.group(1)

            # We can skip old IDs after the first successful scan.
            if item_id in seen:
                continue

            try:
                item = parse_item(link)
            except Exception as e:
                logging.warning("Could not parse %s: %s", link, e)
                continue

            # Mark every inspected item as seen, so it won't be repeatedly
            # fetched/considered on subsequent runs.
            seen.add(item_id)

            if item:
                found.add(item_id)
                msg = (
                    "🏠 НОВАЯ КВАРТИРА НА BINA.AZ\n\n"
                    f"💰 {item['price']:,} AZN\n"
                    f"📐 {item['area']}\n"
                    "🛏 1 otaqlı\n"
                    "🏢 Yeni tikili\n"
                    f"🏗 Этаж: {item['floor']}\n\n"
                    f"🔗 {item['url']}"
                ).replace(",", " ")
                telegram_send(msg)
                logging.info("Sent %s", item["url"])

    save_seen(seen)
    return found


def main():
    logging.info("Bina.az monitor started.")
    logging.info("Filter: 1 otaqlı + Yeni tikili + 28 May + <= %s AZN", MAX_PRICE)

    seen = load_seen()

    # IMPORTANT: first run only establishes a baseline and does not spam
    # Telegram with all existing listings.
    if not seen:
        logging.info("First run: creating baseline without notifications.")
        for page in range(1, 6):
            page_url = BINA_URL if page == 1 else f"{BINA_URL}?page={page}"
            try:
                r = session.get(page_url, timeout=25)
                r.raise_for_status()
                for link in extract_item_links(r.text):
                    m = re.search(r"/items/(\d+)", link)
                    if m:
                        seen.add(m.group(1))
            except Exception as e:
                logging.warning("Baseline page error: %s", e)
        save_seen(seen)

    while True:
        try:
            check_once(seen)
        except Exception:
            logging.exception("Check failed")
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
