import re
from datetime import date

import requests
from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message


SOURCE_URL = 'https://www.brusselsmuzieque.com/concerts'
SOURCE = 'Brussels Muzieque'

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
        '(KHTML, like Gecko) Chrome/125.0 Safari/537.36'
    ),
    'Accept-Language': 'en-GB,en;q=0.9',
}

MONTHS = {
    name: number for number, name in enumerate(
        ('', 'JANUARY', 'FEBRUARY', 'MARCH', 'APRIL', 'MAY', 'JUNE',
         'JULY', 'AUGUST', 'SEPTEMBER', 'OCTOBER', 'NOVEMBER', 'DECEMBER')
    )
        if name
}

# The archive is hand-authored rather than backed by Wix Events. These are all
# venue labels used by its concert headings. Keeping the list explicit also
# prevents performer names or ticket prose from being mistaken for a venue.
VENUES = sorted([
    'ROYAL LIBRARY BRUSSELS (KBR)',
    'ROYAL CONSERVATORY OF BRUSSELS',
    'HET NCONCERTGEBOUW AMSTERDAM',  # spelling used on the source
    'MUSICAL INSTRUMENTS MUSEUM',
    'HUNGARIAN CULTURAL INSTITUTE',
    'DANISH CHURCH OF BRUSSELS',
    'ITALIAN CULTURAL INSTITUTE',
    'COLLEGE SAINT MICHEL',
    'EUROPEAN PARLIEMENT',  # spelling used on the source
    'VENICE - TEATRO LA FENICE',
    'CERCLE ROYAL GAULOIS',
    'FULL CIRCLE THEATER',
    'FULL CIRCLE HOUSE',
    'FULL CIRCLE',
    'ART BASE',
    'LE BAIXU',
], key=len, reverse=True)

DATE_RE = re.compile(
    r'(?P<day>\d{1,2})(?:ST|ND|RD|TH)?\s*'
    r'(?P<month>' + '|'.join(MONTHS) + r')\s*(?P<year>20\d{2})?',
    re.IGNORECASE,
)
TIME_RE = re.compile(
    r'\b(?P<hour>[01]?\d|2[0-3])\s*(?:h\s*(?P<minute>[0-5]\d)?|:\s*(?P<minute_colon>[0-5]\d))\b',
    re.I,
)


def clean_text(value):
    if value is None:
        return ''
    text = value.get_text('\n', strip=True) if hasattr(value, 'get_text') else str(value)
    text = text.replace('\xa0', ' ').replace('\u200b', '')
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r' *\n *', '\n', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def compact_text(element):
    text = re.sub(r'\s+', ' ', element.get_text(' ', strip=True).replace('\u200b', '')).strip()
    # A few old headings split words across independently styled Wix spans.
    return text.replace('ITA LIAN', 'ITALIAN').replace('C OMPOSERS', 'COMPOSERS')


def location_for(venue):
    upper = venue.upper()
    if 'AMSTERDAM' in upper:
        return 'Amsterdam', 'NL'
    if 'VENICE' in upper or 'LA FENICE' in upper:
        return 'Venice', 'IT'
    return 'Brussels', 'BE'


def find_venue(text):
    upper = text.upper()
    matches = [(upper.find(venue), venue) for venue in VENUES if venue in upper]
    matches = [match for match in matches if match[0] >= 0]
    return min(matches, default=(None, None), key=lambda item: item[0])


def valid_date(day, month, year):
    try:
        return date(int(year), MONTHS[month.upper()], int(day)).isoformat()
    except (KeyError, TypeError, ValueError):
        return None


def description_near(heading):
    # Programme text on the older archive is grouped with the title and date.
    parent = heading.parent
    paragraphs = [clean_text(item) for item in parent.select('p')]
    paragraphs = [item for item in paragraphs if item and item != '\u200b']
    return clean_text('\n\n'.join(paragraphs)) or None


def title_before(heading):
    sibling = heading.find_previous_sibling()
    while sibling is not None:
        if sibling.name in ('h1', 'h2', 'h3'):
            text = compact_text(sibling)
            if text and not DATE_RE.search(text) and not text.lower().startswith('season'):
                return text
            return ''
        sibling = sibling.find_previous_sibling()
    return ''


