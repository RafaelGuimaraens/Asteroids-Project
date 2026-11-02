from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------
BASE_URL = "https://ssd-api.jpl.nasa.gov"
ALLOWED_ENDPOINTS = {"cad.api", "fireball.api"}
SUPPORTED_MAJOR_VERSION = "1"          # quebra se a NASA mudar para v2.x
TIMEOUT = (5, 60)                       # (conexão, leitura) em segundos
MAX_RESPONSE_BYTES = 25 * 1024 * 1024   # 25 MB
USER_AGENT = "planetary-defense-etl/0.1 (projeto de portfolio educacional)"

DATA_DIR = Path(os.getenv("PDETL_DATA_DIR", "data"))
RAW_DIR = DATA_DIR / "raw"
STATE_FILE = DATA_DIR / "state.json"

CAD_REQUIRED_FIELDS = {
    "des", "orbit_id", "jd", "cd", "dist", "dist_min", "dist_max",
    "v_rel", "v_inf", "h",
}
FIREBALL_REQUIRED_FIELDS = {
    "date", "lat", "lat-dir", "lon", "lon-dir", "alt", "energy", "impact-e",
}

# "0.05", "10LD", "0.2au" — nada além disso entra na URL
_DIST_PATTERN = re.compile(r"^\d{1,4}(\.\d{1,6})?(LD|au)?$")


class ExtractionError(RuntimeError):
    """Erro ao obter ou validar dados da API."""


