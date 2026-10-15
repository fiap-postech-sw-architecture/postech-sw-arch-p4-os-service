"""Trace da API contra o PostgreSQL: o que a requisicao exporta e o que nao exporta.

O app e o real (lifespan, middlewares, JWT) e o provider dos testes e o de
producao, com o amostrador e a limpeza do texto de erro (``Rastreador``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from opentelemetry.trace import SpanKind, StatusCode
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.integracao.seed_helpers import SENHA_PADRAO
from tests.rastreamento import tudo_que_o_span_leva

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from sqlalchemy import Engine

    from src.autenticacao.dominio.usuario import Usuario
    from tests.rastreamento import Rastreador

_OS = "/api/v1/ordens-de-servico"
_PLACA = "BRA2E19"
_CPF = "529.982.247-25"


def _cabecalhos(api: TestClient, admin_user: Usuario) -> dict[str, str]:
    login = api.post(
        "/api/v1/autenticacao/login",
        json={"email": admin_user.email, "senha": SENHA_PADRAO},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _servidores(rastreador: Rastreador) -> list[object]:
    return [s for s in rastreador.spans() if s.kind is SpanKind.SERVER]


def test_requisicao_gera_span_server_com_os_comandos_de_banco_como_filhos(
    api_rastreada: TestClient, admin_user: Usuario, rastreador: Rastreador
) -> None:
    cabecalhos = _cabecalhos(api_rastreada, admin_user)
    rastreador_antes = len(rastreador.spans())

    resposta = api_rastreada.get(_OS, headers=cabecalhos)

    assert resposta.status_code == 200
    spans = rastreador.spans()[rastreador_antes:]
    (servidor,) = [s for s in spans if s.kind is SpanKind.SERVER]
    consultas = [s for s in spans if s.kind is SpanKind.CLIENT]
    assert servidor.name == f"GET {_OS}"
    assert any(
        "ordens_de_servico" in str(c.attributes.get("db.statement")) for c in consultas
    )
    assert {s.get_span_context().trace_id for s in spans} == {
        servidor.get_span_context().trace_id
    }
    assert {c.parent.span_id for c in consultas if c.parent} == {
        servidor.get_span_context().span_id
    }
    assert all(c.parent is not None for c in consultas)


@pytest.mark.parametrize(
    "caminho", ["/metrics", "/api/v1/saude", "/api/v1/saude/pronto"]
)
def test_raspagem_e_sondas_nao_exportam_span(
    api_rastreada: TestClient, rastreador: Rastreador, caminho: str
) -> None:
    # A readiness consulta o banco e o /metrics roda o coletor: os comandos de
    # banco deles, sem span em volta, sao descartados pelo amostrador.
    assert api_rastreada.get(caminho).status_code == 200

    assert rastreador.spans() == []


def test_post_com_corpo_gera_so_o_server_e_os_comandos_de_banco(
    api_rastreada: TestClient, admin_user: Usuario, rastreador: Rastreador
) -> None:
    # Sem `exclude_spans`, o POST com corpo ganha um span `http receive` e cada
    # resposta, um `http send`: no trace ficam o SERVER e as consultas, e mais nada.
    resposta = api_rastreada.post(
        "/api/v1/autenticacao/login",
        json={"email": admin_user.email, "senha": SENHA_PADRAO},
    )

    assert resposta.status_code == 200
    spans = rastreador.spans()
    servidores = [s for s in spans if s.kind is SpanKind.SERVER]
    consultas = [s for s in spans if s.kind is SpanKind.CLIENT]
    assert len(servidores) == 1
    assert len(spans) == len(servidores) + len(consultas)
    assert not [s for s in spans if s.kind is SpanKind.INTERNAL]


def test_span_da_requisicao_leva_o_template_e_nada_do_que_a_pessoa_enviou(
    api_rastreada: TestClient, admin_user: Usuario, rastreador: Rastreador
) -> None:
    cabecalhos = _cabecalhos(api_rastreada, admin_user)
    ordem_id = uuid4()
    antes = len(rastreador.spans())

    publica = api_rastreada.post(
        "/api/v1/publico/acompanhamento",
        json={"placa": _PLACA, "documento": _CPF},
        headers={"User-Agent": "Mozilla CANARIO-UA"},
    )
    privada = api_rastreada.get(
        f"{_OS}/{ordem_id}?documento={_CPF}",
        headers={**cabecalhos, "User-Agent": "Mozilla CANARIO-UA"},
    )

    assert (publica.status_code, privada.status_code) == (404, 404)
    novos = rastreador.spans()[antes:]
    nomes = {s.name for s in novos if s.kind is SpanKind.SERVER}
    assert nomes == {
        "POST /api/v1/publico/acompanhamento",
        f"GET {_OS}/{{ordem_id}}",
    }
    for span in novos:
        texto = tudo_que_o_span_leva(span)
        for canario in (_PLACA, _CPF, str(ordem_id), "CANARIO-UA", "testclient"):
            assert canario not in texto, f"{canario} em {span.name}"


def test_erro_nao_tratado_marca_o_span_sem_o_texto_da_excecao(
    api_rastreada: TestClient,
    admin_user: Usuario,
    rastreador: Rastreador,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cabecalhos = _cabecalhos(api_rastreada, admin_user)

    def estourar(_sessao: object) -> None:
        msg = f"placa {_PLACA} e CPF {_CPF} no texto da excecao"
        raise RuntimeError(msg)

    monkeypatch.setattr(
        "src.ordem_servico.interfaces.router.obter_listar_ordens", estourar
    )
    antes = len(rastreador.spans())

    resposta = api_rastreada.get(_OS, headers=cabecalhos)

    assert resposta.status_code == 500
    novos = rastreador.spans()[antes:]
    (servidor,) = [s for s in novos if s.kind is SpanKind.SERVER]
    assert servidor.status.status_code is StatusCode.ERROR
    assert servidor.status.description is None
    for span in novos:
        texto = tudo_que_o_span_leva(span)
        assert _PLACA not in texto
        assert _CPF not in texto


def test_erro_do_banco_nao_leva_o_detail_do_driver_ao_span(
    engine_rastreado: Engine, rastreador: Rastreador
) -> None:
    # O PostgreSQL poe o valor da linha no DETAIL do erro de UNIQUE; o
    # instrumentador do SQLAlchemy poe o texto do erro no status do span.
    with engine_rastreado.begin() as conexao:
        conexao.execute(text("CREATE TABLE tmp_placas (placa text UNIQUE)"))
    try:
        with (
            pytest.raises(IntegrityError) as erro,
            rastreador.tracer.start_as_current_span("POST /x", kind=SpanKind.SERVER),
            engine_rastreado.begin() as conexao,
        ):
            conexao.execute(
                text("INSERT INTO tmp_placas VALUES (:p), (:p)"), {"p": _PLACA}
            )
    finally:
        with engine_rastreado.begin() as conexao:
            conexao.execute(text("DROP TABLE tmp_placas"))

    # A precondicao do teste: o texto do erro do driver traz mesmo a placa.
    assert _PLACA in str(erro.value)
    comandos = [
        s
        for s in rastreador.spans()
        if str(s.attributes.get("db.statement", "")).startswith("INSERT")
    ]
    assert comandos
    assert all(c.status.status_code is StatusCode.ERROR for c in comandos)
    for span in rastreador.spans():
        assert span.status.description is None
        assert _PLACA not in tudo_que_o_span_leva(span)
