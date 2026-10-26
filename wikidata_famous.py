"""
Joy Plus · famous places from Wikidata

For each city, ask Wikidata (public domain, CC0) for landmarks within the city's
radius, ranked by how many Wikipedia language editions have an article about
them. That number is the fame score. Famous places are matched to the Overture
places already in the city file (same place nearby with a similar name), or
added when Overture doesn't have them. The top ones are marked "famous".

Any Wikidata error leaves the city file as it was: enrichment never fails a city.
"""

import json
import re
import time
import unicodedata
import urllib.parse
import urllib.request
from math import asin, cos, radians, sin, sqrt

ENDPOINT = "https://query.wikidata.org/sparql"
# Wikimedia asks automated clients to identify themselves with a contact.
USER_AGENT = "JoyPlusPlacesBot/1.0 (https://joyplusapp.com; benjamin@joyplusapp.com)"

FAMOUS_TOP = 25       # this many per city are marked famous
MIN_SITELINKS = 5     # ignore places with fewer Wikipedia articles than this
MAX_RESULTS = 200

# Wikidata classes worth visiting, with the category name used in the city file.
# Anything that is an instance of a subclass of these counts too.
ROOT_CLASSES = {
    "Q570116": "tourist_attraction",
    "Q33506": "museum",
    "Q4989906": "monument",
    "Q2319498": "landmark",
    "Q179700": "monument",              # statue
    "Q24398318": "historic_religious_site",
    "Q23413": "castle",
    "Q16560": "palace",
    "Q57821": "fort",
    "Q839954": "historic_site",         # archaeological site
    "Q15243209": "historic_district",
    "Q22698": "park",
    "Q1107656": "garden",
    "Q174782": "plaza",
    "Q12280": "bridge",
    "Q12518": "tower",
    "Q39715": "lighthouse",
    "Q6017969": "viewpoint",
    "Q40080": "beach",
    "Q8502": "mountain",
    "Q54050": "mountain",               # hill
    "Q34038": "waterfall",
    "Q24354": "theatre",
    "Q153562": "theatre",               # opera house
    "Q330284": "market",
    "Q43501": "zoo",
    "Q2281788": "aquarium",
    "Q194195": "amusement_park",
    "Q483110": "stadium",
}

# Local language per country, used to read the name people see on signs.
COUNTRY_LANGUAGE = {
    "PT": "pt", "BR": "pt", "AO": "pt", "MZ": "pt",
    "ES": "es", "MX": "es", "AR": "es", "CO": "es", "PE": "es", "CL": "es", "EC": "es",
    "VE": "es", "BO": "es", "UY": "es", "PY": "es", "CR": "es", "PA": "es", "GT": "es",
    "CU": "es", "DO": "es", "FR": "fr", "BE": "fr", "MC": "fr", "IT": "it", "DE": "de",
    "AT": "de", "CH": "de", "NL": "nl", "JP": "ja", "CN": "zh", "TW": "zh", "HK": "zh",
    "KR": "ko", "TH": "th", "VN": "vi", "ID": "id", "MY": "ms", "TR": "tr", "RU": "ru",
    "UA": "uk", "PL": "pl", "CZ": "cs", "HU": "hu", "RO": "ro", "GR": "el", "HR": "hr",
    "SE": "sv", "NO": "nb", "DK": "da", "FI": "fi", "EG": "ar", "MA": "ar", "AE": "ar",
    "SA": "ar", "JO": "ar", "IL": "he", "IR": "fa", "IN": "hi", "PK": "ur", "BD": "bn",
    "KH": "km", "LA": "lo", "MM": "my", "GE": "ka", "AM": "hy",
}

QUERY = """
SELECT ?item (SAMPLE(?coord) AS ?c) (SAMPLE(?en) AS ?enName) (SAMPLE(?loc) AS ?localName)
       (MAX(?links) AS ?sitelinks) (SAMPLE(?root) AS ?kind) WHERE {
  SERVICE wikibase:around {
    ?item wdt:P625 ?coord .
    bd:serviceParam wikibase:center "Point(%(lon)f %(lat)f)"^^geo:wktLiteral .
    bd:serviceParam wikibase:radius "%(radius)s" .
  }
  ?item wikibase:sitelinks ?links .
  FILTER(?links >= %(min_links)d)
  FILTER NOT EXISTS { ?item wdt:P576 ?demolished }
  ?item wdt:P31/wdt:P279* ?root .
  hint:Prior hint:gearing "forward" .
  VALUES ?root { %(roots)s }
  OPTIONAL { ?item rdfs:label ?en . FILTER(LANG(?en) = "en") }
  OPTIONAL { ?item rdfs:label ?loc . FILTER(LANG(?loc) = "%(lang)s") }
}
GROUP BY ?item
ORDER BY DESC(?sitelinks)
LIMIT %(limit)d
"""

