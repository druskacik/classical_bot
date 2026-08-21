import json
import re
import unicodedata
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE = "Joan Magrané Figuera"
SOURCE_URL = "https://joanmagrane.com/"
API_URL = "https://joanmagrane.com/wp-json/wp/v2/pages"
PAGE_SLUGS = ("calendar", "calendar-archive")
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ClassicalBot/1.0)"}

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# The calendar is international, but most entries are in Spain.  These are the
# foreign places used in the published calendar and archive; matching the city
# also avoids treating a country label in parentheses as the city.
FOREIGN_PLACES = {
    "aix en provence": ("Aix-en-Provence", "FR"),
    "amsterdam": ("Amsterdam", "NL"),
    "andorra la vella": ("Andorra la Vella", "AD"),
    "aurillac": ("Aurillac", "FR"),
    "avallon": ("Avallon", "FR"),
    "basel": ("Basel", "CH"),
    "berlin": ("Berlin", "DE"),
    "blagnac": ("Blagnac", "FR"),
    "boswil": ("Boswil", "CH"),
    "brussels": ("Brussels", "BE"),
    "carcassonne": ("Carcassonne", "FR"),
    "chambord": ("Chambord", "FR"),
    "clermont-ferrand": ("Clermont-Ferrand", "FR"),
    "conques": ("Conques", "FR"),
    "delemont": ("Delémont", "CH"),
    "draguignan": ("Draguignan", "FR"),
    "entraigues": ("Entraigues", "FR"),
    "gignac": ("Gignac", "FR"),
    "glasgow": ("Glasgow", "GB"),
    "hamburg": ("Hamburg", "DE"),
    "heidenheim": ("Heidenheim", "DE"),
    "heidelberg": ("Heidelberg", "DE"),
    "himara": ("Himara", "AL"),
    "hitzaker": ("Hitzacker", "DE"),
    "huddersfield": ("Huddersfield", "GB"),
    "ibos": ("Ibos", "FR"),
    "jussy": ("Jussy", "CH"),
    "kansas city": ("Kansas City", "US"),
    "la trinite sur mer": ("La Trinité-sur-Mer", "FR"),
    "lausanne": ("Lausanne", "CH"),
    "le noirmont": ("Le Noirmont", "CH"),
    "lago maggiore": ("Lago Maggiore", "IT"),
    "lisboa": ("Lisbon", "PT"),
    "london": ("London", "GB"),
    "lucerne": ("Lucerne", "CH"),
    "maguelone": ("Villeneuve-lès-Maguelone", "FR"),
    "marmoutier": ("Marmoutier", "FR"),
    "marseille": ("Marseille", "FR"),
    "marvejols": ("Marvejols", "FR"),
    "mirepoix": ("Mirepoix", "FR"),
    "narbonne": ("Narbonne", "FR"),
    "neumarkt in der oberpfalz": ("Neumarkt in der Oberpfalz", "DE"),
    "niederbronn-les-bains": ("Niederbronn-les-Bains", "FR"),
    "ojai": ("Ojai", "US"),
    "orleans": ("Orléans", "FR"),
    "oxford": ("Oxford", "GB"),
    "paris": ("Paris", "FR"),
    "perpignan": ("Perpignan", "FR"),
    "quimper": ("Quimper", "FR"),
    "rome": ("Rome", "IT"),
    "rio de janeiro": ("Rio de Janeiro", "BR"),
    "saint-tropez": ("Saint-Tropez", "FR"),
    "saint savin-en-lavedan": ("Saint-Savin", "FR"),
    "santa monica": ("Santa Monica", "US"),
    "saumur": ("Saumur", "FR"),
    "seattle": ("Seattle", "US"),
    "soreze": ("Sorèze", "FR"),
    "strasbourg": ("Strasbourg", "FR"),
    "stuttgart": ("Stuttgart", "DE"),
    "tirana": ("Tirana", "AL"),
    "toulouse": ("Toulouse", "FR"),
    "traunstein": ("Traunstein", "DE"),
    "vitry": ("Vitry-sur-Seine", "FR"),
    "udine": ("Udine", "IT"),
    "universidad de la serena": ("La Serena", "CL"),
    "vendome": ("Vendôme", "FR"),
    "geneve": ("Geneva", "CH"),
    "vezelay": ("Vézelay", "FR"),
    "zulte": ("Zulte", "BE"),
}

