#!/usr/bin/env python3
"""Enrich the 15,350 FM Lunatic listings with the site's three detail tabs."""
import asyncio, json, os, re, sqlite3, hashlib
from pathlib import Path
from urllib.parse import urlparse
from playwright.async_api import async_playwright

BASE="https://fmlunatic.com"
LIST=BASE+"/cardlist"
DB=Path(os.getenv("FM1_DB","base/FM1_Master_Database.db"))
OUT=Path("output")
RAW=OUT/"raw"; LARGE=OUT/"large_images"
RAW.mkdir(parents=True,exist_ok=True); LARGE.mkdir(parents=True,exist_ok=True)
CARD_SEL="img.card-img-top[src*='storage.googleapis.com/fmdb-bcb70.appspot.com/cardlist/']"
MORE=re.compile(r"\d+\s*개\s*더\s*보기")
TABS=["분류별","텍스트","기타"]

def db_open():
 c=sqlite3.connect(DB)
 c.execute("""CREATE TABLE IF NOT EXISTS card_detail_full(
  asset_code TEXT PRIMARY KEY, card_name TEXT, category TEXT,
  large_image_url TEXT, large_image_path TEXT,
  tab_data_json TEXT NOT NULL, detail_text TEXT,
  modal_html TEXT, status TEXT, error TEXT, fetched_at TEXT
 )""")
 c.execute("""CREATE TABLE IF NOT EXISTS detail_crawl_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, asset_code TEXT, status TEXT, note TEXT, fetched_at TEXT
 )""")
 c.commit(); return c

async def ensure_all_cards(page):
 await page.goto(LIST,wait_until="domcontentloaded",timeout=90000)
 await page.get_by_text(re.compile(r"총\s*[\d,]+\s*개의\s*데이터")).wait_for(timeout=90000)
 await page.locator(CARD_SEL).first.wait_for(timeout=90000)
 total_text=await page.locator("body").inner_text()
 m=re.search(r"총\s*([\d,]+)\s*개의\s*데이터",total_text)
 total=int(m.group(1).replace(",","")) if m else 0
 while await page.locator(CARD_SEL).count()<total:
  before=await page.locator(CARD_SEL).count()
  btn=page.get_by_role("button",name=MORE)
  if not await btn.count(): raise RuntimeError(f"더 보기 버튼이 없어짐: {before}/{total}")
  await btn.first.scroll_into_view_if_needed()
  await btn.first.click(timeout=30000)
  await page.wait_for_function("({selector, previous}) => document.querySelectorAll(selector).length > previous",arg={"selector":CARD_SEL,"previous":before},timeout=60000)
  print(f"LIST {await page.locator(CARD_SEL).count()}/{total}",flush=True)
 if await page.locator(CARD_SEL).count()!=total: raise RuntimeError("목록 수량 불일치")
 return total

async def main():
 if not DB.exists(): raise RuntimeError(f"기존 목록 DB 없음: {DB}")
 con=db_open()
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True)
  page=await browser.new_page(viewport={"width":1440,"height":1200})
  page.set_default_timeout(30000)
  total=await ensure_all_cards(page)
  cards=page.locator(CARD_SEL)
  print(f"DETAIL_START {total}",flush=True)
  for idx in range(total):
   try:
    img=cards.nth(idx)
    src=await img.get_attribute("src")
    asset=Path(urlparse(src).path).stem
    category=Path(urlparse(src).path).parts[-3]
    name=(await img.locator("xpath=following-sibling::p[1]").inner_text()).strip()
    existing=con.execute("SELECT status FROM card_detail_full WHERE asset_code=?",(asset,)).fetchone()
    if existing and existing[0]=="ok": continue
    await img.locator("xpath=..").click(timeout=30000)
    modal=page.locator(".modal.show")
    await modal.wait_for(state="visible",timeout=20000)
    await page.wait_for_timeout(100)
    title=(await modal.locator(".modal-title").inner_text()).strip()
    large=await modal.locator(".modal-body img").first.get_attribute("src")
    tabdata={}
    for tab in TABS:
     b=modal.get_by_role("button",name=tab,exact=True)
     if await b.count():
      await b.click(timeout=15000)
      await page.wait_for_timeout(80)
      items=await modal.locator(".modal-body .list-group-item").evaluate_all(
       "els=>els.map(e=>({text:(e.innerText||'').trim(),html:e.outerHTML}))")
      tabdata[tab]=items
    modal_html=await modal.inner_html()
    alltext="\n".join(x["text"] for v in tabdata.values() for x in v)
    # Persist after each card so interrupted runs resume safely.
    con.execute("""INSERT OR REPLACE INTO card_detail_full
     (asset_code,card_name,category,large_image_url,tab_data_json,detail_text,modal_html,status,error,fetched_at)
     VALUES(?,?,?,?,?,?,?,?,NULL,datetime('now'))""",
     (asset,title or name,category,large,json.dumps(tabdata,ensure_ascii=False),alltext,modal_html,"ok"))
    con.commit()
    if large:
     try:
      ext=Path(urlparse(large).path).suffix or ".webp"
      dest=LARGE/(asset+ext)
      if not dest.exists():
       r=await page.request.get(large,timeout=30000)
       if r.ok: dest.write_bytes(await r.body())
       if dest.exists():
        con.execute("UPDATE card_detail_full SET large_image_path=? WHERE asset_code=?",(str(dest),asset)); con.commit()
     except Exception as e:
      con.execute("INSERT INTO detail_crawl_log(asset_code,status,note,fetched_at) VALUES(?,?,?,datetime('now'))",(asset,"image_error",repr(e))); con.commit()
    if idx%25==0:
     print(f"DETAIL_PROGRESS {idx+1}/{total} saved={con.execute('select count(*) from card_detail_full where status=\'ok\'').fetchone()[0]}",flush=True)
    await modal.locator("button[aria-label='Close']").click()
    await modal.wait_for(state="hidden",timeout=10000)
   except Exception as e:
    try:
     con.execute("INSERT OR REPLACE INTO card_detail_full(asset_code,card_name,category,tab_data_json,status,error,fetched_at) VALUES(?,?,?,?,?,?,datetime('now'))",
      (asset if 'asset' in locals() else f"index:{idx}",name if 'name' in locals() else "",category if 'category' in locals() else "", "{}", "error",repr(e)))
     con.execute("INSERT INTO detail_crawl_log(asset_code,status,note,fetched_at) VALUES(?,?,?,datetime('now'))",(asset if 'asset' in locals() else str(idx),"error",repr(e)))
     con.commit()
    except Exception: pass
    try:
     if await page.locator(".modal.show").count():
      await page.locator(".modal.show button[aria-label='Close']").click(timeout=3000)
      await page.wait_for_timeout(100)
    except Exception: pass
    if idx%25==0: print(f"DETAIL_ERROR {idx+1}/{total} {repr(e)}",flush=True)
  summary={"site_total":total,"detail_rows":con.execute("select count(*) from card_detail_full").fetchone()[0],
   "successful":con.execute("select count(*) from card_detail_full where status='ok'").fetchone()[0],
   "failed":con.execute("select count(*) from card_detail_full where status!='ok'").fetchone()[0],
   "large_images":len(list(LARGE.glob("*")))}
  (OUT/"detail_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
  print("DETAIL_SUMMARY "+json.dumps(summary,ensure_ascii=False),flush=True)
  await browser.close()
 con.close()

if __name__=="__main__": asyncio.run(main())
