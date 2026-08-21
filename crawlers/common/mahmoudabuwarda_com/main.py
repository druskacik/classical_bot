import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.mahmoudabuwarda.com/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/posts'
SOURCE = 'Mahmoud Abuwarda'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

CITY_COUNTRIES = {
    'Lewes': 'GB',
    'Manchester': 'GB',
    'Montecassiano': 'IT',
    'Porto Recanati': 'IT',
    'Seixal': 'PT',
    'St Albans': 'GB',
}

EVENT_TERMS = re.compile(
    r'\b(?:concert|recital|festival|live performance|world premiere)\b', re.I
)


def clean_text(value):
    if not value:
        return ''
    text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(text, published_year):
    patterns = (
        r'\b(?:Date\s*:\s*)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
        r'(\d{1,2})(?:st|nd|rd|th)?(?:,|\s)\s*(20\d{2})\b',
        r'\b(?:Date\s*:\s*)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)?,?\s*'
        r'(\d{1,2})(?:st|nd|rd|th)?\s+'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
        r'(20\d{2})\b',
    )
    for index, pattern in enumerate(patterns):
        match = re.search(pattern, text, re.I)
        if not match:
            continue
        if index == 0:
            month_name, day, year = match.groups()
        else:
            day, month_name, year = match.groups()
        try:
            return date(int(year), MONTHS[month_name.lower()], int(day)).isoformat()
        except ValueError:
            return None

    # Some posts omit the year for a near-term event. The publication year is
    # defensible only when the text explicitly introduces it as an event date.
    match = re.search(
        r'\b(?:on|Date\s*:)\s*'
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+'
        r'(\d{1,2})(?:st|nd|rd|th)?\b',
        text,
        re.I,
    )
    if match:
        try:
            return date(
                published_year, MONTHS[match.group(1).lower()], int(match.group(2))
            ).isoformat()
        except ValueError:
            return None
    return None


def parse_time(text):
    match = re.search(
        r'\b(?:Time\s*:\s*)?(\d{1,2})(?::(\d{2}))?\s*(AM|PM)\b', text, re.I
    )
    if match:
        hour = int(match.group(1)) % 12
        if match.group(3).lower() == 'pm':
            hour += 12
        return f'{hour:02d}:{match.group(2) or "00"}'
    match = re.search(r'\bTime\s*:\s*([01]?\d|2[0-3]):([0-5]\d)\b', text, re.I)
    if match:
        return f'{int(match.group(1)):02d}:{match.group(2)}'
    return None


def parse_location(text):
    labelled = re.search(
        r'\b(?:Venue|Location|Where)\s*:\s*\n?([^\n]+?)(?:,|\s+in\s+)\s*'
        r'(Porto Recanati|St Albans|Montecassiano|Manchester|Lewes|Seixal)\b',
        text,
        re.I,
    )
    if labelled:
        venue = clean_venue(labelled.group(1))
        city = canonical_city(labelled.group(2))
        return venue, city

    patterns = (
        r'\bat\s*\n?([^\n,.]+?)\s*\n?\((Montecassiano)(?:,\s*[^)]*)?\)',
        r'\bat\s*\n?([^\n,.]+?)\s*\n?,\s*(Manchester|Lewes|Porto Recanati)\b',
        r'\bat (?:the )?([^\n,.]+?)\s+on Homewood Road in\s+(St Albans)\b',
        r'\bat (?:the )?([^\n,.]+?)\s*\((Montecassiano)(?:,\s*[^)]*)?\)',
        r'\bat (?:the )?(?:scenic\s+)?([^\n,.]+?)\s+in\s+(Seixal)\b',
        r'\bat (?:the )?([^\n,.]+?),\s*(Manchester|Lewes|Porto Recanati)\b',
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return clean_venue(match.group(1)), canonical_city(match.group(2))
    return '', ''


def clean_venue(value):
    venue = value.strip(' ,.-')
    venue = re.sub(r',?\s*Homewood (?:Road|Rd)\.?$', '', venue, flags=re.I)
    return venue.strip(' ,.-')


def canonical_city(value):
    for city in CITY_COUNTRIES:
        if city.lower() == value.strip().lower():
            return city
    return value.strip()


def parse_post(post):
    title = clean_text(post.get('title', {}).get('rendered'))
    description = clean_text(post.get('content', {}).get('rendered'))
    url = str(post.get('link') or '').strip()
    if not title or not url or not description or not EVENT_TERMS.search(description):
        return None

    published = str(post.get('date') or '')
    try:
        published_year = int(published[:4])
    except (TypeError, ValueError):
        return None
    event_date = parse_date(description, published_year)
    venue, city = parse_location(description)
    country_code = CITY_COUNTRIES.get(city)
    if not event_date or not venue or not city or not country_code:
        return None

    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': parse_time(description),
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MahmoudAbuwardaComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='mahmoudabuwarda_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        posts = []
        page = 1
        while True:
            response = requests.get(
                API_URL,
                params={
                    'per_page': 100,
                    'page': page,
                    'orderby': 'date',
                    'order': 'desc',
                    '_fields': 'date,link,title,content',
                },
                headers=HEADERS,
                timeout=45,
            )
            if response.status_code == 400 and page > 1:
                break
            response.raise_for_status()
            batch = response.json()
            posts.extend(batch)
            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        records = []
        for post in posts:
            record = parse_post(post)
            if record:
                records.append(record)
            elif EVENT_TERMS.search(clean_text(post.get('content', {}).get('rendered'))):
                log_message(
                    'Skipped Mahmoud Abuwarda post without complete event data',
                    event='crawler_item_skipped',
                    level='warning',
                    url=str(post.get('link') or ''),
                    error_type='IncompleteEventData',
                    error_message='Required event date, venue, city, or country could not be inferred',
                )
        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    MahmoudAbuwardaComCrawler().run()


if __name__ == '__main__':
    main()
