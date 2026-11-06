"""United-States-only filter, based on the location text a posting carries.

classify_location(text) returns:
  "us"      - a US place is named (state, big US city, "United States", "US", "Remote - US" ...)
  "non_us"  - a non-US country / region / city is named and no US place is
  "unclear" - nothing recognisable ("Remote", "Anywhere", "Global", empty). Held for you to review.

A posting listing several places ("San Francisco, CA | London, UK") counts as US, because a US
office is hiring for it.

Two-letter state codes are only read after a comma ("Austin, TX"). Some of them are also country
codes (IN = Indiana / India, DE = Delaware / Germany, CA = California / Canada), so those "weak" codes
only count as US when no non-US place is named in the same text.
"""
import re

US_STATE_NAMES = [
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "florida", "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana",
    "nebraska", "nevada", "new hampshire", "new jersey", "new mexico", "new york", "north carolina",
    "north dakota", "ohio", "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina",
    "south dakota", "tennessee", "texas", "utah", "vermont", "virginia", "washington", "west virginia",
    "wisconsin", "wyoming", "district of columbia",   # "georgia" left out on purpose: also a country
]
US_CITIES = [
    "new york", "nyc", "brooklyn", "san francisco", "bay area", "oakland", "san jose", "san mateo",
    "redwood city", "palo alto", "menlo park", "mountain view", "sunnyvale", "santa clara", "cupertino",
    "fremont", "los angeles", "san diego", "seattle", "bellevue", "redmond", "austin", "dallas",
    "houston", "boston", "chicago", "denver", "atlanta", "miami", "salt lake city", "pittsburgh",
    "philadelphia", "raleigh", "durham", "minneapolis", "nashville", "phoenix", "las vegas", "detroit",
    "washington dc", "d.c.",
]
# Two-letter codes that are NOT also a country code
STRONG_ABBR = {"AK", "CT", "DC", "FL", "HI", "IA", "KS", "MI", "NH", "NJ", "NM", "NV", "NY", "ND", "OH",
               "OK", "OR", "RI", "TX", "UT", "VT", "WA", "WI", "WV", "WY"}
# State codes that are also country codes (Indiana/India, Delaware/Germany, California/Canada ...)
WEAK_ABBR = {"AL", "AR", "AZ", "CA", "CO", "DE", "GA", "ID", "IL", "IN", "KY", "LA", "MA", "MD", "ME", "MN",
             "MO", "MS", "MT", "NC", "NE", "PA", "SC", "SD", "TN", "VA"}

NON_US = [
    # countries and regions
    "canada", "british columbia", "ontario", "quebec", "united kingdom", "uk", "u.k.", "england", "scotland",
    "wales", "britain", "ireland", "germany", "france", "spain", "italy", "portugal", "netherlands", "belgium",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland", "poland", "czech", "czechia",
    "romania", "bulgaria", "hungary", "greece", "serbia", "croatia", "ukraine", "lithuania", "latvia",
    "estonia", "israel", "uae", "united arab emirates", "saudi arabia", "qatar", "egypt", "nigeria", "kenya",
    "south africa", "india", "pakistan", "bangladesh", "sri lanka", "china", "hong kong", "taiwan", "japan",
    "korea", "singapore", "malaysia", "indonesia", "thailand", "vietnam", "philippines", "australia",
    "new zealand", "brazil", "argentina", "chile", "colombia", "peru", "uruguay", "costa rica", "panama",
    "mexico", "turkey", "emea", "apac", "latam", "europe", "european union", "eu", "asia", "africa",
    "middle east", "oceania",
    # cities
    "london", "manchester", "edinburgh", "dublin", "paris", "berlin", "munich", "hamburg", "frankfurt",
    "amsterdam", "rotterdam", "madrid", "barcelona", "lisbon", "rome", "milan", "zurich", "geneva", "vienna",
    "stockholm", "oslo", "copenhagen", "helsinki", "warsaw", "krakow", "prague", "budapest", "bucharest",
    "sofia", "athens", "belgrade", "kyiv", "kiev", "vilnius", "riga", "tallinn", "tbilisi", "tel aviv",
    "jerusalem", "dubai", "abu dhabi", "riyadh", "cairo", "lagos", "nairobi", "cape town", "johannesburg",
    "mumbai", "delhi", "bangalore", "bengaluru", "hyderabad", "chennai", "pune", "gurgaon", "gurugram",
    "noida", "kolkata", "karachi", "lahore", "dhaka", "beijing", "shanghai", "shenzhen", "taipei", "tokyo",
    "osaka", "seoul", "kuala lumpur", "jakarta", "bangkok", "ho chi minh", "hanoi", "manila", "sydney",
    "melbourne", "brisbane", "auckland", "toronto", "vancouver", "montreal", "ottawa", "calgary",
    "waterloo", "sao paulo", "são paulo", "rio de janeiro", "buenos aires", "santiago", "bogota", "bogotá",
    "lima", "mexico city", "guadalajara", "monterrey",
]


