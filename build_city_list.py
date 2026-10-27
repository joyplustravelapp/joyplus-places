"""
Joy Plus · build the city list automatically

Downloads GeoNames' free list of cities with 15,000+ people and writes
cities.csv with the largest N cities, each with a centre and a radius.

Run:  python build_city_list.py --top 1000
      python build_city_list.py --top 300 --countries PT,ES,FR,TH,VN,ID,JP,BR
      python build_city_list.py --top 1000 --keep cities_extra.csv   (always include your own rows)
      python build_city_list.py --top 10000 --keep cities.csv --keep-url https://.../guide-cities.csv

Priority when the same city appears twice: your own --keep file, then --keep-url lists
(your destination guides), then GeoNames.

GeoNames data is CC BY 4.0: credit "GeoNames" on the attribution page.
"""

import argparse
import csv
import io
import re
import unicodedata
import urllib.request
import zipfile

GEONAMES_URL = "https://download.geonames.org/export/dump/cities15000.zip"

# GeoNames columns (tab-separated): 1 name, 2 asciiname, 4 latitude, 5 longitude,
# 7 feature code, 8 country code, 14 population
NAME, ASCII, LAT, LON, FCODE, COUNTRY, POP = 1, 2, 4, 5, 7, 8, 14


def radius_km(population):
    """Bigger cities need a bigger area to cover their sights."""
    if population >= 5_000_000:
        return 15
    if population >= 1_000_000:
        return 10
    if population >= 300_000:
        return 8
    return 5


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def load_geonames(url=GEONAMES_URL):
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = zipfile.ZipFile(io.BytesIO(response.read()))
    with archive.open("cities15000.txt") as f:
        for line in io.TextIOWrapper(f, encoding="utf-8"):
            yield line.rstrip("\n").split("\t")


def build(rows, top, countries=None):
    """Largest cities first; skip city districts so a city isn't listed twice."""
    cities = []
    for r in rows:
        if r[FCODE] == "PPLX":
            continue
        if countries and r[COUNTRY] not in countries:
            continue
        cities.append(r)
    cities.sort(key=lambda r: int(r[POP] or 0), reverse=True)
    out, slugs = [], set()
    for r in cities:
        slug = f"{slugify(r[ASCII] or r[NAME])}-{r[COUNTRY].lower()}"
        if slug in slugs:
            continue
        slugs.add(slug)
        out.append({
            "slug": slug,
            "city": r[NAME],
            "country": r[COUNTRY],
            "lat": float(r[LAT]),
            "lon": float(r[LON]),
            "radius_km": radius_km(int(r[POP] or 0)),
            "population": int(r[POP] or 0),
        })
        if len(out) >= top:
            break
    return out


FIELDS = ["slug", "city", "country", "lat", "lon", "radius_km", "population"]


def valid_rows(rows, source):
    """Rows with a slug, a name and usable coordinates; anything else is skipped and reported."""
    good = []
    for row in rows:
        try:
            lat, lon, radius = float(row["lat"]), float(row["lon"]), float(row["radius_km"])
            if row["slug"].strip() and row["city"].strip() and -90 <= lat <= 90 and -180 <= lon <= 180 and radius > 0:
                good.append({**row, "slug": row["slug"].strip()})
                continue
        except (KeyError, ValueError, AttributeError):
            pass
        print(f"  skipped a row from {source}: {row}")
    return good


def rows_from_url(url):
    """A city list published online (e.g. your guides). Never fails the run if it's unreachable."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "JoyPlusPlacesBot/1.0 (https://joyplusapp.com)"})
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode("utf-8-sig")
        rows = valid_rows(csv.DictReader(io.StringIO(text)), url)
        print(f"{len(rows):,} cities from {url}")
        return rows
    except Exception as error:
        print(f"Could not read {url} ({error}); continuing without it")
        return []


def main():
    parser = argparse.ArgumentParser(description="Build cities.csv from GeoNames.")
    parser.add_argument("--top", type=int, default=1000)
    parser.add_argument("--countries", help="comma-separated ISO codes, e.g. PT,ES,TH")
    parser.add_argument("--keep", help="CSV of your own cities, always included and never overridden")
    parser.add_argument("--keep-url", action="append", default=[], help="CSV published online, e.g. your guides")
    parser.add_argument("--out", default="cities.csv")
    args = parser.parse_args()

    countries = set(args.countries.upper().split(",")) if args.countries else None
    merged = {}

    def add(rows, override, keep_larger_radius=False):
        for row in rows:
            current = merged.get(row["slug"])
            if current is None or override:
                if current is not None and keep_larger_radius:
                    # Guide centre wins, but a big city keeps its full area (Tokyo 15 km, not 8).
                    row = {**row, "radius_km": max(float(row["radius_km"]), float(current["radius_km"]))}
                merged[row["slug"]] = row

    add(build(load_geonames(), args.top, countries), override=False)
    print(f"{len(merged):,} cities from GeoNames")
    for url in args.keep_url:
        add(rows_from_url(url), override=True, keep_larger_radius=True)
    if args.keep:
        with open(args.keep, newline="", encoding="utf-8") as f:
            add(valid_rows(csv.DictReader(f), args.keep), override=True)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(merged.values())
    print(f"Wrote {len(merged):,} cities to {args.out}")


if __name__ == "__main__":
    main()
