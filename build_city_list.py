"""
Joy Plus · build the city list automatically

Downloads GeoNames' free list of cities with 15,000+ people and writes
cities.csv with the largest N cities, each with a centre and a radius.

Run:  python build_city_list.py --top 1000
      python build_city_list.py --top 300 --countries PT,ES,FR,TH,VN,ID,JP,BR
      python build_city_list.py --top 1000 --keep cities_extra.csv   (always include your own rows)

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


def main():
    parser = argparse.ArgumentParser(description="Build cities.csv from GeoNames.")
    parser.add_argument("--top", type=int, default=1000)
    parser.add_argument("--countries", help="comma-separated ISO codes, e.g. PT,ES,TH")
    parser.add_argument("--keep", help="CSV of extra cities to always include (same columns)")
    parser.add_argument("--out", default="cities.csv")
    args = parser.parse_args()

    countries = set(args.countries.upper().split(",")) if args.countries else None
    cities = build(load_geonames(), args.top, countries)

    if args.keep:
        known = {c["slug"] for c in cities}
        with open(args.keep, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row["slug"] not in known:
                    cities.append(row)

    fields = ["slug", "city", "country", "lat", "lon", "radius_km", "population"]
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(cities)
    print(f"Wrote {len(cities):,} cities to {args.out}")


if __name__ == "__main__":
    main()
