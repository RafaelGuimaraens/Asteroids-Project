"""Testes funcionais, de validação e de segurança da etapa de transformação.

Nenhum teste chama a internet: os DataFrames são montados no mesmo formato
que extract.load_raw devolve (tudo como texto, com os espaços à esquerda que
a API coloca em fullname e as colunas de linhagem _extracted_at,
_api_version e _raw_file). Os valores vêm de extrações reais do JPL.
"""

import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import transform  # noqa: E402
from transform import (  # noqa: E402
    AU_KM,
    GM_EARTH,
    KT_JOULES,
    LD_KM,
    save_processed,
    transform_cad,
    transform_fireball,
)

CAD_FIELDS = ["des", "orbit_id", "jd", "cd", "dist", "dist_min", "dist_max", "v_rel",
              "v_inf", "t_sigma_f", "h", "diameter", "diameter_sigma", "fullname"]
FIREBALL_FIELDS = ["date", "energy", "impact-e", "lat", "lat-dir", "lon", "lon-dir",
                   "alt", "vel"]

CAD_RAW_FILE = "20261101T025331640820Z.json"
CAD_EXTRACTED_AT = "2026-11-01T02:53:31+00:00"
FIREBALL_OLD_FILE = "20261101T024503797628Z.json"
FIREBALL_OLD_AT = "2026-11-01T02:45:03+00:00"
FIREBALL_NEW_FILE = "20261101T025331906817Z.json"
FIREBALL_NEW_AT = "2026-11-01T02:53:31+00:00"

# Aproximação real de 2025 VV2 (v_inf presente, coerente com a equação de energia)
VV2 = {
    "des": "2025 VV2", "orbit_id": "1", "jd": "2460980.853464158", "cd": "2025-Nov-01 08:29",
    "dist": "0.019376242985011", "dist_min": "0.01912786529817",
    "dist_max": "0.0196246009340663", "v_rel": "9.40266280754704",
    "v_inf": "9.38802654917281", "t_sigma_f": "< 00:01", "h": "25.43",
    "diameter": None, "diameter_sigma": None, "fullname": "       (2025 VV2)",
}
# 2010 FX9: único objeto da extração com diâmetro medido (0.030 km)
FX9 = {
    "des": "2010 FX9", "orbit_id": "29", "jd": "2461297.665446474", "cd": "2026-Sep-14 03:58",
    "dist": "0.0240127992318479", "dist_min": "0.0240125798676079",
    "dist_max": "0.0240130185967539", "v_rel": "11.0684108958418",
    "v_inf": "11.0583813537708", "t_sigma_f": "< 00:01", "h": "24.06",
    "diameter": "0.030", "diameter_sigma": "0.002", "fullname": "       (2010 FX9)",
}

# 2026 QQ8 em dezembro: dist derivada de v_rel e v_inf pela equação de energia
# v_esc² = v_rel² - v_inf²  e  v_esc² = 2·GM/d  =>  d = 2·GM / (v_rel² - v_inf²)
QQ8_DEC_V_REL = 0.796698
QQ8_DEC_V_INF = 0.212237
QQ8_DEC_DIST = 2 * GM_EARTH / (QQ8_DEC_V_REL**2 - QQ8_DEC_V_INF**2) / AU_KM
QQ8_DEC_HALF_WIDTH = 5.70984e-07  # meia largura real do intervalo [dist_min, dist_max]
QQ8_BASE = {"des": "2026 QQ8", "orbit_id": "1", "h": "31.367", "diameter": None,
            "diameter_sigma": None, "fullname": "       (2026 QQ8)"}
QQ8 = [
    {**QQ8_BASE, "jd": "2461162.408381293", "cd": "2026-May-01 21:48",
     "dist": "0.00772560278929482", "dist_min": "0.00772321808213006",
     "dist_max": "0.00772798762297442", "v_rel": "0.795217418167035", "v_inf": None,
     "t_sigma_f": "00:06"},
    {**QQ8_BASE, "jd": "2461271.489274951", "cd": "2026-Aug-18 23:45",
     "dist": "0.00358999471838524", "dist_min": "0.00358994476162161",
     "dist_max": "0.00359004467761537", "v_rel": "1.14605686661353", "v_inf": None,
     "t_sigma_f": "< 00:01"},
    {**QQ8_BASE, "jd": "2461388.069865612", "cd": "2026-Dec-13 13:41",
     "dist": repr(QQ8_DEC_DIST), "dist_min": repr(QQ8_DEC_DIST - QQ8_DEC_HALF_WIDTH),
     "dist_max": repr(QQ8_DEC_DIST + QQ8_DEC_HALF_WIDTH), "v_rel": repr(QQ8_DEC_V_REL),
     "v_inf": repr(QQ8_DEC_V_INF), "t_sigma_f": "00:02"},
]