STOP_WORDS = {
    "de", "da", "do", "das", "dos", "the", "of", "la", "le", "les", "el", "los", "las",
    "di", "del", "della", "der", "die", "das", "von", "and", "e", "y", "et", "a", "o",
}


# Scripts written without spaces between words: compare pairs of characters instead.
NO_SPACE_SCRIPTS = re.compile(r"[\u0e00-\u0e7f\u0e80-\u0eff\u1000-\u109f\u1780-\u17ff"
                              r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\u1100-\u11ff]")


def normalise(text):
    """Words of a name in any script, without accents, case or filler words."""
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    tokens = []
    for word in re.findall(r"\w+", text):
        if word in STOP_WORDS:
            continue
        if NO_SPACE_SCRIPTS.search(word) and len(word) > 1:
            tokens.extend(word[i:i + 2] for i in range(len(word) - 1))
        else:
            tokens.append(word)
    return tokens


def similarity(a, b):
    """Share of words two names have in common (0 to 1)."""
    wa, wb = set(normalise(a)), set(normalise(b))
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def contained(a, b):
    """True when every word of the shorter name appears in the longer one."""
    wa, wb = set(normalise(a)), set(normalise(b))
    return bool(wa and wb) and (wa <= wb or wb <= wa)


def distance_m(lat1, lon1, lat2, lon2):
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * 6_371_000 * asin(sqrt(h))


def fetch_famous(lat, lon, radius_km, country, attempts=3):
    """Landmarks around a point, most famous first, or [] if Wikidata can't answer.

    If a big city's query times out, each retry halves the radius (never below 2 km),
    because the most famous places are usually near the centre anyway.
    """
    for attempt in range(attempts):
        query = QUERY % {
            "lat": lat, "lon": lon, "radius": round(max(2.0, radius_km / (2 ** attempt)), 1),
            "min_links": MIN_SITELINKS, "roots": " ".join(f"wd:{q}" for q in ROOT_CLASSES),
            "lang": COUNTRY_LANGUAGE.get(country.upper(), "en"), "limit": MAX_RESULTS,
        }
        url = ENDPOINT + "?" + urllib.parse.urlencode({"query": query, "format": "json"})
        request = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json",
        })
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                rows = json.load(response)["results"]["bindings"]
            return [parse_row(r) for r in rows if parse_row(r)]
        except Exception as error:
            wait = 5 * (3 ** attempt)
            print(f"  wikidata retry {attempt + 1}: {error}; waiting {wait}s")
            time.sleep(wait)
    print("  wikidata unavailable for this city; keeping Overture places only")
    return []


def parse_row(row):
    match = re.match(r"Point\(([-\d.eE]+) ([-\d.eE]+)\)", row.get("c", {}).get("value", ""))
    name = row.get("enName", {}).get("value") or row.get("localName", {}).get("value")
    if not match or not name:
        return None
    return {
        "wd": row["item"]["value"].rsplit("/", 1)[-1],
        "name": name,
        "local": row.get("localName", {}).get("value"),
        "lon": float(match.group(1)),
        "lat": float(match.group(2)),
        "fame": int(row["sitelinks"]["value"]),
        "cat": ROOT_CLASSES.get(row.get("kind", {}).get("value", "").rsplit("/", 1)[-1], "landmark"),
    }


def enrich(places, famous):
    """Mark matching places with fame, add missing landmarks, flag the top ones as famous.

    Returns (places, matched_count, added_count). `places` uses the city-file format.
    """
    matched = added = 0
    used = set()
    for item in famous:
        best, best_score = None, 0.0
        for i, place in enumerate(places):
            if i in used:
                continue
            metres = distance_m(item["lat"], item["lon"], place["lat"], place["lon"])
            if metres > 200:
                continue
            names = [item["name"], item.get("local") or ""]
            score = max(similarity(n, place["name"]) for n in names)
            # "外滩" inside "上海外滩", "Gorky Park" vs "Park": same place if very close
            if score < 0.3 and metres <= 120 and any(contained(n, place["name"]) for n in names):
                score = 0.3
            if score > best_score:
                best, best_score = i, score
        if best is not None and best_score >= 0.3:
            used.add(best)
            places[best]["fame"] = item["fame"]
            places[best]["wd"] = item["wd"]
            matched += 1
        else:
            places.append({
                "id": f"wd:{item['wd']}", "name": item["name"], "cat": item["cat"],
                "lat": round(item["lat"], 7), "lon": round(item["lon"], 7),
                "addr": None, "web": None, "conf": 0.9, "src": "wikidata",
                "fame": item["fame"], "wd": item["wd"],
            })
            used.add(len(places) - 1)
            added += 1
    ranked = sorted((p for p in places if p.get("fame")), key=lambda p: p["fame"], reverse=True)
    for place in ranked[:FAMOUS_TOP]:
        place["famous"] = True
    return places, matched, added
