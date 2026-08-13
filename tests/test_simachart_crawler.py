from datetime import date
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from crawlers.sk.simachart_weebly_com import main as simachart


class FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 13)


class SimachartCrawlerTests(unittest.TestCase):
    @patch.object(simachart, 'date_cls', FixedDate)
    def test_extracts_compact_event_summary_without_inline_style(self):
        soup = BeautifulSoup(
            '''
            <div class="paragraph">
              HUDBA U FULLU<br>
              Sobota 26.9.2026 o 18:00 h<br>
              Synagóga Ružomberok<br>
              Panská 3, Ružomberok<br>
              ČESKOSLOVENSKÉ KOMORNÉ DUO<br>
              Pavel Burdych, husle
            </div>
            ''',
            'html.parser',
        )

        concerts = simachart.extract_concerts(soup)

        self.assertEqual(len(concerts), 1)
        self.assertEqual(
            {key: concerts[0][key] for key in ('title', 'date', 'time_from', 'venue', 'city')},
            {
                'title': 'ČESKOSLOVENSKÉ KOMORNÉ DUO',
                'date': '2026-09-26',
                'time_from': '18:00',
                'venue': 'Synagóga Ružomberok',
                'city': 'Ružomberok',
            },
        )

    @patch.object(simachart, 'date_cls', FixedDate)
    def test_ignores_editorial_paragraph_that_repeats_event_date(self):
        soup = BeautifulSoup(
            '''
            <div class="paragraph" style="text-align:justify;">
              Cyklus Hudba u Fullu<br>
              v sobotu 26. 9. 2026 o 18:00 h Synagóge v Ružomberku.<br>
              S programom vystúpi Československé komorné duo, ktoré patrí medzi popredných
              interpretov komornej hudby a dlhodobo koncertuje doma aj v zahraničí. Tento
              redakčný text pokračuje ďalšími podrobnosťami o programe a interpretoch.<br>
              Ďalší dlhý redakčný odsek, ktorý nie je miestom konania.<br>
              Vstupné dobrovoľné
            </div>
            ''',
            'html.parser',
        )

        self.assertEqual(simachart.extract_concerts(soup), [])

    @patch.object(simachart, 'date_cls', FixedDate)
    def test_accepts_spaced_date_format(self):
        paragraph = BeautifulSoup(
            '''
            <div class="paragraph">
              Nedeľa 4. 10. 2026 o 9:05<br>
              Galéria Ľudovíta Fullu<br>
              Dušana Makovického 1, Ružomberok<br>
              Ranný koncert
            </div>
            ''',
            'html.parser',
        ).div

        concert = simachart.extract_concert_info(paragraph)

        self.assertEqual(concert['date'], '2026-10-04')
        self.assertEqual(concert['time_from'], '9:05')
        self.assertEqual(concert['title'], 'Ranný koncert')


if __name__ == '__main__':
    unittest.main()
