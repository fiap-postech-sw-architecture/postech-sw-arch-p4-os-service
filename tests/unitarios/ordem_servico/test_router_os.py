"""Rotas de OS sobre o app real (middlewares e error handlers), com os casos
de uso reais ligados a repositorio em memoria — sem banco."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import structlog.testing
from fastapi.testclient import TestClient

from src.autenticacao.interfaces.middleware import obter_usuario_atual
from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.placa import Placa
from src.compartilhado.interfaces.dependencies import obter_session
from src.main import criar_app
from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO
from src.ordem_servico.aplicacao.saga.modelo import Envio
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.use_cases import (
    CANCELAMENTO_INDISPONIVEL,
    AbrirOrdem,
    CancelarOrdem,
    ConsultarAcompanhamento,
    ListarOrdens,
    ObterOrdem,
    ObterSaga,
    RegistrarEntrega,
)
from src.ordem_servico.dominio.ordem_de_servico import (
    TAMANHO_MAXIMO_DESCRICAO,
    TAMANHO_MAXIMO_MOTIVO,
)
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import ATOR_ATENDENTE, ordem_em
from tests.unitarios.fakes import (
    ClientePortFake,
    ConsultaAcompanhamentoEspia,
    ConsultaDaOrdemEmMemoria,
    FakeUnitOfWork,
    RepoEmMemoria,
    SagasEmMemoria,
)

_BASE = "/api/v1/ordens-de-servico"
_ROUTER = "src.ordem_servico.interfaces.router"


@pytest.fixture
def repo() -> RepoEmMemoria:
    return RepoEmMemoria()


@pytest.fixture
def consulta() -> ConsultaAcompanhamentoEspia:
    return ConsultaAcompanhamentoEspia()


@pytest.fixture
def sagas() -> SagasEmMemoria:
    return SagasEmMemoria()


@pytest.fixture
def client_como(
    repo: RepoEmMemoria, sagas: SagasEmMemoria, consulta: ConsultaAcompanhamentoEspia
) -> Iterator[Callable[[str], TestClient]]:
    """Fabrica de clientes com o papel pedido; os casos de uso usam os fakes."""
    fabricas = {
        "obter_abrir_ordem": AbrirOrdem(
            repo, FakeUnitOfWork(), ClientePortFake(), sagas
        ),
        "obter_listar_ordens": ListarOrdens(repo),
        "obter_obter_ordem": ObterOrdem(ConsultaDaOrdemEmMemoria(repo, sagas)),
        "obter_obter_saga": ObterSaga(sagas),
        "obter_cancelar_ordem": CancelarOrdem(repo, FakeUnitOfWork(), sagas),
        "obter_registrar_entrega": RegistrarEntrega(repo, FakeUnitOfWork(), sagas),
        "obter_consultar_acompanhamento": ConsultarAcompanhamento(consulta),
    }

    def _como(papel: str) -> TestClient:
        app = criar_app()
        app.dependency_overrides[obter_session] = lambda: MagicMock()
        app.dependency_overrides[obter_usuario_atual] = lambda: {
            "sub": "u-123",
            "papel": papel,
            "type": "access",
        }
        return TestClient(app)

    with ExitStack() as pilha:
        for nome, caso_de_uso in fabricas.items():
            pilha.enter_context(patch(f"{_ROUTER}.{nome}", return_value=caso_de_uso))
        yield _como


@pytest.fixture
def client(client_como: Callable[[str], TestClient]) -> TestClient:
    return client_como("atendente")


def _abrir_corpo(**extra: object) -> dict[str, object]:
    return {
        "cliente_id": str(uuid4()),
        "veiculo_id": str(uuid4()),
        "descricao_problema": "Luz do motor acesa",
        **extra,
    }


class TestAbrir:
    def test_201_com_a_projecao(self, client: TestClient) -> None:
        resp = client.post(_BASE, json=_abrir_corpo())

        assert resp.status_code == 201
        corpo = resp.json()
        assert corpo["status"] == "recebida"
        assert corpo["situacao"] == "Recebida"
        assert corpo["descricao_problema"] == "Luz do motor acesa"
        assert corpo["orcamento"] is None
        assert corpo["pagamento"] is None
        assert corpo["versao"] == 1
        assert corpo["etapa"] == "aguardando_diagnostico"
        assert "historico" not in corpo
        assert "passos" not in corpo

    @pytest.mark.parametrize(
        "corpo",
        [
            {"cliente_id": str(uuid4()), "veiculo_id": str(uuid4())},
            _abrir_corpo(descricao_problema=""),
            _abrir_corpo(descricao_problema="x" * (TAMANHO_MAXIMO_DESCRICAO + 1)),
            _abrir_corpo(itens=[]),
            _abrir_corpo(cliente_id="nao-e-uuid"),
        ],
        ids=["sem-descricao", "vazia", "longa", "campo-extra", "uuid-invalido"],
    )
    def test_corpo_invalido_422(self, client: TestClient, corpo: dict) -> None:
        assert client.post(_BASE, json=corpo).status_code == 422

    def test_descricao_so_espacos_422_no_envelope(self, client: TestClient) -> None:
        resp = client.post(_BASE, json=_abrir_corpo(descricao_problema="   "))

        assert resp.status_code == 422
        assert resp.json()["erro"]["codigo"] == "VALOR_INVALIDO"

    def test_descricao_com_nul_422_sem_mensagem_do_driver(
        self, client: TestClient
    ) -> None:
        resp = client.post(_BASE, json=_abrir_corpo(descricao_problema="freio\x00"))

        assert resp.status_code == 422
        assert resp.json()["erro"]["codigo"] == "VALOR_INVALIDO"
        assert resp.json()["erro"]["mensagem"] == (
            "descricao do problema tem caractere de controle"
        )

    def test_admin_abre(self, client_como: Callable[[str], TestClient]) -> None:
        resp = client_como("admin").post(_BASE, json=_abrir_corpo())
        assert resp.status_code == 201

    def test_descricao_no_tamanho_maximo_201(self, client: TestClient) -> None:
        corpo = _abrir_corpo(descricao_problema="x" * TAMANHO_MAXIMO_DESCRICAO)
        assert client.post(_BASE, json=corpo).status_code == 201


_ROTAS_DE_OS = [
    pytest.param("POST", "", _abrir_corpo(), id="abrir"),
    pytest.param("GET", "", None, id="listar"),
    pytest.param("GET", "/{id}", None, id="obter"),
    pytest.param("GET", "/{id}/historico", None, id="historico"),
    pytest.param("POST", "/{id}/cancelamento", {"motivo": "x"}, id="cancelar"),
    pytest.param("POST", "/{id}/entrega", None, id="entregar"),
]


class TestPapeis:
    """Toda rota de OS e do atendente (admin herda); o mecanico nao tem nenhuma."""

    @pytest.mark.parametrize(("metodo", "sufixo", "corpo"), _ROTAS_DE_OS)
    def test_mecanico_recebe_403_em_toda_rota(
        self,
        client_como: Callable[[str], TestClient],
        repo: RepoEmMemoria,
        metodo: str,
        sufixo: str,
        corpo: dict[str, object] | None,
    ) -> None:
        ordem = ordem_em(StatusOrdem.FINALIZADA)
        repo.ordens[ordem.id] = ordem
        url = _BASE + sufixo.format(id=ordem.id)

        resp = client_como("mecanico").request(metodo, url, json=corpo)

        assert resp.status_code == 403
        assert resp.json()["erro"]["codigo"] == "ACESSO_NEGADO"
        assert repo.salvas == []


class TestConsultas:
    def test_lista_paginada(self, client: TestClient, repo: RepoEmMemoria) -> None:
        ordem = ordem_em(StatusOrdem.EM_EXECUCAO)
        repo.ordens[ordem.id] = ordem

        resp = client.get(_BASE, params={"offset": 0, "limit": 10})

        assert resp.status_code == 200
        corpo = resp.json()
        assert corpo["total"] == 42
        assert (corpo["offset"], corpo["limit"]) == (0, 10)
        (item,) = corpo["items"]
        assert item["id"] == str(ordem.id)
        assert item["situacao"] == "Em execução"
        assert repo.args_listar == (0, 10, False)
        assert repo.args_contar is False

    def test_incluir_encerradas_chega_a_listagem_e_ao_total(
        self, client: TestClient, repo: RepoEmMemoria
    ) -> None:
        resp = client.get(
            _BASE, params={"offset": 0, "limit": 10, "incluir_encerradas": "true"}
        )

        assert resp.status_code == 200
        assert repo.args_listar == (0, 10, True)
        assert repo.args_contar is True

    @pytest.mark.parametrize(
        "params",
        [
            pytest.param({"limit": 0}, id="limit-zero"),
            pytest.param({"limit": 101}, id="limit-acima-de-100"),
            pytest.param({"offset": -1}, id="offset-negativo"),
        ],
    )
    def test_paginacao_invalida_422(self, client: TestClient, params: dict) -> None:
        assert client.get(_BASE, params=params).status_code == 422

    def test_obter_inclui_resumos(
        self, client: TestClient, repo: RepoEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.AGUARDANDO_PAGAMENTO)
        repo.ordens[ordem.id] = ordem

        resp = client.get(f"{_BASE}/{ordem.id}")

        assert resp.status_code == 200
        corpo = resp.json()
        assert corpo["situacao"] == "Aguardando pagamento"
        assert corpo["orcamento"]["total"] == "350.00"
        assert corpo["orcamento"]["moeda"] == "BRL"
        assert corpo["orcamento"]["valido_ate"] == "2026-10-13T12:00:00Z"
        assert corpo["pagamento"]["status"] == "solicitado"
        assert (corpo["pagamento"]["valor"], corpo["pagamento"]["moeda"]) == (
            "350.00",
            "BRL",
        )
        assert corpo["pagamento"]["expira_em"] == "2026-10-07T12:00:00Z"

    def test_obter_inexistente_404_no_envelope(self, client: TestClient) -> None:
        resp = client.get(f"{_BASE}/{uuid4()}")

        assert resp.status_code == 404
        erro = resp.json()["erro"]
        assert erro["codigo"] == "ENTIDADE_NAO_ENCONTRADA"
        assert set(erro) == {"codigo", "mensagem", "id_requisicao"}

    def test_historico(self, client: TestClient, repo: RepoEmMemoria) -> None:
        ordem = ordem_em(StatusOrdem.CANCELADA)
        repo.ordens[ordem.id] = ordem

        resp = client.get(f"{_BASE}/{ordem.id}/historico")

        assert resp.status_code == 200
        corpo = resp.json()
        assert corpo["ordem_id"] == str(ordem.id)
        assert [
            (m["sequencia"], m["de"], m["para"], m["origem"], m["ator"])
            for m in corpo["mudancas"]
        ] == [
            (1, None, "recebida", "atendimento", ATOR_ATENDENTE),
            (2, "recebida", "cancelada", "atendimento", ATOR_ATENDENTE),
        ]
        assert corpo["mudancas"][1]["motivo"] == "cliente desistiu"
        assert corpo["passos"] == []

    def test_historico_e_obter_trazem_a_saga(
        self, client: TestClient, repo: RepoEmMemoria, sagas: SagasEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.RECEBIDA)
        repo.ordens[ordem.id] = ordem
        comando_id = uuid4()
        sagas.sagas[ordem.id] = Saga.iniciar(
            ordem.id,
            envio=Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=comando_id),
            ator=ATOR_ATENDENTE,
            agora=datetime(2026, 10, 7, 12, tzinfo=UTC),
        )

        historico = client.get(f"{_BASE}/{ordem.id}/historico").json()
        detalhe = client.get(f"{_BASE}/{ordem.id}").json()

        assert historico["passos"] == [
            {
                "seq": 1,
                "em": "2026-10-07T12:00:00Z",
                "de": None,
                "para": "aguardando_diagnostico",
                "gatilho": "abertura",
                "mensagem_id": None,
                "comando": "SolicitarDiagnostico",
                "comando_id": str(comando_id),
                "motivo": None,
                "ator": ATOR_ATENDENTE,
                "posicao_na_fila": None,
            }
        ]
        assert detalhe["etapa"] == "aguardando_diagnostico"


class TestCancelamento:
    def test_cancela_e_audita_o_ator(
        self,
        client: TestClient,
        repo: RepoEmMemoria,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Logger novo: o do modulo pode estar cacheado por um
        # configurar_logging() anterior e escaparia do capture_logs.
        monkeypatch.setattr(f"{_ROUTER}._log", structlog.get_logger())
        ordem = ordem_em(StatusOrdem.EM_DIAGNOSTICO)
        repo.ordens[ordem.id] = ordem

        with structlog.testing.capture_logs() as logs:
            resp = client.post(
                f"{_BASE}/{ordem.id}/cancelamento", json={"motivo": "desistiu"}
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelada"
        assert resp.json()["motivo_cancelamento"] == "desistiu"
        assert {
            "event": "order_cancelled_via_api",
            "ordem_id": str(ordem.id),
            "ator": "u-123",
            "log_level": "info",
        } in logs

    @pytest.mark.parametrize("estado", [StatusOrdem.EM_EXECUCAO, StatusOrdem.ENTREGUE])
    def test_depois_do_pivot_409(
        self, client: TestClient, repo: RepoEmMemoria, estado: StatusOrdem
    ) -> None:
        ordem = ordem_em(estado)
        repo.ordens[ordem.id] = ordem

        resp = client.post(f"{_BASE}/{ordem.id}/cancelamento", json={"motivo": "x"})

        assert resp.status_code == 409
        assert resp.json()["erro"]["codigo"] == "TRANSICAO_STATUS_INVALIDA"

    def test_com_a_saga_em_andamento_409_sem_cancelar(
        self, client: TestClient, repo: RepoEmMemoria, sagas: SagasEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.EM_DIAGNOSTICO)
        repo.ordens[ordem.id] = ordem
        sagas.sagas[ordem.id] = Saga.iniciar(
            ordem.id,
            envio=Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=uuid4()),
            ator=ATOR_ATENDENTE,
            agora=ordem.criado_em,
        )

        resp = client.post(f"{_BASE}/{ordem.id}/cancelamento", json={"motivo": "x"})

        assert resp.status_code == 409
        assert resp.json()["erro"]["codigo"] == "TRANSICAO_STATUS_INVALIDA"
        assert resp.json()["erro"]["mensagem"] == CANCELAMENTO_INDISPONIVEL
        assert ordem.status is StatusOrdem.EM_DIAGNOSTICO

    def test_conflito_de_versao_409(
        self, client: TestClient, repo: RepoEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.RECEBIDA)
        repo.ordens[ordem.id] = ordem
        repo.provocar_conflito()

        resp = client.post(f"{_BASE}/{ordem.id}/cancelamento", json={"motivo": "x"})

        assert resp.status_code == 409
        assert resp.json()["erro"]["codigo"] == "CONFLITO_DE_CONCORRENCIA"

    @pytest.mark.parametrize(
        "corpo",
        [
            pytest.param({}, id="sem-motivo"),
            pytest.param({"motivo": ""}, id="motivo-vazio"),
            pytest.param({"motivo": "x" * (TAMANHO_MAXIMO_MOTIVO + 1)}, id="longo"),
            pytest.param({"motivo": "x", "y": 1}, id="campo-extra"),
        ],
    )
    def test_corpo_invalido_422(self, client: TestClient, corpo: dict) -> None:
        resp = client.post(f"{_BASE}/{uuid4()}/cancelamento", json=corpo)
        assert resp.status_code == 422

    def test_motivo_no_tamanho_maximo_cancela(
        self, client: TestClient, repo: RepoEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.RECEBIDA)
        repo.ordens[ordem.id] = ordem

        resp = client.post(
            f"{_BASE}/{ordem.id}/cancelamento",
            json={"motivo": "x" * TAMANHO_MAXIMO_MOTIVO},
        )

        assert resp.status_code == 200


class TestEntrega:
    def test_finalizada_vira_entregue(
        self, client: TestClient, repo: RepoEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.FINALIZADA)
        repo.ordens[ordem.id] = ordem

        resp = client.post(f"{_BASE}/{ordem.id}/entrega")

        assert resp.status_code == 200
        assert resp.json()["situacao"] == "Entregue"

    def test_fora_de_finalizada_409(
        self, client: TestClient, repo: RepoEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.EM_EXECUCAO)
        repo.ordens[ordem.id] = ordem

        assert client.post(f"{_BASE}/{ordem.id}/entrega").status_code == 409


def test_sem_token_401(repo: RepoEmMemoria) -> None:
    app = criar_app()
    app.dependency_overrides[obter_session] = lambda: MagicMock()
    resp = TestClient(app).get(_BASE)
    assert resp.status_code == 401
    assert resp.json()["erro"]["codigo"] == "NAO_AUTENTICADO"


_CORPO_PUBLICO = {"placa": "ABC1D23", "documento": "529.982.247-25"}


class TestAcompanhamentoPublico:
    _ROTA = "/api/v1/publico/acompanhamento"

    def test_encontrada_so_status_e_timestamps(
        self, client: TestClient, consulta: ConsultaAcompanhamentoEspia
    ) -> None:
        consulta.resultado = AcompanhamentoDTO(
            status="em_diagnostico",
            criado_em=datetime(2026, 10, 1, 12, tzinfo=UTC),
            atualizado_em=datetime(2026, 10, 1, 13, tzinfo=UTC),
        )

        resp = client.post(self._ROTA, json=_CORPO_PUBLICO)

        assert resp.status_code == 200
        assert set(resp.json()) == {"status", "situacao", "criado_em", "atualizado_em"}
        assert resp.json()["situacao"] == "Em diagnóstico"
        assert consulta.chamadas == [
            (Placa(valor="ABC1D23"), CPF(numero="52998224725"))
        ]

    def test_nao_encontrada_404_no_envelope_de_erro(self, client: TestClient) -> None:
        resp = client.post(self._ROTA, json=_CORPO_PUBLICO)

        assert resp.status_code == 404
        assert resp.json() == {
            "erro": {
                "codigo": "ENTIDADE_NAO_ENCONTRADA",
                "mensagem": "Ordem nao encontrada",
                "id_requisicao": resp.headers["X-Request-ID"],
            }
        }

    @pytest.mark.parametrize(
        "corpo",
        [
            pytest.param(
                {**_CORPO_PUBLICO, "documento": "529.982.247-26"}, id="cpf-dv"
            ),
            pytest.param(
                {**_CORPO_PUBLICO, "documento": "11111111111"}, id="cpf-iguais"
            ),
            pytest.param({**_CORPO_PUBLICO, "placa": "!!!!!!!"}, id="placa-simbolos"),
        ],
    )
    def test_documento_ou_placa_invalidos_dao_o_mesmo_404_sem_consultar(
        self,
        client: TestClient,
        consulta: ConsultaAcompanhamentoEspia,
        corpo: dict[str, str],
    ) -> None:
        # Mesmo X-Request-ID nas duas: o corpo inteiro tem de ser identico.
        mesmo_id = {"X-Request-ID": "acompanhamento-1"}
        nao_encontrada = client.post(self._ROTA, json=_CORPO_PUBLICO, headers=mesmo_id)
        consulta.chamadas.clear()

        invalida = client.post(
            self._ROTA, json={**_CORPO_PUBLICO, **corpo}, headers=mesmo_id
        )

        assert invalida.status_code == nao_encontrada.status_code == 404
        assert invalida.content == nao_encontrada.content
        assert consulta.chamadas == []

    @pytest.mark.parametrize(
        ("campo", "valor", "esperado"),
        [
            pytest.param("placa", "ABC1D2", 422, id="placa-6"),
            pytest.param("placa", "ABC1D23", 404, id="placa-7"),
            pytest.param("placa", "ABC-1234", 404, id="placa-8-com-hifen"),
            pytest.param("placa", "ABC-12345", 422, id="placa-9"),
            pytest.param("documento", "5299822472", 422, id="documento-10"),
            pytest.param("documento", "52998224725", 404, id="documento-11"),
            pytest.param("documento", "11.222.333/0001-81", 404, id="documento-18"),
            pytest.param("documento", "11.222.333/0001-810", 422, id="documento-19"),
        ],
    )
    def test_fronteiras_de_tamanho(
        self,
        client: TestClient,
        consulta: ConsultaAcompanhamentoEspia,
        campo: str,
        valor: str,
        esperado: int,
    ) -> None:
        resp = client.post(self._ROTA, json={**_CORPO_PUBLICO, campo: valor})

        assert resp.status_code == esperado
        # Dentro do tamanho e com DV/formato validos, a consulta acontece.
        assert len(consulta.chamadas) == (1 if esperado == 404 else 0)

    def test_get_com_pii_na_url_nao_existe(self, client: TestClient) -> None:
        resp = client.get(self._ROTA, params=_CORPO_PUBLICO)
        assert resp.status_code == 405
        assert resp.json()["erro"]["codigo"] == "METODO_NAO_PERMITIDO"
        assert resp.headers["Allow"] == "POST"

    @pytest.mark.parametrize(
        "corpo",
        [
            None,
            {"placa": "AB1", "documento": "52998224725"},
            {"placa": "ABC1D23", "documento": "123"},
            {**_CORPO_PUBLICO, "extra": 1},
        ],
        ids=["sem-corpo", "placa-curta", "documento-curto", "campo-extra"],
    )
    def test_corpo_invalido_422(self, client: TestClient, corpo: dict | None) -> None:
        assert client.post(self._ROTA, json=corpo).status_code == 422

    def test_rate_limit_10_por_minuto(self, client: TestClient) -> None:
        for _ in range(10):
            assert client.post(self._ROTA, json=_CORPO_PUBLICO).status_code == 404
        resp = client.post(self._ROTA, json=_CORPO_PUBLICO)
        assert resp.status_code == 429
        assert resp.json()["erro"]["codigo"] == "RATE_LIMIT_EXCEDIDO"


def test_openapi_documenta_401_403_e_404_das_rotas_de_os() -> None:
    caminhos = TestClient(criar_app()).get("/openapi.json").json()["paths"]
    for rota, metodo in [
        ("/api/v1/ordens-de-servico/{ordem_id}", "get"),
        ("/api/v1/ordens-de-servico/{ordem_id}/historico", "get"),
        ("/api/v1/ordens-de-servico/{ordem_id}/cancelamento", "post"),
        ("/api/v1/ordens-de-servico/{ordem_id}/entrega", "post"),
    ]:
        assert {"401", "403", "404"} <= set(caminhos[rota][metodo]["responses"])
    assert {"401", "403"} <= set(
        caminhos["/api/v1/ordens-de-servico"]["get"]["responses"]
    )
    assert {"401", "403"} <= set(caminhos["/api/v1/clientes"]["post"]["responses"])


class TestSaga:
    """``GET /api/v1/sagas/{ordem_id}``: so o admin, para a operacao."""

    def test_admin_consulta_o_estado(
        self,
        client_como: Callable[[str], TestClient],
        sagas: SagasEmMemoria,
    ) -> None:
        ordem_id = uuid4()
        sagas.sagas[ordem_id] = Saga.iniciar(
            ordem_id,
            envio=Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=uuid4()),
            ator=ATOR_ATENDENTE,
            agora=datetime(2026, 10, 7, 12, tzinfo=UTC),
        )

        resp = client_como("admin").get(f"/api/v1/sagas/{ordem_id}")

        assert resp.status_code == 200
        corpo = resp.json()
        assert {k: v for k, v in corpo.items() if k != "passos"} == {
            "ordem_id": str(ordem_id),
            "etapa": "aguardando_diagnostico",
            "motivo": None,
            "falha": None,
            "plano_compensacao": [],
            "comando_em_voo": None,
            "reenvios": 0,
            "prazo_resposta_em": None,
        }
        assert [p["gatilho"] for p in corpo["passos"]] == ["abertura"]

    @pytest.mark.parametrize("papel", ["atendente", "mecanico"])
    def test_outros_papeis_recebem_403(
        self, client_como: Callable[[str], TestClient], papel: str
    ) -> None:
        resp = client_como(papel).get(f"/api/v1/sagas/{uuid4()}")

        assert resp.status_code == 403
        assert resp.json()["erro"]["codigo"] == "ACESSO_NEGADO"

    def test_ordem_sem_saga_404_no_envelope(
        self, client_como: Callable[[str], TestClient]
    ) -> None:
        resp = client_como("admin").get(f"/api/v1/sagas/{uuid4()}")

        assert resp.status_code == 404
        assert resp.json()["erro"]["codigo"] == "ENTIDADE_NAO_ENCONTRADA"
