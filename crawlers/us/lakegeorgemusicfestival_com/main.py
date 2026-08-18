import re
from collections import Counter
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.lakegeorgemusicfestival.com/'
SCHEDULE_URL = urljoin(SOURCE_URL, 'schedule')
SOURCE = 'Lake George Music Festival'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
}

MONTHS = {
    'january': 1,
    'february': 2,
    'march': 3,
    'april': 4,
    'may': 5,
    'june': 6,
    'july': 7,
    'august': 8,
    'september': 9,
    'october': 10,
    'november': 11,
    'december': 12,
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text('\n', strip=True)
    text = text.replace('\xa0', ' ').replace('\u202f', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def event_sections(soup):
    sections = []
    seen = set()
    for link in soup.select('a[href*="/product-page/"]'):
        section = link.find_parent('section')
        if section is None or id(section) in seen:
            continue
        seen.add(id(section))
        headings = section.find_all('h2')
        if len(headings) >= 2 and re.search(
            r'\b(?:MON|TUE|WED|THU|FRI|SAT|SUN)[A-Z]*,\s+[A-Z]+\s+\d{1,2}\b',
            clean_text(headings[0]).upper(),
        ):
            sections.append((section, link))
    return sections


def season_year(sections):
    years = []
    for section, _ in sections:
        years.extend(re.findall(r'\b(20\d{2})\s+season\b', clean_text(section), re.I))
    if not years:
        raise ValueError('Could not determine the season year from concert content')
    return int(Counter(years).most_common(1)[0][0])


def parse_date(value, year):
    match = re.search(r'\b([A-Z]+)\s+(\d{1,2})\b', value.upper())
    if not match:
        return None
    month = MONTHS.get(match.group(1).lower())
    if month is None:
        return None
    try:
        return date(year, month, int(match.group(2))).isoformat()
    except ValueError:
        return None


def parse_location(text):
    match = re.search(
        r'(The Carriage House|Lake George Steamboat Company),\s*[^\n]+?,\s*'
        r'(Lake George),\s*NY\b',
        text,
        re.I,
    )
    if not match:
        return None
    return match.group(1), match.group(2)


def parse_section(section, link, year):
    headings = section.find_all('h2')
    title = clean_text(headings[1]) if len(headings) >= 2 else ''
    event_date = parse_date(clean_text(headings[0]), year) if headings else None
    section_text = clean_text(section)
    location = parse_location(section_text)
    if not title or not event_date or location is None:
        return None

    # The tea is a fundraising/social event, not a musical performance. Every
    # remaining dated schedule section is explicitly billed as a concert.
    if title.upper() == 'VICTORIAN TEA':
        return None

    time_match = re.search(
        r'\bDeparture:\s*((?:1[0-2]|[1-9]):[0-5]\d)\s*(AM|PM)\b',
        section_text,
        re.I,
    ) or re.search(r'\b((?:1[0-2]|[1-9]):[0-5]\d)\s*(AM|PM)\b', section_text, re.I)
    time_from = None
    if time_match:
        hour, minute = map(int, time_match.group(1).split(':'))
        if time_match.group(2).upper() == 'PM' and hour != 12:
            hour += 12
        if time_match.group(2).upper() == 'AM' and hour == 12:
            hour = 0
        time_from = f'{hour:02d}:{minute:02d}'

    paragraphs = []
    for paragraph in section.find_all('p'):
        value = clean_text(paragraph)
        value = re.sub(
            r'^.*?(?:The Carriage House|Lake George Steamboat Company),\s*'
            r'[^\n]+?,\s*Lake George,\s*NY\s*',
            '',
            value,
            flags=re.I | re.S,
        ).strip()
        if value and not re.match(r'^(?:TICKETS?|Cash bar)\b', value, re.I):
            paragraphs.append(value)
    description = '\n\n'.join(paragraphs) or None
    venue, city = location

    return {
        'title': title,
        'date': event_date,
        'url': urljoin(SOURCE_URL, link.get('href', '')),
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'country_code': 'US',
        'description': description,
        'source_url': SOURCE_URL,
        'source': SOURCE,
    }


class LakeGeorgeMusicFestivalComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='lakegeorgemusicfestival_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='US',
        upload_target='classical',
        columns=[
            'title',
            'date',
            'url',
            'time_from',
            'venue',
            'city',
            'country_code',
            'description',
            'source_url',
            'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(SCHEDULE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Lake George Music Festival schedule',
                event='crawler_fetch_failed',
                level='error',
                url=SCHEDULE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        sections = event_sections(soup)
        year = season_year(sections)
        records = [parse_section(section, link, year) for section, link in sections]
        return sorted(
            (record for record in records if record),
            key=lambda record: (record['date'], record['time_from'] or '', record['title']),
        )


def main():
    LakeGeorgeMusicFestivalComCrawler().run()


if __name__ == '__main__':
    main()