# --------------------------------------------------------------------------
# Sessão HTTP
# --------------------------------------------------------------------------
def build_session() -> requests.Session:
    """Cria uma sessão com retentativas apenas para erros temporários."""
    retry = Retry(
        total=4,
        backoff_factor=2,                       # espera 2s, 4s, 8s, 16s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    session.verify = True                       # nunca desligar
    return session


def _get_json(session: requests.Session, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    """Faz o GET, aplica as checagens de segurança e devolve o JSON."""
    if endpoint not in ALLOWED_ENDPOINTS:
        raise ValueError(f"Endpoint não permitido: {endpoint!r}")

    url = f"{BASE_URL}/{endpoint}"
    logger.info("GET %s params=%s", url, params)

    response = session.get(
        url, params=params, timeout=TIMEOUT, allow_redirects=False, stream=True
    )
    try:
        if 300 <= response.status_code < 400:
            raise ExtractionError(f"Redirecionamento recusado ({response.status_code}).")
        if response.status_code != 200:
            raise ExtractionError(f"API respondeu HTTP {response.status_code}.")

        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            raise ExtractionError(f"Content-Type inesperado: {content_type!r}")

        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
            raise ExtractionError("Resposta maior que o limite permitido.")

        body = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ExtractionError("Resposta maior que o limite permitido.")
    finally:
        response.close()

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ExtractionError("Resposta não é um JSON válido.") from exc

    if not isinstance(payload, dict):
        raise ExtractionError("JSON com formato inesperado (esperado objeto).")
    return payload


# --------------------------------------------------------------------------
# Validação e conversão para DataFrame
# --------------------------------------------------------------------------
def _validate_signature(payload: dict[str, Any]) -> str:
    signature = payload.get("signature")
    if not isinstance(signature, dict):
        raise ExtractionError("Resposta sem 'signature'.")
    source = str(signature.get("source", ""))
    version = str(signature.get("version", ""))
    if not source.startswith("NASA/JPL"):
        raise ExtractionError(f"Fonte inesperada: {source!r}")
    if version.split(".")[0] != SUPPORTED_MAJOR_VERSION:
        raise ExtractionError(f"Versão da API não suportada: {version!r}")
    return version


def payload_to_dataframe(
    payload: dict[str, Any],
    required_fields: set[str],
    extracted_at: datetime | None = None,
) -> pd.DataFrame:
    """Converte {'fields': [...], 'data': [[...]]} em DataFrame, validando o schema."""
    version = _validate_signature(payload)
    extracted_at = extracted_at or datetime.now(timezone.utc)

    try:
        count = int(payload.get("count", 0))
    except (TypeError, ValueError) as exc:
        raise ExtractionError("Campo 'count' inválido.") from exc

    if count == 0:
        df = pd.DataFrame(columns=sorted(required_fields))
    else:
        fields = payload.get("fields")
        rows = payload.get("data")
        if not isinstance(fields, list) or not all(isinstance(f, str) for f in fields):
            raise ExtractionError("Campo 'fields' ausente ou inválido.")
        if not isinstance(rows, list):
            raise ExtractionError("Campo 'data' ausente ou inválido.")

        missing = required_fields - set(fields)
        if missing:
            raise ExtractionError(f"Campos obrigatórios ausentes: {sorted(missing)}")

        for i, row in enumerate(rows):
            if not isinstance(row, list) or len(row) != len(fields):
                raise ExtractionError(f"Linha {i} com número de colunas diferente do esperado.")

        if len(rows) != count:
            logger.warning("count=%s, mas vieram %s linhas.", count, len(rows))

        df = pd.DataFrame(rows, columns=fields)

    # Metadados de linhagem: de onde e quando veio cada linha
    df["_extracted_at"] = extracted_at.isoformat(timespec="seconds")
    df["_api_version"] = version
    return df


# --------------------------------------------------------------------------
# Camada raw e estado incremental
# --------------------------------------------------------------------------
def _atomic_write_json(path: Path, obj: Any) -> None:
    """Escreve num arquivo temporário e renomeia: nunca deixa arquivo pela metade."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


RAW_SOURCES = {"cad": CAD_REQUIRED_FIELDS, "fireball": FIREBALL_REQUIRED_FIELDS}
_STAMP_FORMAT = "%Y%m%dT%H%M%S%fZ"
_RAW_NAME = re.compile(r"^\d{8}T\d{12}Z\.json$")


def save_raw(payload: dict[str, Any], source: str, raw_dir: Path = RAW_DIR) -> Path:
    if source not in RAW_SOURCES:
        raise ValueError(f"Fonte inválida: {source!r}")
    stamp = datetime.now(timezone.utc).strftime(_STAMP_FORMAT)
    path = raw_dir / source / f"{stamp}.json"
    _atomic_write_json(path, payload)
    return path


def load_raw(source: str, mode: str = "latest", raw_dir: Path = RAW_DIR) -> pd.DataFrame:
    """Lê os JSONs já salvos em data/raw, sem chamar a API.

    mode="latest": só a extração mais recente (no CAD, a "foto" atual do céu).
    mode="all":    todas as extrações empilhadas, com a coluna _raw_file
                   indicando de qual arquivo veio cada linha. Use para o
                   Fireball (cada execução incremental traz só os eventos
                   novos) e para estudar como as órbitas do CAD mudaram.
    Linhas repetidas entre arquivos NÃO são removidas aqui: deduplicar é
    tarefa da transformação.
    """
    if source not in RAW_SOURCES:
        raise ValueError(f"Fonte inválida: {source!r}")
    if mode not in {"latest", "all"}:
        raise ValueError("mode deve ser 'latest' ou 'all'.")

    folder = raw_dir / source
    # só aceita arquivos com o nome gerado pelo próprio pipeline
    files = sorted(
        p for p in folder.glob("*.json") if p.is_file() and _RAW_NAME.fullmatch(p.name)
    ) if folder.is_dir() else []
    if not files:
        raise FileNotFoundError(
            f"Nenhuma extração de '{source}' em {folder}. Rode 'python extract.py' primeiro."
        )
    if mode == "latest":
        files = files[-1:]

    frames = []
    for path in files:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            raise ExtractionError(f"Arquivo bruto grande demais: {path.name}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ExtractionError(f"JSON corrompido: {path.name}") from exc
        if not isinstance(payload, dict):
            raise ExtractionError(f"JSON com formato inesperado: {path.name}")
        stamp = datetime.strptime(path.stem, _STAMP_FORMAT).replace(tzinfo=timezone.utc)
        df = payload_to_dataframe(payload, RAW_SOURCES[source], extracted_at=stamp)
        df["_raw_file"] = path.name
        frames.append(df)

    return pd.concat(frames, ignore_index=True)


def load_state(state_file: Path = STATE_FILE) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.warning("state.json ilegível; recomeçando a carga completa.")
        return {}
    return state if isinstance(state, dict) else {}


def save_state(state: dict[str, Any], state_file: Path = STATE_FILE) -> None:
    _atomic_write_json(state_file, state)


# --------------------------------------------------------------------------
# Extratores
# --------------------------------------------------------------------------
def fetch_cad(
    session: requests.Session,
    date_min: date,
    date_max: date,
    dist_max: str = "10LD",
    raw_dir: Path = RAW_DIR,
) -> pd.DataFrame:
    """Aproximações de NEOs à Terra entre date_min e date_max."""
    if not isinstance(date_min, date) or not isinstance(date_max, date):
        raise ValueError("date_min e date_max devem ser objetos date.")
    if date_min > date_max:
        raise ValueError("date_min não pode ser posterior a date_max.")
    if not _DIST_PATTERN.fullmatch(dist_max):
        raise ValueError(f"dist_max inválido: {dist_max!r}")

    params = {
        "date-min": date_min.isoformat(),
        "date-max": date_max.isoformat(),
        "dist-max": dist_max,
        "diameter": "true",
        "fullname": "true",
    }
    payload = _get_json(session, "cad.api", params)
    save_raw(payload, "cad", raw_dir)
    return payload_to_dataframe(payload, CAD_REQUIRED_FIELDS)


def fetch_fireball(
    session: requests.Session,
    date_min: date | None = None,
    raw_dir: Path = RAW_DIR,
) -> pd.DataFrame:
    """Bólidos registrados. Sem date_min, traz o histórico completo."""
    params: dict[str, str] = {}
    if date_min is not None:
        if not isinstance(date_min, date):
            raise ValueError("date_min deve ser um objeto date.")
        params["date-min"] = date_min.isoformat()
    payload = _get_json(session, "fireball.api", params)
    save_raw(payload, "fireball", raw_dir)
    return payload_to_dataframe(payload, FIREBALL_REQUIRED_FIELDS)


def run_extraction(
    today: date | None = None,
    days_back: int = 365,
    days_ahead: int = 60,
    data_dir: Path = DATA_DIR,
    session: requests.Session | None = None,
) -> dict[str, pd.DataFrame]:
    """Executa a extração completa e devolve {'cad': df, 'fireball': df}."""
    today = today or datetime.now(timezone.utc).date()
    raw_dir = data_dir / "raw"
    state_file = data_dir / "state.json"
    session = session or build_session()

    cad = fetch_cad(
        session,
        date_min=date.fromordinal(today.toordinal() - days_back),
        date_max=date.fromordinal(today.toordinal() + days_ahead),
        raw_dir=raw_dir,
    )

    state = load_state(state_file)
    last = state.get("fireball_last_date")
    since = None
    if isinstance(last, str):
        try:
            since = date.fromisoformat(last[:10])
        except ValueError:
            since = None
    # date-min é inclusivo: o último dia volta e a transformação deduplica
    fireball = fetch_fireball(session, date_min=since, raw_dir=raw_dir)

    if not fireball.empty:
        state["fireball_last_date"] = str(fireball["date"].max())
        save_state(state, state_file)

    logger.info("CAD: %s linhas | Fireball: %s linhas", len(cad), len(fireball))
    return {"cad": cad, "fireball": fireball}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    frames = run_extraction()
    for name, df in frames.items():
        print(f"\n=== {name} ({len(df)} linhas) ===")
        print(df.head())