def _phrases(words):
    alt = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(r"(?<![a-z])(?:" + alt + r")(?![a-z])")


_US_PLACES = _phrases(US_STATE_NAMES + US_CITIES)
_NON_US = _phrases(NON_US)
_US_WORDS = re.compile(r"(?<![A-Za-z])(?:united states(?: of america)?|usa|u\.s\.a?)(?![A-Za-z])", re.I)
_US_UPPER = re.compile(r"\bUS\b")                  # "Remote - US" (upper case only, so not the word "us")
_ABBR = re.compile(r",\s*([A-Z]{2})\b")


def classify_location(text) -> str:
    loc = text or ""
    low = loc.lower()
    codes = set(_ABBR.findall(loc))
    strong = bool(_US_WORDS.search(loc) or _US_UPPER.search(loc) or _US_PLACES.search(low)
                  or codes & STRONG_ABBR)
    non_us = bool(_NON_US.search(low))
    if strong:
        return "us"
    if non_us:
        return "non_us"
    if codes & WEAK_ABBR:
        return "us"
    return "unclear"


# "US" / "USA" count only in capitals, so the pronoun "us" never matches
_TOK = r"(?:united states|u\.s\.a?\.?|(?-i:USA|US))(?![A-Za-z])"
_DESC_US = re.compile(
    r"\bremote\b[^.\n]{0,20}\b" + _TOK
    + r"|\b(?:based|located|reside|residing|live|living|work|working)\s+(?:in|within)\s+the\s+" + _TOK
    + r"|(?-i:\bUS)[- ]based\b"
    + r"|authori[sz]ed to work in the " + _TOK, re.I)


def us_in_description(description: str) -> bool:
    """Used only when the location field itself was unclear."""
    return bool(_DESC_US.search(description or ""))


_REMOTE_ABROAD = re.compile(r"remote\s*[-–(,:]?\s*(?:in\s+)?(emea|europe|eu|uk|india|canada|latam|apac|"
                            r"germany|france|poland|spain|portugal|brazil|mexico|australia|singapore|japan)", re.I)


def where_with_description(location: str, description: str) -> str:
    """classify_location, but an unclear location (e.g. just 'Remote' or '2 Locations') is decided by the text:
    'Remote (US)' / 'authorized to work in the US' -> us; only non-US places named (London, India, EMEA) -> non_us."""
    where = classify_location(location)
    if where != "unclear":
        return where
    text = description or ""
    if us_in_description(text):
        return "us"
    low = text.lower()
    if _REMOTE_ABROAD.search(text):
        return "non_us"
    non_us = len(_NON_US.findall(low))
    us = len(_US_PLACES.findall(low)) + len(_US_WORDS.findall(text)) + len(_US_UPPER.findall(text))
    if non_us and not us:
        return "non_us"
    return "unclear"
