import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from html import unescape

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://sodre.gub.uy/'
SOURCE = 'Sodre'
CALENDAR_URL = f'{SOURCE_URL}espectaculos/calendario/'
ARCHIVE_START_YEAR = 2023

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'es-UY,es;q=0.9,en;q=0.7',
}

# The calendar also contains touring/territorial entries. Only infer Montevideo
# for explicitly named Sodre venues and rooms whose location is unambiguous.
MONTEVIDEO_VENUE_MARKERS = (
    'adela reta',
    'eduardo fabini',
    'hugo balzo',
    'nelly goitiño',
    'nelly goitino',
    'vaz ferreira',
    'héctor tosar',
    'hector tosar',
    'archivo nacional de la imagen y la palabra',
)


def clean_text(element):
    if element is None:
        return ''
    text = unescape(element.get_text('\n', strip=True))
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_calendar_config(html):
    match = re.search(r'CalendarVars\s*=\s*(\{.*?\});', html, re.DOTALL)
    if not match:
        raise ValueError('Calendar API configuration was not found')
    values = json.loads(match.group(1))
    if not all(values.get(key) for key in ('ajaxurl', 'nonce', 'action')):
        raise ValueError('Calendar API configuration is incomplete')
    return values


def parse_occurrence(event):
    start = event.get('start')
    title = str(event.get('title') or '').strip()
    url = str(event.get('url') or '').strip()
    if not start or not title or not url:
        return None
    try:
        start_at = datetime.fromisoformat(start)
    except (TypeError, ValueError):
        return None
    return {
        'title': title,
        'date': start_at.date().isoformat(),
        'url': url,
        'time_from': start_at.strftime('%H:%M'),
    }


def parse_detail(html):
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.select_one('section.content--espectaculo')
    description = clean_text(content) or None

    location = soup.select_one('aside.cyg--sidebar p.lugar img[alt]')
    location_name = (location.get('alt') or '').strip() if location else ''
    room = soup.select_one('aside.cyg--sidebar p.sala')
    room_name = clean_text(room)
    room_name = re.sub(r'^Sala\s*', '', room_name, flags=re.IGNORECASE).strip()
    venue = room_name or location_name

    geography_text = f'{location_name} {venue}'.casefold()
    city = 'Montevideo' if any(marker in geography_text for marker in MONTEVIDEO_VENUE_MARKERS) else None
    if not venue or not city:
        return None
    return {'venue': venue, 'city': city, 'description': description}


class SodreGubUyCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='sodre_gub_uy',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='UY',
        upload_target='potential',
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        try:
            calendar_response = session.get(CALENDAR_URL, timeout=45)
            calendar_response.raise_for_status()
            api = parse_calendar_config(calendar_response.text)

            events = []
            for year in range(ARCHIVE_START_YEAR, date.today().year + 3):
                response = session.get(
                    api['ajaxurl'],
                    params={
                        'action': api['action'],
                        'nonce': api['nonce'],
                        's': '',
                        'from': f'{year}-01-01',
                        'to': f'{year + 1}-01-01',
                    },
                    timeout=90,
                )
                response.raise_for_status()
                payload = response.json()
                if not payload.get('success') or not isinstance(payload.get('data'), list):
                    raise ValueError(f'Calendar API returned an invalid response for {year}')
                events.extend(payload['data'])
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            log_message(
                'Failed to fetch Sodre calendar',
                event='crawler_fetch_failed',
                level='error',
                url=CALENDAR_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        occurrences = []
        seen_occurrences = set()
        for event in events:
            occurrence = parse_occurrence(event)
            if occurrence is None:
                continue
            key = (occurrence['url'], occurrence['date'], occurrence['time_from'])
            if key not in seen_occurrences:
                seen_occurrences.add(key)
                occurrences.append(occurrence)

        details = {}
        failed_urls = set()

        def fetch_detail(url):
            response = session.get(url, timeout=45)
            response.raise_for_status()
            return parse_detail(response.text)

        urls = sorted({occurrence['url'] for occurrence in occurrences})
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = {executor.submit(fetch_detail, url): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail = future.result()
                    if detail is None:
                        failed_urls.add(url)
                    else:
                        details[url] = detail
                except (requests.RequestException, ValueError) as error:
                    failed_urls.add(url)
                    log_message(
                        'Failed to fetch Sodre event detail',
                        event='crawler_detail_fetch_failed',
                        level='warning',
                        url=url,
                        error_type=type(error).__name__,
                        error_message=str(error),
                    )

        records = []
        for occurrence in occurrences:
            detail = details.get(occurrence['url'])
            if detail:
                records.append({**occurrence, **detail})

        skipped_count = len(occurrences) - len(records)
        if skipped_count:
            log_message(
                'Skipped Sodre occurrences without a defensible venue and city',
                event='crawler_records_skipped',
                level='warning',
                record_count=skipped_count,
                detail_url_count=len(failed_urls),
            )
        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    SodreGubUyCrawler().run()


if __name__ == '__main__':
    main()
