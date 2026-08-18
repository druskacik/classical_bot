import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.jaxsymphony.org/'
SEASON_URL = f'{SOURCE_URL}26-27-season/'
ARCHIVE_URL = f'{SOURCE_URL}25-26-season/'
SOURCE = 'Jacksonville Symphony'
CITY = 'Jacksonville'
DEFAULT_VENUE = 'Jacoby Symphony Hall'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    month: number
    for number, month in enumerate(
        ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
         'September', 'October', 'November', 'December'),
        start=1,
    )
}


def clean_text(value):
    if not value:
        return ''
    return re.sub(r'\s+', ' ', str(value).replace('\xa0', ' ')).strip()


def season_years(soup):
    match = re.search(r'(20\d{2})\s*/\s*(\d{2})\s+Season', soup.get_text(' ', strip=True))
    if not match:
        raise ValueError('Could not determine season years')
    return int(match.group(1)), int(match.group(1)[:2] + match.group(2))


def parse_dates(value, start_year, end_year):
    value = clean_text(value).replace(',', ' ')
    month_matches = list(re.finditer('|'.join(MONTHS), value, re.I))
    dates = []
    for index, month_match in enumerate(month_matches):
        month_name = month_match.group(0).title()
        next_start = month_matches[index + 1].start() if index + 1 < len(month_matches) else len(value)
        days = re.findall(r'\b([0-3]?\d)\b', value[month_match.end():next_start])
        year = start_year if MONTHS[month_name] >= 7 else end_year
        for day in days:
            try:
                dates.append(datetime(year, MONTHS[month_name], int(day)).date().isoformat())
            except ValueError:
                continue
    return dates


def card_data(link):
    legacy_card = link.find_parent('div', class_='event')
    if not legacy_card:
        document = link.find_parent()
        while document and document.parent:
            document = document.parent
        if document:
            matching_link = document.select_one(
                f'.event a[href="{link.get("href", "")}"]'
            )
            legacy_card = matching_link.find_parent('div', class_='event') if matching_link else None
    if legacy_card:
        date_node = legacy_card.select_one('.date')
        title_node = legacy_card.select_one('.event-title')
        title = clean_text(title_node.get_text(' ', strip=True)) if title_node else ''
        date_text = clean_text(date_node.get_text(' ', strip=True)) if date_node else ''
        for abbreviation, month_name in {
            'Jan': 'January', 'Feb': 'February', 'Mar': 'March', 'Apr': 'April',
            'May': 'May', 'Jun': 'June', 'Jul': 'July', 'Aug': 'August',
            'Sep': 'September', 'Oct': 'October', 'Nov': 'November', 'Dec': 'December',
        }.items():
            date_text = re.sub(rf'\b{abbreviation}\b', month_name, date_text)
        date_text = re.sub(r'(?<=\d)/(?=\d)', ' & ', date_text)
        return (title, date_text, None) if title and date_text else None

    card = link.find_parent('div', class_=lambda value: value and 'e-child' in value)
    if not card:
        return None
    headings = card.find_all(['h2', 'h3', 'h4', 'h5', 'h6'])
    heading = next(
        (item for item in headings if clean_text(item.get_text(' ', strip=True)).lower()
         not in {'past performance', 'upcoming performance'}),
        None,
    )
    if not heading:
        return None
    title = clean_text(heading.get_text(' ', strip=True))
    text = clean_text(card.get_text(' ', strip=True))
    date_match = re.search(
        r'(' + '|'.join(MONTHS) + r')\s+\d{1,2}(?:\s*(?:,|&)\s*\d{1,2})*'
        r'(?:\s*&\s*(' + '|'.join(MONTHS) + r')\s+\d{1,2})?',
        text,
        re.I,
    )
    if not title or not date_match:
        return None
    description = text.replace('Learn More', '').strip()
    return title, date_match.group(0), description or None


def scrape_season(session, season_url):
    response = session.get(season_url, timeout=45)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    start_year, end_year = season_years(soup)

    records = []
    seen_links = set()
    for section in soup.select('.events-section'):
        heading = section.find_previous(['h2', 'h3', 'h4', 'h5', 'h6'])
        section_name = clean_text(heading.get_text(' ', strip=True)) if heading else ''
        if 'Jazz Series' in section_name:
            continue
        for link in section.select('a[href*="my.jaxsymphony.org/overview/"]'):
            url = link.get('href', '').split('?', 1)[0]
            if not url or url in seen_links:
                continue
            seen_links.add(url)
            data = card_data(link)
            if not data:
                continue
            title, date_text, description = data
            venue = 'Moran Theater at the Jacksonville Center for the Performing Arts' \
                if 'Nutcracker' in title else DEFAULT_VENUE
            time_from = '11:00' if 'Coffee Series' in section_name else None
            if 'Composer Sessions' in section_name:
                time_from = '18:30'
            for event_date in parse_dates(date_text, start_year, end_year):
                records.append({
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
                })

    # These intimate performances are concrete dated events on the season page,
    # but unlike the other series they intentionally have no ticket-detail links.
    chamber_anchor = soup.find(id='chamber')
    if chamber_anchor:
        chamber_section = chamber_anchor.find_next_sibling()
        chamber_section = chamber_section.find_next_sibling() if chamber_section else None
        chamber_text = clean_text(chamber_section.get_text(' ', strip=True)) if chamber_section else ''
        description_node = chamber_section.find('p') if chamber_section else None
        description = clean_text(description_node.get_text(' ', strip=True)) if description_node else None
        for month, day, title in re.findall(
            r'\b(Oct|Feb|Jun)\s+(\d{1,2})\s+(.+? by Candlelight)'
            r'(?=\s+(?:Oct|Feb|Jun)\s+\d|\s+This is|$)',
            chamber_text,
        ):
            month_name = {'Oct': 'October', 'Feb': 'February', 'Jun': 'June'}[month]
            year = start_year if month_name == 'October' else end_year
            event_date = datetime(year, MONTHS[month_name], int(day)).date().isoformat()
            records.append({
                'title': clean_text(title),
                'date': event_date,
                'url': f'{season_url}#chamber',
                'time_from': None,
                'venue': 'Jean and Ross Krueger Chorus Room',
                'city': CITY,
                'country_code': 'US',
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })

    return records


def scrape_concerts(session=None):
    session = session or requests.Session()
    session.headers.update(HEADERS)
    records = []
    for season_url in (ARCHIVE_URL, SEASON_URL):
        records.extend(scrape_season(session, season_url))

    if not records:
        log_message(
            'No concert occurrences found',
            event='crawler_empty_listing',
            level='warning',
            url=SEASON_URL,
            record_count=0,
        )
    return sorted(records, key=lambda item: (item['date'], item['title'], item['url']))


class JaxSymphonyOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='jaxsymphony_org',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city', 'country_code',
            'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        return scrape_concerts()


def main():
    JaxSymphonyOrgCrawler().run()


if __name__ == '__main__':
    main()
