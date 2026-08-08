from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import urllib.request
import xml.etree.ElementTree as ET

from crawlers.cities import normalize_city_key


GEONAMES_RDF_URL = "https://sws.geonames.org/{external_id}/about.rdf"
GEONAMES_TIMEOUT_SECONDS = 10
GEONAMES_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
GEONAMES_USER_AGENT = "classical-bot/1.0 (city identity validation)"
GN_NAMESPACE = "http://www.geonames.org/ontology#"
RDF_NAMESPACE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"


class GeoNamesValidationError(ValueError):
    pass


@dataclass(frozen=True)
class GeoNamesRecord:
    external_id: str
    country_code: str
    feature_class: str
    feature_code: str
    names: frozenset[str]


def _resource_suffix(element: ET.Element | None) -> str:
    if element is None:
        return ""
    resource = element.attrib.get(f"{{{RDF_NAMESPACE}}}resource", "")
    return resource.rsplit("#", 1)[-1]


@lru_cache(maxsize=1024)
def fetch_geonames_record(external_id: str) -> GeoNamesRecord:
    if not external_id.isdigit() or int(external_id) <= 0:
        raise GeoNamesValidationError("GeoNames ID must be a positive integer")

    request = urllib.request.Request(
        GEONAMES_RDF_URL.format(external_id=external_id),
        headers={"User-Agent": GEONAMES_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(
            request, timeout=GEONAMES_TIMEOUT_SECONDS
        ) as response:
            payload = response.read(GEONAMES_MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError) as error:
        raise GeoNamesValidationError(
            f"GeoNames record {external_id} is unavailable"
        ) from error
    if len(payload) > GEONAMES_MAX_RESPONSE_BYTES:
        raise GeoNamesValidationError(
            f"GeoNames record {external_id} exceeds the response limit"
        )

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as error:
        raise GeoNamesValidationError(
            f"GeoNames record {external_id} returned malformed RDF"
        ) from error

    feature = root.find(f"{{{GN_NAMESPACE}}}Feature")
    if feature is None:
        raise GeoNamesValidationError(f"GeoNames record {external_id} was not found")
    about = feature.attrib.get(f"{{{RDF_NAMESPACE}}}about", "").rstrip("/")
    if about.rsplit("/", 1)[-1] != external_id:
        raise GeoNamesValidationError("GeoNames response ID does not match the request")

    country = feature.findtext(f"{{{GN_NAMESPACE}}}countryCode", "").strip()
    feature_class = _resource_suffix(feature.find(f"{{{GN_NAMESPACE}}}featureClass"))
    feature_code = _resource_suffix(feature.find(f"{{{GN_NAMESPACE}}}featureCode"))
    name_tags = ("name", "alternateName", "officialName", "shortName")
    names = frozenset(
        normalized
        for tag in name_tags
        for element in feature.findall(f"{{{GN_NAMESPACE}}}{tag}")
        if (normalized := normalize_city_key(element.text))
    )
    if not country or not feature_class or not feature_code or not names:
        raise GeoNamesValidationError(
            f"GeoNames record {external_id} is missing required identity fields"
        )
    return GeoNamesRecord(
        external_id=external_id,
        country_code=country,
        feature_class=feature_class,
        feature_code=feature_code,
        names=names,
    )


def validate_geonames_city(
    record: GeoNamesRecord,
    *,
    country_code: str,
    proposed_names: tuple[str, ...],
    legitimate_raw_name: str | None = None,
) -> None:
    if record.country_code != country_code:
        raise GeoNamesValidationError("GeoNames country does not match the proposal")
    if record.feature_class != "P" or not record.feature_code.startswith("P."):
        raise GeoNamesValidationError("GeoNames identity is not a populated place")
    normalized_names = {
        normalized
        for name in proposed_names
        if (normalized := normalize_city_key(name))
    }
    if not normalized_names.intersection(record.names):
        raise GeoNamesValidationError("GeoNames names do not match the proposed city")
    if legitimate_raw_name is not None:
        normalized_raw = normalize_city_key(legitimate_raw_name)
        if normalized_raw not in record.names:
            raise GeoNamesValidationError(
                "raw city name does not match the GeoNames identity"
            )
