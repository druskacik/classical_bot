import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://teatrodasfiguras.pt/'
AGENDA_URL = urljoin(SOURCE_URL, 'agenda')
SOURCE = 'Teatro das Figuras'
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pt-PT,pt;q=0.9,en;q=0.7',
}
TIME_PATTERN = re.compile(r'\b([01]?\d|2[0-3])\s*[h:]\s*([0-5]\d)\b', re.I)


def flight_payload(html):
    """Decode the text chunks embedded by the site's Next.js server renderer."""
    parts = []
    soup = BeautifulSoup(html, 'html.parser')
    for script in soup.find_all('script'):
        text = script.string or ''
        marker = 'self.__next_f.push('
        if marker not in text:
            continue
        raw = text.split(marker, 1)[1].rsplit(')', 1)[0]
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if len(value) > 1 and isinstance(value[1], str):
            parts.append(value[1])
    return '\n'.join(parts)


def embedded_object(payload, key):
    marker = f'"{key}":'
    start = payload.find(marker)
    if start < 0:
        raise ValueError(f'Missing {key} in Next.js page data')
    return json.JSONDecoder(strict=False).raw_decode(payload[start + len(marker):])[0]


def listing_events(html):
    events_by_month = embedded_object(flight_payload(html), 'events')
    return [event for events in events_by_month.values() for event in events]


def clean_html(value):
    if not value:
        return None
    text = BeautifulSoup(value, 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    return text or None


def referenced_text(payload, reference):
    if not isinstance(reference, str) or not re.fullmatch(r'\$[0-9a-f]+', reference):
        return None
    ref = re.escape(reference[1:])
    match = re.search(
        rf'(?:^|\n){ref}:T[0-9a-f]+,(.*?)(?=\n?[0-9a-f]+:\[)',
        payload,
        re.DOTALL,
    )
    return clean_html(match.group(1)) if match else None


def detail_data(html):
    payload = flight_payload(html)
    page = embedded_object(payload, 'pageData')
    event = next(
        (module.get('data') for module in page.get('modules', [])
         if module.get('moduleCode') == 'e4' and isinstance(module.get('data'), dict)),
        {},
    )
    descriptions = []
    for module in page.get('modules', []):
        data = module.get('data')
        if not isinstance(data, dict):
            continue
        value = data.get('value')
        description = referenced_text(payload, value) or (
            clean_html(value) if isinstance(value, str) and not value.startswith('$') else None
        )
        if description and description not in descriptions:
            descriptions.append(description)
    return event, '\n\n'.join(descriptions) or None


def parse_time(value):
    match = TIME_PATTERN.search(value or '')
    return f'{int(match.group(1)):02d}:{match.group(2)}' if match else None


class TeatroDasFigurasPtCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='teatrodasfiguras_pt',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PT',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        session = requests.Session()
        session.headers.update(HEADERS)
        end_year = date.today().year + 5
        params = {'startDate': '01-01-2000', 'endDate': f'31-12-{end_year}'}
        try:
            response = session.get(AGENDA_URL, params=params, timeout=60)
            response.raise_for_status()
            events = listing_events(response.text)
            if not events:
                raise ValueError('No events found in agenda page data')
        except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
            log_message(
                'Failed to fetch Teatro das Figuras agenda',
                event='crawler_fetch_failed', level='error', url=AGENDA_URL,
                error_type=type(error).__name__, error_message=str(error),
            )
            raise

        details = {}
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {}
            for event in events:
                url = urljoin(SOURCE_URL, event.get('link', ''))
                if url != SOURCE_URL:
                    futures[executor.submit(session.get, url, timeout=45)] = url
            for future in as_completed(futures):
                url = futures[future]
                try:
                    detail_response = future.result()
                    detail_response.raise_for_status()
                    details[url] = detail_data(detail_response.text)
                except (requests.RequestException, ValueError, json.JSONDecodeError) as error:
                    log_message(
                        'Failed to fetch Teatro das Figuras event detail',
                        event='crawler_detail_failed', level='warning', url=url,
                        error_type=type(error).__name__, error_message=str(error),
                    )

        records = []
        for event in events:
            url = urljoin(SOURCE_URL, event.get('link', ''))
            detail, description = details.get(url, ({}, None))
            title = (detail.get('title') or event.get('title') or '').strip()
            # The listing exposes the intended local calendar date. Detail pages
            # sometimes serialize local midnight as the preceding UTC date.
            raw_date = event.get('startDate') or detail.get('startDate') or ''
            event_date = raw_date[:10]
            try:
                date.fromisoformat(event_date)
            except ValueError:
                continue
            if not title or url == SOURCE_URL:
                continue
            records.append({
                'title': title,
                'date': event_date,
                'url': url,
                'time_from': parse_time(detail.get('schedule') or event.get('schedule')),
                'venue': 'Teatro das Figuras',
                'city': 'Faro',
                'description': description,
            })

        if not records:
            raise ValueError('No valid Teatro das Figuras event records parsed')
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title']))


def main():
    TeatroDasFigurasPtCrawler().run()


if __name__ == '__main__':
    main()