# Bólidos reais
FB_SOUTH_WEST = {"date": "2026-10-04 03:14:45", "energy": "5.4", "impact-e": "0.18",
                 "lat": "41.8", "lat-dir": "S", "lon": "173.1", "lon-dir": "W",
                 "alt": "26.9", "vel": "13.8"}
FB_NORTH_WEST = {"date": "2026-09-11 01:12:11", "energy": "24.6", "impact-e": "0.67",
                 "lat": "54.4", "lat-dir": "N", "lon": "100.1", "lon-dir": "W",
                 "alt": "38.0", "vel": "20.7"}
FB_NORTH_EAST = {"date": "1991-10-04 09:22:47", "energy": "55.0", "impact-e": "1.4",
                 "lat": "78.7", "lat-dir": "N", "lon": "6.3", "lon-dir": "E",
                 "alt": None, "vel": None}
FB_NO_LOCATION = {"date": "2024-08-20 12:53:51", "energy": "6.0", "impact-e": "0.19",
                  "lat": None, "lat-dir": None, "lon": None, "lon-dir": None,
                  "alt": None, "vel": None}


def make_cad(rows, raw_file=CAD_RAW_FILE, extracted_at=CAD_EXTRACTED_AT):
    df = pd.DataFrame(rows, columns=CAD_FIELDS)
    df["_extracted_at"] = extracted_at
    df["_api_version"] = "1.5"
    df["_raw_file"] = raw_file
    return df


def make_fireball(rows, raw_file=FIREBALL_OLD_FILE, extracted_at=FIREBALL_OLD_AT):
    df = pd.DataFrame(rows, columns=FIREBALL_FIELDS)
    df["_extracted_at"] = extracted_at
    df["_api_version"] = "1.2"
    df["_raw_file"] = raw_file
    return df


def cad_with(**overrides):
    return make_cad([{**VV2, **overrides}])


