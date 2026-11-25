"""Read the Federal Reserve's official monetary policy announcements."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree
from urllib.parse import urlparse
from urllib.request import urlopen

import market_cycle

FEED = "https://www.federalreserve.gov/feeds/press_monetary.xml"


def published_news(xml, now):
    items = []
    for item in ElementTree.fromstring(xml).findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        origin = urlparse(url)
        stamp = item.findtext("pubDate")
        if not title or not stamp or origin.scheme != "https" or origin.netloc != "www.federalreserve.gov":
            continue
        published = parsedate_to_datetime(stamp)
        if published.tzinfo is None or published > now:
            continue
        items.append({"title": title, "url": url, "published_at": published.astimezone(timezone.utc).isoformat()})
    if not items:
        raise ValueError("No verified Federal Reserve announcements")
    return {"source": FEED, "items": sorted(items, key=lambda item: item["published_at"], reverse=True)[:2]}


def refresh():
    with urlopen(FEED, timeout=8) as response:
        update = published_news(response.read(), datetime.now(timezone.utc))
    filename = "market-news.json"
    if (market_cycle.DATA / filename).exists():
        previous = market_cycle.read(filename)
        if update == previous or update["items"][0]["published_at"] < previous["items"][0]["published_at"]:
            return
    market_cycle.write(filename, update)
