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
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.placa import Placa
from src.compartilhado.interfaces.dependencies import obter_session
from src.main import criar_app
from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO
from src.ordem_servico.aplicacao.use_cases import (
    AbrirOrdem,
    CancelarOrdem,
    ConsultarAcompanhamento,
    ListarOrdens,
    ObterOrdem,
    RegistrarEntrega,
)
from src.ordem_servico.dominio.ordem_de_servico import TAMANHO_MAXIMO_DESCRICAO
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import ordem_em
from tests.unitarios.fakes import (
    ClientePortFake,
    ConsultaAcompanhamentoEspia,
    FakeUnitOfWork,
    RepoEmMemoria,
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
def client_como(
    repo: RepoEmMemoria, consulta: ConsultaAcompanhamentoEspia
) -> Iterator[Callable[[str], TestClient]]:
    """Fabrica de clientes com o papel pedido; os casos de uso usam os fakes."""
    fabricas = {
        "obter_abrir_ordem": AbrirOrdem(repo, FakeUnitOfWork(), ClientePortFake()),
        "obter_listar_ordens": ListarOrdens(repo),
        "obter_obter_ordem": ObterOrdem(repo),
        "obter_cancelar_ordem": CancelarOrdem(repo, FakeUnitOfWork()),
        "obter_registrar_entrega": RegistrarEntrega(repo, FakeUnitOfWork()),
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
        assert "historico" not in corpo

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

    @pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 101}, {"offset": -1}])
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
            (m["sequencia"], m["de"], m["para"], m["origem"]) for m in corpo["mudancas"]
        ] == [
            (1, None, "recebida", "atendimento"),
            (2, "recebida", "cancelada", "atendimento"),
        ]
        assert corpo["mudancas"][1]["motivo"] == "cliente desistiu"


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

    def test_conflito_de_versao_409(
        self, client: TestClient, repo: RepoEmMemoria
    ) -> None:
        ordem = ordem_em(StatusOrdem.RECEBIDA)
        repo.ordens[ordem.id] = ordem
        repo._conflito = True

        resp = client.post(f"{_BASE}/{ordem.id}/cancelamento", json={"motivo": "x"})

        assert resp.status_code == 409
        assert resp.json()["erro"]["codigo"] == "CONFLITO_DE_CONCORRENCIA"

    @pytest.mark.parametrize("corpo", [{}, {"motivo": ""}, {"motivo": "x", "y": 1}])
    def test_corpo_invalido_422(self, client: TestClient, corpo: dict) -> None:
        resp = client.post(f"{_BASE}/{uuid4()}/cancelamento", json=corpo)
        assert resp.status_code == 422


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

    def test_nao_encontrada_404_com_a_resposta_do_p3(self, client: TestClient) -> None:
        resp = client.post(self._ROTA, json=_CORPO_PUBLICO)

        assert resp.status_code == 404
        assert resp.json() == {"detail": "Ordem nao encontrada"}

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
        nao_encontrada = client.post(self._ROTA, json=_CORPO_PUBLICO)
        consulta.chamadas.clear()

        invalida = client.post(self._ROTA, json={**_CORPO_PUBLICO, **corpo})

        assert invalida.status_code == nao_encontrada.status_code == 404
        assert invalida.json() == nao_encontrada.json()
        assert consulta.chamadas == []

    def test_get_com_pii_na_url_nao_existe(self, client: TestClient) -> None:
        assert client.get(self._ROTA, params=_CORPO_PUBLICO).status_code == 405

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
