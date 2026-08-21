import re
from datetime import datetime
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


BASE_URL = "https://gavinbryars.com/"
SOURCE = "Gavin Bryars"
HEADERS = {"User-Agent": "classical-concert-crawler/1.0"}

# The calendar follows a UK composer on international engagements. Locations on
# the site are free text, so these first-party spellings are resolved explicitly.
CITY_COUNTRIES = {
    "adelaide": ("Adelaide", "AU"), "antwerp": ("Antwerp", "BE"),
    "annecy": ("Annecy", "FR"), "aulus-les-bains": ("Aulus-les-Bains", "FR"),
    "aulus les bains": ("Aulus-les-Bains", "FR"), "berlin": ("Berlin", "DE"),
    "brighton": ("Brighton", "GB"), "bristol": ("Bristol", "GB"),
    "brussels": ("Brussels", "BE"), "cadaques": ("Cadaqués", "ES"),
    "cambridge": ("Cambridge", "GB"), "chiswick": ("London", "GB"),
    "como": ("Como", "IT"), "cuerres": ("Cuerres", "ES"),
    "dundalk": ("Dundalk", "IE"), "gent": ("Ghent", "BE"),
    "glasgow": ("Glasgow", "GB"), "goole": ("Goole", "GB"),
    "hobart": ("Hobart", "AU"), "hitchin": ("Hitchin", "GB"),
    "ipswich": ("Ipswich", "GB"), "leeds": ("Leeds", "GB"),
    "leicester": ("Leicester", "GB"), "lichfield": ("Lichfield", "GB"),
    "liverpool": ("Liverpool", "GB"), "lodzi": ("Łódź", "PL"),
    "london": ("London", "GB"), "ludwigshafen": ("Ludwigshafen", "DE"),
    "lyon": ("Lyon", "FR"), "madrid": ("Madrid", "ES"),
    "manchester": ("Manchester", "GB"), "milano": ("Milan", "IT"),
    "moers": ("Moers", "DE"), "new york": ("New York", "US"),
    "norwich": ("Norwich", "GB"), "oxford": ("Oxford", "GB"),
    "paris": ("Paris", "FR"), "pärnu": ("Pärnu", "EE"),
    "prato": ("Prato", "IT"), "ravenna": ("Ravenna", "IT"),
    "regensburg": ("Regensburg", "DE"), "rezé": ("Rezé", "FR"),
    "rimini": ("Rimini", "IT"), "sheffield": ("Sheffield", "GB"),
    "sion": ("Sion", "CH"), "southampton": ("Southampton", "GB"),
    "tallinn": ("Tallinn", "EE"), "tours": ("Tours", "FR"),
    "venezia": ("Venice", "IT"), "venice": ("Venice", "IT"),
}
COUNTRY_NAMES = {
    "australia": "AU", "belgium": "BE", "estonia": "EE", "france": "FR",
    "germany": "DE", "ireland": "IE", "italy": "IT", "montenegro": "ME",
    "poland": "PL", "spain": "ES", "switzerland": "CH",
}


def clean(value):
    return re.sub(r"\s+", " ", value or "").strip()


def get_soup(session, url):
    log_message("Fetching page", event="crawler_url_fetch", url=url)
    response = session.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


def parse_date(value):
    value = clean(value).replace("–", "-")
    # Ranges are represented by their first advertised occurrence. This avoids
    # inventing a performance on every intervening day for residencies/runs.
    value = re.sub(r"^[A-Za-z]+,\s*", "", value)
    match = re.fullmatch(r"(\d{1,2})\s+[A-Za-z]+\s*-\s*\d{1,2}\s+([A-Za-z]+)\s+(\d{4})", value)
    if not match:
        match = re.fullmatch(r"(\d{1,2})(?:\s*-\s*\d{1,2})?\s+([A-Za-z]+)\s+(\d{4})", value)
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.groups()), "%d %B %Y").date().isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", value, re.I)
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3).lower()
    if hour == 12:
        hour = 0
    if meridiem == "p":
        hour += 12
    return f"{hour:02d}:{minute:02d}"


