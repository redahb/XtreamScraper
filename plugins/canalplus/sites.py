"""The Canal+ (M7) country websites the plugin can read.

Each website has its own catalogue in its own language: its own Algolia index (one search account
for all of them), its own host for page addresses and its own words for seasons and episodes in
those addresses. Records, fields and the ``/stream/film|series/`` structure are the same
everywhere. Read from the websites on 2026-10-15.

The plugin's required *Country and language* setting has no default: the user picks one of these
(its values are language tags, ``nl-NL``, ``de-AT``...).
"""

from __future__ import annotations

import re
from typing import NamedTuple


class Site(NamedTuple):
    #: The setting's value: a language tag (language-COUNTRY).
    tag: str
    label: str
    #: The page whose inline ``aa('init', {...})`` call holds the public search settings.
    settings_url: str
    #: The host of the page addresses in the records.
    host: str
    #: The Algolia index the website searches.
    index: str
    #: A season's page segment (``seizoen-2``) and an episode's (``aflevering-3``); group 1 is the number.
    season_segment: re.Pattern[str]
    episode_segment: re.Pattern[str]

    @property
    def language(self) -> str:
        return self.tag.split("-", 1)[0]


NL = Site("nl-NL", "Nederland (Nederlands)", "https://www.canalplus.nl/stream/", "www.canalplus.nl",
          "prd_CANALPLUSNL_nl", re.compile(r"seizoen-(\d{1,4})"), re.compile(r"aflevering-(\d{1,5})"))
AT = Site("de-AT", "Österreich (Deutsch)", "https://www.canalplus.at/stream/", "www.canalplus.at",
          "prd_CANALPLUSAT_de", re.compile(r"staffel-(\d{1,4})"), re.compile(r"folge-(\d{1,5})"))
CZ = Site("cs-CZ", "Česko (Čeština)", "https://www.canalplus.cz/stream/", "www.canalplus.cz",
          "prd_CANALPLUSCZ_cs", re.compile(r"serie-(\d{1,4})"), re.compile(r"epizoda-(\d{1,5})"))
SK = Site("sk-SK", "Slovensko (Slovenčina)", "https://www.canalplus.sk/stream/", "www.canalplus.sk",
          "prd_CANALPLUSSK_sk", re.compile(r"seria-(\d{1,4})"), re.compile(r"epizoda-(\d{1,5})"))
HU = Site("hu-HU", "Magyarország (Magyar)", "https://elofizetek.canalplus.com/stream/", "elofizetek.canalplus.com",
          "prd_CANALPLUSHU_hu", re.compile(r"(\d{1,4})\.-evad"), re.compile(r"(\d{1,5})\.-resz"))

SITES: tuple[Site, ...] = (NL, AT, CZ, SK, HU)
#: Only a fallback for code paths that run without a configured country (and the host used to read
#: a bare ``film/<slug>`` manual page address); the plugin is not configured until one is chosen.
DEFAULT = NL
_BY_TAG = {site.tag: site for site in SITES}
SITE_TAGS = frozenset(_BY_TAG)


def site_for(tag: object) -> Site:
    """The site of a setting value; an unknown or missing value gives :data:`DEFAULT`."""
    return _BY_TAG.get(tag if isinstance(tag, str) else "", DEFAULT)
