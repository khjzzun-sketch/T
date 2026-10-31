#!/usr/bin/env python3
"""FM1 card-list/detail crawler. Saves only pages actually verified as cards."""
import asyncio, json, os, re, sqlite3, hashlib, time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from playwright.async_api import async_playwright

BASE = "https://fmlunatic.com"
OUT = Path(os.getenv("FM1_OUT", "output"))
IMG = OUT / "images"
RAW = OUT / "raw"
OUT.mkdir(exist_ok=True); IMG.mkdir(exist_ok=True); RAW.mkdir(exist_ok=True)
DB = OUT / "FM1_Master_Database.db"
ID_RE = re.compile(r"/card/(\d{11})(?:[/?#]|$)")
SEEDS = ["16113028641", "16121533861", "16080333797"]

def init_db():
    con=sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS cards(
      card_id TEXT PRIMARY KEY, name TEXT, detail_url TEXT, page_title TEXT,
      description TEXT, card_text TEXT, image_url TEXT, image_path TEXT,
      metadata_json TEXT, raw_html_path TEXT, status TEXT, fetched_at TEXT
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS crawl_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT, status TEXT, note TEXT, fetched_at TEXT
    )""")
    con.commit(); return con

def ids_in(obj):
    found=set()
    def walk(x):
      if isinstance(x,dict):
        for v in x.values(): walk(v)
      elif isinstance(x,list):
        for v in x: walk(v)
      elif isinstance(x,str):
        found.update(ID_RE.findall(x))
        found.update(re.findall(r'(?<!\d)(1[0-9]{10})(?!\d)',x))
    walk(obj); return found

async def main():
  con=init_db(); discovered=set(SEEDS); response_json=[]
  async with async_playwright() as p:
    browser=await p.chromium.launch(headless=True)
    page=await browser.new_page(viewport={"width":1440,"height":1000})
    async def on_response(resp):
      try:
        ct=resp.headers.get("content-type","")
        if "json" in ct and resp.status==200:
          body=await resp.json()
          response_json.append({"url":resp.url,"body":body})
          discovered.update(ids_in(body))
      except Exception: pass
    page.on("response",on_response)
    try:
      await page.goto(BASE+"/cardlist",wait_until="domcontentloaded",timeout=90000)
      await page.wait_for_timeout(8000)
      # Scroll to bottom repeatedly to trigger infinite-loading lists.
      last=-1; stable=0
      for _ in range(100):
        links=await page.locator('a[href*="/card/"]').evaluate_all("(els)=>els.map(e=>e.href)")
        for u in links: discovered.update(ID_RE.findall(u))
        h=await page.evaluate("document.body.scrollHeight")
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1200)
        if h==last: stable+=1
        else: stable=0
        last=h
        if stable>=5: break
      # Capture app-visible API payloads and any IDs embedded in the rendered document.
      html=await page.content()
      discovered.update(ID_RE.findall(html))
      (RAW/"cardlist.html").write_text(html,encoding="utf-8")
      for i,item in enumerate(response_json):
        (RAW/f"response_{i:04d}.json").write_text(json.dumps(item,ensure_ascii=False),encoding="utf-8")
    except Exception as e:
      con.execute("INSERT INTO crawl_log(url,status,note,fetched_at) VALUES(?,?,?,datetime('now'))",(BASE+"/cardlist","error",repr(e)))
    # For each known 10-digit timestamp prefix, test all ten final digits.
    # This is deliberately bounded to the three user-supplied prefixes, not a blind
    # scan across billions of 11-digit values.
    for seed in SEEDS:
      discovered.update(seed[:-1]+str(d) for d in range(10))
    (OUT/"discovered_ids.txt").write_text("\n".join(sorted(discovered)),encoding="utf-8")
    successes=0
    for cid in sorted(discovered):
      url=f"{BASE}/card/{cid}"
      try:
        resp=await page.goto(url,wait_until="domcontentloaded",timeout=45000)
        await page.wait_for_timeout(1000)
        if not resp or resp.status!=200:
          con.execute("INSERT INTO crawl_log(url,status,note,fetched_at) VALUES(?,?,?,datetime('now'))",(url,"http",str(resp.status if resp else "no response"))); con.commit(); continue
        title=(await page.title()).strip()
        text=(await page.locator("body").inner_text(timeout=10000)).strip()
        # Reject SPA fallback, generic error, and pages with no meaningful card content.
        if len(text)<25 or "You need to enable JavaScript to run this app" in text or ("404" in title and len(text)<300):
          con.execute("INSERT INTO crawl_log(url,status,note,fetched_at) VALUES(?,?,?,datetime('now'))",(url,"not_card",title)); con.commit(); continue
        meta=await page.locator('meta[property="og:image"],meta[name="twitter:image"]').evaluate_all("(els)=>els.map(e=>e.content).filter(Boolean)")
        imgs=await page.locator("img").evaluate_all("(els)=>els.map(e=>({src:e.currentSrc||e.src,alt:e.alt||''})).filter(x=>x.src)")
        image_url=(meta[0] if meta else (imgs[0]["src"] if imgs else None))
        image_path=None
        if image_url:
          try:
            image_url=urljoin(url,image_url)
            ir=await page.request.get(image_url,timeout=30000)
            if ir.ok:
              data=await ir.body()
              ext=Path(urlparse(image_url).path).suffix or ".img"
              image_path=str(IMG/f"{cid}{ext}")
              Path(image_path).write_bytes(data)
          except Exception: image_path=None
        raw_path=RAW/f"{cid}.html"
        raw_path.write_text(await page.content(),encoding="utf-8")
        # Preserve complete visible text and metadata for later field-specific parsing.
        name=title.split("|")[0].strip() if title else None
        description=""
        desc=await page.locator('meta[name="description"]').get_attribute("content")
        description=desc or ""
        metadata={"og_images":meta,"images":imgs,"url":url}
        con.execute("""INSERT INTO cards(card_id,name,detail_url,page_title,description,card_text,image_url,image_path,metadata_json,raw_html_path,status,fetched_at)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
          ON CONFLICT(card_id) DO UPDATE SET name=excluded.name,detail_url=excluded.detail_url,page_title=excluded.page_title,
          description=excluded.description,card_text=excluded.card_text,image_url=excluded.image_url,image_path=excluded.image_path,
          metadata_json=excluded.metadata_json,raw_html_path=excluded.raw_html_path,status=excluded.status,fetched_at=excluded.fetched_at""",
          (cid,name,url,title,description,text,image_url,image_path,json.dumps(metadata,ensure_ascii=False),str(raw_path),"verified_http_page"))
        con.commit(); successes+=1
      except Exception as e:
        con.execute("INSERT INTO crawl_log(url,status,note,fetched_at) VALUES(?,?,?,datetime('now'))",(url,"error",repr(e))); con.commit()
      await page.wait_for_timeout(500)
    await browser.close()
  con.close()
  summary={"candidate_ids":len(discovered),"verified_pages":successes,"db":str(DB),"image_files":len(list(IMG.glob("*")))}
  (OUT/"crawl_summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
  print(json.dumps(summary,ensure_ascii=False))

if __name__=="__main__": asyncio.run(main())