def records_from_heading(heading):
    text = compact_text(heading)
    matches = list(DATE_RE.finditer(text))
    years = [match.group('year') for match in matches if match.group('year')]
    if not matches or not years:
        return []

    fallback_year = years[-1]
    records = []
    cursor = 0
    title = ''
    for match in matches:
        prefix = text[cursor:match.start()].strip(' ,-')
        venue_pos, venue = find_venue(prefix)
        if not venue:
            cursor = match.end()
            continue
        if not title and venue_pos:
            title = prefix[:venue_pos].strip(' ,-')
        if not title:
            title = title_before(heading)
        event_date = valid_date(match.group('day'), match.group('month'), match.group('year') or fallback_year)
        if not title or not event_date:
            cursor = match.end()
            continue

        time_match = TIME_RE.search(text[match.end():match.end() + 12])
        time_from = None
        if time_match:
            minute = time_match.group('minute') or time_match.group('minute_colon') or '00'
            time_from = f'{int(time_match.group("hour")):02d}:{minute}'
        city, country_code = location_for(venue)
        link = heading.select_one('a[href]')
        url = link.get('href') if link and link.get('href', '').startswith('http') else SOURCE_URL
        records.append({
            'title': title,
            'date': event_date,
            'url': url,
            'time_from': time_from,
            'venue': venue.title(),
            'city': city,
            'country_code': country_code,
            'description': description_near(heading),
            'source_url': SOURCE_URL,
            'source': SOURCE,
        })
        cursor = match.end()
    return records


def current_season_records(soup):
    records = []
    for heading in soup.select('h1'):
        text = compact_text(heading)
        match = re.search(r'(?P<month>' + '|'.join(MONTHS) + r')\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?', text, re.I)
        if not match or re.search(r'20\d{2}', text):
            continue
        season = soup.select_one('h2')
        year_match = re.search(r'20\d{2}', compact_text(season)) if season else None
        parent = list(heading.parents)[1]
        venue_link = parent.select_one('a[href*="maps"]')
        venue = clean_text(venue_link) if venue_link else ''
        paragraphs = [clean_text(item) for item in parent.select('p')]
        description = clean_text('\n\n'.join(item for item in paragraphs if item)) or None
        event_date = valid_date(match.group('day'), match.group('month'), year_match.group() if year_match else None)
        if not venue or not description or not event_date:
            continue
        title = 'Family Concert: The Magic Flute' if 'Magic Flute' in description else 'Family Concert'
        times = list(TIME_RE.finditer(text))
        for time_match in times or [None]:
            city, country_code = location_for(venue)
            minute = None
            if time_match:
                minute = time_match.group('minute') or time_match.group('minute_colon') or '00'
            records.append({
                'title': title,
                'date': event_date,
                'url': SOURCE_URL,
                'time_from': f'{int(time_match.group("hour")):02d}:{minute}' if time_match else None,
                'venue': venue,
                'city': city,
                'country_code': country_code,
                'description': description,
                'source_url': SOURCE_URL,
                'source': SOURCE,
            })
    return records


class BrusselsMuziequeComCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='brusselsmuzieque_com',
        source=SOURCE,
        source_url=SOURCE_URL,
        country_code='BE',
        upload_target='potential',
        columns=[
            'title', 'date', 'url', 'time_from', 'venue', 'city',
            'country_code', 'description', 'source_url', 'source',
        ],
        dedupe_subset=['title', 'date', 'time_from', 'venue'],
    )

    def scrape(self):
        try:
            response = requests.get(SOURCE_URL, headers=HEADERS, timeout=45)
            response.raise_for_status()
        except requests.RequestException as error:
            log_message(
                'Failed to fetch Brussels Muzieque concerts',
                event='crawler_fetch_failed',
                level='error',
                url=SOURCE_URL,
                error_type=type(error).__name__,
                error_message=str(error),
            )
            raise

        soup = BeautifulSoup(response.text, 'html.parser')
        records = current_season_records(soup)
        for heading in soup.select('h1, h2, h3'):
            text = compact_text(heading)
            if re.search(r'\b20\d{2}\b', text) and not text.lower().startswith(('season', 'spring season')):
                records.extend(records_from_heading(heading))
        return sorted(records, key=lambda item: (item['date'], item['time_from'] or '', item['title'], item['venue']))


def main():
    BrusselsMuziequeComCrawler().run()


if __name__ == '__main__':
    main()
