# Joy Plus · real places for the AI trip planner

Every month, automatically: build the list of cities, pull each city's best real
places from Overture Maps, write one small JSON file per city, and upload it to
Cloudflare R2. The planner then reads `places/{city}.json` instead of asking the
AI to invent places and coordinates.

## Files

| File | What it does |
|---|---|
| `build_city_list.py` | Builds `cities.csv` from GeoNames: the largest N cities, each with a centre and radius |
| `cities.csv` | The cities to process. Starts with 9; regenerate or edit by hand |
| `build_city_files.py` | For each city: Overture → best travel places → `places/{slug}.json` on R2, plus `places/index.json` |
| `overture_places_import.py` | Shared filtering logic; also exports one CSV if you prefer a database |
| `.github/workflows/refresh-places.yml` | Runs everything on the 28th of each month, in 10 parallel jobs |
| `places_setup.sql` | Optional: the Supabase table, if you later want places in the database too |

## One-time setup (about 30 minutes)

1. **Cloudflare R2**: create a bucket (for example `joyplus-places`), then an R2 API
   token with read and write access to that bucket. Note the account ID, access key ID
   and secret.
2. **GitHub**: create a new repository (separate from the Lovable app) and add every
   file in this folder, keeping the `.github/workflows` path.
3. **Secrets**: in the repository, Settings → Secrets and variables → Actions, add
   `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET`.
4. **City list**: run `python build_city_list.py --top 1000` once on your computer and
   commit the new `cities.csv`. Add or remove rows by hand any time.
5. **First run**: Actions tab → "Refresh city places" → Run workflow. Check the R2
   bucket for `places/index.json` and one file per city.

After that it runs by itself every month. Cities already built from the current
Overture release are skipped, so re-running after a failure only redoes what's missing.

## Test locally first

```bash
pip install overturemaps boto3
python build_city_files.py                 # writes city_files/*.json, no upload
python build_city_files.py --upload        # same, then uploads (needs the 4 R2 variables)
```

Each file holds up to 1,000 places (100 per category), about 0.25 MB. 1,000 cities is
roughly 250 MB, well inside R2's free 10 GB.

## How the planner reads it

- `places/index.json` lists every city with its centre and radius: match the traveller's
  destination to the nearest city.
- `places/{slug}.json` holds that city's places: `id, name, cat, lat, lon, addr, web, conf, src`.
- Reading options: connect a custom domain to the bucket (if joyplusapp.com's DNS is on
  Cloudflare), or have the planner's backend read files with the R2 keys stored as secrets.

## Attribution

Show on the site's attribution page: "Places data © Overture Maps Foundation and
contributors (CDLA Permissive 2.0, Apache 2.0)" and "City list: GeoNames (CC BY 4.0)".
Each city file lists the exact licences of its places.
