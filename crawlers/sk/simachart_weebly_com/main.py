import re
from datetime import date as date_cls

import requests

from bs4 import BeautifulSoup

from ...base import BaseCrawler, CrawlerConfig
from observability import log_message
from ...extractors import clean_string, extract_city, extract_time

URL = 'https://simachart.weebly.com/bude.html'
DATE_PATTERN = re.compile(r'(?<!\d)(\d{1,2})\.\s*(\d{1,2})\.\s*\.?\s*(\d{4})(?!\d)')
MAX_SUMMARY_LINE_LENGTH = 160


def extract_concert_info(paragraph):
    lines = [clean_string(line) for line in paragraph.get_text('\n', strip=True).splitlines()]
    lines = [line for line in lines if line]
    date_line_index = next((i for i, line in enumerate(lines) if DATE_PATTERN.search(line)), None)
    if date_line_index is None:
        return None

    # Simachart has no event markup. Its reliable structure is a compact summary:
    # date/time, venue, address, then title. The page also repeats the date in a
    # long editorial paragraph, which must not become a duplicate malformed event.
    summary_lines = lines[date_line_index:date_line_index + 4]
    if (
        len(summary_lines) < 4
        or extract_time(summary_lines[0]) is None
        or any(len(line) > MAX_SUMMARY_LINE_LENGTH for line in summary_lines)
    ):
        return None

    date_match = DATE_PATTERN.search(lines[date_line_index])
    day, month, year = [int(part) for part in date_match.groups()]
    date = f'{year}-{month:02d}-{day:02d}'
    if date_cls.fromisoformat(date) < date_cls.today():
        return None

    time = extract_time(summary_lines[0])
    venue = summary_lines[1]
    address = summary_lines[2]
    city = extract_city(address)
    if city is None:
        return None
    title = summary_lines[3]
    
    return {
		'title': title,
		'date': date,
		'time_from': time,
		'venue': venue,
		'city': city,
		'description': paragraph.get_text().strip()
	}

def extract_concerts(soup):
    paragraphs = soup.select('div.paragraph')
    concerts = []
    for p in paragraphs:
        try:
            concert = extract_concert_info(p)
            if concert is not None:
                concerts.append(concert)
        except Exception as e:
            log_message('Error extracting concert info', event='crawler_item_failed', level='warning', error_type=type(e).__name__, error_message=str(e))
    return concerts


class SimachartCrawler(BaseCrawler):
    config = CrawlerConfig(
        slug='simachart_weebly_com',
        source='Simachart',
        source_url='https://simachart.weebly.com',
        columns=['title', 'date', 'time_from', 'venue', 'city', 'description'],
        front_fields=[
            ('url', URL),
            ('source_url', 'https://simachart.weebly.com'),
            ('source', 'Simachart'),
        ],
    )

    def scrape(self):
        r = requests.get(URL)
        soup = BeautifulSoup(r.content, 'html.parser')
        return extract_concerts(soup)


def main():
    SimachartCrawler().run()


if __name__ == '__main__':
    main()