def resolve_location(value):
    location = clean(value)
    lowered = location.lower()
    if lowered in {"online", "worldwide"} or "to be confirmed" in lowered:
        return None
    city = country = None
    for token, result in sorted(CITY_COUNTRIES.items(), key=lambda item: -len(item[0])):
        if token in lowered:
            city, country = result
            break
    if not country:
        for name, code in COUNTRY_NAMES.items():
            if re.search(rf"\b{re.escape(name)}\b", lowered):
                country = code
                break
    if not city or not country:
        return None
    venue = clean(location.split(",", 1)[0])
    if venue.lower() in {city.lower(), "montenegro"} or "/" in venue:
        return None
    return venue, city, country


def detail_data(session, url):
    soup = get_soup(session, url)
    article = soup.select_one("main article")
    if not article:
        return None, None
    description_node = article.select_one(".prose")
    description = clean(description_node.get_text("\n", strip=True)) if description_node else None
    time_from = None
    for heading in article.find_all(["h4", "h3"]):
        if clean(heading.get_text(" ", strip=True)).lower() == "when":
            parent_text = clean(heading.parent.get_text(" ", strip=True))
            time_from = parse_time(parent_text)
            break
    return time_from, description or None


def detail_record(session, url):
    """Build a record when Next.js emits a listing only in its flight data."""
    soup = get_soup(session, url)
    article = soup.select_one("main article")
    if not article:
        return None
    title_node = article.find("h1")
    when = where = None
    for heading in article.find_all(["h4", "h3"]):
        label = clean(heading.get_text(" ", strip=True)).lower()
        if label == "when":
            when = clean(heading.parent.get_text(" ", strip=True))[len("When"):].strip()
        elif label == "where":
            where = clean(heading.parent.get_text(" ", strip=True))[len("Where"):].strip()
    event_date = parse_date(re.sub(r"\b\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\b", "", when or "", flags=re.I))
    location = resolve_location(where)
    if not title_node or not event_date or not location:
        return None
    description_node = article.select_one(".prose")
    venue, city, country_code = location
    return {
        "title": clean(title_node.get_text(" ", strip=True)), "date": event_date,
        "url": url, "time_from": parse_time(when or ""), "venue": venue,
        "city": city, "country_code": country_code,
        "description": clean(description_node.get_text("\n", strip=True)) if description_node else None,
    }


def listing_urls():
    yield urljoin(BASE_URL, "events")
    for page in range(1, 5):
        yield urljoin(BASE_URL, f"events?tab=past&page={page}")


class GavinBryarsCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="gavinbryars_com",
        source=SOURCE,
        source_url=BASE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", BASE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "url"],
    )

    def scrape(self):
        session = requests.Session()
        records = []
        seen_listing_urls = set()
        for listing_url in listing_urls():
            soup = get_soup(session, listing_url)
            articles = soup.select("article")
            if not articles:
                paths = dict.fromkeys(re.findall(r'\\?"(/events/[A-Za-z0-9_-]+)', str(soup)))
                for path in paths:
                    url = urljoin(BASE_URL, path)
                    try:
                        record = detail_record(session, url)
                    except requests.RequestException as error:
                        log_message("Event detail fetch failed", event="crawler_url_fetch_failed", url=url,
                                    error_type=type(error).__name__, error_message=str(error))
                        continue
                    if record:
                        records.append(record)
                        seen_listing_urls.add((url, record["date"]))
                continue
            for article in articles:
                link = article.find_parent("a", href=re.compile(r"^/events/"))
                title_node = article.find("h3")
                paragraphs = article.find_all("p")
                if not link or not title_node or len(paragraphs) < 2:
                    continue
                title = clean(title_node.get_text(" ", strip=True))
                event_date = parse_date(paragraphs[-2].get_text(" ", strip=True))
                location = resolve_location(paragraphs[-1].get_text(" ", strip=True))
                if not title or not event_date or not location:
                    continue
                url = urljoin(BASE_URL, link["href"])
                detail_key = (url, event_date)
                if detail_key in seen_listing_urls:
                    continue
                seen_listing_urls.add(detail_key)
                try:
                    time_from, description = detail_data(session, url)
                except requests.RequestException as error:
                    log_message("Event detail fetch failed", event="crawler_url_fetch_failed", url=url,
                                error_type=type(error).__name__, error_message=str(error))
                    time_from, description = None, None
                venue, city, country_code = location
                records.append({
                    "title": title, "date": event_date, "url": url,
                    "time_from": time_from, "venue": venue, "city": city,
                    "country_code": country_code, "description": description,
                })
        return records


def main():
    GavinBryarsCrawler().run()


if __name__ == "__main__":
    main()
