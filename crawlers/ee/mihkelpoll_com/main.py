import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://mihkelpoll.com/'
CONCERTS_URL = f'{SOURCE_URL}concerts/'
SOURCE = 'Mihkel Poll'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}

# The artist tours internationally. The calendar supplies a venue but often
# omits its city, so these first-party venue labels are resolved explicitly.
LOCATIONS = {
    'philharmonie de paris: grande salle pierre boulez': ('Paris', 'FR'),
    'estonia concert hall': ('Tallinn', 'EE'),
    'tubin hall, tartu, estonia': ('Tartu', 'EE'),
    'pärnu concert house': ('Pärnu', 'EE'),
    'jõhvi concert house': ('Jõhvi', 'EE'),
    'arvo pärt centre, estonia': ('Laulasmaa', 'EE'),
    'great hall of lithuanian academy of music and theatre': ('Vilnius', 'LT'),
    'great hall of estonian academy of music and theatre': ('Tallinn', 'EE'),
    'great hall of estonian academy of music and theatre, tallinn': ('Tallinn', 'EE'),
    'yokote city hall': ('Yokote', 'JP'),
    'akita atorion music hall': ('Akita', 'JP'),
    'noshiro bunka hall': ('Noshiro', 'JP'),
    'luunja rose garden, tartumaa': ('Luunja', 'EE'),
    'kärdla church, hiiumaa': ('Kärdla', 'EE'),
    'tartu university hall': ('Tartu', 'EE'),
    'cēsis concert hall (lv)': ('Cēsis', 'LV'),
    'concert hall of helsinki sibelius academy (fin)': ('Helsinki', 'FI'),
    'viimsi artium': ('Viimsi', 'EE'),
    'tartu vanemuise concert house': ('Tartu', 'EE'),
    'staatsorchester braunschweig (ger)': ('Braunschweig', 'DE'),
    'staatstheater braunschweig (ger)': ('Braunschweig', 'DE'),
    'london queen elisabeth hall (uk)': ('London', 'GB'),
}

DATE_RE = re.compile(
    r'^(\d{1,2})(?:st|nd|rd|th)\s+(?:of\s+)?([A-Za-z]+)'
    r'(?:\s+(20\d{2}))?(?:\s*\|\s*(\d{1,2})[.:](\d{2}))?$',
    re.I,
)


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True) if hasattr(element, 'get_text') else str(element)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date_heading(value, default_year=None):
    match = DATE_RE.fullmatch(clean_text(value))
    if not match:
        return None
    month = MONTHS.get(match.group(2).lower())
    year = int(match.group(3)) if match.group(3) else default_year
    if not month or not year:
        return None
    try:
        event_date = date(year, month, int(match.group(1))).isoformat()
    except ValueError:
        return None
    time_from = None
    if match.group(4):
        hour = int(match.group(4))
        minute = int(match.group(5))
        if hour > 23 or minute > 59:
            return None
        time_from = f'{hour:02d}:{minute:02d}'
    return event_date, time_from, month, bool(match.group(3))


def event_title(description):
    prefix = re.split(r'\b(?:Conductor|Program)\s*:?', description, maxsplit=1, flags=re.I)[0]
    prefix = clean_text(prefix).replace('\n', ' ').strip(' ,-')
    return prefix or 'Mihkel Poll concert'


def parse_page(html):
    soup = BeautifulSoup(html, 'html.parser')
    widgets = soup.select('[data-widget_type]')
    records = []
    current_year = None
    previous_month = None
    index = 0

    while index < len(widgets):
        heading = widgets[index].select_one('h2.elementor-heading-title')
        heading_text = clean_text(heading)
        if re.fullmatch(r'20\d{2}', heading_text):
            current_year = int(heading_text)
            previous_month = None
            index += 1
            continue

        parsed = parse_date_heading(heading_text, current_year)
        if not parsed:
            index += 1
            continue

        event_date, time_from, month, has_explicit_year = parsed
        if has_explicit_year:
            current_year = int(event_date[:4])
        elif previous_month is not None and month > previous_month:
            # The archive is reverse chronological; this handles the transition
            # from January 2026 to the yearless October 2025 entries.
            current_year -= 1
            event_date, time_from, month, _ = parse_date_heading(heading_text, current_year)
        previous_month = month

        venue = ''
        description = ''
        event_url = ''
        cursor = index + 1
        while cursor < len(widgets):
            next_heading = widgets[cursor].select_one('h2.elementor-heading-title')
            next_text = clean_text(next_heading)
            if DATE_RE.fullmatch(next_text) or re.fullmatch(r'20\d{2}', next_text):
                break
            if not venue:
                venue = clean_text(widgets[cursor].select_one('h3.elementor-icon-box-title'))
            if not description and widgets[cursor].get('data-widget_type') == 'text-editor.default':
                description = clean_text(widgets[cursor].select_one('.elementor-widget-container'))
            if not event_url:
                button = widgets[cursor].select_one('a.elementor-button[href]')
                if button:
                    event_url = button.get('href', '').strip()
            cursor += 1

        location = LOCATIONS.get(venue.casefold())
        if venue and description and location:
            city, country_code = location
            records.append({
                'title': event_title(description),
                'date': event_date,
                'url': event_url or CONCERTS_URL,
                'time_from': time_from,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
        index = cursor

    return sorted(
        records,
        key=lambda record: (
            record['date'], record['time_from'] or '', record['title'], record['venue']
        ),
    )


class MihkelpollComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mihkelpoll_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='EE',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        try:
            response = requests.get(CONCERTS_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Mihkel Poll concert archive',
                event='crawler_fetch_failed',
                level='error',
                url=CONCERTS_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise
        return parse_page(response.text)


def main():
    MihkelpollComCrawler().run()


if __name__ == '__main__':
    main()
