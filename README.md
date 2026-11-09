# flight-price-tracker

Collects flight prices for fixed future trips every day, from several sources. Over time the data
shows how a fare changes as departure gets closer, and whether the weekday of the search matters.

The initial searches cover Berlin to Rio (GIG) and Berlin to Belo Horizonte (CNF) around Carnival:

| trip id | outbound | return | 2027 dates |
|---|---|---|---|
| `wed_to_thu_after_carnival` | Wednesday about a month before | Thursday after Ash Wednesday | Wed 13 Jan → Thu 11 Feb |
| `wed_before_carnival_plus_1m` | Wednesday before Carnival | Wednesday a month later | Wed 3 Feb → Wed 3 Mar |

Each trip is searched three ways:
- `round_trip`: one ticket covering both legs
- `one_way_outbound`: a separate ticket for the outbound leg
- `one_way_return`: a separate ticket for the return leg

Dates are computed from Easter. Once a trip departs, it rolls over to the next Carnival, so collection continues on its own.

## Sources

| provider | what it is | key | cadence / budget (default) |
|---|---|---|---|
| `google_direct` | Google Flights, scraped with [fast-flights](https://github.com/AWeirdDev/flights). Unofficial; may break or be rate-limited | none | daily, unlimited |
| `serpapi` | Google Flights via [SerpApi](https://serpapi.com). Richest data: layovers, legroom, CO2, price insights, Google's own price history | `SERPAPI_API_KEY` | every 2 days, ≤235 calls/month (free plan: 250) |
| `ignav` | [Ignav](https://ignav.com) live fares API. Round trips include both legs, plus baggage info | `IGNAV_API_KEY` | daily, ≤450/month, ≤950 lifetime (1,000 free requests) |
| `travelpayouts` | [Aviasales Data API](https://support.travelpayouts.com/hc/en-us/articles/203956163). Cached prices from other users' searches; patchy but free | `TRAVELPAYOUTS_TOKEN` | daily, unlimited |

A provider without its secret is skipped. The others still run.

**Budgets.** Each provider spreads its monthly budget evenly over the run days left in the month. If a run's share is smaller than the number of searches, the searches with the highest priority number are skipped first. Priority comes from `mode_priority` + route + trip priorities in the config.

**Cadence.** `every_n_days` uses a value with no common factor with 7, so the runs cycle through all weekdays.

## Setup

1. Create the keys you want:
   - SerpApi: <https://serpapi.com/users/sign_up>
   - Ignav: <https://ignav.com/signup>
   - Travelpayouts: sign up, join the Aviasales program, then get the token under Profile → API token
2. Add each key under **Settings → Secrets and variables → Actions → New repository secret**:
   - `SERPAPI_API_KEY`
   - `IGNAV_API_KEY`
   - `TRAVELPAYOUTS_TOKEN`
3. To start, run **Actions → collect → Run workflow**, optionally with `force` ticked. After that it runs daily at 05:17 UTC.

## Adding searches

Edit `config/searches.yaml`. Add routes or trips to the existing group, or add a new group. A trip date can be:
- a fixed date: `{date: 2027-07-01}`
- relative to an anchor (`easter`, `carnival_tuesday`, `ash_wednesday`, …), with `offset_days/weeks/months`
- relative to the other date of the same trip: `{from: outbound, offset_months: 1}`

Any of these can then be moved to a given weekday with `weekday` and `snap` (`nearest|next|previous`).

Before committing, check what the change will do:

```bash
pip install -r requirements.txt
python -m fptracker plan                    # today's searches and the API calls per provider
python -m fptracker plan --date 2027-01-20  # plan for another day
python -m fptracker run --providers google_direct --only BER-GIG   # a real run, writes to data/
python -m pytest -q
```

## Data layout

Files are append-only. Each run writes new gzipped CSVs and never edits old ones.

```
data/raw/<provider>/YYYY/MM/DD/<run_id>__<search_id>.json.gz   full response for each search
data/tables/searches/YYYY/MM/<run_id>__<provider>.csv.gz       one row per search (status, min price, Google price level)
data/tables/itineraries/...                                     one row per offered itinerary
data/tables/segments/...                                        one row per flight segment
data/tables/price_history/...                                   Google's price history (SerpApi only)
```

Every table starts with the same key columns, so the tables join on `(run_id, provider, search_id)`. Segments join to itineraries on `itinerary_uid`. The key columns are:
- `query_ts_utc`, `query_ts_local`, `query_weekday`, `query_hour_local`, `days_to_departure`
- `route_id`, `trip_id`, `trip_year`, `mode`
- `outbound_date`, `return_date`, `flight_date`

`price_scope` tells you what a price covers: `round_trip_total` or `one_way`.

```python
import duckdb
duckdb.sql("""
  SELECT provider, route_id, trip_id, mode, query_date_local, days_to_departure, min(price) AS min_price
  FROM read_csv_auto('data/tables/itineraries/**/*.csv.gz', union_by_name=true)
  GROUP BY ALL ORDER BY ALL
""").df()
```

```python
import glob, pandas as pd
itins = pd.concat(pd.read_csv(f) for f in glob.glob("data/tables/itineraries/**/*.csv.gz", recursive=True))
```
