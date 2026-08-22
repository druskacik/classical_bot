import re
from datetime import datetime, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://sarahtysman.com/en/home-en/"
SOURCE = "Sarah Tysman"

# The calendar is international and often puts the location in the event title.
# Each rule requires a venue (not merely a city) and supplies the corresponding
# ISO country code.  Longer/more specific names must precede shorter ones.
VENUE_RULES = [
    (r"Concert Hall of Slovac Philharmonic", "Concert Hall of the Slovak Philharmonic", "Bratislava", "SK"),
    (r"Festsaal des Walter Schwarzkopf Hauses", "Festsaal des Walter Schwarzkopf Hauses", "Breitenwang", "AT"),
    (r"Palacio de Congresos(?:, Sala Mozart)?", "Palacio de Congresos, Sala Mozart", "Zaragoza", "ES"),
    (r"Palau de la Musica de Valencia", "Palau de la Música de Valencia", "Valencia", "ES"),
    (r"Palau (?:de|di) la M[uú]sica", "Palau de la Música Catalana", "Barcelona", "ES"),
    (r"Teatro P[eé]rez Gald[oó]s", "Teatro Pérez Galdós", "Las Palmas de Gran Canaria", "ES"),
    (r"Gustav Mahler-Saal, Staatsoper Wien", "Gustav Mahler-Saal, Wiener Staatsoper", "Vienna", "AT"),
    (r"Gl[aä]serner Saal, Musikverein", "Gläserner Saal, Musikverein", "Vienna", "AT"),
    (r"Wiener Staatsoper", "Wiener Staatsoper", "Vienna", "AT"),
    (r"Opernhaus Z[uü]rich", "Opernhaus Zürich", "Zurich", "CH"),
    (r"Komische Oper Berlin", "Komische Oper Berlin", "Berlin", "DE"),
    (r"Oper(?:nhaus)? Frankfurt", "Oper Frankfurt", "Frankfurt", "DE"),
    (r"Universit[aä]t der K[uü]nste Berlin|UdK Berlin", "Universität der Künste Berlin", "Berlin", "DE"),
    (r"Waldb[uü]hne Berlin", "Waldbühne Berlin", "Berlin", "DE"),
    (r"Festspielhaus Baden[ -]Baden", "Festspielhaus Baden-Baden", "Baden-Baden", "DE"),
    (r"Bruckner ?Haus Linz", "Brucknerhaus Linz", "Linz", "AT"),
    (r"Musikverein Graz", "Musikverein Graz", "Graz", "AT"),
    (r"Capitole de Toulouse", "Théâtre du Capitole", "Toulouse", "FR"),
    (r"Konzerthaus Bielefeld", "Konzerthaus Bielefeld", "Bielefeld", "DE"),
    (r"Mozarteum Stiftung", "Stiftung Mozarteum", "Salzburg", "AT"),
    (r"Haus f[uü]r Mozart", "Haus für Mozart", "Salzburg", "AT"),
    (r"Osterfestspiele Salzburg", "Osterfestspiele Salzburg", "Salzburg", "AT"),
    (r"Palazzo Colonna", "Palazzo Colonna", "Rome", "IT"),
    (r"Festival (?:de )?[ÚU]beda", "Festival Internacional de Música y Danza de Úbeda", "Úbeda", "ES"),
    (r"Festival Grafenegg", "Grafenegg Festival", "Grafenegg", "AT"),
    (r"Schloss Elmau", "Schloss Elmau", "Krün", "DE"),
    (r"Copenhagen Opera Festival", "Copenhagen Opera Festival", "Copenhagen", "DK"),
    (r"Teatro Liceu", "Gran Teatre del Liceu", "Barcelona", "ES"),
    (r"Auditorio Principe Felipe", "Auditorio Príncipe Felipe", "Oviedo", "ES"),
    (r"Centre de Congressos", "Centre de Congressos d’Andorra la Vella", "Andorra la Vella", "AD"),
    (r"Teatro de la Maestranza", "Teatro de la Maestranza", "Seville", "ES"),
    (r"Teatro Col[oó]n", "Teatro Colón", "A Coruña", "ES"),
    (r"Auditorio de Girona", "Auditori de Girona", "Girona", "ES"),
    (r"Teatro alla Scala", "Teatro alla Scala", "Milan", "IT"),
    (r"Konzerthaus Dortmund", "Konzerthaus Dortmund", "Dortmund", "DE"),
    (r"Landestheater Linz", "Landestheater Linz", "Linz", "AT"),
    (r"Op[eé]ra Paris", "Opéra national de Paris", "Paris", "FR"),
    (r"Ev.-Luth.Kirche, Oldenburg", "Evangelisch-Lutherische Kirche", "Oldenburg", "DE"),
    (r"Forum, Ludwigsburg", "Forum am Schlosspark", "Ludwigsburg", "DE"),
]

