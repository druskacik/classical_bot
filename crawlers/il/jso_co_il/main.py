import html
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = "https://www.jso.co.il/"
SOURCE = "Jerusalem Symphony Orchestra"
FEED_URL = f"{SOURCE_URL}?feed=eo-events"

# Event Organiser's feed contains venue names rather than separate cities.  The
# calendar is Jerusalem-based, but it also includes performances elsewhere, so
# explicit touring locations must take precedence over the home-city default.
CITY_MARKERS = (
    ("ירושלים", "Jerusalem"),
    ("הנרי קראון", "Jerusalem"),
    ("אולם שרובר", "Jerusalem"),
    ("אולם רבקה קראון", "Jerusalem"),
    ("האוניברסיטה המורמונית", "Jerusalem"),
    ("בנייני האומה", "Jerusalem"),
    ("עין כרם", "Jerusalem"),
    ("ימקא", "Jerusalem"),
    ("מלון המלך דוד", "Jerusalem"),
    ("גן הבונים", "Jerusalem"),
    ("כיכר ספרא", "Jerusalem"),
    ("בריכת הסולטן", "Jerusalem"),
    ("הר הצופים", "Jerusalem"),
    ("אשקלון", "Ashkelon"),
    ("אשדוד", "Ashdod"),
    ("המשכן לאמנויות הבמה", "Ashdod"),
    ("תל אביב", "Tel Aviv"),
    ("הרצליה", "Herzliya"),
    ("פתח תקווה", "Petah Tikva"),
    ("באר שבע", "Beersheba"),
    ("חיפה", "Haifa"),
    ("קריית מוצקין", "Kiryat Motzkin"),
    ("בית גבריאל", "Tzemah"),
    ("קיבוץ דורות", "Dorot"),
    ("ראשון לציון", "Rishon LeZion"),
    ("כפר סבא", "Kfar Saba"),
    ("קיבוץ מזרע", "Mizra"),
    ("קיסריה", "Caesarea"),
    ("אופקים", "Ofakim"),
    ("רעננה", "Ra'anana"),
    ("ריו דה ז'נירו", "Rio de Janeiro"),
    ("בואנוס איירס", "Buenos Aires"),
    ("קמפינאס", "Campinas"),
    ("סאו פאולו", "Sao Paulo"),
    ("רומא", "Rome"),
    ("פריז", "Paris"),
    ("אייזנשטט", "Eisenstadt"),
    ("אקס-אן-פרובנס", "Aix-en-Provence"),
    ("ברלין", "Berlin"),
    ("ארזינג", "Eresing"),
    ("באד פילבל", "Bad Vilbel"),
    ("וינה", "Vienna"),
    ("מינכן", "Munich"),
    ("ואדוץ", "Vaduz"),
    ("רגנסבורג", "Regensburg"),
    ("קלן", "Cologne"),
    ("פירסן", "Viersen"),
    ("אסן", "Essen"),
    ("קובנה", "Kaunas"),
    ("הליטאית", "Vilnius"),
)


def _unfold_ical(value: str) -> str:
    return re.sub(r"\r?\n[ \t]", "", value)


def _unescape_ical(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _properties(block: str) -> dict[str, str]:
    properties = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        properties[key.split(";", 1)[0]] = _unescape_ical(value)
    return properties


def _description(properties: dict[str, str]) -> str | None:
    rich = properties.get("X-ALT-DESC")
    if rich:
        text = BeautifulSoup(html.unescape(rich), "html.parser").get_text("\n", strip=True)
    else:
        text = properties.get("DESCRIPTION", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or None


def _city_for_venue(venue: str) -> str | None:
    for marker, city in CITY_MARKERS:
        if marker in venue:
            return city
    return None


def _parse_datetime(value: str) -> tuple[str, str | None]:
    digits = value.removesuffix("Z")
    if len(digits) == 8:
        parsed = datetime.strptime(digits, "%Y%m%d")
        return parsed.date().isoformat(), None
    parsed = datetime.strptime(digits, "%Y%m%dT%H%M%S")
    # The publisher marks local wall times with Z (the detail pages confirm
    # this), so deliberately do not apply a UTC conversion here.
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def parse_feed(content: str) -> list[dict]:
    records = []
    for block in _unfold_ical(content).split("BEGIN:VEVENT")[1:]:
        properties = _properties(block.split("END:VEVENT", 1)[0])
        required = ("SUMMARY", "DTSTART", "URL", "LOCATION")
        if not all(properties.get(field) for field in required):
            continue

        venue = html.unescape(properties["LOCATION"]).strip()
        city = _city_for_venue(venue)
        if not city:
            log_message(
                "Skipping event with unresolved city",
                event="crawler_event_skipped",
                url=properties["URL"],
            )
            continue

        try:
            event_date, time_from = _parse_datetime(properties["DTSTART"])
        except ValueError as error:
            log_message(
                "Skipping event with invalid date",
                event="crawler_event_skipped",
                url=properties["URL"],
                error_type=type(error).__name__,
                error_message=str(error),
            )
            continue

        records.append(
            {
                "title": html.unescape(properties["SUMMARY"]).strip(),
                "date": event_date,
                "url": properties["URL"].strip(),
                "time_from": time_from,
                # The feed generally publishes 23:59 as a placeholder end;
                # detail pages do not claim a real finishing time.
                "time_to": None,
                "venue": venue,
                "city": city,
                "description": _description(properties),
            }
        )
    return records


class JsoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug="jso_co_il",
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code="IL",
        upload_target="potential",
        dedupe_subset=["url", "date", "time_from", "venue"],
        front_fields=[("source_url", SOURCE_URL), ("source", SOURCE)],
    )

    def scrape(self) -> list[dict]:
        log_message("Fetching event feed", event="crawler_url_fetch", url=FEED_URL)
        response = requests.get(FEED_URL, timeout=60)
        response.raise_for_status()
        return parse_feed(response.text)


def main():
    JsoCrawler().run()


if __name__ == "__main__":
    main()
