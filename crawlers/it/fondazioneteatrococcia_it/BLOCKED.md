<!-- crawler-factory-metadata
{"url":"https://www.fondazioneteatrococcia.it/","geographic_scope":"country","country_code":"IT","reason_code":"access_blocked","attempted_at":"2026-08-20","retry_after":"2026-09-19"}
-->

# Fondazione Teatro Carlo Coccia crawler blocked

Original URL: https://www.fondazioneteatrococcia.it/

The source is an Italian, Novara-based theatre calendar, but it was not reachable
from the crawler environment during this attempt. Playwright navigation waited 60
seconds without reaching `DOMContentLoaded`. Browser request inspection and browser
shutdown then also timed out. Direct HTTP checks against the `www` host, apex host,
HTTPS, HTTP, and forced IPv4 all resolved the domain to `151.11.48.23` but timed out
without receiving response headers. A direct `scrape()` fetch consequently could
not retrieve the calendar either.

API/network investigation was attempted first, as required. No completed browser
request trace was available from which to reconstruct an API because the initial
document request never completed. Searches for indexed API paths and `wp-json`
did not reveal a structured endpoint. The public pages appear to be static HTML,
with stable first-party category pages rather than API query parameters.

HTML investigation found recently indexed, concrete performances and archives:

- `spettacoli.html`: mixed current calendar
- `spettacoli_opera_danza_concerti.html`: Opera, Danza, Concerti
- `spettacoli_crescendo.html`: Chi ha paura del melodramma?, Teatro scuola
- `spettacoli_eventi.html`: Varietà, Eventi, Aperitivi in musica
- `spettacoli-2025.html`: mixed annual archive; the site also exposes year links
  for 2024, 2023, 2022, and 2021

The category pages are independent static feeds and expose no pagination or date
query values to test. Their identifiers therefore do not participate in
pagination. Indexed representative detail pages showed concrete dated
performances with separate date/time lines and venues, including `TURANDOT`
(Opera), `LA BOHÈME IN UNA STANZA` (Chi ha paura del melodramma?), `I VIAGGI DI
GULLIVER` (family opera), and adjacent nonclassical theatre, comedy, jazz, and
school events. The narrow Opera/Danza/Concerti feed is in-scope but incomplete:
it omits eligible family opera from Crescendo. Crescendo itself contains ordinary
non-musical school theatre, while Eventi/Aperitivi contains substantial jazz/pop
contamination and potentially ambiguous crossover. Annual archives are mixed and
unfiltered.

If access is restored, the appropriate comprehensive source is the mixed
`spettacoli.html` feed plus every linked `spettacoli-YYYY.html` archive, followed
by each first-party detail page. It should use `upload_target="potential"` because
the only comprehensive current-and-past feed is mixed, while the first-party
category feeds cannot be combined without either omitting eligible family opera
or admitting uncertain/nonclassical records. Implementation can proceed once the
origin accepts connections from the crawler environment (or the publisher
provides a reachable first-party API or mirror), allowing actual HTML selectors,
archive coverage, record expansion, and representative output to be validated.
