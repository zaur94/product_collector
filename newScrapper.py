#!/usr/bin/env python3
"""Health-Diet parser. Normal run recreates CSV; --translate-only resumes translation."""
import argparse
import csv
import json
import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from deep_translator import GoogleTranslator
from deep_translator.exceptions import TooManyRequests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Fill this mapping with category URLs and their exact labels.
CATEGORIES = {
    "https://health-diet.ru/base_of_food/food_101275/": {
        "categoryNameRu": "Чудо",
        "categoryNameEn": "Chudo",
        "categoryNameAz": "Çudo",
    },
    "https://health-diet.ru/base_of_food/food_101632/": {
        "categoryNameRu": "Савушкин",
        "categoryNameEn": "Savushkin",
        "categoryNameAz": "Savuşkin",
    },
    "https://health-diet.ru/base_of_food/food_139370/": {
        "categoryNameRu": "Milla",
        "categoryNameEn": "Milla",
        "categoryNameAz": "Milla",
    },
}

FIELDS = ["sku", "name_ru", "name_en", "name_az", "category_ru", "category_en",
          "category_az", "kcal", "protein", "fat", "carbs"]
PRODUCT_ID = re.compile(r"/base_of_food/sostav/(\d+)\.php(?:[?#]|$)")
NUMBER = re.compile(r"[-+]?\d+(?:[.,]\d+)?")
LOG = logging.getLogger("health_diet")


def atomic_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def load_json(path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def get_session():
    session = requests.Session()
    retry = Retry(total=5, connect=5, read=5, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504],
                  allowed_methods=["GET"], respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    })
    return session


def normalize_number(value):
    raw = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    match = NUMBER.search(raw)
    if not match:
        return None
    number = float(match.group())
    return str(int(number)) if number.is_integer() else format(number, ".12g")


def extract_products(html, page_url, category):
    soup = BeautifulSoup(html, "lxml")
    products = []
    seen = set()
    for table in soup.select("table.uk-table"):
        for tr in table.select("tr"):
            link = tr.select_one('a[href*="/base_of_food/sostav/"]')
            if not link:
                continue
            full_url = urljoin(page_url, link.get("href", ""))
            match = PRODUCT_ID.search(full_url)
            if not match:
                continue
            sku = "HD-" + match.group(1)
            if sku in seen:
                continue
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 5:
                continue
            name = link.get_text(" ", strip=True)
            if not name:
                continue
            nutrients = [normalize_number(td.get_text(" ", strip=True)) for td in cells[1:5]]
            if any(n is None for n in nutrients):
                LOG.warning("Missing nutrients for %s (%s)", sku, name)
                continue
            products.append({
                "sku": sku, "name_ru": name, "name_en": "", "name_az": "",
                "category_ru": category["categoryNameRu"],
                "category_en": category["categoryNameEn"],
                "category_az": category["categoryNameAz"],
                "kcal": nutrients[0], "protein": nutrients[1],
                "fat": nutrients[2], "carbs": nutrients[3],
            })
            seen.add(sku)
    return products


def read_existing_csv(path):
    """Read old CSV safely. Back up incompatible files instead of overwriting them."""
    rows = {}
    if not path.exists():
        return rows
    if path.stat().st_size == 0:
        LOG.warning("Empty CSV detected: %s; starting fresh", path)
        return rows

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(8192)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = ","
        reader = csv.DictReader(f, delimiter=delimiter)
        fieldnames = [name.strip().lstrip("\ufeff") for name in (reader.fieldnames or [])]
        if set(FIELDS).issubset(fieldnames):
            for original in reader:
                row = {(k or "").strip().lstrip("\ufeff"): v for k, v in original.items() if k is not None}
                sku = (row.get("sku") or "").strip()
                if sku:
                    rows[sku] = {key: (row.get(key) or "") for key in FIELDS}
            LOG.info("Loaded %d existing products from %s", len(rows), path)
            return rows

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = path.with_name(f"{path.stem}.backup_{stamp}{path.suffix}")
    path.rename(backup)
    LOG.warning(
        "Incompatible CSV header (%s). Preserved original as %s; starting a new CSV.",
        ", ".join(fieldnames) or "empty", backup,
    )
    return rows


def write_csv(path, rows):
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows.values())
    temp.replace(path)


class TranslationRateLimited(Exception):
    """Stop a translation run cleanly when Google rate-limits the client."""