VENUE_CITY_DEFAULTS = {
    "auditorio de antigua": ("Antigua", "ES"),
    "auditorio nacional (s. de camara). madrid": ("Madrid", "ES"),
    "ateneu barcelones": ("Barcelona", "ES"),
    "catedral de barcelona": ("Barcelona", "ES"),
    "centre cultural papa calixte iii": ("Canals", "ES"),
    "donosti, teatro victoria eugenia": ("San Sebastián", "ES"),
    "eglise de santa maria d'aneu": ("La Guingueta d'Àneu", "ES"),
    "festival messiaen au pays de la meije": ("La Grave", "FR"),
    "france musique": ("Paris", "FR"),
    "iglesia vieja del real monasterio de san lorenzo de el escorial": ("San Lorenzo de El Escorial", "ES"),
    "iec, pati de la casa de convalescencia": ("Barcelona", "ES"),
    "palau de la musica catalana": ("Barcelona", "ES"),
    "palau del musica": ("Barcelona", "ES"),
    "sant miquel de cuixa": ("Codalet", "FR"),
    "santa maria d'aneu": ("La Guingueta d'Àneu", "ES"),
    "teatro circo de marte": ("Santa Cruz de La Palma", "ES"),
    "teatro victor fernandez gopar": ("Arrecife", "ES"),
}


def _plain(value):
    return unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()


def _clean_lines(paragraph):
    return [
        line.strip(" \xa0+")
        for line in paragraph.get_text("\n", strip=True).splitlines()
        if line.strip(" \xa0+").lower() not in {"", "info"}
    ]


def _parse_times(text):
    matches = re.findall(
        r"(?i)(\d{1,2})(?:h(\d{0,2})|[:.](\d{2}))\s*(am|pm)?|\b(\d{1,2})\s*(am|pm)\b",
        text,
    )
    values = []
    for hour, hminute, colon_minute, meridiem, bare_hour, bare_meridiem in matches[-2:]:
        hour = int(hour or bare_hour)
        minute = hminute or colon_minute or "0"
        meridiem = meridiem or bare_meridiem
        if meridiem and hour < 12:
            hour += 12 if meridiem.lower() == "pm" else 0
        if meridiem and hour == 12 and meridiem.lower() == "am":
            hour = 0
        if 0 <= hour <= 23:
            values.append(f"{hour:02d}:{int(minute or 0):02d}")
    return (values[0] if values else None, values[1] if len(values) > 1 else None)


def _strip_time(text):
    text = re.sub(r"\s*\([^)]*\d{1,2}\s*(?:(?:h|:|\.)(?:\d{2})?|[ap]m)[^)]*\)\s*$", "", text, flags=re.I)
    text = re.sub(r"^\s*\d{1,2}(?:h\d{0,2}|[:.]\d{2})(?:\s*-\s*\d{1,2}(?:h\d{0,2}|[:.]\d{2}))?\s*,?\s*", "", text, flags=re.I)
    return text.rstrip(" +,-")


def _place(location):
    cleaned = _strip_time(location)
    plain = _plain(cleaned)
    for needle, result in FOREIGN_PLACES.items():
        if needle in plain:
            return cleaned, result[0], result[1]

    for needle, (city, country_code) in VENUE_CITY_DEFAULTS.items():
        if needle in plain:
            return cleaned, city, country_code

    if re.search(r"\([^)]+\)\s*$", cleaned):
        candidate = re.findall(r"\(([^)]+)\)", cleaned)[-1].strip()
        if not re.search(r"\d|\b(?:spain|france|germany|belgium|albania|uk)\b", candidate, re.I):
            return cleaned, candidate, "ES"

    parts = [part.strip() for part in cleaned.split(",") if part.strip()]
    if len(parts) >= 2:
        city = re.sub(r"\s*\([^)]*\)\s*$", "", parts[-1]).strip()
        if city.lower() in {"mallorca", "menorca"} and len(parts) >= 3:
            city = re.sub(r"\s*\([^)]*\)\s*$", "", parts[-2]).strip()
        if city and not re.search(r"\d", city):
            return cleaned, city, "ES"

    parentheses = [x.strip() for x in re.findall(r"\(([^)]+)\)", cleaned)]
    for candidate in reversed(parentheses):
        if not re.search(r"\d|\b(?:spain|france|germany|belgium|albania|uk)\b", candidate, re.I):
            return cleaned, candidate, "ES"
    return None


