"""Location heuristics: is a posting remote / hybrid, does it name a US or non-US place, and what is its remote scope.

`infer_scope` returns one of: "us" | "worldwide" | "other" | "unknown". It is only meaningful for remote postings.
"""
from __future__ import annotations

import re

_REMOTE = re.compile(
    r"\b(remote|work(ing)? from home|wfh|distributed|telecommut\w*|home[- ]based|work from anywhere|anywhere)\b", re.I
)
_HYBRID = re.compile(r"\bhybrid\b|\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b", re.I)
_WORLD = re.compile(
    r"\b(worldwide|world[- ]wide|anywhere|global(ly)?|everywhere|international|any location|all countries|earth)\b", re.I
)
_US_CS = re.compile(r"\b(US|USA|U\.S\.A?\.?|AMER|NYC|DC|SF)\b")  # case-sensitive tokens
_US_CI = re.compile(
    r"\b(united states|u\.s\.|usa|north america|americas|nationwide|us[- ]only|us[- ]based|continental us|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|"
    r"indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|"
    r"montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|"
    r"oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west virginia|wisconsin|wyoming|san francisco|seattle|austin|boston|chicago|denver|atlanta|los angeles|"
    r"new york city|sf bay area|bay area|dallas|houston|san antonio|san diego|san jose|phoenix|philadelphia|miami|portland|"
    r"minneapolis|detroit|nashville|charlotte|raleigh|pittsburgh|salt lake city|las vegas|orlando|tampa|sacramento|columbus|"
    r"cleveland|cincinnati|indianapolis|kansas city|st\.? louis|baltimore|boulder|san mateo|palo alto|mountain view|sunnyvale|"
    r"menlo park|redwood city|oakland|brooklyn|manhattan|jersey city|durham|honolulu|omaha|milwaukee|albuquerque|tucson|boise|"
    r"new orleans|louisville|memphis|buffalo|providence|hartford|scottsdale|fort worth|jacksonville|oklahoma city|"
    r"fort lauderdale|santa monica|irvine|santa clara|cupertino|fremont|bellevue|spokane|ann arbor|des moines)\b",
    re.I,
)
_STATE = re.compile(
    r",\s*(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC)\b"
)
_NON_US = re.compile(
    r"\b(emea|europe|european|eu|eea|uk|united kingdom|england|scotland|germany|france|spain|italy|india|apac|asia|latam|"
    r"latin america|brazil|canada|canadian|australia|ireland|netherlands|poland|portugal|singapore|japan|israel|mexico|"
    r"philippines|nigeria|argentina|colombia|romania|ukraine|turkey|south africa|switzerland|sweden|norway|denmark|"
    r"finland|belgium|austria|czech|hungary|greece|kenya|egypt|pakistan|bangladesh|indonesia|vietnam|thailand|"
    r"china|korea|new zealand|chile|peru|serbia|bulgaria|croatia|estonia|lithuania|latvia|gmt|cet|cest|"
    r"berlin|london|paris|madrid|amsterdam|toronto|vancouver|montreal|bangalore|bengaluru|mumbai|delhi|tel aviv|"
    r"dublin|lisbon|warsaw|krakow|sydney|melbourne|dubai|cairo|lagos|nairobi|sao paulo|buenos aires|bogota|gurugram|"
    r"gurgaon|pune|hyderabad|chennai|kolkata|noida|manila|jakarta|kuala lumpur|bangkok|hanoi|ho chi minh|taipei|seoul|"
    r"tokyo|osaka|istanbul|athens|prague|budapest|vienna|zurich|geneva|stockholm|oslo|copenhagen|helsinki|brussels|"
    r"milan|rome|barcelona|munich|hamburg|frankfurt|cologne|edinburgh|manchester|belfast|cork|gdansk|wroclaw|bucharest|"
    r"sofia|belgrade|zagreb|vilnius|riga|tallinn|kyiv|kiev|moscow|riyadh|abu dhabi|doha|karachi|lahore|dhaka|colombo|"
    r"accra|johannesburg|cape town|casablanca|tunis|lima|santiago|montevideo|medellin|quito|costa rica|guadalajara|"
    r"monterrey|ottawa|calgary|edmonton|winnipeg|ontario|quebec|british columbia|alberta|saudi|uae|qatar|morocco|ghana|"
    r"uruguay|ecuador|lebanon|jordan|hong kong|malaysia|taiwan)\b",
    re.I,
)


