import re
from datetime import date
from urllib.parse import urlencode, urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nospr.org.pl/'
CALENDAR_URL = urljoin(SOURCE_URL, 'pl/kalendarz/')
SOURCE = 'NOSPR'
HOME_CITY = 'Katowice'
HOME_VENUES = {
    'sala koncertowa': 'Sala koncertowa NOSPR',
    'sala kameralna': 'Sala kameralna NOSPR',
    'amfiteatr': 'Amfiteatr NOSPR',
}
HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'pl-PL,pl;q=0.9,en;q=0.7',
}


def clean_text(node):
    if not node:
        return ''
    text = node.get_text('\n', strip=True) if hasattr(node, 'get_text') else str(node)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def month_values():
    """Return every month for which this institution may have published events."""
    today = date.today()
    # The present NOSPR building opened in October 2014. The calendar retains
    # old pages, while announced seasons extend beyond the current year.
    start_year, start_month = 2014, 10
    end_year, end_month = today.year + 2, 12
    values = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        values.append(f'{year:04d}-{month:02d}')
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return values


def get_soup(session, url, params=None):
    response = session.get(url, params=params, timeout=45)
    response.raise_for_status()
    return BeautifulSoup(response.text, 'html.parser')


def calendar_items(soup):
    items = []
    for row in soup.select('#calendar-content .calendar__row'):
        date_node = row.select_one(':scope > .term time[datetime]')
        if not date_node:
            continue
        event_date = date_node.get('datetime', '')[:10]
        try:
            date.fromisoformat(event_date)
        except ValueError:
            continue
        for tile in row.select('.tile--calendar'):
            link = tile.select_one('a.tile__link[href]')
            title = clean_text(tile.select_one('.tile__title'))
            if not link or not title:
                continue
            time_match = re.search(r'\b([01]?\d|2[0-3]):[0-5]\d\b', clean_text(tile.select_one('.hour')))
            items.append({
                'title': title,
                'date': event_date,
                'time_from': time_match.group(0).zfill(5) if time_match else None,
                'url': urljoin(CALENDAR_URL, link.get('href', '').strip()),
            })
    return items


def event_detail(soup):
    hero = soup.select_one('.m-hero--event')
    if not hero:
        return None
    title = clean_text(hero.select_one('.m-hero__title--event'))
    main_info = hero.select_one('.m-hero__content .m-hero__info--event')
    time_node = main_info.select_one('time[datetime]') if main_info else None
    if not title or not time_node:
        return None

    descriptions = main_info.select('.description') if main_info else []
    venue_raw = clean_text(descriptions[-1]) if descriptions else ''
    venue_key = venue_raw.casefold()
    venue = HOME_VENUES.get(venue_key, venue_raw)
    if not venue:
        return None

    # NOSPR's own halls are unambiguously in Katowice. Touring listings name
    # their external venue; accept only those that also state a city.
    if venue_key in HOME_VENUES:
        city = HOME_CITY
    else:
        city = ''
        for separator in (',', ' – ', ' - '):
            if separator in venue_raw:
                parts = [part.strip() for part in venue_raw.split(separator) if part.strip()]
                if len(parts) >= 2:
                    city = parts[-1]
                    break
        if not city:
            return None

    description_nodes = hero.select('.program, .text')
    description = '\n\n'.join(filter(None, (clean_text(node) for node in description_nodes))) or None
    datetime_value = time_node.get('datetime', '')
    try:
        event_date = date.fromisoformat(datetime_value[:10]).isoformat()
    except ValueError:
        return None
    time_match = re.search(r'\b([01]\d|2[0-3]):[0-5]\d\b', datetime_value)
    return {
        'title': title,
        'date': event_date,
        'time_from': time_match.group(0) if time_match else None,
        'venue': venue,
        'city': city,
        'description': description,
    }


def get_concerts():
    session = requests.Session()
    session.headers.update(HEADERS)
    occurrences = {}
    for month in month_values():
        url = f'{CALENDAR_URL}?{urlencode({"miesiac": month})}'
        try:
            for item in calendar_items(get_soup(session, CALENDAR_URL, {'miesiac': month})):
                occurrences[(item['url'], item['date'], item['time_from'])] = item
        except requests.RequestException as error:
            log_message(
                'Failed to scrape NOSPR calendar month', event='crawler_page_failed',
                level='warning', url=url, error_type=type(error).__name__,
                error_message=str(error),
            )

    details = {}
    records = []
    for item in occurrences.values():
        try:
            if item['url'] not in details:
                details[item['url']] = event_detail(get_soup(session, item['url']))
            detail = details[item['url']]
            if not detail:
                continue
            record = {**item, **detail, 'country_code': 'PL',
                      'source_url': SOURCE_URL, 'source': SOURCE}
            records.append(record)
        except requests.RequestException as error:
            log_message(
                'Failed to scrape NOSPR event detail', event='crawler_item_failed',
                level='warning', url=item['url'], error_type=type(error).__name__,
                error_message=str(error),
            )
    return sorted(records, key=lambda value: (value['date'], value['time_from'] or '', value['title']))


class NosprOrgPlCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nospr_org_pl',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='PL',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['url', 'date', 'time_from'],
    )

    def scrape(self):
        return get_concerts()


def main():
    NosprOrgPlCrawler().run()


if __name__ == '__main__':
    main()