def _dates(year, date_text):
    match = re.search(r"—?([a-z]+)\s+([\d/]+)", date_text, re.I)
    if not match or match.group(1).lower() not in MONTHS:
        return []
    result = []
    for day_text in match.group(2).split("/"):
        try:
            result.append(date(year, MONTHS[match.group(1).lower()], int(day_text)).isoformat())
        except ValueError:
            continue
    return result


def _parse_page(page):
    grid = json.loads(page["grid"])
    records = []
    for block in grid.get("cont", []):
        if block.get("type") != "text":
            continue
        soup = BeautifulSoup(block.get("cont", ""), "html.parser")
        year = None
        for paragraph in soup.find_all("p"):
            lines = _clean_lines(paragraph)
            if not lines:
                continue
            year_match = re.fullmatch(r"(20\d{2})(?:\s*\(past events\))?", lines[0], re.I)
            if year_match:
                year = int(year_match.group(1))
                continue
            marker = paragraph.find(class_="_small_caps")
            date_text = marker.get_text(" ", strip=True) if marker else lines[0]
            if year is None or not re.match(r"^—?[a-z]+\s+\d", date_text, re.I):
                continue
            event_dates = _dates(year, date_text)
            if not event_dates or len(lines) < 3:
                continue

            # Multiple independent performances occasionally share one malformed
            # paragraph.  They cannot be split reliably, so omit rather than
            # attach the wrong venue or title.
            if len(re.findall(r"\+\s*info", paragraph.get_text(" ", strip=True), re.I)) > 1:
                continue

            italic = [tag.get_text(" ", strip=True) for tag in paragraph.find_all("i")]
            title = " ".join(x for x in italic if x).strip() or lines[1]
            title = re.sub(r"\s+", " ", title).strip()
            if not title:
                continue

            location = lines[-1]
            if re.fullmatch(r"\(?\d{1,2}(?:[h:.]\d{0,2})?(?:\s*-\s*\d{1,2}(?:[h:.]\d{0,2})?)?\)?", location, re.I):
                if len(lines) < 4:
                    continue
                location = lines[-2]
            place = _place(location)
            if not place or _plain(place[0]) == _plain(place[1]):
                continue
            venue, city, country_code = place
            if "france musique" in _plain(venue):
                continue  # Radio broadcast, not a concrete public performance.

            time_from, time_to = _parse_times(" ".join(lines[-2:]))
            link = paragraph.find("a", href=True)
            url = link["href"].strip() if link else page["link"]
            description = "\n".join(lines[1:])
            for event_date in event_dates:
                records.append({
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": time_from,
                    "time_to": time_to,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description,
                })
    return records


class JoanMagraneCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="joanmagrane_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "time_from", "venue", "city"],
    )

    def scrape(self):
        records = []
        with requests.Session() as session:
            session.headers.update(HEADERS)
            for slug in PAGE_SLUGS:
                log_message("Fetching calendar page", event="crawler_url_fetch", url=f"{API_URL}?slug={slug}")
                response = session.get(API_URL, params={"slug": slug}, timeout=30)
                response.raise_for_status()
                pages = response.json()
                if not pages:
                    log_message("Calendar page not found", event="crawler_page_missing", url=response.url)
                    continue
                records.extend(_parse_page(pages[0]))
        log_message("Calendar parsed", event="crawler_scrape_completed", record_count=len(records))
        return records


def main():
    JoanMagraneCrawler().run()


if __name__ == "__main__":
    main()
