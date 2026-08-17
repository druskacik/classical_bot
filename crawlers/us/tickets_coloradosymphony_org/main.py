import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://tickets.coloradosymphony.org/'
SOURCE = 'Colorado Symphony'
EVENTS_URL = 'https://coloradosymphony.org/view-all-events/'
CALENDAR_URL = 'https://coloradosymphony.org/calendar/'
EVENTS_API_URL = 'https://coloradosymphony.org/wp/wp-admin/admin-post.php'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

PERFORMANCE_RE = re.compile(
    r'^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*'
    r'([A-Z][a-z]{2})\s+(\d{1,2}),\s+'
    r'(\d{1,2}(?::\d{2})?\s*[AP]M)$'
)
CALENDAR_DATE_RE = re.compile(
    r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*'
    r'([A-Z][a-z]+)\s+(\d{1,2})'
)


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_time(value):
    value = clean_text(value).upper()
    for pattern in ('%I:%M %p', '%I %p'):
        try:
            return datetime.strptime(value, pattern).strftime('%H:%M')
        except ValueError:
            pass
    return None


def venue_and_city(title, description, image_url='', performance_url=''):
    evidence = ' '.join((title, description or '', image_url)).lower()
    evidence = re.sub(r'[-_]+', ' ', evidence)
    if 'studio loft' in evidence:
        return 'The Studio Loft', 'Denver'
    if 'arvada center' in evidence:
        return 'Arvada Center for the Arts and Humanities', 'Arvada'
    if 'red rocks' in evidence:
        return 'Red Rocks Amphitheatre', 'Morrison'

    # The Symphony's own ticket links are its normal Boettcher Concert Hall
    # inventory. External ticket links are skipped unless the venue is named
    # elsewhere, since they are commonly off-site summer performances.
    if 'tickets.coloradosymphony.org' not in performance_url:
        return None, None
    return 'Boettcher Concert Hall', 'Denver'


def fetch_event_pages(session):
    response = session.get(EVENTS_URL, timeout=45)
    response.raise_for_status()
    yield response.text

    start_at = 10
    while True:
        response = session.post(
            EVENTS_API_URL,
            data={
                'category': '',
                'start_at': start_at,
                'action': 'fetch_events_ajax',
            },
            headers={'X-Requested-With': 'XMLHttpRequest', 'Referer': EVENTS_URL},
            timeout=45,
        )
        response.raise_for_status()
        payload = response.json()
        html = payload.get('event_html') or ''
        if html:
            yield html
        if not payload.get('has_more_events'):
            break
        start_at += 10


def production_data(session):
    productions = []
    for html in fetch_event_pages(session):
        soup = BeautifulSoup(html, 'html.parser')
        for card in soup.select('.production-event'):
            title_node = card.select_one('.headline-link')
            if not title_node:
                continue
            description_node = card.select_one('.editor-content')
            image_node = card.select_one('img.the-image')
            productions.append({
                'title': clean_text(title_node),
                'description': clean_text(description_node) or None,
                'image_url': image_node.get('src', '') if image_node else '',
                'performances': card.select('.performance-link'),
            })
    return productions


def records_from_productions(productions):
    records = []
    current_year = datetime.now().year
    previous_month = None

    for production in productions:
        for link in production['performances']:
            time_node = link.select_one('.performance-time')
            match = PERFORMANCE_RE.fullmatch(clean_text(time_node))
            if not match:
                continue
            month_text, day_text, time_text = match.groups()
            month = datetime.strptime(month_text, '%b').month
            if previous_month is not None and month < previous_month:
                current_year += 1
            previous_month = month
            try:
                event_date = datetime(current_year, month, int(day_text)).date().isoformat()
            except ValueError:
                continue

            url = link.get('href', '').strip()
            venue, city = venue_and_city(
                production['title'],
                production['description'],
                production['image_url'],
                url,
            )
            if not url.startswith(('http://', 'https://')) or not venue or not city:
                continue
            records.append(make_record(
                production['title'], event_date, url, parse_time(time_text),
                venue, city, production['description'],
            ))
    return records


def make_record(title, event_date, url, time_from, venue, city, description):
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


def past_calendar_records(session, descriptions):
    response = session.get(CALENDAR_URL, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    header = soup.select_one('.js-calendar-header[data-year][data-month]')
    if not header:
        return []
    displayed_year = int(header['data-year'])
    displayed_month = datetime.strptime(header['data-month'], '%B').month

    records = []
    for day in soup.select('li.list-item-day'):
        date_match = CALENDAR_DATE_RE.search(clean_text(day))
        if not date_match:
            continue
        month_text, day_text = date_match.groups()
        month = datetime.strptime(month_text, '%B').month
        year = displayed_year
        if month - displayed_month > 6:
            year -= 1
        elif displayed_month - month > 6:
            year += 1
        try:
            event_date = datetime(year, month, int(day_text)).date().isoformat()
        except ValueError:
            continue

        for event in day.select('.calendar-event li.event'):
            link = event.select_one('.event-name a[href]')
            if not link:
                continue
            title = clean_text(link)
            url = link.get('href', '').strip()
            description, image_url = descriptions.get(title, (None, ''))
            venue, city = venue_and_city(title, description, image_url, url)
            if not title or not venue or not city or not url.startswith(('http://', 'https://')):
                continue
            records.append(make_record(
                title,
                event_date,
                url,
                parse_time(clean_text(event.select_one('.event-time'))),
                venue,
                city,
                description,
            ))
    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    productions = production_data(session)
    descriptions = {
        item['title']: (item['description'], item['image_url']) for item in productions
    }
    records = records_from_productions(productions)
    records.extend(past_calendar_records(session, descriptions))

    unique = {}
    for record in records:
        key = (record['title'], record['date'], record['time_from'], record['venue'])
        # Production-feed links are preferred over duplicate calendar links
        # because they are the first-party selected sales URL for that event.
        unique.setdefault(key, record)
    result = sorted(unique.values(), key=lambda item: (item['date'], item['time_from'] or '', item['title']))
    if not result:
        log_message(
            'No Colorado Symphony performances found',
            event='crawler_empty_listing',
            level='warning',
            url=EVENTS_URL,
            record_count=0,
        )
    return result


class TicketsColoradoSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='tickets_coloradosymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    TicketsColoradoSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
