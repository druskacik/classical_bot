import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://nationalchamberensemble.org/'
SOURCE = 'National Chamber Ensemble'
PERFORMANCES_URL = f'{SOURCE_URL}performances.htm'

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

# The season page currently uses these Arlington venues. Keeping the mapping
# explicit prevents a future touring engagement from inheriting the home city.
VENUE_CITIES = {
    'gunston arts center - theater 1': 'Arlington',
    'marymount university - ballston center': 'Arlington',
    'unitarian universalist church of arlington': 'Arlington',
}


def clean_text(element):
    if element is None:
        return ''
    text = element.get_text(' ', strip=True).replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def parse_datetime(value):
    match = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|'
        r'November|December)\s+(\d{1,2}),\s+(20\d{2}),\s+'
        r'(\d{1,2}):(\d{2})\s*([AP]M)',
        value,
        re.IGNORECASE,
    )
    if not match:
        return None

    try:
        event_date = date(
            int(match.group(3)), MONTHS[match.group(1).lower()], int(match.group(2))
        ).isoformat()
    except ValueError:
        return None

    hour = int(match.group(4))
    minute = int(match.group(5))
    if hour < 1 or hour > 12 or minute > 59:
        return None
    if match.group(6).upper() == 'PM' and hour != 12:
        hour += 12
    elif match.group(6).upper() == 'AM' and hour == 12:
        hour = 0
    return event_date, f'{hour:02d}:{minute:02d}'


def parse_section(section):
    headings = section.select('.con-heading3-inner h3')
    title_heading = next(
        (heading for heading in headings if clean_text(heading).lower().startswith('concert ')),
        None,
    )
    date_heading = next(
        (heading for heading in headings if parse_datetime(clean_text(heading))),
        None,
    )
    if title_heading is None or date_heading is None:
        return None

    title = clean_text(title_heading)
    parsed_datetime = parse_datetime(clean_text(date_heading))
    location_paragraph = next(
        (
            paragraph
            for paragraph in section.select('p')
            if clean_text(paragraph).lower().startswith('location:')
        ),
        None,
    )
    venue = re.sub(r'^location:\s*', '', clean_text(location_paragraph), flags=re.I).strip()
    city = VENUE_CITIES.get(venue.casefold())
    if not title or parsed_datetime is None or not venue or not city:
        return None

    description_parts = []
    for paragraph in section.select('p'):
        text = clean_text(paragraph)
        if not text or paragraph is location_paragraph:
            continue
        description_parts.append(text)

    event_date, time_from = parsed_datetime
    return {
        'title': title,
        'date': event_date,
        'url': PERFORMANCES_URL,
        'time_from': time_from,
        'venue': venue,
        'city': city,
        'description': '\n\n'.join(description_parts) or None,
    }


class NationalChamberEnsembleOrgCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='nationalchamberensemble_org',
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
            'description',
        ],
        front_fields=[('source_url', SOURCE_URL), ('source', SOURCE)],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(PERFORMANCES_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch National Chamber Ensemble performances',
                event='crawler_fetch_failed',
                level='error',
                url=PERFORMANCES_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        records = []
        for event_container in soup.select('section.section2 div.col-sm-7'):
            record = parse_section(event_container)
            if record:
                records.append(record)

        return sorted(
            records,
            key=lambda record: (
                record['date'], record['time_from'] or '', record['title'], record['venue']
            ),
        )


def main():
    NationalChamberEnsembleOrgCrawler().run()


if __name__ == '__main__':
    main()
