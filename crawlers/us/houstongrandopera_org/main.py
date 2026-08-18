import re
from datetime import datetime, timedelta
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.houstongrandopera.org/'
SOURCE = 'Houston Grand Opera'
API_URL = 'https://graphql.datocms.com/'
CITY = 'Houston'
DEFAULT_VENUE = 'Wortham Theater Center'

# This read-only DatoCMS API token is published by the site's browser application.
API_TOKEN = 'e792d4edd646c8a8c6f8d6985ce1b2'
HEADERS = {
    'Authorization': f'Bearer {API_TOKEN}',
    'Content-Type': 'application/json',
    'X-Environment': 'main',
    'X-Exclude-Invalid': 'true',
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
}

QUERY = r'''
query CrawlerInventory($first: IntType!, $skip: IntType!) {
  _allOnStagesMeta { count }
  _allEventSlugsMeta { count }
  allOnStages(first: $first, skip: $skip, orderBy: startDate_ASC) {
    id title slug startDate endDate previewButtonCta composer
    _allReferencingSchedulerRightRails(first: 100, orderBy: dateTime_ASC) {
      dateTime cta
    }
    rightRail { rightRail { title location paragraph } }
    description { value }
    cardTeaserCopy { value }
  }
  allEventSlugs(first: $first, skip: $skip, orderBy: startDate_ASC) {
    id title startDate endDate previewButtonCta composer
    _allReferencingSchedulerRightRails(first: 100, orderBy: dateTime_ASC) {
      dateTime cta
    }
    rightRail { rightRail { title location paragraph } }
    description { value }
    cardTeaserCopy { value }
  }
}
'''


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(unescape(str(value)), 'html.parser').get_text(' ', strip=True)
    return re.sub(r'\s+', ' ', text.replace('\xa0', ' ')).strip()


def dast_text(field):
    values = []

    def visit(node):
        if isinstance(node, dict):
            value = node.get('value')
            if isinstance(value, str) and value.strip():
                values.append(value)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(field or {})
    return clean_text(' '.join(values))


def parse_datetime(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    return parsed.date().isoformat(), parsed.strftime('%H:%M')


def fallback_occurrence(item):
    event_date = item.get('startDate')
    if not event_date:
        return None
    try:
        datetime.strptime(event_date, '%Y-%m-%d')
    except ValueError:
        return None

    time_from = None
    rail_text = ' '.join(
        clean_text(part.get('title')) + ' ' + clean_text(part.get('paragraph'))
        for part in ((item.get('rightRail') or {}).get('rightRail') or [])
    )
    match = re.search(r'(?<!\d)(\d{1,2})(?::(\d{2}))?\s*([AP])\.?M\.?', rail_text, re.I)
    if match:
        hour = int(match.group(1)) % 12 + (12 if match.group(3).upper() == 'P' else 0)
        time_from = f'{hour:02d}:{int(match.group(2) or 0):02d}'
    return event_date, time_from


def description_for(item):
    parts = [dast_text(item.get('description')), dast_text(item.get('cardTeaserCopy'))]
    composer = clean_text(item.get('composer'))
    if composer:
        parts.insert(0, f'Composer / site label: {composer}')
    return '\n\n'.join(part for part in parts if part) or None


def item_records(item, model):
    title = clean_text(item.get('title'))
    path = item.get('previewButtonCta') or (
        f"/on-stage/{item.get('slug')}" if item.get('slug') else ''
    )
    url = urljoin(SOURCE_URL, path)
    if not title or not path or not url.startswith(('http://', 'https://')):
        return []

    venue = DEFAULT_VENUE
    if model == 'event':
        # Event records use the otherwise composer-named field as their location label.
        venue = clean_text(item.get('composer'))
        if not re.search(r'\b(?:theatre|theater|center|centre|park|hall)\b', venue, re.I):
            # This excludes undated overviews and touring programmes whose event-level
            # location cannot be defensibly inferred (for example Opera To-Go!).
            return []

    occurrences = []
    for scheduler in item.get('_allReferencingSchedulerRightRails') or []:
        parsed = parse_datetime(scheduler.get('dateTime'))
        if parsed:
            occurrences.append(parsed)
    if not occurrences:
        fallback = fallback_occurrence(item)
        if fallback:
            occurrences.append(fallback)

    teaser = dast_text(item.get('cardTeaserCopy'))
    if (
        model == 'event'
        and len(occurrences) == 1
        and re.search(r'\bdaily\b', teaser, re.I)
        and item.get('endDate')
    ):
        start = datetime.strptime(occurrences[0][0], '%Y-%m-%d').date()
        end = datetime.strptime(item['endDate'], '%Y-%m-%d').date()
        if start < end and (end - start).days <= 31:
            occurrences = [
                ((start + timedelta(days=offset)).isoformat(), occurrences[0][1])
                for offset in range((end - start).days + 1)
            ]

    description = description_for(item)
    return [
        {
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue,
            'city': CITY,
            'country_code': 'US',
            'description': description,
            'source_url': SOURCE_URL,
            'source': SOURCE,
        }
        for event_date, time_from in occurrences
        if venue
    ]


def fetch_inventory(session):
    on_stage = []
    events = []
    skip = 0
    page_size = 100
    while True:
        response = session.post(
            API_URL,
            headers=HEADERS,
            json={'query': QUERY, 'variables': {'first': page_size, 'skip': skip}},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get('errors'):
            raise RuntimeError(f"DatoCMS GraphQL error: {payload['errors'][0].get('message', 'unknown error')}")
        data = payload['data']
        on_stage.extend(data.get('allOnStages') or [])
        events.extend(data.get('allEventSlugs') or [])
        total = max(
            data['_allOnStagesMeta']['count'],
            data['_allEventSlugsMeta']['count'],
        )
        skip += page_size
        if skip >= total:
            break
    return on_stage, events


def scrape_concerts(session=None):
    session = session or requests.Session()
    on_stage, events = fetch_inventory(session)
    records = []
    for item in on_stage:
        records.extend(item_records(item, 'on_stage'))
    for item in events:
        records.extend(item_records(item, 'event'))

    if not records:
        log_message(
            'No concrete Houston Grand Opera occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SOURCE_URL,
            record_count=0,
        )
    return sorted(records, key=lambda row: (row['date'], row['time_from'] or '', row['title']))


class HoustonGrandOperaOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='houstongrandopera_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    HoustonGrandOperaOrgCrawler().run()


if __name__ == '__main__':
    main()
