#!/usr/bin/env python3
"""Collect the rendered FM LUNATIC card list by repeatedly using its real pagination UI."""
import asyncio, json, os, re, sqlite3, hashlib, time
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

BASE = "https://fmlunatic.com"
LIST_URL = BASE + "/cardlist"
OUT = Path(os.getenv("FM1_OUT", "output"))
IMG = OUT / "images"
RAW = OUT / "raw"
for p in (OUT, IMG, RAW):
    p.mkdir(parents=True, exist_ok=True)
DB = OUT / "FM1_Master_Database.db"
CARD_SELECTOR = "img.card-img-top[src*='storage.googleapis.com/fmdb-bcb70.appspot.com/cardlist/']"
MORE_RE = re.compile(r"\d+\s*개\s*더\s*보기")
network_requests, network_responses = [], []

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS card_listings(
      listing_key TEXT PRIMARY KEY, list_position INTEGER, card_id TEXT,
      name TEXT, category TEXT, asset_code TEXT, thumbnail_url TEXT,
      thumbnail_path TEXT, detail_url TEXT, card_text TEXT,
      list_html TEXT, source_page INTEGER, fetched_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS card_details(
      card_id TEXT PRIMARY KEY, listing_key TEXT, detail_url TEXT,
      page_title TEXT, description TEXT, detail_text TEXT,
      image_urls_json TEXT, image_paths_json TEXT, raw_html_path TEXT,
      network_json TEXT, fetched_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS crawl_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, stage TEXT, status TEXT,
      item_count INTEGER, note TEXT, fetched_at TEXT
    )""")
    # Discard the earlier guessed-ID rows: they were SPA loading placeholders,
    # not verified card records. Real records now live in card_listings.
    con.execute("DELETE FROM cards") if con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards'"
    ).fetchone() else None
    con.commit()
    return con

def parse_counts(text):
    total_m = re.search(r"총\s*([\d,]+)\s*개의\s*데이터", text)
    shown_m = re.search(r"\(([\d,]+)\s*개\s*표시중\)", text)
    to_int = lambda m: int(m.group(1).replace(",", "")) if m else None
    return to_int(total_m), to_int(shown_m)

def listing_key(src, name):
    return hashlib.sha256((src + "\n" + name).encode("utf-8")).hexdigest()

async def get_rendered_cards(page):
    return await page.locator(CARD_SELECTOR).evaluate_all("""els => els.map((img, i) => {
      const card = img.parentElement;
      const p = card ? card.querySelector('p') : null;
      return {
        src: img.getAttribute('src') || img.src || '',
        name: p ? (p.textContent || '').trim() : '',
        html: card ? card.outerHTML : img.outerHTML
      };
    }).filter(x => x.src && x.name)""")

async def record_detail_probe(page, con):
    cards = await get_rendered_cards(page)
    if not cards:
        return
    first = cards[0]
    before_url = page.url
    before_requests = len(network_requests)
    try:
        await page.locator(CARD_SELECTOR).first.locator("xpath=..").click(timeout=15000)
        await page.wait_for_timeout(2200)
        detail_html = await page.content()
        detail_text = (await page.locator("body").inner_text(timeout=10000)).strip()
        detail_url = page.url
        title = await page.title()
        image_urls = await page.locator("img").evaluate_all(
            "els => els.map(e => e.currentSrc || e.src).filter(Boolean)"
        )
        (RAW / "first_card_detail.html").write_text(detail_html, encoding="utf-8")
        (RAW / "first_card_detail.txt").write_text(
            "URL: " + detail_url + "\nTITLE: " + title + "\n\n" + detail_text,
            encoding="utf-8"
        )
        (RAW / "first_card_detail_images.json").write_text(
            json.dumps(image_urls, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # The card ID is accepted only when it is actually exposed by the clicked UI route.
        m = re.search(r"/card/(\d{11})(?:[/?#]|$)", detail_url)
        key = listing_key(first["src"], first["name"])
        if m:
            con.execute("UPDATE card_listings SET card_id=?, detail_url=? WHERE listing_key=?",
                        (m.group(1), detail_url, key))
        con.execute("""INSERT OR REPLACE INTO card_details
          (card_id,listing_key,detail_url,page_title,detail_text,image_urls_json,raw_html_path,network_json,fetched_at)
          VALUES(?,?,?,?,?,?,?,?,datetime('now'))""",
          (m.group(1) if m else "probe:" + key, key, detail_url, title, detail_text,
           json.dumps(image_urls, ensure_ascii=False), str(RAW / "first_card_detail.html"),
           json.dumps(network_requests[before_requests:], ensure_ascii=False)))
        con.execute("INSERT INTO crawl_log(stage,status,item_count,note,fetched_at) VALUES(?,?,?,?,datetime('now'))",
                    ("detail_probe","ok",1,"first visible card click; see raw/first_card_detail.*"))
        con.commit()
    except Exception as e:
        con.execute("INSERT INTO crawl_log(stage,status,item_count,note,fetched_at) VALUES(?,?,?,?,datetime('now'))",
                    ("detail_probe","error",0,repr(e)))
        con.commit()
    # A fresh list navigation restores a clean, known state after the probe.
    await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=90000)
    await page.get_by_text(re.compile(r"총\s*[\d,]+\s*개의\s*데이터")).wait_for(timeout=60000)
    await page.locator(CARD_SELECTOR).first.wait_for(timeout=60000)

async def main():
    con = init_db()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1200})
        page.set_default_timeout(30000)

        def on_request(req):
            # Save request URL/method/body (not cookies or authorization headers).
            if any(x in req.url for x in ("firestore.googleapis.com", "googleapis.com", "firebase")):
                try:
                    network_requests.append({
                        "url": req.url, "method": req.method,
                        "resource_type": req.resource_type,
                        "post_data": (req.post_data or "")[:12000]
                    })
                except Exception:
                    pass

        async def on_response(resp):
            if any(x in resp.url for x in ("firestore.googleapis.com", "googleapis.com", "firebase")):
                item = {"url": resp.url, "status": resp.status,
                        "content_type": resp.headers.get("content-type", "")}
                try:
                    body = await resp.body()
                    if len(body) <= 2_000_000 and (
                        "json" in item["content_type"] or "firestore" in resp.url
                    ):
                        item["body"] = body.decode("utf-8", "replace")
                except Exception as e:
                    item["body_error"] = str(e)
                network_responses.append(item)

        page.on("request", on_request)
        page.on("response", on_response)

        await page.goto(LIST_URL, wait_until="domcontentloaded", timeout=90000)
        await page.get_by_text(re.compile(r"총\s*[\d,]+\s*개의\s*데이터")).wait_for(timeout=90000)
        await page.locator(CARD_SELECTOR).first.wait_for(timeout=90000)
        await page.wait_for_timeout(1800)

        body_text = await page.locator("body").inner_text()
        total, shown = parse_counts(body_text)
        if not total:
            raise RuntimeError("Could not read total card count from rendered card list.")
        if not shown:
            shown = len(await get_rendered_cards(page))
        (RAW / "cardlist_initial.html").write_text(await page.content(), encoding="utf-8")
        (RAW / "initial_counts.json").write_text(
            json.dumps({"total": total, "shown": shown}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

        # Click a card once to learn the actual detail route/modal and network contract.
        await record_detail_probe(page, con)

        seen = {}
        batches = 0
        no_progress = 0
        while True:
            cards = await get_rendered_cards(page)
            if not cards:
                raise RuntimeError("No card entries were rendered by the real card-list UI.")
            for i, row in enumerate(cards, start=1):
                src, name = row["src"], row["name"]
                key = listing_key(src, name)
                if key not in seen:
                    path = urlparse(src).path
                    parts = [x for x in path.split("/") if x]
                    category = parts[-3] if len(parts) >= 3 else None
                    asset_code = Path(path).stem
                    seen[key] = {
                        "listing_key": key, "list_position": len(seen) + 1,
                        "name": name, "category": category, "asset_code": asset_code,
                        "thumbnail_url": src, "list_html": row["html"],
                        "source_page": (len(seen) // 30) + 1
                    }
            current = len(cards)
            con.executemany("""INSERT OR REPLACE INTO card_listings
              (listing_key,list_position,name,category,asset_code,thumbnail_url,list_html,source_page,fetched_at)
              VALUES(:listing_key,:list_position,:name,:category,:asset_code,:thumbnail_url,:list_html,:source_page,datetime('now'))""",
              list(seen.values()))
            con.execute("INSERT INTO crawl_log(stage,status,item_count,note,fetched_at) VALUES(?,?,?,?,datetime('now'))",
                        ("list_page","ok",len(seen),f"loaded visible {current} / expected {total}"))
            con.commit()
            print(f"LIST_PROGRESS {len(seen)}/{total}", flush=True)

            if current >= total:
                break
            more = page.get_by_role("button", name=MORE_RE)
            if await more.count() == 0 or not await more.first.is_visible():
                # Some versions remove the button and keep a separate count label.
                await page.wait_for_timeout(1000)
                fresh_total, fresh_shown = parse_counts(await page.locator("body").inner_text())
                if fresh_shown and fresh_shown >= total:
                    break
                raise RuntimeError(f"Pagination button missing at {current}/{total} cards.")
            before = current
            clicked = False
            for attempt in range(4):
                try:
                    await more.first.scroll_into_view_if_needed()
                    await more.first.click(timeout=20000)
                    clicked = True
                    await page.wait_for_function(
                        """({selector, previous}) => document.querySelectorAll(selector).length > previous""",
                        arg={"selector": CARD_SELECTOR, "previous": before}, timeout=45000
                    )
                    break
                except Exception:
                    if attempt == 3:
                        raise RuntimeError(f"'더 보기' click made no progress after 4 attempts at {before}/{total}.")
                    await page.wait_for_timeout(1200)
            if clicked:
                await page.wait_for_timeout(250)
            after = await page.locator(CARD_SELECTOR).count()
            if after <= before:
                no_progress += 1
                if no_progress >= 2:
                    raise RuntimeError(f"Pagination stalled at {after}/{total}.")
            else:
                no_progress = 0
            batches += 1

        # Final DOM is preserved as evidence that pagination reached the reported total.
        final_html = await page.content()
        (RAW / "cardlist_all_loaded.html").write_text(final_html, encoding="utf-8")
        final_text = await page.locator("body").inner_text()
        final_total, final_shown = parse_counts(final_text)
        if final_shown != final_total or final_total != total:
            raise RuntimeError(f"Final count mismatch: reported {final_shown}/{final_total}; expected {total}.")

        (RAW / "network_requests.json").write_text(
            json.dumps(network_requests, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (RAW / "network_responses.json").write_text(
            json.dumps(network_responses, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # Download each real list thumbnail. Card artwork URLs are taken from the page,
        # never synthesized from guessed numeric IDs.
        sem = asyncio.Semaphore(16)
        async def download_one(rec):
            async with sem:
                url = rec["thumbnail_url"]
                ext = Path(urlparse(url).path).suffix or ".img"
                dest = IMG / (rec["asset_code"] + ext)
                if dest.exists() and dest.stat().st_size > 1000:
                    return str(dest), None
                try:
                    response = await page.request.get(url, timeout=45000)
                    if not response.ok:
                        return None, f"HTTP {response.status}"
                    data = await response.body()
                    if len(data) < 1000:
                        return None, "image response too small"
                    dest.write_bytes(data)
                    return str(dest), None
                except Exception as e:
                    return None, repr(e)

        records = list(seen.values())
        for start in range(0, len(records), 200):
            chunk = records[start:start+200]
            results = await asyncio.gather(*(download_one(x) for x in chunk))
            for rec, (path, err) in zip(chunk, results):
                if path:
                    con.execute("UPDATE card_listings SET thumbnail_path=? WHERE listing_key=?",
                                (path, rec["listing_key"]))
                elif err:
                    con.execute("INSERT INTO crawl_log(stage,status,item_count,note,fetched_at) VALUES(?,?,?,?,datetime('now'))",
                                ("thumbnail","error",0,rec["asset_code"] + ": " + err))
            con.commit()
            print(f"IMAGE_PROGRESS {min(start+len(chunk),len(records))}/{len(records)}", flush=True)

        summary = {
            "total_reported_by_site": total,
            "cards_rendered_and_saved": len(seen),
            "pagination_batches_clicked": batches,
            "thumbnail_files": len(list(IMG.glob("*"))),
            "first_card_detail_probe": "raw/first_card_detail.html",
            "network_requests_logged": len(network_requests),
            "network_responses_logged": len(network_responses),
            "final_display_count": final_shown,
            "db": str(DB)
        }
        (OUT / "crawl_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("CRAWL_SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
        await browser.close()
    con.close()

if __name__ == "__main__":
    asyncio.run(main())
