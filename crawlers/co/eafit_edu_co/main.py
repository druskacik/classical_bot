import html
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.eafit.edu.co/'
CALENDAR_URL = urljoin(SOURCE_URL, 'calendario-eventos')
SOURCE = 'Universidad EAFIT'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-CO,es;q=0.9',
}

# EAFIT's calendar is a broad university calendar. Select music events here,
# then let the potential-event classifier make the final classical decision.
MUSIC_EVENT = re.compile(
    r'\b(?:conciert\w*|sinf[oó]nic\w*|orquesta\w*|recital\w*|[oó]pera\w*|'
    r'coro\b|coral\b|m[uú]sica\s+de\s+c[aá]mara|piano\b|pian[ií]st\w*|'
    r'viol[ií]n\b|violonchelo\b|beethoven\b|mozart\b|chopin\b|'
    r'tchaikovsky\b|festival\s+de\s+la\s+canci[oó]n)\b',
    re.IGNORECASE,
)


def clean_text(value):
    if value is None:
        return ''
    if hasattr(value, 'get_text'):
        value = value.get_text('\n', strip=True)
    value = html.unescape(str(value)).replace('\xa0', ' ').replace('\u200b', '')
    value = re.sub(r'[ \t]+', ' ', value)
    value = re.sub(r' *\n *', '\n', value)
    return re.sub(r'\n{3,}', '\n\n', value).strip()


def fetch_soup(session, url):
    response = session.get(url, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_events(soup):
    settings_tag = soup.select_one(
        'script[type="application/json"][data-drupal-selector="drupal-settings-json"]'
    )
    if not settings_tag or not settings_tag.string:
        raise ValueError('Drupal calendar settings were not found')
    settings = json.loads(settings_tag.string)
    events = []
    for view in settings.get('fullCalendarView') or []:
        options = view.get('calendar_options') or '{}'
        if isinstance(options, str):
            options = json.loads(options)
        events.extend(options.get('events') or [])
    return [event for event in events if MUSIC_EVENT.search(clean_text(event.get('title')))]


def labelled_value(soup, label):
    for heading in soup.find_all(['h2', 'h3', 'h4']):
        if clean_text(heading).casefold() != label.casefold():
            continue
        card = heading.find_parent(class_=lambda value: value and 'cards-cifras' in value)
        if card:
            paragraph = card.find('p')
            if paragraph:
                return clean_text(paragraph)
    return ''


def detail_description(soup, title):
    heading = soup.find('h1')
    if not heading:
        return None
    parts = []
    for element in heading.find_all_next(['h2', 'h3', 'h4', 'p', 'li']):
        text = clean_text(element)
        if text.casefold() == 'área responsable':
            break
        if (
            text
            and text.casefold() not in {'inicio', 'calendario eventos', title.casefold()}
            and text not in parts
        ):
            parts.append(text)
    description = clean_text('\n'.join(parts))
    return description or None


def city_for_venue(venue):
    normalized = venue.casefold()
    if 'llanogrande' in normalized or 'rionegro' in normalized:
        return 'Rionegro'
    # EAFIT's main campus and the named metropolitan cultural venues in its
    # calendar are in Medellín. Explicit Llanogrande events are handled above.
    return 'Medellín'


def make_record(event, soup, url):
    title = clean_text(event.get('title'))
    start = clean_text(event.get('start'))
    match = re.match(r'(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})', start)
    venue = labelled_value(soup, 'Lugar/plataforma')
    if not title or not match or not venue:
        return None
    try:
        event_date = date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': f'{match.group(2)}:{match.group(3)}',
        'venue': venue,
        'city': city_for_venue(venue),
        'country_code': 'CO',
        'description': detail_description(soup, title),
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class EafitEduCoCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='eafit_edu_co',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='CO',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        calendar_soup = fetch_soup(session, CALENDAR_URL)
        events = calendar_events(calendar_soup)
        records = []
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {}
            for event in events:
                url = urljoin(SOURCE_URL, event.get('url') or '')
                if url != SOURCE_URL:
                    futures[executor.submit(fetch_soup, session, url)] = (event, url)
            for future in as_completed(futures):
                event, url = futures[future]
                try:
                    record = make_record(event, future.result(), url)
                except (requests.RequestException, ValueError) as error:
                    log_message(
                        'Failed to scrape EAFIT event detail',
                        event='crawler_item_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )
                    continue
                if record:
                    records.append(record)
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    EafitEduCoCrawler().run()


if __name__ == '__main__':
    main()
