# Data Dictionary

Processed layer produced by `transform.py` and saved by `save_processed` to
`data/processed/cad.parquet` and `data/processed/fireball.parquet`.

> `transform_layout.ipynb` shows the transformation done by hand, cell by cell, as it was
> first explored. `transform.py` is the version used by the pipeline: the same logic as
> reusable, tested functions, with extra validations and a few additional columns
> (`diameter_est_m`, `diameter_best_m`, `number`, `name`, `provisional_des`). Some names
> differ, for example `gravitationally_bound` in the notebook is `is_gravitationally_bound`
> here. This dictionary describes the output of `transform.py`.

**Origin** column:
- **API**: field returned by the NASA/JPL API (only cleaned and type-converted).
- **Calculated**: derived in `transform.py`.
- **Pipeline**: lineage metadata added by `extract.py`.

Constants used in the calculations (defined at the top of `transform.py`):

| Constant | Value | Meaning |
|---|---|---|
| `AU_KM` | 149 597 870.7 km | 1 astronomical unit |
| `LD_KM` | 384 400 km | Mean Earth–Moon distance (lunar distance) |
| `GM_EARTH` | 398 600.4418 km³/s² | Earth's standard gravitational parameter |
| `KT_JOULES` | 4.185 × 10¹² J | 1 kiloton of TNT |

---

## CAD: close approaches (`cad.parquet`)

