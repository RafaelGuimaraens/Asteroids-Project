"""Transformação dos dados brutos do CAD e do Fireball (camada processed).

As funções recebem os DataFrames devolvidos por extract.load_raw (todas as
colunas como texto), trabalham numa cópia, validam o próprio resultado e
levantam ValueError quando algo não bate.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# Constantes físicas e de conversão
# --------------------------------------------------------------------------
AU_KM = 149_597_870.7          # 1 unidade astronômica em km
LD_KM = 384_400                # distância média Terra-Lua em km
GM_EARTH = 398_600.4418        # parâmetro gravitacional da Terra (km³/s²)
KT_JOULES = 4.185e12           # 1 quiloton de TNT em joules

FIREBALL_ENERGY_UNIT_J = 1e10  # a API do Fireball informa "energy" em 10^10 J
M_PER_KM = 1_000
MINUTES_PER_HOUR = 60
HOURS_PER_DAY = 24
MINUTES_PER_DAY = HOURS_PER_DAY * MINUTES_PER_HOUR
PERCENT = 100

# Diâmetro a partir da magnitude absoluta: D_km = 1329 / sqrt(p) * 10^(-H/5)
DIAMETER_COEFF_KM = 1329
ALBEDO_BRIGHT = 0.25           # superfície clara -> menor diâmetro possível
ALBEDO_TYPICAL = 0.14          # valor médio usado para a estimativa central
ALBEDO_DARK = 0.05             # superfície escura -> maior diâmetro possível

ENERGY_CHECK_RTOL = 1e-2       # tolerância de v_rel² - v_inf² ≈ v_esc²
LAT_LIMIT_DEG = 90
LON_LIMIT_DEG = 180
ALT_MAX_KM = 200

CD_FORMAT = "%Y-%b-%d %H:%M"
FIREBALL_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# "< 00:01", "04:54", "2_09:42" -> menor que 1 min?, dias, horas, minutos
T_SIGMA_PATTERN = r"^(?P<lt><)?\s*(?:(?P<days>\d+)_)?(?P<hours>\d{2}):(?P<minutes>\d{2})$"
# "99942 Apophis (2004 MN4)" -> número, nome, designação provisória
FULLNAME_PATTERN = (
    r"^(?:(?P<number>\d+)\s+)?(?P<name>[^()]*?)\s*(?:\((?P<provisional_des>[^()]+)\))?$"
)

META_COLUMNS = {"_extracted_at", "_api_version", "_raw_file"}
CAD_COLUMNS = {
    "des", "orbit_id", "jd", "cd", "dist", "dist_min", "dist_max", "v_rel", "v_inf",
    "t_sigma_f", "h", "diameter", "diameter_sigma", "fullname", "_extracted_at",
}
CAD_FLOAT_COLUMNS = [
    "jd", "dist", "dist_min", "dist_max", "v_rel", "v_inf", "h", "diameter", "diameter_sigma",
]
FIREBALL_COLUMNS = {
    "date", "lat", "lat-dir", "lon", "lon-dir", "alt", "energy", "impact-e", "_extracted_at",
}
# "vel" nem sempre vem na resposta da API; é convertida apenas se existir
FIREBALL_FLOAT_COLUMNS = ["alt", "vel", "energy", "impact-e"]

PROCESSED_NAMES = frozenset({"cad", "fireball"})
PROCESSED_DIR = Path("data/processed")


# --------------------------------------------------------------------------
# Auxiliares
# --------------------------------------------------------------------------
def _require_columns(df: pd.DataFrame, required: set[str], source: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{source}: missing required columns {sorted(missing)}.")


def _to_float(df: pd.DataFrame, column: str) -> pd.Series:
    """Converte texto em float sem nunca transformar lixo em NaN silenciosamente."""
    raw = df[column]
    try:
        values = pd.to_numeric(raw, errors="raise").astype("float64")
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Column {column!r} contains non-numeric values: {exc}") from exc
    if (values.isna() & raw.notna()).any():
        raise ValueError(f"Column {column!r} contains values that became NaN after conversion.")
    return values


def _check(ok: pd.Series, message: str) -> None:
    """Levanta ValueError listando até 5 índices das linhas que falharam."""
    bad = ok.index[~ok.to_numpy(dtype=bool)]
    if len(bad):
        raise ValueError(f"{message} ({len(bad)} row(s), e.g. index {list(bad[:5])}).")


def _finite_positive(s: pd.Series, allow_null: bool = False) -> pd.Series:
    ok = np.isfinite(s) & (s > 0)
    return ok | s.isna() if allow_null else ok


def _diameter_m(h: pd.Series, albedo: float) -> pd.Series:
    return DIAMETER_COEFF_KM / np.sqrt(albedo) * 10 ** (-h / 5) * M_PER_KM


def _parse_t_sigma(raw: pd.Series) -> tuple[pd.Series, pd.Series]:
    """'dias_hh:mm' (dias opcionais, '<' opcional) -> (minutos, menor que 1 min?)."""
    parts = raw.str.strip().str.extract(T_SIGMA_PATTERN)
    if parts["hours"].isna().any():
        bad = raw[parts["hours"].isna()].head().tolist()
        raise ValueError(f"Unexpected t_sigma_f format: {bad}.")
    days = pd.to_numeric(parts["days"].fillna("0"), errors="raise").astype("int64")
    hours = pd.to_numeric(parts["hours"], errors="raise").astype("int64")
    minutes = pd.to_numeric(parts["minutes"], errors="raise").astype("int64")
    if (hours >= HOURS_PER_DAY).any() or (minutes >= MINUTES_PER_HOUR).any():
        raise ValueError("t_sigma_f has hours >= 24 or minutes >= 60.")
    total = days * MINUTES_PER_DAY + hours * MINUTES_PER_HOUR + minutes
    return total, parts["lt"].notna().astype(bool)


def _parse_fullname(fullname: pd.Series) -> pd.DataFrame:
    """Separa fullname em number (Int64), name e provisional_des (string, pd.NA se vazio)."""
    parts = fullname.str.extract(FULLNAME_PATTERN)
    if parts["name"].isna().any():
        bad = fullname[parts["name"].isna()].head().tolist()
        raise ValueError(f"Unexpected fullname format: {bad}.")
    number = pd.to_numeric(parts["number"], errors="raise").astype("Int64")
    name = parts["name"].astype("string")
    return pd.DataFrame(
        {
            "number": number,
            "name": name.mask(name == ""),
            "provisional_des": parts["provisional_des"].astype("string"),
        },
        index=fullname.index,
    )


# --------------------------------------------------------------------------
# CAD
# --------------------------------------------------------------------------
def transform_cad(df: pd.DataFrame) -> pd.DataFrame:
    """Limpa e enriquece as aproximações do CAD."""
    _require_columns(df, CAD_COLUMNS, "CAD")
    cad = df.copy()

    for col in ["des", "fullname"]:
        cad[col] = cad[col].str.strip()
    for col in CAD_FLOAT_COLUMNS:
        cad[col] = _to_float(cad, col)
    cad["cd"] = pd.to_datetime(cad["cd"], format=CD_FORMAT, errors="raise")
    cad["_extracted_at"] = pd.to_datetime(cad["_extracted_at"], utc=True, errors="raise")

    # Distância e incerteza
    cad["dist_km"] = cad["dist"] * AU_KM
    cad["dist_ld"] = cad["dist_km"] / LD_KM
    cad["dist_uncertainty_km"] = (cad["dist_max"] - cad["dist_min"]) * AU_KM
    cad["dist_uncertainty_pct"] = (cad["dist_max"] - cad["dist_min"]) / cad["dist"] * PERCENT

    # Velocidade de escape na distância de máxima aproximação
    # (distância negativa vira NaN aqui e é rejeitada em _validate_cad)
    with np.errstate(invalid="ignore"):
        cad["v_esc"] = np.sqrt(2 * GM_EARTH / cad["dist_km"])
    cad["gravity_boost_kms"] = cad["v_rel"] - cad["v_inf"]
    cad["is_gravitationally_bound"] = (cad["v_rel"] < cad["v_esc"]).astype(bool)

    cad["t_sigma_min"], cad["t_sigma_lt_1min"] = _parse_t_sigma(cad["t_sigma_f"])

    # Diâmetro: faixa estimada pelo brilho e o melhor valor disponível
    cad["diameter_est_min_m"] = _diameter_m(cad["h"], ALBEDO_BRIGHT)
    cad["diameter_est_max_m"] = _diameter_m(cad["h"], ALBEDO_DARK)
    cad["diameter_est_m"] = _diameter_m(cad["h"], ALBEDO_TYPICAL)
    cad["has_measured_diameter"] = cad["diameter"].notna().astype(bool)
    cad["diameter_best_m"] = (cad["diameter"] * M_PER_KM).where(
        cad["has_measured_diameter"], cad["diameter_est_m"]
    )

    names = _parse_fullname(cad["fullname"])
    for col in names.columns:
        cad[col] = names[col]

    _validate_cad(cad)
    return cad


def _validate_cad(cad: pd.DataFrame) -> None:
    for col in ["dist", "dist_min", "dist_max", "dist_km", "v_rel", "v_esc"]:
        _check(_finite_positive(cad[col]), f"CAD: {col} must be finite and positive")
    _check(_finite_positive(cad["v_inf"], allow_null=True),
           "CAD: v_inf must be finite and positive when present")
    for col in ["jd", "h"]:
        _check(pd.Series(np.isfinite(cad[col]), index=cad.index), f"CAD: {col} must be finite")
    _check(_finite_positive(cad["diameter"], allow_null=True),
           "CAD: diameter must be finite and positive when present")
    _check((cad["dist_min"] <= cad["dist"]) & (cad["dist"] <= cad["dist_max"]),
           "CAD: dist must lie within [dist_min, dist_max]")

    has_v_inf = cad["v_inf"].notna()
    energy_ok = np.isclose(
        cad["v_rel"] ** 2 - cad["v_inf"] ** 2, cad["v_esc"] ** 2, rtol=ENERGY_CHECK_RTOL
    )
    _check(pd.Series(energy_ok, index=cad.index) | ~has_v_inf,
           "CAD: v_rel² - v_inf² does not match v_esc² (energy equation)")


# --------------------------------------------------------------------------
# Fireball
# --------------------------------------------------------------------------
def transform_fireball(df: pd.DataFrame) -> pd.DataFrame:
    """Limpa, aplica o sinal das coordenadas e remove eventos duplicados."""
    _require_columns(df, FIREBALL_COLUMNS, "Fireball")
    fb = df.copy()

    fb["date"] = pd.to_datetime(fb["date"], format=FIREBALL_DATE_FORMAT, errors="raise")
    fb["_extracted_at"] = pd.to_datetime(fb["_extracted_at"], utc=True, errors="raise")

    # Coordenadas: a API manda o módulo e o hemisfério separados
    for value_col, dir_col, negative, valid in [
        ("lat", "lat-dir", "S", {"N", "S"}),
        ("lon", "lon-dir", "W", {"E", "W"}),
    ]:
        fb[dir_col] = fb[dir_col].str.strip()
        fb[value_col] = _to_float(fb, value_col)
        present = fb[value_col].notna()
        _check(~present | fb[dir_col].isin(valid),
               f"Fireball: {dir_col} must be one of {sorted(valid)} when {value_col} is present")
        fb[value_col] = fb[value_col].where(fb[dir_col] != negative, -fb[value_col])

    for col in FIREBALL_FLOAT_COLUMNS:
        if col in fb.columns:
            fb[col] = _to_float(fb, col)
    fb["energy_joules"] = fb["energy"] * FIREBALL_ENERGY_UNIT_J
    fb["impact_energy_joules"] = fb["impact-e"] * KT_JOULES

    # Extrações incrementais repetem eventos: fica a versão mais recente
    fb = (
        fb.sort_values("_extracted_at", kind="stable")
        .drop_duplicates(subset=["date", "lat", "lon"], keep="last")
        .reset_index(drop=True)
    )
    fb = fb.rename(columns=lambda c: c.replace("-", "_"))

    _validate_fireball(fb)
    return fb


def _validate_fireball(fb: pd.DataFrame) -> None:
    _check(fb["date"].notna(), "Fireball: date must not be null")
    _check(fb["lat"].isna() | fb["lat"].between(-LAT_LIMIT_DEG, LAT_LIMIT_DEG),
           f"Fireball: lat must be within [-{LAT_LIMIT_DEG}, {LAT_LIMIT_DEG}]")
    _check(fb["lon"].isna() | fb["lon"].between(-LON_LIMIT_DEG, LON_LIMIT_DEG),
           f"Fireball: lon must be within [-{LON_LIMIT_DEG}, {LON_LIMIT_DEG}]")
    for col in ["energy", "impact_e", "energy_joules", "impact_energy_joules"]:
        _check(_finite_positive(fb[col]), f"Fireball: {col} must be finite and positive")
    _check(fb["alt"].isna() | fb["alt"].between(0, ALT_MAX_KM),
           f"Fireball: alt must be within [0, {ALT_MAX_KM}] km when present")
    if "vel" in fb.columns:
        _check(_finite_positive(fb["vel"], allow_null=True),
               "Fireball: vel must be finite and positive when present")


# --------------------------------------------------------------------------
# Gravação
# --------------------------------------------------------------------------
def save_processed(df: pd.DataFrame, name: str, out_dir: Path = PROCESSED_DIR) -> Path:
    """Grava data/processed/<name>.parquet de forma atômica (temporário + os.replace)."""
    # lista fechada de nomes: impede path traversal ("../../etc", "cad/../x")
    if not isinstance(name, str) or name not in PROCESSED_NAMES:
        raise ValueError(f"Invalid dataset name: {name!r}. Expected one of {sorted(PROCESSED_NAMES)}.")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{name}.parquet"

    fd, tmp = tempfile.mkstemp(dir=out_dir, prefix=f".{name}-", suffix=".tmp")
    os.close(fd)  # no Windows o arquivo precisa estar fechado para o pyarrow escrever
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path
