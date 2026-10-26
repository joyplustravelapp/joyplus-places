-- Joy Plus · places database (Supabase / Lovable Cloud)
-- Run once. Safe to run again: everything uses "if not exists" or "or replace".

-- 1. Map support: lets the database answer "places near this point"
create extension if not exists postgis with schema extensions;

-- 2. The places table, one row per Overture place
create table if not exists public.places (
  overture_id      text primary key,          -- Overture's stable ID, used for monthly refreshes
  name             text not null,
  basic_category   text,
  city             text not null,
  country          text,
  address          text,
  website          text,
  phone            text,
  lat              double precision not null,
  lon              double precision not null,
  location         extensions.geography(Point, 4326),
  confidence       double precision,
  operating_status text,
  sources          text,                      -- keep for attribution
  licenses         text,                      -- keep for attribution
  release          text,                      -- Overture release the row came from
  updated_at       timestamptz not null default now()
);

-- 3. Fill the map point automatically from lat and lon on every insert or update
create or replace function public.places_set_location()
returns trigger
language plpgsql
set search_path = public, extensions
as $$
begin
  new.location := extensions.st_setsrid(extensions.st_makepoint(new.lon, new.lat), 4326)::extensions.geography;
  new.updated_at := now();
  return new;
end;
$$;

drop trigger if exists places_set_location on public.places;
create trigger places_set_location
  before insert or update of lat, lon on public.places
  for each row execute function public.places_set_location();

-- 4. Indexes so planner queries stay fast as the table grows
create index if not exists places_location_idx on public.places using gist (location);
create index if not exists places_city_category_idx on public.places (city, basic_category);

-- 5. Anyone can read places; only the backend (service role) can write
alter table public.places enable row level security;
drop policy if exists "Places are public to read" on public.places;
create policy "Places are public to read" on public.places for select using (true);

-- 6. Planner query: the best open places in a city, optionally for some categories
create or replace function public.candidate_places(
  p_city text,
  p_categories text[] default null,
  p_limit int default 60
)
returns setof public.places
language sql
stable
set search_path = public, extensions
as $$
  select *
  from public.places
  where city = p_city
    and (p_categories is null or basic_category = any (p_categories))
    and coalesce(operating_status, 'open') = 'open'
  order by confidence desc nulls last
  limit p_limit;
$$;

-- 7. Planner query: open places within a walking radius of a point (in metres)
create or replace function public.places_near(
  p_lat double precision,
  p_lon double precision,
  p_radius_m int default 1500,
  p_limit int default 30
)
returns setof public.places
language sql
stable
set search_path = public, extensions
as $$
  select *
  from public.places
  where extensions.st_dwithin(
          location,
          extensions.st_setsrid(extensions.st_makepoint(p_lon, p_lat), 4326)::extensions.geography,
          p_radius_m
        )
    and coalesce(operating_status, 'open') = 'open'
  order by confidence desc nulls last
  limit p_limit;
$$;
