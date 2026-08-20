import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.alice-officialwebsite.com/"
CONCERTS_URL = f"{SOURCE_URL}concerti.html"
SOURCE = "Alice Official Website"

MONTHS = {
    "gennaio": 1,
    "febbraio": 2,
    "marzo": 3,
    "aprile": 4,
    "maggio": 5,
    "giugno": 6,
    "luglio": 7,
    "agosto": 8,
    "settembre": 9,
    "ottobre": 10,
    "novembre": 11,
    "dicembre": 12,
}


def _clean_lines(text):
    return [re.sub(r"\s+", " ", line).strip() for line in text.splitlines() if line.strip()]


def _tour_title(table):
    node = table.previous_sibling
    while node is not None:
        if getattr(node, "name", None) == "table":
            break
        if getattr(node, "name", None) == "img" and node.get("alt"):
            return node["alt"].strip()
        node = node.previous_sibling
    return "Alice in concert"


def _month_year(value):
    match = re.fullmatch(r"([A-Za-zÀ-ÿ]+)\s+(\d{4})", value.strip())
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    return (month, int(match.group(2))) if month else None


def _place_details(text):
    lines = _clean_lines(text)
    time_from = None
    content = []
    for line in lines:
        time_match = re.search(r"\bore\s+(\d{1,2})[,.](\d{2})\b", line, re.IGNORECASE)
        if time_match:
            time_from = f"{int(time_match.group(1)):02d}:{time_match.group(2)}"
            continue
        content.append(line)

    if len(content) < 2:
        return None
    city = re.sub(r"\s*\([A-Z]{2}\)\s*$", "", content[0]).strip()
    venue = content[1].strip()
    if not city or not venue:
        return None
    return city, venue, time_from, "\n".join(content)


def _table_records(table, title):
    records = []
    current_month_year = None
    for row in table.select("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) == 4:
            parsed_month = _month_year(cells[0].get_text(" ", strip=True))
            if not parsed_month:
                continue
            current_month_year = parsed_month
            day_cell, place_cell = cells[1], cells[2]
        elif len(cells) == 3 and current_month_year:
            day_cell, place_cell = cells[0], cells[1]
        else:
            continue

        day_match = re.fullmatch(r"\d{1,2}", day_cell.get_text(" ", strip=True))
        place = _place_details(place_cell.get_text("\n", strip=True))
        if not day_match or not place:
            continue
        month, year = current_month_year
        try:
            event_date = date(year, month, int(day_match.group())).isoformat()
        except ValueError:
            continue
        city, venue, time_from, detail = place
        records.append(
            {
                "title": title,
                "date": event_date,
                "url": CONCERTS_URL,
                "time_from": time_from,
                "venue": venue,
                "city": city,
                "country_code": "IT",
                "description": f"{title}\n{detail}",
            }
        )
    return records


def _feature_record(table):
    lines = _clean_lines(table.get_text("\n", strip=True))
    text = "\n".join(lines)
    date_matches = list(
        re.finditer(
            r"\b(\d{1,2})\s+(GENNAIO|FEBBRAIO|MARZO|APRILE|MAGGIO|GIUGNO|LUGLIO|AGOSTO|SETTEMBRE|OTTOBRE|NOVEMBRE|DICEMBRE)\s+(\d{4})\b",
            text,
            re.IGNORECASE,
        )
    )
    if not date_matches:
        return None
    postponed = re.search(
        r"RINVIATO\s+AL\s+(\d{1,2})\s+"
        r"(GENNAIO|FEBBRAIO|MARZO|APRILE|MAGGIO|GIUGNO|LUGLIO|AGOSTO|SETTEMBRE|OTTOBRE|NOVEMBRE|DICEMBRE)\s+"
        r"(\d{4})",
        text,
        re.IGNORECASE,
    )
    chosen = postponed or date_matches[0]
    event_date = date(
        int(chosen.group(3)), MONTHS[chosen.group(2).lower()], int(chosen.group(1))
    ).isoformat()

    time_match = re.search(r"\bore\s+(\d{1,2})[,.](\d{2})\b", text, re.IGNORECASE)
    time_from = f"{int(time_match.group(1)):02d}:{time_match.group(2)}" if time_match else None
    if "MADRID" in text.upper():
        city, venue, country_code = "Madrid", "Teatros del Canal", "ES"
    elif "PANTHEON ROMA" in text.upper():
        city, venue, country_code = "Roma", "Pantheon", "IT"
    elif "AUDITORIUM PARCO DELLA MUSICA" in text.upper():
        city, venue, country_code = "Roma", "Auditorium Parco della Musica - Cavea", "IT"
    else:
        return None

    title_lines = [line for line in lines[:4] if not re.search(r"\d{4}|RINVIATO", line)]
    title = " - ".join(title_lines[:2]) or "Alice in concert"
    return {
        "title": title,
        "date": event_date,
        "url": CONCERTS_URL,
        "time_from": time_from,
        "venue": venue,
        "city": city,
        "country_code": country_code,
        "description": text,
    }


class AliceOfficialWebsiteCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="alice_officialwebsite_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="IT",
        upload_target="potential",
        dedupe_subset=["title", "date", "venue", "city"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self):
        log_message("Fetching concert archive", event="crawler_url_fetch", url=CONCERTS_URL)
        response = requests.get(CONCERTS_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        records = []
        for table in soup.select("table"):
            if table.select_one("th"):
                records.extend(_table_records(table, _tour_title(table)))
            elif "concerti-annullati" in (table.get("class") or []):
                record = _feature_record(table)
                if record:
                    records.append(record)

        log_message(
            "Concert archive parsed",
            event="crawler_scrape_completed",
            url=CONCERTS_URL,
            record_count=len(records),
        )
        return records


def main():
    AliceOfficialWebsiteCrawler().run()


if __name__ == "__main__":
    main()