NON_EVENT = re.compile(
    r"Asia Tour|Konzerte mit Piotr|Aufnahme|Studienleiterin|Wettbewerb", re.I
)
DATE_RE = re.compile(r"^(\d{2}/\d{2}/\d{4})(?:\s*-\s*(\d{2}/\d{2}/\d{4}))?$")


def _dates(value: str, description: str) -> list[str]:
    match = DATE_RE.fullmatch(value.strip())
    if not match:
        return []
    start = datetime.strptime(match.group(1), "%d/%m/%Y").date()
    if not match.group(2):
        return [start.isoformat()]
    end = datetime.strptime(match.group(2), "%d/%m/%Y").date()
    if end < start or (end - start).days > 7:
        return []
    # Adjacent dates are performances on each day.  For a wider range, the
    # calendar text uses a slash to identify the two actual performance dates.
    if (end - start).days <= 2:
        return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    if re.search(rf"{start.day:02d}\.{start.month:02d}\s*/\s*{end.day:02d}\.{end.month:02d}", description):
        return [start.isoformat(), end.isoformat()]
    return []


def _location(text: str):
    for pattern, venue, city, country_code in VENUE_RULES:
        if re.search(pattern, text, re.I):
            return venue, city, country_code
    return None


class SarahTysmanCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="sarahtysman_com",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target="potential",
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
        dedupe_subset=["title", "date", "venue", "city"],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching calendar", event="crawler_url_fetch", url=SOURCE_URL)
        response = requests.get(SOURCE_URL, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")
        records = []

        for box in soup.select("div.whitebox"):
            heading = box.find("h2")
            if heading is None:
                continue
            parts = list(box.stripped_strings)
            if len(parts) < 2:
                continue
            date_text = heading.get_text(" ", strip=True)
            description = "\n".join(parts[1:]).strip()
            title_node = box.find("strong")
            title = title_node.get_text(" ", strip=True) if title_node else parts[1]
            if not title or NON_EVENT.search(title + "\n" + description):
                continue
            location = _location("\n".join(parts))
            dates = _dates(date_text, description)
            if not location or not dates:
                continue

            venue, city, country_code = location
            link = box.select_one("a[href]")
            url = urljoin(SOURCE_URL, link["href"]) if link else f"{SOURCE_URL}#calendar"
            time_match = re.search(r"\b(?:um|at)\s*(\d{1,2})(?::(\d{2}))?\s*(?:Uhr|h)?\b", description, re.I)
            time_from = None
            if time_match:
                time_from = f"{int(time_match.group(1)):02d}:{int(time_match.group(2) if time_match.group(2) else 0):02d}"

            for event_date in dates:
                records.append({
                    "title": title,
                    "date": event_date,
                    "url": url,
                    "time_from": time_from,
                    "time_to": None,
                    "venue": venue,
                    "city": city,
                    "country_code": country_code,
                    "description": description or None,
                })

        log_message("Calendar parsed", event="crawler_records_parsed", record_count=len(records))
        return records


def main():
    SarahTysmanCrawler().run()


if __name__ == "__main__":
    main()