def fireball_with(**overrides):
    return make_fireball([{**FB_SOUTH_WEST, **overrides}])


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Qualquer chamada HTTP que escape dos testes falha na hora."""
    def blocked(*args, **kwargs):
        raise RuntimeError("Network access is disabled during tests.")
    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture
def cad_raw():
    return make_cad([VV2, FX9, *QQ8])


@pytest.fixture
def qq8():
    return transform_cad(make_cad(QQ8))


@pytest.fixture
def fireball_raw():
    return make_fireball([FB_SOUTH_WEST, FB_NORTH_WEST, FB_NORTH_EAST, FB_NO_LOCATION])


def test_network_is_blocked():
    with pytest.raises(RuntimeError, match="Network access is disabled"):
        requests.Session().get("https://ssd-api.jpl.nasa.gov/cad.api")


# ==========================================================================
# CAD funcional
# ==========================================================================
def test_cad_output_dtypes(cad_raw):
    out = transform_cad(cad_raw)
    floats = ["jd", "dist", "dist_min", "dist_max", "v_rel", "v_inf", "h", "diameter",
              "diameter_sigma", "dist_km", "dist_ld", "dist_uncertainty_km",
              "dist_uncertainty_pct", "v_esc", "gravity_boost_kms", "diameter_est_min_m",
              "diameter_est_max_m", "diameter_est_m", "diameter_best_m"]
    for col in floats:
        assert out[col].dtype == np.float64, col
    for col in ["is_gravitationally_bound", "t_sigma_lt_1min", "has_measured_diameter"]:
        assert out[col].dtype == np.bool_, col
    assert pd.api.types.is_datetime64_dtype(out["cd"])
    assert isinstance(out["_extracted_at"].dtype, pd.DatetimeTZDtype)
    assert str(out["_extracted_at"].dt.tz) == "UTC"
    assert pd.api.types.is_integer_dtype(out["t_sigma_min"])


def test_cad_strip_only_removes_edges():
    out = transform_cad(cad_with(des="  2025 VV2 ", fullname="   (2025 VV2)"))
    assert out.loc[0, "fullname"] == "(2025 VV2)"
    assert out.loc[0, "des"] == "2025 VV2"
    assert out.loc[0, "provisional_des"] == "2025 VV2"


def test_cad_close_approach_date_parsed():
    out = transform_cad(make_cad([QQ8[0]]))
    assert out.loc[0, "cd"] == pd.Timestamp("2026-05-01 21:48")


def test_cad_close_approach_date_wrong_format_raises():
    with pytest.raises(ValueError):
        transform_cad(cad_with(cd="2026-05-01 21:48"))


def test_cad_extracted_at_is_utc():
    out = transform_cad(cad_with())
    assert out.loc[0, "_extracted_at"] == pd.Timestamp("2026-11-01 02:53:31", tz="UTC")


def test_cad_one_au_is_au_km():
    out = transform_cad(cad_with(dist="1", dist_min="0.9", dist_max="1.1", v_inf=None))
    assert out.loc[0, "dist_km"] == pytest.approx(AU_KM)


def test_cad_one_lunar_distance():
    d = repr(LD_KM / AU_KM)
    out = transform_cad(cad_with(dist=d, dist_min=d, dist_max=d, v_inf=None))
    assert out.loc[0, "dist_km"] == pytest.approx(LD_KM)
    assert out.loc[0, "dist_ld"] == pytest.approx(1)


def test_cad_qq8_uncertainty(qq8):
    assert qq8["dist_uncertainty_km"].iloc[0] == pytest.approx(713.5, abs=0.1)
    assert qq8["dist_uncertainty_km"].iloc[1] == pytest.approx(14.9, abs=0.1)


def test_cad_qq8_december_distance_is_coherent(qq8):
    assert qq8["dist_ld"].iloc[2] == pytest.approx(3.517, abs=1e-3)
    assert qq8["v_esc"].iloc[2] ** 2 == pytest.approx(QQ8_DEC_V_REL**2 - QQ8_DEC_V_INF**2)


def test_cad_qq8_gravitationally_bound(qq8):
    assert qq8["is_gravitationally_bound"].tolist() == [True, True, False]


def test_cad_bound_matches_missing_v_inf(cad_raw):
    out = transform_cad(cad_raw)
    assert out["is_gravitationally_bound"].tolist() == out["v_inf"].isna().tolist()


def test_cad_gravity_boost():
    out = transform_cad(cad_with())
    assert out.loc[0, "gravity_boost_kms"] == pytest.approx(9.40266280754704 - 9.38802654917281)


@pytest.mark.parametrize(
    ("raw", "minutes", "lt_1min"),
    [
        ("< 00:01", 1, True),
        ("00:01", 1, False),
        ("04:54", 294, False),
        ("2_09:42", 3462, False),
        ("6_04:57", 8937, False),
    ],
)
def test_cad_t_sigma(raw, minutes, lt_1min):
    out = transform_cad(cad_with(t_sigma_f=raw))
    assert out.loc[0, "t_sigma_min"] == minutes
    assert out.loc[0, "t_sigma_lt_1min"] is np.bool_(lt_1min)


@pytest.mark.parametrize("raw", ["abc", "1:2"])
def test_cad_t_sigma_bad_format_raises(raw):
    with pytest.raises(ValueError, match="t_sigma_f"):
        transform_cad(cad_with(t_sigma_f=raw))


def test_cad_diameter_estimates_for_h22():
    out = transform_cad(cad_with(h="22"))
    assert out.loc[0, "diameter_est_min_m"] == pytest.approx(105.8, abs=0.1)
    assert out.loc[0, "diameter_est_m"] == pytest.approx(141.4, abs=0.1)
    assert out.loc[0, "diameter_est_max_m"] == pytest.approx(236.6, abs=0.1)


def test_cad_diameter_best_and_measured_flag():
    out = transform_cad(make_cad([VV2, FX9]))
    assert out["has_measured_diameter"].tolist() == [False, True]
    assert out.loc[0, "diameter_best_m"] == pytest.approx(out.loc[0, "diameter_est_m"])
    assert out.loc[1, "diameter_best_m"] == pytest.approx(30.0)


@pytest.mark.parametrize(
    ("fullname", "number", "name", "provisional"),
    [
        ("       (2025 VV2)", None, None, "2025 VV2"),
        ("  99942 Apophis (2004 MN4)", 99942, "Apophis", "2004 MN4"),
        ("  163693 (2003 CP20)", 163693, None, "2003 CP20"),
        ("  1620 Geographos", 1620, "Geographos", None),
    ],
)
def test_cad_fullname_split(fullname, number, name, provisional):
    out = transform_cad(cad_with(fullname=fullname))
    row = out.iloc[0]
    for col, expected in [("number", number), ("name", name), ("provisional_des", provisional)]:
        if expected is None:
            assert row[col] is pd.NA, col
        else:
            assert row[col] == expected, col


def test_cad_fullname_bad_format_raises():
    with pytest.raises(ValueError, match="fullname"):
        transform_cad(cad_with(fullname="  Apophis (2004 (MN4)"))


def test_cad_does_not_modify_input(cad_raw):
    before = cad_raw.copy()
    transform_cad(cad_raw)
    pd.testing.assert_frame_equal(cad_raw, before)


def test_cad_is_deterministic(cad_raw):
    pd.testing.assert_frame_equal(transform_cad(cad_raw), transform_cad(cad_raw))


def test_cad_empty_input():
    out = transform_cad(make_cad([]))
    assert out.empty
    assert {"dist_km", "t_sigma_min", "number", "name", "provisional_des"} <= set(out.columns)


def test_cad_missing_column_raises():
    with pytest.raises(ValueError, match="missing required columns"):
        transform_cad(cad_with().drop(columns=["t_sigma_f"]))


# ==========================================================================
# CAD validação
# ==========================================================================
@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"dist": "0.5", "dist_min": "0.6", "dist_max": "0.7"}, "dist_min, dist_max"),
        ({"dist": "-0.01", "dist_min": "-0.02", "dist_max": "0.01"}, "dist must be finite"),
        ({"dist": "inf", "dist_max": "inf"}, "must be finite"),
        ({"v_rel": "-inf"}, "v_rel must be finite"),
        ({"v_rel": "abc"}, "non-numeric"),
        ({"v_inf": "5.0"}, "energy equation"),
        ({"h": "inf"}, "h must be finite"),
        ({"diameter": "-0.03"}, "diameter must be finite"),
    ],
    ids=["dist-outside-range", "dist-negative", "dist-inf", "v_rel-minus-inf",
         "v_rel-text", "v_inf-incoherent", "h-inf", "diameter-negative"],
)
def test_cad_validation_rejects(overrides, match):
    with pytest.raises(ValueError, match=match):
        transform_cad(cad_with(**overrides))


def test_cad_validation_does_not_turn_text_into_nan():
    """'abc' não pode virar NaN e passar despercebido numa coluna que aceita nulos."""
    with pytest.raises(ValueError, match="v_inf"):
        transform_cad(cad_with(v_inf="abc"))


# ==========================================================================
# Fireball funcional
# ==========================================================================
@pytest.mark.parametrize(
    ("column", "value", "direction", "expected"),
    [
        ("lat", "41.8", "S", -41.8),
        ("lat", "54.4", "N", 54.4),
        ("lon", "173.1", "W", -173.1),
        ("lon", "6.3", "E", 6.3),
    ],
)
def test_fireball_hemisphere_sign(column, value, direction, expected):
    out = transform_fireball(fireball_with(**{column: value, f"{column}-dir": direction}))
    assert out.loc[0, column] == pytest.approx(expected)


def test_fireball_direction_with_spaces_is_stripped():
    out = transform_fireball(fireball_with(**{"lat-dir": " S ", "lon-dir": "W "}))
    assert out.loc[0, "lat"] == pytest.approx(-41.8)
    assert out.loc[0, "lon"] == pytest.approx(-173.1)


def test_fireball_real_rows(fireball_raw):
    out = transform_fireball(fireball_raw).set_index("date")
    assert out.loc[pd.Timestamp("2026-09-11 01:12:11"), ["lat", "lon"]].tolist() == \
        pytest.approx([54.4, -100.1])
    assert out.loc[pd.Timestamp("1991-10-04 09:22:47"), ["lat", "lon"]].tolist() == \
        pytest.approx([78.7, 6.3])


def test_fireball_without_location_keeps_nulls():
    out = transform_fireball(make_fireball([FB_NO_LOCATION]))
    assert len(out) == 1
    assert out[["lat", "lon", "alt", "vel"]].isna().all().all()


def test_fireball_dtypes(fireball_raw):
    out = transform_fireball(fireball_raw)
    for col in ["lat", "lon", "alt", "vel", "energy", "impact_e", "energy_joules",
                "impact_energy_joules"]:
        assert out[col].dtype == np.float64, col
    assert pd.api.types.is_datetime64_dtype(out["date"])
    assert str(out["_extracted_at"].dt.tz) == "UTC"


def test_fireball_duplicate_keeps_most_recent_extraction():
    # a extração nova vem primeiro de propósito: sem ordenar, keep="last" ficaria com a antiga
    new = make_fireball([FB_SOUTH_WEST], raw_file=FIREBALL_NEW_FILE, extracted_at=FIREBALL_NEW_AT)
    old = make_fireball([FB_SOUTH_WEST], raw_file=FIREBALL_OLD_FILE, extracted_at=FIREBALL_OLD_AT)
    out = transform_fireball(pd.concat([new, old], ignore_index=True))
    assert len(out) == 1
    assert out.loc[0, "_raw_file"] == FIREBALL_NEW_FILE
    assert out.loc[0, "_extracted_at"] == pd.Timestamp(FIREBALL_NEW_AT)
    assert out.index.tolist() == [0]


def test_fireball_same_second_different_place_is_kept():
    other_place = {**FB_SOUTH_WEST, "lat": "12.0", "lon": "45.0"}
    out = transform_fireball(make_fireball([FB_SOUTH_WEST, other_place]))
    assert len(out) == 2


def test_fireball_energy_conversions():
    out = transform_fireball(fireball_with(energy="5.4", **{"impact-e": "1"}))
    assert out.loc[0, "energy_joules"] == pytest.approx(5.4e10)
    assert out.loc[0, "impact_energy_joules"] == pytest.approx(KT_JOULES)
    assert out.loc[0, "impact_energy_joules"] == pytest.approx(4.185e12)


def test_fireball_column_names(fireball_raw):
    out = transform_fireball(fireball_raw)
    assert not [c for c in out.columns if "-" in c]
    assert {"impact_e", "lat_dir", "lon_dir"} <= set(out.columns)


def test_fireball_vel_is_optional():
    """O schema mínimo da extração não exige vel."""
    out = transform_fireball(fireball_with().drop(columns=["vel"]))
    assert "vel" not in out.columns


def test_fireball_does_not_modify_input(fireball_raw):
    before = fireball_raw.copy()
    transform_fireball(fireball_raw)
    pd.testing.assert_frame_equal(fireball_raw, before)


def test_fireball_is_deterministic(fireball_raw):
    pd.testing.assert_frame_equal(transform_fireball(fireball_raw), transform_fireball(fireball_raw))


def test_fireball_empty_input():
    out = transform_fireball(make_fireball([]))
    assert out.empty
    assert {"energy_joules", "impact_energy_joules", "impact_e"} <= set(out.columns)


# ==========================================================================
# Fireball validação
# ==========================================================================
@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"lat": "95"}, "lat must be within"),
        ({"lon": "181", "lon-dir": "W"}, "lon must be within"),
        ({"energy": "-5.4"}, "energy must be finite"),
        ({"impact-e": "inf"}, "impact_e must be finite"),
        ({"alt": "500"}, "alt must be within"),
        ({"alt": "high"}, "non-numeric"),
        ({"lat-dir": "X"}, "lat-dir must be one of"),
        ({"vel": "-13.8"}, "vel must be finite"),
    ],
    ids=["lat-95", "lon-minus-181", "energy-negative", "impact-e-inf", "alt-500",
         "alt-text", "lat-dir-invalid", "vel-negative"],
)
def test_fireball_validation_rejects(overrides, match):
    with pytest.raises(ValueError, match=match):
        transform_fireball(fireball_with(**overrides))


def test_fireball_unparseable_date_raises():
    with pytest.raises(ValueError):
        transform_fireball(fireball_with(date="04/10/2026"))


# ==========================================================================
# save_processed
# ==========================================================================
@pytest.mark.parametrize("name", ["cad", "fireball"])
def test_save_processed_round_trip(tmp_path, cad_raw, fireball_raw, name):
    df = transform_cad(cad_raw) if name == "cad" else transform_fireball(fireball_raw)
    path = save_processed(df, name, out_dir=tmp_path / "processed")
    assert path == tmp_path / "processed" / f"{name}.parquet"
    pd.testing.assert_frame_equal(pd.read_parquet(path), df)


def test_save_processed_creates_folder_and_overwrites(tmp_path, cad_raw):
    out_dir = tmp_path / "a" / "b"
    df = transform_cad(cad_raw)
    save_processed(df, "cad", out_dir=out_dir)
    path = save_processed(df.head(1), "cad", out_dir=out_dir)
    assert len(pd.read_parquet(path)) == 1


@pytest.mark.parametrize("name", ["../../etc", "cad/../x", "", "CAD", "cad.parquet", None])
def test_save_processed_rejects_bad_names(tmp_path, cad_raw, name):
    out_dir = tmp_path / "x" / "y" / "processed"
    with pytest.raises(ValueError, match="Invalid dataset name"):
        save_processed(transform_cad(cad_raw), name, out_dir=out_dir)
    assert list(tmp_path.rglob("*")) == []


def test_save_processed_leaves_no_tmp_file(tmp_path, cad_raw):
    save_processed(transform_cad(cad_raw), "cad", out_dir=tmp_path)
    assert list(tmp_path.glob("*.tmp")) == []
    assert [p.name for p in tmp_path.iterdir()] == ["cad.parquet"]


def test_save_processed_cleans_tmp_on_failure(tmp_path, monkeypatch):
    def broken(self, *args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(pd.DataFrame, "to_parquet", broken)
    with pytest.raises(OSError):
        save_processed(pd.DataFrame({"a": [1]}), "cad", out_dir=tmp_path)
    assert list(tmp_path.iterdir()) == []


# ==========================================================================
# Segurança
# ==========================================================================
@pytest.mark.parametrize("payload", ["'; DROP TABLE cad; --", "<script>"])
def test_injection_like_text_is_kept_as_plain_text(payload):
    out = transform_cad(cad_with(fullname=f"   {payload}  "))
    assert out.loc[0, "fullname"] == payload
    assert out.loc[0, "name"] == payload


def test_fullname_redos_is_fast():
    adversarial = "1 " + "a " * 25000 + "("
    assert len(adversarial) > 50_000
    start = time.perf_counter()
    try:
        transform_cad(cad_with(fullname=adversarial))
    except ValueError:
        pass
    assert time.perf_counter() - start < 2


def test_t_sigma_redos_is_fast():
    start = time.perf_counter()
    with pytest.raises(ValueError):
        transform_cad(cad_with(t_sigma_f="<" + " " * 50_000 + "1_" * 25_000))
    assert time.perf_counter() - start < 2


@pytest.mark.parametrize(
    ("build", "transformer"),
    [
        (lambda: cad_with(dist="1e308", dist_min="1e308", dist_max="1e308"), transform_cad),
        (lambda: cad_with(dist="nan", dist_min="nan", dist_max="nan"), transform_cad),
        (lambda: cad_with(v_rel="1e308"), transform_cad),
        (lambda: cad_with(v_inf="nan"), transform_cad),
        (lambda: fireball_with(energy="1e308"), transform_fireball),
        (lambda: fireball_with(energy="nan"), transform_fireball),
        (lambda: fireball_with(alt="nan"), transform_fireball),
    ],
    ids=["cad-dist-1e308", "cad-dist-nan", "cad-v_rel-1e308", "cad-v_inf-nan",
         "fireball-energy-1e308", "fireball-energy-nan", "fireball-alt-nan"],
)
def test_extreme_numbers_are_rejected(build, transformer):
    with pytest.raises(ValueError):
        transformer(build())


def test_constants_match_spec():
    assert transform.AU_KM == 149_597_870.7
    assert transform.LD_KM == 384_400
    assert transform.GM_EARTH == 398_600.4418
    assert transform.KT_JOULES == 4.185e12
