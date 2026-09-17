"""Question set for the citizen feedback survey.

Values are stored; labels are shown. Keeping both here means the form, the template and
the export all read the same list, and a wording change never desynchronises them.

Nothing in this file may identify a respondent: location stops at county, and there is
no field for a name, an address, a postcode, a date of birth or anything in the GDPR
special categories. See ``docs`` in :mod:`observatory.models` for how the optional email
is kept apart from the answers.
"""
from __future__ import annotations

SCHEMA_VERSION = "1.0"
CONSENT_TEXT_VERSION = "1.0"

CONTROLLER_NAME = "University of Galway"
CONTACT_EMAIL = "liz.coleman@universityofgalway.ie"
RETENTION_PERIOD = "24 months after the project ends"

# The 32 counties, alphabetical within each jurisdiction. "Derry / Londonderry" carries
# both names because this is a cross-border survey and either alone excludes someone.
COUNTIES_IRELAND = [
    "Carlow", "Cavan", "Clare", "Cork", "Donegal", "Dublin", "Galway", "Kerry", "Kildare",
    "Kilkenny", "Laois", "Leitrim", "Limerick", "Longford", "Louth", "Mayo", "Meath",
    "Monaghan", "Offaly", "Roscommon", "Sligo", "Tipperary", "Waterford", "Westmeath",
    "Wexford", "Wicklow",
]
COUNTIES_NORTHERN_IRELAND = [
    "Antrim", "Armagh", "Derry / Londonderry", "Down", "Fermanagh", "Tyrone",
]
PREFER_NOT_SAY = "prefer_not_say"

COUNTY_GROUPS = [
    ("Ireland", [(c, c) for c in COUNTIES_IRELAND]),
    ("Northern Ireland", [(c, c) for c in COUNTIES_NORTHERN_IRELAND]),
]
COUNTY_CHOICES = [(c, c) for c in COUNTIES_IRELAND + COUNTIES_NORTHERN_IRELAND] + [
    (PREFER_NOT_SAY, "Prefer not to say"),
]

AREA_TYPE = [
    ("city_large_town", "A city or large town"),
    ("small_town_village", "A small town or village"),
    ("rural", "The countryside or an isolated area"),
    (PREFER_NOT_SAY, "Prefer not to say"),
]

AIR_QUALITY_RATING = [
    ("very_good", "Very good"),
    ("good", "Good"),
    ("neither", "Neither good nor poor"),
    ("poor", "Poor"),
    ("very_poor", "Very poor"),
    ("not_sure", "Not sure"),
]

POLLUTION_SOURCES = [
    ("home_heating", "Home heating — turf, coal, wood or other solid fuel"),
    ("road_traffic", "Road traffic"),
    ("agriculture", "Farming and agriculture"),
    ("industry", "Industry or commercial premises"),
    ("transboundary", "Pollution carried in from elsewhere, including across the border"),
    ("none", "None of these"),
    ("not_sure", "Not sure"),
]
POLLUTION_SOURCES_EXCLUSIVE = {"none", "not_sure"}

DASHBOARD_USEFULNESS = [
    ("very_useful", "Very useful"),
    ("fairly_useful", "Fairly useful"),
    ("not_very_useful", "Not very useful"),
    ("not_useful", "Not useful at all"),
    ("not_used", "I have not used it"),
]

DASHBOARD_IMPROVEMENTS = [
    ("plain_language", "Plain-language explanations of what the readings mean"),
    ("health_advice", "Advice on what to do when air quality is poor"),
    ("more_sensors", "More sensors, covering more places"),
    ("alerts", "Alerts when air quality gets worse"),
    ("comparisons", "Comparisons with other areas or with official limits"),
    ("raw_data", "Access to the underlying data to download"),
    ("other", "Something else"),
]
DASHBOARD_IMPROVEMENTS_MAX = 3
DASHBOARD_IMPROVEMENTS_OTHER_MAX = 120

SENSOR_TRUST = [
    ("trust_a_lot", "I trust them a lot"),
    ("trust_somewhat", "I trust them somewhat"),
    ("neither", "Neither trust nor distrust them"),
    ("distrust_somewhat", "I distrust them somewhat"),
    ("distrust_a_lot", "I distrust them a lot"),
    ("not_sure", "Not sure"),
]
# Asking about doubts only makes sense when some doubt was expressed.
SENSOR_TRUST_WITH_DOUBTS = {"neither", "distrust_somewhat", "distrust_a_lot", "not_sure"}

TRUST_CONCERNS = [
    ("accuracy", "How accurate the readings are"),
    ("who_measures", "Who is doing the measuring"),
    ("validation", "Whether readings are checked against official monitors"),
    ("follow_through", "Whether anything is actually done with the results"),
    ("no_concerns", "I have no concerns"),
]
TRUST_CONCERNS_EXCLUSIVE = {"no_concerns"}

PARTICIPATION = [
    ("host_sensor", "Yes — I'd host a sensor at my home, school, workplace or community building"),
    ("portable_sensor", "Yes — I'd carry a portable sensor for a while"),
    ("attend_session", "Yes — I'd come to an information session or workshop"),
    ("need_more_info", "Maybe — I'd need to know more first"),
    ("not_interested", "No — I'm not interested"),
    ("concerns", "I have concerns about taking part"),
]
PARTICIPATION_EXCLUSIVE = {"not_interested"}

OPEN_DATA_SUPPORT = [
    ("strongly_agree", "Strongly agree"),
    ("agree", "Agree"),
    ("neutral", "Neither agree nor disagree"),
    ("disagree", "Disagree"),
    ("strongly_disagree", "Strongly disagree"),
]

ADDITIONAL_COMMENTS_MAX = 1000
FREE_TEXT_WARNING = "Please don't include names, addresses or other personal details."

# Every checkbox group that has an "answers on its own" option, for the form and the page.
EXCLUSIVE_OPTIONS = {
    "pollution_sources": POLLUTION_SOURCES_EXCLUSIVE,
    "trust_concerns": TRUST_CONCERNS_EXCLUSIVE,
    "participation": PARTICIPATION_EXCLUSIVE,
}


def labels(choices) -> dict[str, str]:
    return dict(choices)