def location_flags(location: str, title: str = "") -> dict:
    """Boolean signals found in a location string (and optionally the title)."""
    loc = f"{location or ''} | {title or ''}"
    return {
        "remote": bool(_REMOTE.search(loc)),
        "hybrid": bool(_HYBRID.search(loc)),
        "us": bool(_US_CS.search(loc) or _US_CI.search(loc)),
        "us_state": bool(_STATE.search(location or "")),
        "world": bool(_WORLD.search(loc)),
        "non_us": bool(_NON_US.search(loc)),
    }


# Description text that explicitly accepts applicants from anywhere / the US (overrides a named non-US office location).
_OPEN_ANYWHERE = re.compile(
    r"((open to|accept\w*|welcome|consider\w*)[^.\n]{0,40}(applicants?|candidates?|talent|applications)[^.\n]{0,40}"
    r"(anywhere|worldwide|globally|the (us|u\.s\.|united states)\b|\busa\b))|"
    r"(hir\w+|recruit\w*) (from|in) (anywhere|worldwide|the (us|u\.s\.|united states)\b)|"
    r"(applicants?|candidates?)[^.\n]{0,40}(from|in|located in|based in|residing in)[^.\n]{0,25}(anywhere|worldwide|the (us|u\.s\.|united states)\b)|"
    r"\b(work|remote)\b[^.\n]{0,20}\bfrom anywhere\b|\bremote[^.\n]{0,15}\b(anywhere|worldwide)\b|"
    r"(us|u\.s\.|united states)[- ]based (applicants?|candidates?)\b",
    re.I,
)
# Description text that places a (location-less) remote job in the US.
_US_REMOTE_TEXT = re.compile(
    r"(remote[^.\n]{0,60}\b(in|within|across|from)\s+the\s+(us|u\.s\.|united states|continental)|"
    r"open to (candidates|applicants)[^.\n]{0,30}(residing|located|based|living)\s+(in|within)\s+the\s+(us|u\.s\.|united states)|"
    r"(candidates|applicants)[^.\n]{0,20}(residing|located|based|living)\s+(in|within)\s+the\s+(us|u\.s\.|united states))",
    re.I,
)
_TITLE_WORLD = re.compile(r"\b(worldwide|world[- ]wide|anywhere|work from anywhere)\b", re.I)


def infer_scope(location: str, title: str = "", desc_head: str = "") -> str:
    """Remote scope: us | worldwide | other | unknown.

    The LOCATION decides first (a title like 'Global Services' must not make a Bulgaria-based role worldwide).
    A named non-US location with no US marker is `other` unless the description explicitly accepts US/anywhere applicants."""
    lf = location_flags(location, "")
    tf = location_flags("", title)
    full = desc_head or ""
    if lf["us"]:
        return "us"
    if lf["non_us"]:
        return "worldwide" if _OPEN_ANYWHERE.search(full[:3000]) else "other"
    if lf["world"]:
        return "worldwide"
    if lf["us_state"]:
        return "us"
    if tf["us"]:
        return "us"
    if tf["non_us"]:
        return "other"
    if _TITLE_WORLD.search(title or ""):
        return "worldwide"
    head = full[:600]
    if head and _REMOTE.search(head):
        h = location_flags(head)
        if h["us"]:
            return "us"
        if h["world"]:
            return "worldwide"
    if full and _US_REMOTE_TEXT.search(full):
        return "us"
    return "unknown"
