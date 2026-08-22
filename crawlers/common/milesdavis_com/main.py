import html
import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.milesdavis.com/'
SOURCE = 'Miles Davis Official Site'
API_URL = 'https://www.milesdavis.com/wp-json/wp/v2/posts'
NEWS_CATEGORY_ID = 2

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

US_REGIONS = {
    'AL', 'AK', 'AZ', 'AR', 'CA', 'CO', 'CT', 'DE', 'FL', 'GA', 'HI',
    'ID', 'IL', 'IN', 'IA', 'KS', 'KY', 'LA', 'ME', 'MD', 'MA', 'MI',
    'MN', 'MS', 'MO', 'MT', 'NE', 'NV', 'NH', 'NJ', 'NM', 'NY', 'NC',
    'ND', 'OH', 'OK', 'OR', 'PA', 'RI', 'SC', 'SD', 'TN', 'TX', 'UT',
    'VT', 'VA', 'WA', 'WV', 'WI', 'WY', 'DC',
}

COUNTRIES = {
    'australia': 'AU', 'austria': 'AT', 'belgium': 'BE', 'brazil': 'BR',
    'canada': 'CA', 'denmark': 'DK', 'finland': 'FI', 'france': 'FR',
    'germany': 'DE', 'ireland': 'IE', 'italy': 'IT', 'japan': 'JP',
    'netherlands': 'NL', 'norway': 'NO', 'poland': 'PL', 'portugal': 'PT',
    'spain': 'ES', 'sweden': 'SE', 'switzerland': 'CH',
    'united kingdom': 'GB', 'uk': 'GB', 'united states': 'US', 'usa': 'US',
}


def clean_text(value):
    if not value:
        return ''
    if hasattr(value, 'get_text'):
        text = value.get_text('\n', strip=True)
    else:
        text = BeautifulSoup(str(value), 'html.parser').get_text('\n', strip=True)
    text = html.unescape(text).replace('\xa0', ' ').replace('\u202f', ' ')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def parse_date(value):
    match = re.search(
        r'\b(' + '|'.join(MONTHS) + r')\s+(\d{1,2})(?:st|nd|rd|th)?'
        r'(?:\s*,)?\s+(20\d{2})\b',
        value,
        re.IGNORECASE,
    )
    if match:
        try:
            return date(
                int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2))
            ).isoformat()
        except ValueError:
            return None

    match = re.search(r'\b(20\d{2})-(\d{1,2})-(\d{1,2})\b', value)
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None
    return None


def parse_location(value):
    value = re.sub(r'^\s*(?:venue|location)\s*:\s*', '', value, flags=re.IGNORECASE)
    parts = [part.strip(' .') for part in re.split(r'\s+[—–-]\s+|\s*\|\s*', value) if part.strip(' .')]
    if len(parts) < 2:
        return None

    venue = parts[0]
    location = parts[-1]
    comma_parts = [part.strip(' .') for part in location.split(',') if part.strip(' .')]
    if len(comma_parts) < 2:
        return None

    city = comma_parts[0]
    region = comma_parts[-1]
    if region.upper() in US_REGIONS:
        country_code = 'US'
    else:
        country_code = COUNTRIES.get(region.lower())
    if not venue or not city or not country_code:
        return None
    return venue, city, country_code


def labelled_value(lines, label):
    pattern = re.compile(rf'^\s*{label}\s*:\s*(.+)$', re.IGNORECASE)
    for index, line in enumerate(lines):
        match = pattern.match(line)
        if match:
            return index, match.group(1).strip()
    return None, None


def parse_post(post):
    description = clean_text(post.get('content', {}).get('rendered', ''))
    lines = [line.strip() for line in description.splitlines() if line.strip()]
    date_index, date_value = labelled_value(lines, 'date')
    _, venue_value = labelled_value(lines, r'(?:venue|location)')
    if date_index is None or not venue_value:
        return None

    event_date = parse_date(date_value)
    location = parse_location(venue_value)
    if not event_date or not location:
        return None

    title = ''
    if date_index > 0:
        candidate = lines[date_index - 1]
        if not re.match(r'^(?:tickets?|doors|show|time)\s*:', candidate, re.IGNORECASE):
            title = candidate
    if not title:
        title = clean_text(post.get('title', {}).get('rendered', ''))

    time_match = re.search(
        r'\b(?:show|start|time)\s*:\s*(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?',
        description,
        re.IGNORECASE,
    )
    time_from = None
    if time_match:
        hour = int(time_match.group(1)) % 12
        if time_match.group(3).lower() == 'p':
            hour += 12
        time_from = f'{hour:02d}:{time_match.group(2) or "00"}'

    venue, city, country_code = location
    url = post.get('link', '').strip()
    if not title or not url:
        return None
    return {
        'title': title,
        'date': event_date,
        'url': url,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': country_code,
        'description': description or None,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class MilesdavisComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='milesdavis_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code=None,
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
        records = []
        page = 1

        while True:
            params = {
                'categories': NEWS_CATEGORY_ID,
                'per_page': 100,
                'page': page,
                '_fields': 'title,link,content',
            }
            try:
                response = session.get(API_URL, params=params, timeout=45)
                response.raise_for_status()
                posts = response.json()
            except (requests.RequestException, ValueError) as error:
                log_message(
                    'Failed to fetch Miles Davis news API',
                    event='crawler_fetch_failed',
                    level='error',
                    url=API_URL,
                    page=page,
                    error_type=type(error).__name__,
                    error_message=str(error),
                )
                raise

            for post in posts:
                record = parse_post(post)
                if record:
                    records.append(record)

            total_pages = int(response.headers.get('X-WP-TotalPages', page))
            if page >= total_pages:
                break
            page += 1

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['url']
            ),
        )


def main():
    MilesdavisComCrawler().run()


if __name__ == '__main__':
    main()
