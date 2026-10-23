"""Os erros que o OpenAPI declara em registrar, login, logout, refresh e clientes.

Um caso para cada status que o OpenAPI passou a declarar nessas rotas, contra o app
real e o PostgreSQL. Cada caso confere o status e o ``codigo`` do envelope, e que a
declaracao da rota nomeia esse ``codigo`` (no exemplo ou na descricao): o contrato
diz o que a API faz. Que o OpenAPI declare o status e conferido em todo teste de API
pela fixture ``respostas_vistas``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from src.autenticacao.dominio.papel import Papel
from src.main import app
from tests.integracao.seed_helpers import (
    SENHA_PADRAO,
    cpf_unico,
    criar_usuario,
    placa_unica,
)

if TYPE_CHECKING:
    import httpx
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session, sessionmaker

    from src.autenticacao.dominio.usuario import Usuario

_AUTH = "/api/v1/autenticacao"
_CLIENTES = "/api/v1/clientes"
_CLIENTE = _CLIENTES + "/{cliente_id}"
_INEXISTENTE = "6f1b8e4a-5c1d-4c53-9a43-1b2f6c7d8e90"
_VEICULO = {"placa": "ABC1D23", "marca": "Fiat", "modelo": "Uno", "ano": 2020}


def _confere(
    resposta: httpx.Response, metodo: str, rota: str, status: int, codigo: str
) -> None:
    """A API respondeu ``status`` com ``codigo`` e o OpenAPI nomeia esse ``codigo``."""
    assert (resposta.status_code, resposta.json()["erro"]["codigo"]) == (status, codigo)
    declarada = app.openapi()["paths"][rota][metodo.lower()]["responses"][str(status)]
    exemplo = declarada["content"]["application/json"]["example"]["erro"]["codigo"]
    assert codigo == exemplo or f"`{codigo}`" in declarada["description"], (
        f"{metodo} {rota} -> {status}: o OpenAPI nao nomeia {codigo}"
    )


def _entrar(api_client: TestClient, email: str) -> dict[str, str]:
    resposta = api_client.post(
        f"{_AUTH}/login", json={"email": email, "senha": SENHA_PADRAO}
    )
    assert resposta.status_code == 200
    return {"Authorization": f"Bearer {resposta.json()['access_token']}"}


@pytest.fixture
def admin(api_client: TestClient, admin_user: Usuario) -> dict[str, str]:
    return _entrar(api_client, admin_user.email)


def _cliente(
    api_client: TestClient,
    admin: dict[str, str],
    veiculo: dict[str, Any] | None = None,
) -> str:
    """Cadastra um cliente (CPF novo) e devolve o id; com ``veiculo``, anexa um."""
    corpo = {
        "nome": "Maria Silva",
        "documento": cpf_unico(),
        "tipo_documento": "cpf",
        "contato": "maria@exemplo.com",
    }
    criado = api_client.post(_CLIENTES, headers=admin, json=corpo)
    assert criado.status_code == 201
    cliente_id: str = criado.json()["id"]
    if veiculo is not None:
        anexado = api_client.post(
            f"{_CLIENTES}/{cliente_id}/veiculos", headers=admin, json=veiculo
        )
        assert anexado.status_code == 201
    return cliente_id


class TestRegistrar:
    _ROTA = f"{_AUTH}/registrar"
    _CORPO: ClassVar[dict[str, str]] = {
        "email": "novo@test.com",
        "senha": SENHA_PADRAO,
        "papel": "mecanico",
    }

    def test_sem_token_401(self, api_client: TestClient) -> None:
        resposta = api_client.post(self._ROTA, json=self._CORPO)

        _confere(resposta, "POST", self._ROTA, 401, "NAO_AUTENTICADO")

    def test_atendente_403(
        self, api_client: TestClient, session_factory: sessionmaker[Session]
    ) -> None:
        criar_usuario(session_factory, email="atend@test.com", papel=Papel.ATENDENTE)

        resposta = api_client.post(
            self._ROTA, json=self._CORPO, headers=_entrar(api_client, "atend@test.com")
        )

        _confere(resposta, "POST", self._ROTA, 403, "ACESSO_NEGADO")

    def test_email_repetido_409(
        self, api_client: TestClient, admin: dict[str, str]
    ) -> None:
        primeiro = api_client.post(self._ROTA, json=self._CORPO, headers=admin)
        assert primeiro.status_code == 201

        repetido = api_client.post(self._ROTA, json=self._CORPO, headers=admin)

        _confere(repetido, "POST", self._ROTA, 409, "ENTIDADE_DUPLICADA")


class TestLimitePorRota:
    """A (limite + 1)-esima chamada do minuto responde 429, na rota que a declara."""

    def test_login(self, api_client: TestClient) -> None:
        rota = f"{_AUTH}/login"
        corpo = {"email": "ninguem@test.com", "senha": SENHA_PADRAO}

        respostas = [api_client.post(rota, json=corpo) for _ in range(6)]

        assert [r.status_code for r in respostas] == [401] * 5 + [429]
        _confere(respostas[-1], "POST", rota, 429, "RATE_LIMIT_EXCEDIDO")
        assert "5 per 1 minute" in respostas[-1].json()["erro"]["mensagem"]

    def test_registrar(self, api_client: TestClient, admin: dict[str, str]) -> None:
        rota = f"{_AUTH}/registrar"
        corpo = {"email": "novo@test.com", "senha": SENHA_PADRAO, "papel": "mecanico"}

        respostas = [api_client.post(rota, json=corpo, headers=admin) for _ in range(6)]

        assert [r.status_code for r in respostas] == [201] + [409] * 4 + [429]
        _confere(respostas[-1], "POST", rota, 429, "RATE_LIMIT_EXCEDIDO")

    def test_logout(self, api_client: TestClient, admin: dict[str, str]) -> None:
        rota = f"{_AUTH}/logout"

        respostas = [api_client.post(rota, headers=admin) for _ in range(11)]

        assert [r.status_code for r in respostas] == [200] * 10 + [429]
        _confere(respostas[-1], "POST", rota, 429, "RATE_LIMIT_EXCEDIDO")
        assert "10 per 1 minute" in respostas[-1].json()["erro"]["mensagem"]

    def test_refresh(self, api_client: TestClient) -> None:
        rota = f"{_AUTH}/refresh"

        respostas = [
            api_client.post(rota, json={"refresh_token": "x"}) for _ in range(11)
        ]

        assert [r.status_code for r in respostas] == [401] * 10 + [429]
        _confere(respostas[-1], "POST", rota, 429, "RATE_LIMIT_EXCEDIDO")


# Cada rota de cliente (e as filhas) responde 404 para o cliente que nao existe.
_COM_CLIENTE_INEXISTENTE: list[tuple[str, str, dict[str, Any]]] = [
    ("GET", _CLIENTE, {}),
    ("PUT", _CLIENTE, {"json": {"nome": "Maria", "contato": "m@exemplo.com"}}),
    ("DELETE", _CLIENTE, {}),
    ("POST", _CLIENTE + "/veiculos", {"json": _VEICULO}),
    ("GET", _CLIENTE + "/veiculos", {}),
    ("DELETE", _CLIENTE + "/veiculos/{veiculo_id}", {}),
    ("GET", _CLIENTE + "/dados-pessoais", {}),
    ("GET", _CLIENTE + "/dados-pessoais/exportar", {}),
    ("DELETE", _CLIENTE + "/dados-pessoais", {}),
    ("POST", _CLIENTE + "/consentimento", {"json": {"tipo": "marketing"}}),
    ("DELETE", _CLIENTE + "/consentimento", {"params": {"tipo": "marketing"}}),
]


class TestClienteInexistente:
    @pytest.mark.parametrize(
        ("metodo", "rota", "requisicao"),
        [pytest.param(m, r, q, id=f"{m} {r}") for m, r, q in _COM_CLIENTE_INEXISTENTE],
    )
    def test_404(
        self,
        api_client: TestClient,
        admin: dict[str, str],
        metodo: str,
        rota: str,
        requisicao: dict[str, Any],
    ) -> None:
        caminho = rota.format(cliente_id=_INEXISTENTE, veiculo_id=_INEXISTENTE)

        resposta = api_client.request(metodo, caminho, headers=admin, **requisicao)

        _confere(resposta, metodo, rota, 404, "ENTIDADE_NAO_ENCONTRADA")


class TestRegrasDoCliente:
    @pytest.mark.parametrize("desativado", [False, True], ids=["ativo", "desativado"])
    def test_documento_ja_cadastrado_409(
        self, api_client: TestClient, admin: dict[str, str], *, desativado: bool
    ) -> None:
        corpo = {
            "nome": "Maria Silva",
            "documento": cpf_unico(),
            "tipo_documento": "cpf",
            "contato": "maria@exemplo.com",
        }
        criado = api_client.post(_CLIENTES, headers=admin, json=corpo)
        assert criado.status_code == 201
        if desativado:  # a desativacao nao libera o documento
            apagado = api_client.delete(
                f"{_CLIENTES}/{criado.json()['id']}", headers=admin
            )
            assert apagado.status_code == 204

        repetido = api_client.post(_CLIENTES, headers=admin, json=corpo)

        _confere(repetido, "POST", _CLIENTES, 409, "ENTIDADE_DUPLICADA")

    def test_placa_ja_cadastrada_409(
        self, api_client: TestClient, admin: dict[str, str]
    ) -> None:
        placa = {**_VEICULO, "placa": placa_unica()}
        _cliente(api_client, admin, placa)
        outro = _cliente(api_client, admin)

        resposta = api_client.post(
            f"{_CLIENTES}/{outro}/veiculos", headers=admin, json=placa
        )

        _confere(resposta, "POST", _CLIENTE + "/veiculos", 409, "ENTIDADE_DUPLICADA")

    def test_cliente_desativado_nao_muda_409(
        self, api_client: TestClient, admin: dict[str, str]
    ) -> None:
        veiculo = {**_VEICULO, "placa": placa_unica()}
        cliente_id = _cliente(api_client, admin, veiculo)
        base = f"{_CLIENTES}/{cliente_id}"
        assert api_client.delete(base, headers=admin).status_code == 204
        veiculo_id = api_client.get(f"{base}/veiculos", headers=admin).json()[0]["id"]
        regra = "VIOLACAO_REGRA_NEGOCIO"

        atualizado = api_client.put(
            base, headers=admin, json={"nome": "Outro", "contato": "o@exemplo.com"}
        )
        com_veiculo = api_client.post(
            f"{base}/veiculos", headers=admin, json={**veiculo, "placa": placa_unica()}
        )
        sem_veiculo = api_client.delete(f"{base}/veiculos/{veiculo_id}", headers=admin)

        _confere(atualizado, "PUT", _CLIENTE, 409, regra)
        _confere(com_veiculo, "POST", _CLIENTE + "/veiculos", 409, regra)
        _confere(sem_veiculo, "DELETE", _CLIENTE + "/veiculos/{veiculo_id}", 409, regra)