def translate(text, lang, cache, translator, cache_path, delay):
    if not text.strip():
        return ""
    key = f"ru:{lang}:{text}"
    if key in cache:
        return cache[key]
    # A request delay belongs BEFORE the next request, including the first one.
    time.sleep(max(0, delay) + random.uniform(0.3, 1.0))
    try:
        result = translator[lang].translate(text)
    except TooManyRequests as exc:
        raise TranslationRateLimited(str(exc)) from exc
    except Exception as exc:
        LOG.warning("Translation error (%s, %s): %s; leaving translation empty", lang, text, exc)
        return ""
    if not result or not result.strip():
        LOG.warning("Empty translation (%s, %s)", lang, text)
        return ""
    cache[key] = result.strip()
    atomic_json(cache_path, cache)
    return cache[key]


def validate_categories(categories):
    if not categories:
        raise ValueError("CATEGORIES must not be empty")
    for url, category in categories.items():
        if not url.startswith(("https://", "http://")):
            raise ValueError(f"Invalid category URL: {url}")
        for key in ("categoryNameRu", "categoryNameEn", "categoryNameAz"):
            if not isinstance(category.get(key), str) or not category[key].strip():
                raise ValueError(f"Missing {key} for {url}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="health_diet_products.csv")
    parser.add_argument("--cache", default="health_diet_translations.json")
    parser.add_argument("--checkpoint", default="health_diet_kbju_checkpoint.jsonl")
    parser.add_argument("--delay", type=float, default=3.0, help="Minimum seconds between Google requests (default 3)")
    parser.add_argument("--request-delay", type=float, default=1.0)
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--translate-only", action="store_true",
                        help="Continue translating the current CSV instead of collecting data anew")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    output = Path(args.output)
    cache_path = Path(args.cache)
    checkpoint = Path(args.checkpoint)
    if args.translate_only:
        if not output.is_file():
            parser.error(f"CSV not found for --translate-only: {output}")
        rows = read_existing_csv(output)
    else:
        # Normal runs always start with a fresh CSV. No old CSV is read or merged.
        rows = {}
        write_csv(output, rows)
        if checkpoint.exists():
            checkpoint.unlink()
        LOG.info("Starting fresh CSV: %s (previous data discarded)", output)
    cache = load_json(cache_path, {})
    translators = {} if args.no_translate else {
        "en": GoogleTranslator(source="ru", target="en"),
        "az": GoogleTranslator(source="ru", target="az"),
    }

    if not args.translate_only:
        validate_categories(CATEGORIES)
        session = get_session()
        for i, (url, category) in enumerate(CATEGORIES.items(), 1):
            LOG.info("Category %d/%d: %s", i, len(CATEGORIES), url)
            try:
                response = session.get(url, timeout=(15, 45))
                response.raise_for_status()
                found = extract_products(response.text, response.url, category)
                if not found:
                    LOG.error("No products found at %s; check URL or table markup", url)
                    continue
                added = 0
                for item in found:
                    sku = item["sku"]
                    if sku not in rows:
                        rows[sku] = item
                        added += 1
                # Durable raw nutrition checkpoint; append after a successful category parse.
                with checkpoint.open("a", encoding="utf-8") as f:
                    for item in found:
                        f.write(json.dumps(item, ensure_ascii=False) + "\n")
                write_csv(output, rows)
                LOG.info("Found %d; new %d; total %d", len(found), added, len(rows))
            except requests.RequestException as exc:
                LOG.error("Category request failed: %s: %s", url, exc)
            time.sleep(args.request_delay + random.uniform(0, 0.4))

    rate_limited = False
    if not args.no_translate:
        total = len(rows)
        try:
            for index, row in enumerate(rows.values(), 1):
                for lang, field in (("en", "name_en"), ("az", "name_az")):
                    if not row[field].strip():
                        row[field] = translate(row["name_ru"], lang, cache, translators, cache_path, args.delay)
                        if row[field]:
                            write_csv(output, rows)
                if index % 50 == 0:
                    LOG.info("Translated/checked %d/%d", index, total)
        except TranslationRateLimited as exc:
            rate_limited = True
            LOG.error("Google Translate rate limit reached: %s", exc)
            LOG.error("CSV and translation cache are saved. Continue later with --translate-only; "
                      "increase --delay if the limit repeats.")
        except KeyboardInterrupt:
            LOG.warning("Interrupted: saving partial CSV")
            write_csv(output, rows)
            raise SystemExit(130)

    write_csv(output, rows)
    incomplete = sum(not r["name_en"] or not r["name_az"] for r in rows.values())
    LOG.info("DONE: %d unique products; %d incomplete translations; %s", len(rows), incomplete, output)
    if rate_limited:
        LOG.warning("Translation halted by Google throttling; data collection completed")


if __name__ == "__main__":
    main()
