"""
Joy Plus · one places file per city, uploaded to Cloudflare R2

For every city in cities.csv: pull its places from the latest Overture release,
keep the best travel places, write city_files/{slug}.json and (with --upload)
put it on R2 at places/{slug}.json. Also writes places/index.json, the list of
cities the planner can look up.

Run locally:   python build_city_files.py --upload
Split in 10:   python build_city_files.py --shard 3 --shards 10 --upload
Index only:    python build_city_files.py --index-only --upload

R2 credentials come from environment variables:
R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

from overture_places_import import (
    FILTER_VERSION, best_rows, bbox_from_centre, city_records, keep, latest_release, to_row,
)
from overturemaps import core
from wikidata_famous import enrich, fetch_famous

ATTRIBUTION = "© Overture Maps Foundation and contributors"


def read_cities(path):
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lat, lon = float(row["lat"]), float(row["lon"])
            yield {
                "slug": row["slug"],
                "city": row["city"],
                "country": row["country"],
                "lat": lat,
                "lon": lon,
                "radius_km": float(row["radius_km"]),
                "bbox": bbox_from_centre(lat, lon, float(row["radius_km"])),
            }


def city_places(city, release, args):
    """The city's best travel places, retried if the network hiccups."""
    for attempt in range(3):
        try:
            rows = [
                to_row(record, point, city["city"], city["country"], release)
                for record, point in city_records(city["bbox"], release)
                if keep(record, args.min_confidence)
            ]
            return best_rows(rows, args.max_per_category, args.max_per_city)
        except Exception as error:  # network or read error: wait, then retry
            if attempt == 2:
                raise
            print(f"  retry {attempt + 1} for {city['slug']}: {error}")
            time.sleep(10 * (attempt + 1))


def city_document(city, places, release):
    """Compact JSON: short keys keep each file small and fast to load."""
    licenses = sorted({lic for p in places for lic in (p["licenses"] or "").split(";") if lic})
    return {
        "slug": city["slug"],
        "city": city["city"],
        "country": city["country"],
        "center": [city["lat"], city["lon"]],
        "radius_km": city["radius_km"],
        "release": release,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "filter_version": FILTER_VERSION,
        "attribution": ATTRIBUTION,
        "licenses": licenses,
        "count": len(places),
        "places": [
            {
                "id": p["overture_id"],
                "name": p["name"],
                "cat": p["basic_category"],
                "lat": p["lat"],
                "lon": p["lon"],
                "addr": p["address"],
                "web": p["website"],
                "conf": round(p["confidence"], 3) if p["confidence"] is not None else None,
                "src": p["sources"],
            }
            for p in places
        ],
    }


class R2:
    def __init__(self):
        import boto3  # only needed when uploading
        self.bucket = os.environ["R2_BUCKET"]
        self.client = boto3.client(
            "s3",
            endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
            aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
            region_name="auto",
        )

    def built_with(self, key):
        """(release, filter version) a file was built with, or (None, None)."""
        try:
            meta = self.client.head_object(Bucket=self.bucket, Key=key)["Metadata"]
            return meta.get("release"), meta.get("filter")
        except Exception:
            return None, None

    def put_json(self, key, document, release):
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
            CacheControl="public, max-age=86400",
            Metadata={"release": release, "filter": FILTER_VERSION},
        )


def write_local(path, document):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(document, f, ensure_ascii=False, separators=(",", ":"))


def index_document(cities, release):
    return {
        "release": release,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cities": [
            {k: c[k] for k in ("slug", "city", "country", "lat", "lon", "radius_km")}
            for c in cities
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Build one Overture places file per city.")
    parser.add_argument("--cities", default="cities.csv")
    parser.add_argument("--out-dir", default="city_files")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--max-per-category", type=int, default=100)
    parser.add_argument("--max-per-city", type=int, default=1000)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    parser.add_argument("--release", default=None, help="defaults to the latest release")
    parser.add_argument("--upload", action="store_true", help="upload to Cloudflare R2")
    parser.add_argument("--index-only", action="store_true", help="only write places/index.json")
    parser.add_argument("--force", action="store_true", help="rebuild cities already on this release")
    parser.add_argument("--no-wikidata", action="store_true", help="skip the famous-places step")
    args = parser.parse_args()

    release = args.release or latest_release()
    cities = list(read_cities(args.cities))
    r2 = R2() if args.upload else None
    print(f"Overture release {release} · {len(cities):,} cities · shard {args.shard + 1}/{args.shards}")

    if args.index_only:
        document = index_document(cities, release)
        write_local(os.path.join(args.out_dir, "index.json"), document)
        if r2:
            r2.put_json("places/index.json", document, release)
        print("Index written.")
        return

    done = skipped = 0
    failed = []
    for i, city in enumerate(cities):
        if i % args.shards != args.shard:
            continue
        key = f"places/{city['slug']}.json"
        if r2 and not args.force and r2.built_with(key) == (release, FILTER_VERSION):
            skipped += 1
            continue
        try:
            places = city_places(city, release, args)
        except Exception as error:
            failed.append(city["slug"])
            print(f"{city['slug']}: FAILED ({error})")
            continue
        document = city_document(city, places, release)
        fame_note = ""
        if not args.no_wikidata:
            famous = fetch_famous(city["lat"], city["lon"], city["radius_km"], city["country"])
            document["places"], matched, added = enrich(document["places"], famous)
            document["count"] = len(document["places"])
            if added:
                document["licenses"] = sorted(set(document["licenses"]) | {"CC0-1.0"})
            document["famous_source"] = "Wikidata"
            fame_note = f" · {len(famous)} famous ({matched} matched, {added} added)"
            time.sleep(1)  # be gentle with Wikidata's free service
        write_local(os.path.join(args.out_dir, f"{city['slug']}.json"), document)
        if r2:
            r2.put_json(key, document, release)
        done += 1
        print(f"{city['slug']}: {len(places):,} places{fame_note}")

    print(f"Done: {done} built, {skipped} already up to date, {len(failed)} failed")
    if failed:
        print("Failed cities: " + ", ".join(failed))
        sys.exit(1)


if __name__ == "__main__":
    main()
