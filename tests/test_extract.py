"""Testes funcionais e de segurança da etapa de extração.

Nenhum teste chama a internet: uma sessão falsa devolve respostas no mesmo
formato documentado pelo JPL.
"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import extract  # noqa: E402

CAD_PAYLOAD = {
    "signature": {"source": "NASA/JPL SBDB Close Approach Data API", "version": "1.5"},
    "count": 2,
    "fields": ["des", "orbit_id", "jd", "cd", "dist", "dist_min", "dist_max",
               "v_rel", "v_inf", "t_sigma_f", "h", "diameter", "diameter_sigma", "fullname"],
    "data": [
        ["2026 TX1", "3", "2461340.5", "2026-Oct-30 12:00", "0.0012", "0.0011", "0.0013",
         "8.1", "8.0", "00:01", "28.4", None, None, "       (2026 TX1)"],
        ["99942", "220", "2462240.4", "2029-Apr-13 21:46", "0.000254", "0.000254", "0.000254",
         "7.42", "5.84", "< 00:01", "19.09", "0.34", None, "  99942 Apophis (2004 MN4)"],
    ],
}

FIREBALL_PAYLOAD = {
    "signature": {"source": "NASA/JPL Fireball Data API", "version": "1.2"},
    "count": 2,
    "fields": ["date", "lat", "lat-dir", "lon", "lon-dir", "alt", "energy", "impact-e"],
    "data": [
        ["2026-10-20 03:11:09", "12.3", "S", "45.0", "W", "31.0", "4.1", "0.13"],
        ["2026-09-02 18:40:00", None, None, None, None, None, "2.0", "0.073"],
    ],
}


class FakeResponse:
    def __init__(self, status=200, body=b"", content_type="application/json", headers=None):
        self.status_code = status
        self._body = body
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def ok(payload):
    return FakeResponse(body=json.dumps(payload).encode())


# ---------------------------------------------------------------- funcionais
def test_cad_vira_dataframe_com_metadados(tmp_path):
    s = FakeSession([ok(CAD_PAYLOAD)])
    df = extract.fetch_cad(s, date(2026, 1, 1), date(2026, 12, 31), raw_dir=tmp_path)
    assert len(df) == 2
    assert {"des", "dist", "fullname", "_extracted_at", "_api_version"} <= set(df.columns)
    assert df["_api_version"].iloc[0] == "1.5"


def test_cad_salva_json_bruto_intacto(tmp_path):
    s = FakeSession([ok(CAD_PAYLOAD)])
    extract.fetch_cad(s, date(2026, 1, 1), date(2026, 12, 31), raw_dir=tmp_path)
    files = list((tmp_path / "cad").glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text()) == CAD_PAYLOAD
    assert not list(tmp_path.rglob("*.tmp"))  # escrita atômica não deixa lixo


def test_parametros_enviados_corretamente(tmp_path):
    s = FakeSession([ok(CAD_PAYLOAD)])
    extract.fetch_cad(s, date(2026, 1, 1), date(2026, 3, 1), dist_max="0.05", raw_dir=tmp_path)
    url, kwargs = s.calls[0]
    assert url == "https://ssd-api.jpl.nasa.gov/cad.api"
    assert kwargs["params"]["date-min"] == "2026-01-01"
    assert kwargs["params"]["dist-max"] == "0.05"


def test_resposta_vazia_gera_dataframe_vazio_com_colunas(tmp_path):
    empty = {"signature": {"source": "NASA/JPL Fireball Data API", "version": "1.2"}, "count": 0}
    s = FakeSession([ok(empty)])
    df = extract.fetch_fireball(s, raw_dir=tmp_path)
    assert df.empty
    assert extract.FIREBALL_REQUIRED_FIELDS <= set(df.columns)


def test_fireball_aceita_nulos(tmp_path):
    s = FakeSession([ok(FIREBALL_PAYLOAD)])
    df = extract.fetch_fireball(s, raw_dir=tmp_path)
    assert df["lat"].isna().sum() == 1


def test_carga_incremental_usa_e_atualiza_estado(tmp_path):
    (tmp_path).mkdir(exist_ok=True)
    extract.save_state({"fireball_last_date": "2026-08-01 00:00:00"}, tmp_path / "state.json")
    s = FakeSession([ok(CAD_PAYLOAD), ok(FIREBALL_PAYLOAD)])
    extract.run_extraction(today=date(2026, 10, 31), data_dir=tmp_path, session=s)
    _, fb_kwargs = s.calls[1]
    assert fb_kwargs["params"]["date-min"] == "2026-08-01"
    assert extract.load_state(tmp_path / "state.json")["fireball_last_date"] == "2026-10-20 03:11:09"


def test_estado_corrompido_recomeca_sem_quebrar(tmp_path):
    (tmp_path / "state.json").write_text("{isso não é json")
    assert extract.load_state(tmp_path / "state.json") == {}


# ------------------------------------------------------- validação de schema
def test_campo_obrigatorio_ausente_falha():
    bad = json.loads(json.dumps(CAD_PAYLOAD))
    i = bad["fields"].index("dist")
    bad["fields"].pop(i)
    for row in bad["data"]:
        row.pop(i)
    with pytest.raises(extract.ExtractionError, match="ausentes"):
        extract.payload_to_dataframe(bad, extract.CAD_REQUIRED_FIELDS)


def test_linha_com_colunas_a_menos_falha():
    bad = json.loads(json.dumps(CAD_PAYLOAD))
    bad["data"][0] = bad["data"][0][:-1]
    with pytest.raises(extract.ExtractionError, match="Linha 0"):
        extract.payload_to_dataframe(bad, extract.CAD_REQUIRED_FIELDS)


def test_versao_nova_da_api_e_recusada():
    bad = json.loads(json.dumps(CAD_PAYLOAD))
    bad["signature"]["version"] = "2.0"
    with pytest.raises(extract.ExtractionError, match="Versão"):
        extract.payload_to_dataframe(bad, extract.CAD_REQUIRED_FIELDS)


def test_fonte_desconhecida_e_recusada():
    bad = json.loads(json.dumps(CAD_PAYLOAD))
    bad["signature"]["source"] = "Servidor Qualquer"
    with pytest.raises(extract.ExtractionError, match="Fonte"):
        extract.payload_to_dataframe(bad, extract.CAD_REQUIRED_FIELDS)


# ---------------------------------------------------------------- segurança
def test_sessao_mantem_verificacao_tls():
    assert extract.build_session().verify is True


def test_sessao_tem_retentativa_para_erros_temporarios():
    adapter = extract.build_session().get_adapter("https://ssd-api.jpl.nasa.gov")
    assert {429, 503} <= set(adapter.max_retries.status_forcelist)
    assert 400 not in adapter.max_retries.status_forcelist  # erro do cliente não repete


def test_base_url_e_https():
    assert extract.BASE_URL.startswith("https://")


def test_requisicao_usa_timeout_e_nao_segue_redirect(tmp_path):
    s = FakeSession([ok(FIREBALL_PAYLOAD)])
    extract.fetch_fireball(s, raw_dir=tmp_path)
    _, kwargs = s.calls[0]
    assert kwargs["timeout"] == extract.TIMEOUT
    assert kwargs["allow_redirects"] is False


def test_redirecionamento_e_recusado():
    resp = FakeResponse(status=302, headers={"Location": "https://evil.example"})
    with pytest.raises(extract.ExtractionError, match="Redirecionamento"):
        extract._get_json(FakeSession([resp]), "cad.api", {})
    assert resp.closed


def test_endpoint_fora_da_lista_e_recusado():
    with pytest.raises(ValueError):
        extract._get_json(FakeSession([]), "../../outra-coisa", {})


@pytest.mark.parametrize("valor", ["10LD&limit=999999", "0.05;DROP", "abc", "", "1e9", "10LD "])
def test_injecao_em_dist_max_e_recusada(valor, tmp_path):
    with pytest.raises(ValueError):
        extract.fetch_cad(FakeSession([]), date(2026, 1, 1), date(2026, 2, 1), dist_max=valor, raw_dir=tmp_path)


def test_datas_como_texto_sao_recusadas(tmp_path):
    with pytest.raises(ValueError):
        extract.fetch_cad(FakeSession([]), "2026-01-01&x=1", date(2026, 2, 1), raw_dir=tmp_path)


def test_http_de_erro_falha(tmp_path):
    with pytest.raises(extract.ExtractionError, match="HTTP 400"):
        extract._get_json(FakeSession([FakeResponse(status=400)]), "cad.api", {})


def test_content_type_nao_json_e_recusado():
    resp = FakeResponse(body=b"<html>erro</html>", content_type="text/html")
    with pytest.raises(extract.ExtractionError, match="Content-Type"):
        extract._get_json(FakeSession([resp]), "cad.api", {})


def test_json_malformado_e_recusado():
    resp = FakeResponse(body=b'{"signature": ')
    with pytest.raises(extract.ExtractionError, match="JSON"):
        extract._get_json(FakeSession([resp]), "cad.api", {})


def test_resposta_gigante_e_interrompida(monkeypatch):
    monkeypatch.setattr(extract, "MAX_RESPONSE_BYTES", 100)
    resp = FakeResponse(body=b"x" * 1000)
    with pytest.raises(extract.ExtractionError, match="limite"):
        extract._get_json(FakeSession([resp]), "cad.api", {})
    assert resp.closed


def test_content_length_declarado_gigante_e_recusado(monkeypatch):
    monkeypatch.setattr(extract, "MAX_RESPONSE_BYTES", 100)
    resp = FakeResponse(body=b"{}", headers={"Content-Length": "999999"})
    with pytest.raises(extract.ExtractionError, match="limite"):
        extract._get_json(FakeSession([resp]), "cad.api", {})


def test_save_raw_nao_aceita_nome_arbitrario(tmp_path):
    with pytest.raises(ValueError):
        extract.save_raw({}, "../../etc", tmp_path)
