from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import esprima
import pandas as pd
import requests

from observability import log_message

from .base import BaseCrawler


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
TICKETPORTAL_EPOCH_MINUTES = 23_000_000
EVENT_COLUMNS = (
    "id_delta", "title", "primary_category", "categories", "priority", "image",
    "out_id_delta", "featured", "score",
)
PERFORMANCE_COLUMNS = (
    "id_delta", "event_id_delta", "start_delta", "end_delta", "day_delta",
    "schedule_description", "venue_id_delta", "stage_id_delta", "operator_id_delta",
    "performance_category", "status", "quick_purchase", "is_ht", "changed",
    "sale_start", "date_display_type", "status_id", "external_url",
)
MUSIC_PATTERNS = (
    r"\borgan\w*",
    r"\bvarhan\w*",
    r"\bfilharm\w*",
    r"\bkomorn\w*",
    r"\borchester\b",
    r"\borchestra\b",
    r"\bsymfon\w*",
    r"\bsymphon\w*",
    r"\bpiano\b",
    r"\bklav[ií]r\w*",
    r"\bopera\b",
    r"\bopern\w*",
    r"\bbach\b",
    r"\bmozart\b",
    r"\bverdi\b",
)
AUXILIARY_PATTERNS = (
    r"\bparkovac[ií]\s+(?:l[ií]stek|l[ií]stok)\b",
    r"\bfast\s+track\b",
)
FILM_CONTEXT_PATTERN = re.compile(r"\b(?:film\w*|projekc\w*|soundtrack\w*)\b", re.IGNORECASE)
LIVE_PERFORMANCE_PATTERNS = (
    re.compile(
        r"\bživ\w*(?:\s+[\w-]+){0,3}\s+"
        r"(?:hudb\w*|orchestr\w*|soundtrack\w*|doprov\w*)",
        re.IGNORECASE,
    ),
    re.compile(r"\bkoncertní\s+(?:premiér\w*|podob\w*)", re.IGNORECASE),
    re.compile(r"\b(?:film\w*|projekc\w*)\b.{0,120}\bdoprov\w*", re.IGNORECASE),
)
CLASSICAL_FORCE_PATTERN = re.compile(
    r"\b(?:orchestr\w*|filharm\w*|symfon\w*|komorní\s+(?:soubor\w*|ansámbl\w*)|"
    r"smyčcov\w*|smy[cč]cov\w*|kvartet\w*|quartet\w*|sbor\w*|chorus\w*|"
    r"choir\w*|varhan\w*)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TicketportalSiteConfig:
    base_url: str
    grid_url: str
    language: str
    category_names: frozenset[str]
    detail_filter_category_names: frozenset[str] = field(default_factory=frozenset)
    timezone_name: str = "Europe/Prague"
    music_patterns: tuple[str, ...] = MUSIC_PATTERNS
    auxiliary_patterns: tuple[str, ...] = AUXILIARY_PATTERNS
    excluded_organizer_urls: frozenset[str] = field(default_factory=frozenset)
    request_workers: int = 8
    request_timeout: int = 30


@dataclass(frozen=True)
class GridEvent:
    event_id: int
    title: str
    category_ids: frozenset[int]
    out_id: int


def extract_variable(ast: Any, name: str) -> Any:
    for node in ast.body:
        if node.type != "VariableDeclaration":
            continue
        for declaration in node.declarations:
            if declaration.id.name == name:
                return declaration.init
    raise ValueError(f"Ticketportal grid variable {name!r} is missing")


def eval_element(element: Any) -> Any:
    if element.type == "Literal":
        return element.value
    if element.type == "ArrayExpression":
        return [eval_element(item) for item in element.elements]
    if element.type == "UnaryExpression":
        value = eval_element(element.argument)
        if element.operator == "-":
            return -value
        if element.operator == "+":
            return +value
    raise ValueError(f"Unsupported Ticketportal JavaScript element: {element.type}")


def parse_grid_variables(text: str, names: tuple[str, ...]) -> dict[str, list[Any]]:
    ast = esprima.parse(text)
    return {name: eval_element(extract_variable(ast, name)) for name in names}


def chunked(values: list[Any], size: int) -> list[list[Any]]:
    if len(values) % size:
        raise ValueError(f"Ticketportal array has {len(values)} values, expected a multiple of {size}")
    return [values[index:index + size] for index in range(0, len(values), size)]


def decode_categories(values: list[Any]) -> dict[int, str]:
    return {row[0]: row[1] for row in chunked(values, 4)}


def decode_cities(values: list[Any]) -> dict[int, str]:
    city_id = 0
    cities = {}
    for row in chunked(values, 4):
        city_id += row[0]
        cities[city_id] = row[1]
    return cities


def decode_venues(values: list[Any], cities: dict[int, str]) -> dict[int, tuple[str, str | None]]:
    venue_id = city_id = 0
    venues = {}
    for row in chunked(values, 10):
        venue_id += row[0]
        city_id += row[2]
        venues[venue_id] = (row[1], cities.get(city_id))
    return venues


def decode_outputs(values: list[Any]) -> dict[int, tuple[str, str]]:
    out_id = 0
    outputs = {}
    for row in chunked(values, 5):
        out_id += row[0]
        outputs[out_id] = (row[1], row[2])
    return outputs


def decode_events(values: list[Any]) -> dict[int, GridEvent]:
    event_id = out_id = 0
    events = {}
    for row in chunked(values, len(EVENT_COLUMNS)):
        event_id += row[0]
        out_id += row[6]
        categories = set(row[3] or [])
        if row[2] is not None:
            categories.add(row[2])
        events[event_id] = GridEvent(event_id, str(row[1]).strip(), frozenset(categories), out_id)
    return events


def is_relevant_title(title: str, patterns: tuple[str, ...]) -> bool:
    normalized = title.casefold()
    return any(re.search(pattern, normalized) for pattern in patterns)


def is_auxiliary_title(title: str, patterns: tuple[str, ...]) -> bool:
    normalized = title.casefold()
    return any(re.search(pattern, normalized) for pattern in patterns)


def is_live_film_music_event(description: str | None) -> bool:
    if not description:
        return False
    normalized = " ".join(description.split())
    return (
        bool(FILM_CONTEXT_PATTERN.search(normalized))
        and bool(CLASSICAL_FORCE_PATTERN.search(normalized))
        and any(pattern.search(normalized) for pattern in LIVE_PERFORMANCE_PATTERNS)
    )


def ticketportal_datetime(value: int, timezone_name: str) -> datetime:
    instant = datetime.fromtimestamp((value + TICKETPORTAL_EPOCH_MINUTES) * 60, tz=timezone.utc)
    return instant.astimezone(ZoneInfo(timezone_name))


def decode_occurrences(
    values: list[Any],
    *,
    events: dict[int, GridEvent],
    outputs: dict[int, tuple[str, str]],
    venues: dict[int, tuple[str, str | None]],
    selected_event_ids: set[int],
    detail_filtered_event_ids: set[int] | None = None,
    base_url: str,
    timezone_name: str,
    today: Any | None = None,
) -> list[dict[str, Any]]:
    performance_id = event_id = start = end = day = venue_id = stage_id = operator_id = 0
    records = []
    local_today = today or datetime.now(ZoneInfo(timezone_name)).date()
    detail_filtered_event_ids = detail_filtered_event_ids or set()
    for row in chunked(values, len(PERFORMANCE_COLUMNS)):
        performance_id += row[0]
        event_id += row[1]
        start += row[2]
        end += row[3]
        day += row[4]
        venue_id += row[6]
        stage_id += row[7]
        operator_id += row[8]
        if event_id not in selected_event_ids:
            continue
        event = events.get(event_id)
        output = outputs.get(event.out_id) if event else None
        venue = venues.get(venue_id)
        if not event or not output or not output[1] or not venue or not venue[1]:
            continue
        starts_at = ticketportal_datetime(start, timezone_name)
        if starts_at.date() < local_today:
            continue
        records.append({
            "title": event.title,
            "date": starts_at.date().isoformat(),
            "time_from": starts_at.strftime("%H:%M"),
            "venue": venue[0],
            "city": venue[1],
            "url": urljoin(base_url.rstrip("/") + "/", f"event/{output[1]}"),
            "_ticketportal_requires_detail_filter": event_id in detail_filtered_event_ids,
        })
    return records


def extract_organizer_url(soup: BeautifulSoup, base_url: str) -> str | None:
    content = soup.find("div", class_="detail-content")
    header = content.find("h1") if content else None
    link = header.find("a", href=True) if header else None
    return urljoin(base_url, link["href"]) if link else None


def extract_description(soup: BeautifulSoup) -> str | None:
    section = soup.find("section", class_="popis")
    if not section:
        return None
    guarantee = section.find("div", class_="ticket-guarantee-container")
    if guarantee:
        guarantee.decompose()
    text = section.get_text(" ", strip=True)
    return text or None


class TicketportalCrawler(BaseCrawler):
    site: TicketportalSiteConfig

    def _get(self, url: str) -> requests.Response:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=self.site.request_timeout,
        )
        response.raise_for_status()
        return response

    def _grid_records(self) -> list[dict[str, Any]]:
        variables = parse_grid_variables(
            self._get(self.site.grid_url).text,
            ("data_kategorie", "data_mesto", "data_hladisko", "data_podujatie_out", "data_podujatie", "data_predstavenie"),
        )
        categories = decode_categories(variables["data_kategorie"])
        category_ids = {identifier for identifier, name in categories.items() if name in self.site.category_names}
        missing = self.site.category_names - {categories[identifier] for identifier in category_ids}
        if missing:
            raise ValueError(f"Ticketportal categories are missing: {', '.join(sorted(missing))}")
        detail_filter_category_ids = {
            identifier
            for identifier, name in categories.items()
            if name in self.site.detail_filter_category_names
        }
        missing_detail_categories = self.site.detail_filter_category_names - {
            categories[identifier] for identifier in detail_filter_category_ids
        }
        if missing_detail_categories:
            raise ValueError(
                "Ticketportal detail-filter categories are missing: "
                f"{', '.join(sorted(missing_detail_categories))}"
            )
        events = decode_events(variables["data_podujatie"])
        directly_selected = {
            identifier
            for identifier, event in events.items()
            if not is_auxiliary_title(event.title, self.site.auxiliary_patterns)
            and (
                bool(event.category_ids & category_ids)
                or is_relevant_title(event.title, self.site.music_patterns)
            )
        }
        detail_filtered = {
            identifier
            for identifier, event in events.items()
            if not is_auxiliary_title(event.title, self.site.auxiliary_patterns)
            and bool(event.category_ids & detail_filter_category_ids)
            and identifier not in directly_selected
        }
        cities = decode_cities(variables["data_mesto"])
        return decode_occurrences(
            variables["data_predstavenie"],
            events=events,
            outputs=decode_outputs(variables["data_podujatie_out"]),
            venues=decode_venues(variables["data_hladisko"], cities),
            selected_event_ids=directly_selected | detail_filtered,
            detail_filtered_event_ids=detail_filtered,
            base_url=self.site.base_url,
            timezone_name=self.site.timezone_name,
        )

    def _detail(self, url: str) -> tuple[str | None, str | None]:
        soup = BeautifulSoup(self._get(url).text, "html.parser")
        return extract_description(soup), extract_organizer_url(soup, self.site.base_url)

    def scrape(self) -> list[dict[str, Any]]:
        records = self._grid_records()
        detail_by_url: dict[str, tuple[str | None, str | None]] = {}
        urls = sorted({record["url"] for record in records})
        with ThreadPoolExecutor(max_workers=self.site.request_workers) as executor:
            futures = {executor.submit(self._detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_by_url[url] = future.result()
                except Exception as error:
                    detail_by_url[url] = (None, None)
                    log_message(
                        "Ticketportal detail failed",
                        event="crawler_item_failed",
                        level="warning",
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
        for record in records:
            record["description"], record["organizer_url"] = detail_by_url[record["url"]]
        retained = []
        for record in records:
            requires_filter = record.pop("_ticketportal_requires_detail_filter")
            if not requires_filter or is_live_film_music_event(record["description"]):
                retained.append(record)
        return retained

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        df = df[~df["organizer_url"].isin(self.site.excluded_organizer_urls)].copy()
        return df.drop_duplicates(subset=["title", "date", "time_from", "venue", "city", "url"])
