import unittest
from unittest.mock import MagicMock, patch

from agent_utils import geonames


def rdf(
    *,
    external_id="4259418",
    country="US",
    feature_class="P",
    feature_code="P.PPLA",
    names=("Indianapolis", "Indpls"),
):
    name_xml = "".join(
        f"<gn:alternateName>{name}</gn:alternateName>" for name in names
    )
    return f"""<?xml version="1.0"?>
    <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
      xmlns:gn="http://www.geonames.org/ontology#">
      <gn:Feature rdf:about="https://sws.geonames.org/{external_id}/">
        {name_xml}
        <gn:featureClass rdf:resource="https://www.geonames.org/ontology#{feature_class}"/>
        <gn:featureCode rdf:resource="https://www.geonames.org/ontology#{feature_code}"/>
        <gn:countryCode>{country}</gn:countryCode>
      </gn:Feature>
    </rdf:RDF>""".encode()


class GeoNamesTests(unittest.TestCase):
    def setUp(self):
        geonames.fetch_geonames_record.cache_clear()

    @staticmethod
    def _response(payload):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = payload
        return response

    def test_fetches_and_caches_structured_record(self):
        with patch.object(
            geonames.urllib.request,
            "urlopen",
            return_value=self._response(rdf()),
        ) as urlopen:
            first = geonames.fetch_geonames_record("4259418")
            second = geonames.fetch_geonames_record("4259418")

        self.assertIs(first, second)
        self.assertEqual(first.feature_code, "P.PPLA")
        self.assertIn("indianapolis", first.names)
        urlopen.assert_called_once()

    def test_rejects_bad_or_oversized_responses(self):
        cases = (
            (b"not xml", "malformed RDF"),
            (
                b"x" * (geonames.GEONAMES_MAX_RESPONSE_BYTES + 1),
                "response limit",
            ),
            (rdf(external_id="999"), "response ID"),
        )
        for payload, message in cases:
            geonames.fetch_geonames_record.cache_clear()
            with (
                patch.object(
                    geonames.urllib.request,
                    "urlopen",
                    return_value=self._response(payload),
                ),
                self.assertRaisesRegex(geonames.GeoNamesValidationError, message),
            ):
                geonames.fetch_geonames_record("4259418")

    def test_network_failure_is_validation_failure(self):
        with (
            patch.object(
                geonames.urllib.request, "urlopen", side_effect=TimeoutError()
            ),
            self.assertRaisesRegex(
                geonames.GeoNamesValidationError, "unavailable"
            ),
        ):
            geonames.fetch_geonames_record("4259418")

    def test_validates_country_feature_and_names(self):
        record = geonames.GeoNamesRecord(
            "4259418", "US", "P", "P.PPLA", frozenset({"indianapolis"})
        )
        geonames.validate_geonames_city(
            record,
            country_code="US",
            proposed_names=("Indianapolis",),
            legitimate_raw_name="Indianapolis",
        )

        invalid = (
            (record, "SE", ("Indianapolis",), None, "country"),
            (
                geonames.GeoNamesRecord(
                    "1", "US", "A", "A.ADM2", record.names
                ),
                "US",
                ("Indianapolis",),
                None,
                "populated place",
            ),
            (record, "US", ("South Bend",), None, "proposed city"),
            (record, "US", ("Indianapolis",), "South Bend", "raw city"),
        )
        for candidate, country, names, raw_name, message in invalid:
            with self.assertRaisesRegex(geonames.GeoNamesValidationError, message):
                geonames.validate_geonames_city(
                    candidate,
                    country_code=country,
                    proposed_names=names,
                    legitimate_raw_name=raw_name,
                )


if __name__ == "__main__":
    unittest.main()
