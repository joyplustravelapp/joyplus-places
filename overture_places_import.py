"""
Joy Plus · Overture places import

Pulls travel-relevant places for every city in cities.csv from the latest
Overture release and writes places.csv, ready to import into the Supabase
`places` table (see places_setup.sql).

Only the area of each city is downloaded, never the whole world.

Setup (once):   pip install overturemaps
Run:            python overture_places_import.py
Options:        --cities cities.csv --out places.csv --min-confidence 0.5
                --max-per-category 100 --max-per-city 1000 --resume
See a city's categories before importing:
                python overture_places_import.py --list-categories Lisbon

cities.csv rows can give either a box (min_lon,min_lat,max_lon,max_lat)
or a centre and radius (lat,lon,radius_km), which is easier for 1,000 cities.
"""

import argparse
import csv
import math
import os
import sys
from collections import Counter, defaultdict

import urllib.request

import pyarrow.fs as pafs
import shapely
from overturemaps import core

# Overture's catalog server rejects Python's default identity ("403 Forbidden"),
# so every request introduces itself like a normal client.
_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", "Mozilla/5.0 (compatible; joyplus-places/1.0; +https://joyplusapp.com)")]
urllib.request.install_opener(_opener)

# Set to False if the catalog is unreachable; reads then go straight to storage.
USE_STAC = True

# A place is kept when one word of its basic_category matches one of these.
# Matching whole words (split on "_") avoids false hits like "art" in "apartment".
TRAVEL_WORDS = {
    "museum", "gallery", "art", "arts", "landmark", "monument", "memorial", "historic",
    "historical", "heritage", "castle", "palace", "fort", "ruins", "church", "cathedral",
    "basilica", "temple", "mosque", "synagogue", "shrine", "monastery", "viewpoint",
    "lookout", "scenic", "park", "garden", "botanical", "beach", "nature", "trail",
    "hiking", "waterfall", "lake", "island", "zoo", "aquarium", "attraction", "tourist",
    "tour", "tours", "market", "cafe", "coffee", "bakery", "eatery", "restaurant", "food",
    "bistro", "bar", "pub", "brewery", "winery", "wine", "cocktail", "nightclub", "club",
    "music", "theater", "theatre", "cinema", "concert", "festival", "accommodation",
    "hostel", "hotel", "guesthouse", "lodging", "campground", "camping", "spa", "surf",
    "diving", "plaza", "square", "bridge", "pier", "harbor", "harbour", "stadium",
}

CSV_COLUMNS = [
    "overture_id", "name", "basic_category", "city", "country", "address", "website",
    "phone", "lat", "lon", "confidence", "operating_status", "sources", "licenses", "release",
]


def is_travel_place(basic_category):
    if not basic_category:
        return False
    return any(word in TRAVEL_WORDS for word in basic_category.lower().split("_"))


def first(items):
    return items[0] if items else None


def to_row(record, point, city, country, release):
    """Turn one Overture record into one CSV row."""
    names = record.get("names") or {}
    address = first(record.get("addresses")) or {}
    sources = record.get("sources") or []
    return {
        "overture_id": record.get("id"),
        "name": names.get("primary"),
        "basic_category": record.get("basic_category"),
        "city": city,
        "country": address.get("country") or country,
        "address": address.get("freeform"),
        "website": first(record.get("websites")),
        "phone": first(record.get("phones")),
        "lat": round(point.y, 7),
        "lon": round(point.x, 7),
        "confidence": record.get("confidence"),
        "operating_status": record.get("operating_status"),
        "sources": ";".join(sorted({s.get("dataset") or "" for s in sources} - {""})),
        "licenses": ";".join(sorted({s.get("license") or "" for s in sources} - {""})),
        "release": release,
    }


def keep(record, min_confidence):
    """Travel category, named, confident enough, and not marked closed."""
    if not (record.get("names") or {}).get("primary"):
        return False
    if not is_travel_place(record.get("basic_category")):
        return False
    confidence = record.get("confidence")
    if confidence is None or confidence < min_confidence:
        return False
    return record.get("operating_status") in (None, "open")


def bbox_from_centre(lat, lon, radius_km):
    """Box around a city centre: radius_km in every direction."""
    dlat = radius_km / 111.32
    dlon = radius_km / (111.32 * max(math.cos(math.radians(lat)), 0.01))
    return (round(lon - dlon, 5), round(lat - dlat, 5), round(lon + dlon, 5), round(lat + dlat, 5))


def read_cities(path):
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("min_lon"):
                bbox = tuple(float(row[k]) for k in ("min_lon", "min_lat", "max_lon", "max_lat"))
            else:
                bbox = bbox_from_centre(float(row["lat"]), float(row["lon"]), float(row["radius_km"]))
            yield row["city"].strip(), row["country"].strip(), bbox


def best_rows(rows, max_per_category, max_per_city):
    """Keep the most confident places, balanced so no category crowds out the rest."""
    by_category = defaultdict(list)
    for row in rows:
        by_category[row["basic_category"]].append(row)
    capped = []
    for group in by_category.values():
        group.sort(key=lambda r: r["confidence"] or 0, reverse=True)
        capped.extend(group[:max_per_category])
    capped.sort(key=lambda r: r["confidence"] or 0, reverse=True)
    return capped[:max_per_city]


def done_cities(path):
    """Cities already written to the output file, so a stopped run can resume."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as f:
        return {row["city"] for row in csv.DictReader(f)}


def latest_release():
    """Latest Overture release, from the catalog or, if it refuses, from public storage."""
    global USE_STAC
    try:
        return core.get_latest_release()
    except Exception as error:
        print(f"Catalog unavailable ({error}). Reading the release list from storage instead.")
        USE_STAC = False
        s3 = pafs.S3FileSystem(anonymous=True, region="us-west-2")
        entries = s3.get_file_info(pafs.FileSelector("overturemaps-us-west-2/release/"))
        releases = sorted(e.base_name for e in entries if e.type == pafs.FileType.Directory)
        if not releases:
            raise RuntimeError("No Overture releases found in storage") from error
        return releases[-1]


def open_reader(bbox, release):
    """Use the catalog to read only the files that cover the bbox; fall back to scanning storage."""
    global USE_STAC
    if USE_STAC:
        try:
            return core.record_batch_reader("place", bbox=bbox, release=release, stac=True)
        except Exception as error:
            print(f"Catalog lookup failed ({error}). Scanning storage directly from now on.")
            USE_STAC = False
    return core.record_batch_reader("place", bbox=bbox, release=release, stac=False)


def city_records(bbox, release):
    """Stream every Overture place inside the bbox, with its point geometry."""
    reader = open_reader(bbox, release)
    if reader is None:
        return
    for batch in reader:
        points = shapely.from_wkb(batch.column("geometry").to_pylist())
        for record, point in zip(batch.to_pylist(), points):
            yield record, point


def list_categories(cities_path, city_name, release):
    for city, _country, bbox in read_cities(cities_path):
        if city.lower() != city_name.lower():
            continue
        counts = Counter(r.get("basic_category") for r, _ in city_records(bbox, release))
        for category, n in counts.most_common():
            mark = "keep" if is_travel_place(category) else "    "
            print(f"{mark}  {n:>6}  {category}")
        return
    sys.exit(f"{city_name} is not in {cities_path}")


def main():
    parser = argparse.ArgumentParser(description="Import Overture travel places per city.")
    parser.add_argument("--cities", default="cities.csv")
    parser.add_argument("--out", default="places.csv")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--max-per-category", type=int, default=100)
    parser.add_argument("--max-per-city", type=int, default=1000)
    parser.add_argument("--resume", action="store_true", help="skip cities already in --out")
    parser.add_argument("--release", default=None, help="defaults to the latest release")
    parser.add_argument("--list-categories", metavar="CITY")
    args = parser.parse_args()

    release = args.release or latest_release()
    print(f"Overture release {release}")

    if args.list_categories:
        list_categories(args.cities, args.list_categories, release)
        return

    skip = done_cities(args.out) if args.resume else set()
    append = args.resume and os.path.exists(args.out)
    seen = set()
    written = 0
    with open(args.out, "a" if append else "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if not append:
            writer.writeheader()
        for city, country, bbox in read_cities(args.cities):
            if city in skip:
                continue
            total = 0
            rows = []
            for record, point in city_records(bbox, release):
                total += 1
                if record.get("id") in seen or not keep(record, args.min_confidence):
                    continue
                seen.add(record.get("id"))
                rows.append(to_row(record, point, city, country, release))
            best = best_rows(rows, args.max_per_category, args.max_per_city)
            writer.writerows(best)
            f.flush()
            written += len(best)
            print(f"{city}: kept {len(best):,} of {total:,} places")
    print(f"Done. {written:,} places written to {args.out}")


if __name__ == "__main__":
    main()
