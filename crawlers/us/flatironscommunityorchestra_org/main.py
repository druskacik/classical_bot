import re
from datetime import date, datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://flatironscommunityorchestra.org/'
API_URL = f'{SOURCE_URL}wp-json/wp/v2/pages'
SOURCE = 'Flatirons Community Orchestra'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6,
    'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}


def clean_text(value):
    if not value:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def season_years(title):
    years = [int(value) for value in re.findall(r'20\d{2}', clean_text(title))]
    if not years:
        return None
    return years[0], years[-1]


def parse_date(day, month_number, year):
    if not day or not month_number or not year:
        return None
    try:
        return date(year, month_number, int(clean_text(day))).isoformat()
    except ValueError:
        return None


def parse_time(value):
    match = re.search(r'(\d{1,2}:\d{2})\s*([AP]M)', clean_text(value), re.I)
    if not match:
        return None
    return datetime.strptime(' '.join(match.groups()), '%I:%M %p').strftime('%H:%M')


def card_value(card, selector):
    element = card.select_one(selector)
    return clean_text(element)


def card_venue(card):
    venue = card_value(card, '.evtb-event-description')
    if venue:
        return venue
    for paragraph in card.find_all('p'):
        classes = set(paragraph.get('class', []))
        if classes & {'evtb-event-time', 'evtb-event-location', 'evtb-event-price'}:
            continue
        candidate = clean_text(paragraph)
        if candidate and not re.search(r'\b(?:donation|free|TBD)\b', candidate, re.I):
            return candidate
    return ''


def detail_description(url):
    if not url:
        return None
    try:
        response = requests.get(url, headers=HEADERS, timeout=30)
        response.raise_for_status()
    except requests.RequestException as error:
        log_message(
            'Failed to fetch Flatirons concert detail',
            event='crawler_item_failed',
            level='warning',
            url=url,
            error_type=type(error).__name__,
            error_message=str(error),
        )
        return None
    soup = BeautifulSoup(response.text, 'html.parser')
    content = soup.select_one('main article .entry-content, main article')
    text = clean_text(content)
    return text if len(text) >= 40 else None


def parse_season(page):
    years = season_years(page.get('title', {}).get('rendered', ''))
    html = page.get('content', {}).get('rendered', '')
    soup = BeautifulSoup(html, 'html.parser')
    grid = soup.select_one('.evtb-events-grid-container')
    if not grid:
        return []

    records = []
    cards = grid.find_all('div', class_='evtb-event-item', recursive=False)
    event_year = years[0] if years else None
    previous_month = None
    for card in cards:
        title = card_value(card, '.evtb-event-title')
        month_number = MONTHS.get(card_value(card, '.evtb-date-month').lower()[:3])
        if month_number and previous_month and month_number < previous_month:
            event_year += 1
        if month_number:
            previous_month = month_number
        event_date = parse_date(
            card_value(card, '.evtb-date-day'),
            month_number,
            event_year,
        )
        venue = card_venue(card)
        location = card_value(card, '.evtb-event-location')
        city_match = re.match(r'([^,]+),\s*CO\b', location, re.I)
        city = city_match.group(1).strip() if city_match else ''
        link = card.select_one('.evtb-event-read-more a[href]')
        detail_url = link.get('href', '').strip() if link else ''
        url = detail_url or page.get('link', '')

        if not all((title, event_date, url, venue, city)):
            continue

        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': parse_time(card_value(card, '.evtb-event-time')),
            'venue': venue,
            'city': city,
            'country_code': 'US',
            'description': detail_description(detail_url),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
    return records


class FlatironsCommunityOrchestraOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='flatironscommunityorchestra_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue', 'city'],
    )

    def scrape(self):
        response = requests.get(
            API_URL,
            params={
                'per_page': 100,
                '_fields': 'link,slug,title,content',
            },
            headers=HEADERS,
            timeout=45,
        )
        response.raise_for_status()
        pages = response.json()

        records = []
        for page in pages:
            if re.fullmatch(r'fall-20\d{2}-(?:spring|summer)-20\d{2}-season', page.get('slug', '')):
                records.extend(parse_season(page))

        return sorted(
            records,
            key=lambda item: (
                item['date'], item['time_from'] or '', item['title'], item['venue']
            ),
        )


def main():
    FlatironsCommunityOrchestraOrgCrawler().run()


if __name__ == '__main__':
    main()