Source: [SBDB Close-Approach Data API](https://ssd-api.jpl.nasa.gov/doc/cad.html). One row per close approach of an object to Earth.

### Fields from the API

| Column | Type | Unit | Origin | Description |
|---|---|---|---|---|
| `des` | string | — | API | Primary designation of the object (leading/trailing spaces removed). |
| `orbit_id` | string | — | API | ID of the orbit solution used for the computation. |
| `jd` | float64 | days (Julian Date, TDB) | API | Time of the close approach. |
| `cd` | datetime64 | TDB (no timezone) | API | Time of the close approach, parsed from `YYYY-Mon-DD hh:mm`. TDB differs from UTC by about a minute, so the column is intentionally not timezone-aware. |
| `dist` | float64 | au | API | Nominal approach distance. |
| `dist_min` | float64 | au | API | Minimum (3-sigma) approach distance. |
| `dist_max` | float64 | au | API | Maximum (3-sigma) approach distance. |
| `v_rel` | float64 | km/s | API | Velocity relative to Earth at close approach, including Earth's gravity. |
| `v_inf` | float64 | km/s | API | Velocity relative to a massless Earth (hyperbolic excess velocity). Null when the object is gravitationally bound to Earth. |
| `t_sigma_f` | string | `d_hh:mm` | API | 3-sigma uncertainty in the approach time, as text (e.g. `< 00:01`, `2_09:42`). |
| `h` | float64 | mag | API | Absolute magnitude H. |
| `diameter` | float64 | km | API | Measured diameter. Null when unknown (most objects). |
| `diameter_sigma` | float64 | km | API | 1-sigma uncertainty of `diameter`. |
| `fullname` | string | — | API | Full name/designation, e.g. `99942 Apophis (2004 MN4)` (edges stripped, inner spaces kept). |

### Calculated fields

| Column | Type | Unit | Origin | Formula / description |
|---|---|---|---|---|
| `dist_km` | float64 | km | Calculated | `dist × AU_KM` |
| `dist_ld` | float64 | LD | Calculated | `dist_km / LD_KM` (1 LD = Earth–Moon distance) |
| `dist_uncertainty_km` | float64 | km | Calculated | `(dist_max − dist_min) × AU_KM` |
| `dist_uncertainty_pct` | float64 | % | Calculated | `(dist_max − dist_min) / dist × 100` |
| `v_esc` | float64 | km/s | Calculated | Earth escape velocity at the approach distance: `sqrt(2 × GM_EARTH / dist_km)` |
| `gravity_boost_kms` | float64 | km/s | Calculated | Speed gained from Earth's gravity: `v_rel − v_inf`. Null when `v_inf` is null. |
| `is_gravitationally_bound` | bool | — | Calculated | `v_rel < v_esc`: the object was too slow to escape Earth's gravity. |
| `t_sigma_min` | int64 | minutes | Calculated | `t_sigma_f` converted to minutes (`days × 1440 + hours × 60 + minutes`). `< 00:01` becomes 1. |
| `t_sigma_lt_1min` | bool | — | Calculated | True when `t_sigma_f` starts with `<` (uncertainty below 1 minute). |
| `diameter_est_min_m` | float64 | m | Calculated | Diameter estimated from H with albedo 0.25 (bright surface, smallest size): `1329 / sqrt(0.25) × 10^(−H/5) × 1000` |
| `diameter_est_m` | float64 | m | Calculated | Same formula with albedo 0.14 (typical value). |
| `diameter_est_max_m` | float64 | m | Calculated | Same formula with albedo 0.05 (dark surface, largest size). |
| `has_measured_diameter` | bool | — | Calculated | True when the API returned `diameter`. |
| `diameter_best_m` | float64 | m | Calculated | `diameter × 1000` when measured, otherwise `diameter_est_m`. |
| `number` | Int64 (nullable) | — | Calculated | Permanent catalogue number parsed from `fullname` (e.g. `99942`). Null when the object is not numbered. |
| `name` | string (nullable) | — | Calculated | Proper name parsed from `fullname` (e.g. `Apophis`). Null when the object has no name. |
| `provisional_des` | string (nullable) | — | Calculated | Provisional designation inside parentheses (e.g. `2004 MN4`). Null when absent. |

### Lineage fields

| Column | Type | Unit | Origin | Description |
|---|---|---|---|---|
| `_extracted_at` | datetime64 (UTC) | — | Pipeline | When the extraction that produced the row ran. |
| `_api_version` | string | — | Pipeline | API version reported in the response signature. |
| `_raw_file` | string | — | Pipeline | Name of the raw JSON file in `data/raw/cad/`. |

### Validation rules (`transform_cad` raises `ValueError` if broken)

- `dist`, `dist_min`, `dist_max`, `dist_km`, `v_rel` and `v_esc` are finite and positive; `v_inf` and `diameter` too, when present.
- `jd` and `h` are finite.
- `dist_min ≤ dist ≤ dist_max` in every row.
- Where `v_inf` is present, `v_rel² − v_inf² ≈ v_esc²` (relative tolerance 1%).
- `t_sigma_f` and `fullname` match the expected formats.
- No value that was present in the raw text may turn into NaN during conversion.

---

## Fireball: bolides (`fireball.parquet`)

Source: [Fireball Data API](https://ssd-api.jpl.nasa.gov/doc/fireball.html). One row per fireball event, deduplicated across incremental extractions.
Columns whose names contained `-` in the API are renamed with `_` (`impact-e` → `impact_e`, `lat-dir` → `lat_dir`, `lon-dir` → `lon_dir`).

### Fields from the API

| Column | Type | Unit | Origin | Description |
|---|---|---|---|---|
| `date` | datetime64 | UTC (no timezone) | API | Date and time of peak brightness. |
| `energy` | float64 | 10¹⁰ J | API | Approximate total optical radiated energy. |
| `impact_e` | float64 | kt (TNT) | API | Approximate total impact energy (API field `impact-e`). |
| `lat` | float64 | degrees | API (sign calculated) | Latitude. The API sends the absolute value; south is made negative. Null when the location is unknown. |
| `lat_dir` | string | — | API | Latitude hemisphere: `N` or `S`. |
| `lon` | float64 | degrees | API (sign calculated) | Longitude. The API sends the absolute value; west is made negative. Null when the location is unknown. |
| `lon_dir` | string | — | API | Longitude hemisphere: `E` or `W`. |
| `alt` | float64 | km | API | Altitude above the geoid at peak brightness. Often null. |
| `vel` | float64 | km/s | API | Velocity at peak brightness. Often null; the column only exists if the API returned it. |

### Calculated fields

| Column | Type | Unit | Origin | Formula / description |
|---|---|---|---|---|
| `energy_joules` | float64 | J | Calculated | `energy × 10¹⁰` |
| `impact_energy_joules` | float64 | J | Calculated | `impact_e × KT_JOULES` |

### Lineage fields

| Column | Type | Unit | Origin | Description |
|---|---|---|---|---|
| `_extracted_at` | datetime64 (UTC) | — | Pipeline | When the extraction that produced the row ran. |
| `_api_version` | string | — | Pipeline | API version reported in the response signature. |
| `_raw_file` | string | — | Pipeline | Name of the raw JSON file in `data/raw/fireball/`. |

### Deduplication

Incremental extractions use an inclusive `date-min`, so the last day returns again. Rows are sorted by `_extracted_at` and duplicates on (`date`, `lat`, `lon`) are dropped with `keep="last"`: the row kept always comes from the most recent extraction. Different events in the same second but at different locations are kept.

### Validation rules (`transform_fireball` raises `ValueError` if broken)

- `date` is not null.
- `lat` within [−90, 90] and `lon` within [−180, 180] when present, with a valid hemisphere (`N`/`S`, `E`/`W`).
- `energy`, `impact_e`, `energy_joules` and `impact_energy_joules` are finite and positive.
- `alt` within [0, 200] km when present; `vel` finite and positive when present.
- No value that was present in the raw text may turn into NaN during conversion.
