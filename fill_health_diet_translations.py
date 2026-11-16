#!/usr/bin/env python3
"""Заполнить name_en/name_az в Health-Diet CSV через Google Translate GTX.

Использует тот же endpoint, что и исходный translate_google.py:
https://translate.googleapis.com/translate_a/single?client=gtx...
Без deep-translator. Пакеты здесь — группы задач, а НЕ один Google API запрос.
"""
import argparse
import csv
import json
import logging
import os
import random
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

LOG = logging.getLogger("health_diet_translator")
GOOGLE_URL = "https://translate.googleapis.com/translate_a/single"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"
REQUIRED = ("name_ru", "name_en", "name_az")


def atomic_json(path, content):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def atomic_csv(path, rows, fields):
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, path)


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames
        if not fields or not set(REQUIRED).issubset(fields):
            raise ValueError("CSV должен содержать: " + ", ".join(REQUIRED))
        rows = list(reader)
    return rows, fields


def load_cache(path):
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError("Неверный формат translation_cache.json")
    return data


def google_translate(text, target, retries, timeout):
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": "ru", "tl": target, "dt": "t", "q": text
    })
    req = urllib.request.Request(
        GOOGLE_URL + "?" + params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.load(resp)
            segments = data[0]
            result = "".join(str(seg[0]) for seg in segments if seg and seg[0] is not None).strip()
            if not result:
                raise ValueError("Google вернул пустой перевод")
            return result, None
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError, IndexError,
                KeyError, TypeError, TimeoutError, json.JSONDecodeError) as exc:
            error = str(exc)
            if attempt == retries:
                return None, error
            if isinstance(exc, urllib.error.HTTPError) and exc.code in (403, 429):
                delay = min(60.0, (2 ** attempt) * 4.0) + random.uniform(0, 1)
            else:
                delay = min(12.0, (2 ** attempt)) + random.uniform(0, .5)
            time.sleep(delay)
    return None, "Неизвестная ошибка"


def translate_job(job, retries, timeout, delay):
    text, target = job
    if delay:
        time.sleep(random.uniform(0, delay))
    translated, error = google_translate(text, target, retries, timeout)
    return text, target, translated, error


def apply_cache(rows, cache):
    changed = 0
    for row in rows:
        original = (row.get("name_ru") or "").strip()
        if not original:
            continue
        translations = cache.get(original) or {}
        if not isinstance(translations, dict):
            continue
        for lang in ("en", "az"):
            col = f"name_{lang}"
            translated = translations.get(lang)
            if not (row.get(col) or "").strip() and isinstance(translated, str) and translated.strip():
                row[col] = translated.strip()
                changed += 1
    return changed


def main():
    parser = argparse.ArgumentParser(description="Google GTX перевод названий продуктов Health-Diet")
    parser.add_argument("file", nargs="?", help="CSV (как в старом скрипте)")
    parser.add_argument("--input", dest="input_file", help="CSV (как в новом скрипте)")
    parser.add_argument("--workers", type=int, default=8, help="Параллельные потоки (по умолчанию 8)")
    parser.add_argument("--batch-size", type=int, default=50, help="Размер группы задач (по умолчанию 50)")
    parser.add_argument("--delay", type=float, default=0.2, help="Случайная задержка каждого задания, сек.")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--cache", default="translation_cache.json")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    csv_path = Path(args.input_file or args.file or "health_diet_products.csv").expanduser().resolve()
    cache_path = Path(args.cache).expanduser().resolve()
    if args.workers < 1 or args.batch_size < 1 or args.delay < 0:
        parser.error("--workers и --batch-size должны быть >0; --delay >=0")
    if args.retries < 0 or args.timeout <= 0:
        parser.error("--retries >=0; --timeout >0")

    rows, fields = read_csv(csv_path)
    cache = load_cache(cache_path)

    # Учесть переводы, уже находящиеся в CSV, чтобы повторно их не заказывать.
    for row in rows:
        name = (row.get("name_ru") or "").strip()
        if not name:
            continue
        entry = cache.setdefault(name, {})
        if not isinstance(entry, dict):
            cache[name] = entry = {}
        for lang in ("en", "az"):
            val = (row.get(f"name_{lang}") or "").strip()
            if val and not entry.get(lang):
                entry[lang] = val

    apply_cache(rows, cache)
    jobs = []
    seen = set()
    for row in rows:
        source = (row.get("name_ru") or "").strip()
        if not source:
            continue
        for lang in ("en", "az"):
            if not (row.get(f"name_{lang}") or "").strip() and (source, lang) not in seen:
                jobs.append((source, lang))
                seen.add((source, lang))

    LOG.info("CSV: %s; products=%d; unique translation jobs=%d", csv_path, len(rows), len(jobs))
    if not jobs:
        atomic_csv(csv_path, rows, fields)
        LOG.info("Nothing to translate")
        return

    backup = csv_path.with_name(csv_path.stem + ".before_translation" + csv_path.suffix)
    if not backup.exists():
        shutil.copy2(csv_path, backup)
        LOG.info("Backup: %s", backup)

    successful, failures = 0, 0
    total_batches = (len(jobs) + args.batch_size - 1) // args.batch_size
    try:
        for offset in range(0, len(jobs), args.batch_size):
            batch = jobs[offset:offset + args.batch_size]
            batch_success, batch_failed = 0, 0
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = [
                    executor.submit(translate_job, job, args.retries, args.timeout, args.delay)
                    for job in batch
                ]
                for future in as_completed(futures):
                    source, lang, translated, error = future.result()
                    if translated:
                        cache.setdefault(source, {})[lang] = translated
                        batch_success += 1
                    else:
                        batch_failed += 1
                        LOG.warning("Translation failed [%s] %r: %s", lang, source[:70], error)
            successful += batch_success
            failures += batch_failed
            apply_cache(rows, cache)
            atomic_json(cache_path, cache)
            atomic_csv(csv_path, rows, fields)
            LOG.info("Batch %d/%d: successful=%d failed=%d; saved CSV/cache",
                     offset // args.batch_size + 1, total_batches, batch_success, batch_failed)
            if batch_failed and batch_success == 0:
                LOG.warning("Entire batch failed; stopping to avoid excessive requests. Retry later.")
                break
    except KeyboardInterrupt:
        apply_cache(rows, cache)
        atomic_json(cache_path, cache)
        atomic_csv(csv_path, rows, fields)
        LOG.warning("Interrupted. Saved completed translations.")
        return

    missing = sum(not (row.get(f"name_{lang}") or "").strip()
                  for row in rows for lang in ("en", "az"))
    LOG.info("DONE: new successful requests=%d; failed=%d; missing CSV cells=%d; %s",
             successful, failures, missing, csv_path)


if __name__ == "__main__":
    main()
