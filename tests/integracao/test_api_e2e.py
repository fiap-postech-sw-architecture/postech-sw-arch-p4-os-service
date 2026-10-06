"""API ponta a ponta contra o app real (lifespan, middlewares, JWT) e Postgres.

Cobre o que mocks nao pegam: wiring da session no lifespan, mapeamento real,
envelope de erro, commit real com outbox e o filtro de OS ativa entre os
contextos de OS e de Cliente+Veiculo.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import httpx
import jwt
import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from scripts.validar_token import validar_access_token
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.chaves_jwt import (
    OUTRA_CHAVE,
    assinar,
    claims,
    forjar_hmac_com_a_chave_publica,
)
from tests.fabricas import FLUXO, aplicar_fato
from tests.integracao.seed_helpers import SENHA_PADRAO, criar_usuario

if TYPE_CHECKING:
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session, sessionmaker

    from src.autenticacao.dominio.usuario import Usuario

_OS = "/api/v1/ordens-de-servico"


def _login(api_client: TestClient, email: str) -> dict[str, str]:
    resp = api_client.post(
        "/api/v1/autenticacao/login", json={"email": email, "senha": SENHA_PADRAO}
    )
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _cliente_com_veiculo(
    api_client: TestClient,
    headers: dict[str, str],
    *,
    documento: str,
    placa: str,
) -> tuple[str, str]:
    cliente = api_client.post(
        "/api/v1/clientes",
        headers=headers,
        json={
            "nome": "Maria Silva",
            "documento": documento,
            "tipo_documento": "cpf",
            "contato": "maria@exemplo.com",
        },
    )
    assert cliente.status_code == 201
    veiculo = api_client.post(
        f"/api/v1/clientes/{cliente.json()['id']}/veiculos",
        headers=headers,
        json={"placa": placa, "marca": "Fiat", "modelo": "Uno", "ano": 2020},
    )
    assert veiculo.status_code == 201
    return cliente.json()["id"], veiculo.json()["id"]


def _abrir(
    api_client: TestClient, headers: dict[str, str], cliente_id: str, veiculo_id: str
) -> dict[str, object]:
    resp = api_client.post(
        _OS,
        headers=headers,
        json={
            "cliente_id": cliente_id,
            "veiculo_id": veiculo_id,
            "descricao_problema": "Motor falhando",
        },
    )
    assert resp.status_code == 201
    return resp.json()


def _levar_ate(
    session_factory: sessionmaker[Session], ordem_id: str, status: StatusOrdem
) -> None:
    """Aplica os fatos da saga direto no dominio (a mensageria vem depois)."""
    with session_factory() as sess:
        repo = OrdemDeServicoSQLAlchemyRepository(sess)
        ordem = repo.obter_por_id(UUID(ordem_id))
        assert ordem is not None
        for proximo in FLUXO[FLUXO.index(ordem.status) + 1 : FLUXO.index(status) + 1]:
            aplicar_fato(ordem, proximo)
            repo.salvar(ordem)
        sess.commit()


class TestProbes:
    def test_liveness_e_readiness_com_o_banco_real(
        self, api_client: TestClient
    ) -> None:
        assert api_client.get("/api/v1/saude").json() == {"status": "ok"}
        pronto = api_client.get("/api/v1/saude/pronto")
        assert pronto.status_code == 200
        assert pronto.json() == {"status": "ok"}


class TestAutenticacao:
    def test_login_e_rotas_protegidas(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        resp = api_client.post(
            "/api/v1/autenticacao/login",
            json={"email": admin_user.email, "senha": "senha-errada-123"},
        )
        assert resp.status_code == 401
        assert api_client.get(_OS).status_code == 401
        invalido = {"Authorization": "Bearer token-invalido"}
        assert api_client.get(_OS, headers=invalido).status_code == 401
        headers = _login(api_client, admin_user.email)
        assert api_client.get(_OS, headers=headers).status_code == 200


class TestCicloDaOrdem:
    def test_abrir_consultar_listar_historico_e_cancelar(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="ABC1D23"
        )

        ordem = _abrir(api_client, headers, cliente_id, veiculo_id)
        ordem_id = ordem["id"]
        assert (ordem["status"], ordem["versao"]) == ("recebida", 1)

        detalhe = api_client.get(f"{_OS}/{ordem_id}", headers=headers).json()
        assert detalhe["descricao_problema"] == "Motor falhando"
        lista = api_client.get(_OS, headers=headers).json()
        assert [i["id"] for i in lista["items"]] == [ordem_id]
        assert lista["total"] == 1

        cancelada = api_client.post(
            f"{_OS}/{ordem_id}/cancelamento",
            headers=headers,
            json={"motivo": "cliente desistiu"},
        )
        assert cancelada.status_code == 200
        assert cancelada.json()["versao"] == 2

        historico = api_client.get(f"{_OS}/{ordem_id}/historico", headers=headers)
        assert [
            (m["de"], m["para"], m["origem"], m["motivo"])
            for m in historico.json()["mudancas"]
        ] == [
            (None, "recebida", "atendimento", None),
            ("recebida", "cancelada", "atendimento", "cliente desistiu"),
        ]

        de_novo = api_client.post(
            f"{_OS}/{ordem_id}/cancelamento", headers=headers, json={"motivo": "x"}
        )
        assert de_novo.status_code == 409
        assert de_novo.json()["erro"]["codigo"] == "TRANSICAO_STATUS_INVALIDA"
        # Encerrada sai da fila padrao e volta com incluir_encerradas.
        padrao = api_client.get(_OS, headers=headers).json()
        assert (padrao["items"], padrao["total"]) == ([], 0)
        completa = api_client.get(
            _OS, headers=headers, params={"incluir_encerradas": "true"}
        ).json()
        assert [i["id"] for i in completa["items"]] == [ordem_id]
        assert completa["total"] == 1

    def test_fatos_da_saga_ate_a_entrega(
        self,
        api_client: TestClient,
        admin_user: Usuario,
        session_factory: sessionmaker[Session],
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="52998224725", placa="DEF4G56"
        )
        ordem_id = str(_abrir(api_client, headers, cliente_id, veiculo_id)["id"])

        _levar_ate(session_factory, ordem_id, StatusOrdem.EM_EXECUCAO)
        tarde = api_client.post(
            f"{_OS}/{ordem_id}/cancelamento", headers=headers, json={"motivo": "x"}
        )
        assert tarde.status_code == 409  # pivot: execucao ja comecou

        _levar_ate(session_factory, ordem_id, StatusOrdem.FINALIZADA)
        entregue = api_client.post(f"{_OS}/{ordem_id}/entrega", headers=headers)
        assert entregue.status_code == 200
        corpo = entregue.json()
        assert corpo["situacao"] == "Entregue"
        assert corpo["orcamento"]["total"] == "350.00"
        assert corpo["pagamento"]["status"] == "solicitado"

        mudancas = api_client.get(
            f"{_OS}/{ordem_id}/historico", headers=headers
        ).json()["mudancas"]
        assert [m["para"] for m in mudancas] == [s.value for s in FLUXO]

    def test_abrir_rejeita_veiculo_de_outro_cliente(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_a, _ = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="AAA1A11"
        )
        _, veiculo_b = _cliente_com_veiculo(
            api_client, headers, documento="57648016648", placa="BBB2B22"
        )

        resp = api_client.post(
            _OS,
            headers=headers,
            json={
                "cliente_id": cliente_a,
                "veiculo_id": veiculo_b,
                "descricao_problema": "x",
            },
        )

        assert resp.status_code == 404
        assert resp.json()["erro"]["mensagem"] == (
            f"Veiculo {veiculo_b} nao encontrado para o cliente informado"
        )

    def test_mecanico_nao_acessa_a_os(
        self,
        api_client: TestClient,
        admin_user: Usuario,
        session_factory: sessionmaker[Session],
    ) -> None:
        from src.autenticacao.dominio.papel import Papel

        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="MEC1A23"
        )
        ordem_id = _abrir(api_client, headers, cliente_id, veiculo_id)["id"]
        mecanico = criar_usuario(
            session_factory, email="mecanico@test.com", papel=Papel.MECANICO
        )
        h_mecanico = _login(api_client, mecanico.email)

        for url in (_OS, f"{_OS}/{ordem_id}", f"{_OS}/{ordem_id}/historico"):
            negado = api_client.get(url, headers=h_mecanico)
            assert negado.status_code == 403
            assert negado.json()["erro"]["codigo"] == "ACESSO_NEGADO"
        resp = api_client.post(
            f"{_OS}/{ordem_id}/cancelamento", headers=h_mecanico, json={"motivo": "x"}
        )
        assert resp.status_code == 403


class TestOutboxViaApi:
    def test_abertura_e_cancelamento_gravam_eventos_no_mesmo_commit(
        self,
        api_client: TestClient,
        admin_user: Usuario,
        session_factory: sessionmaker[Session],
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="OBX1A23"
        )
        ordem_id = _abrir(api_client, headers, cliente_id, veiculo_id)["id"]
        api_client.post(
            f"{_OS}/{ordem_id}/cancelamento", headers=headers, json={"motivo": "x"}
        )

        with session_factory() as sess:
            linhas = sess.execute(
                text(
                    "SELECT tipo, payload FROM outbox "
                    "WHERE agregado_id = :id ORDER BY id"
                ),
                {"id": ordem_id},
            ).all()
        assert [linha.tipo for linha in linhas] == [
            "OrdemAbertaEvent",
            "StatusDaOrdemAlteradoEvent",
        ]
        assert linhas[1].payload["status_novo"] == "cancelada"
        assert "motivo" not in linhas[1].payload


class TestAcompanhamentoPublico:
    _ROTA = "/api/v1/publico/acompanhamento"

    def test_par_valido_e_respostas_identicas_para_par_invalido(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="52998224725", placa="PUB1A23"
        )
        _abrir(api_client, headers, cliente_id, veiculo_id)

        achada = api_client.post(
            self._ROTA, json={"placa": "pub-1a23", "documento": "529.982.247-25"}
        )
        assert achada.status_code == 200
        assert achada.json()["situacao"] == "Recebida"

        mesmo_id = {"X-Request-ID": "acompanhamento-e2e"}
        placa_errada = api_client.post(
            self._ROTA,
            json={"placa": "ZZZ9Z99", "documento": "52998224725"},
            headers=mesmo_id,
        )
        documento_errado = api_client.post(
            self._ROTA,
            json={"placa": "PUB1A23", "documento": "11144477735"},
            headers=mesmo_id,
        )
        # Anti-enumeracao: mesmo status e mesmo corpo, no envelope de erro.
        assert placa_errada.status_code == documento_errado.status_code == 404
        assert placa_errada.content == documento_errado.content
        assert placa_errada.json() == {
            "erro": {
                "codigo": "ENTIDADE_NAO_ENCONTRADA",
                "mensagem": "Ordem nao encontrada",
                "id_requisicao": "acompanhamento-e2e",
            }
        }

    def test_documento_ou_placa_invalidos_dao_o_404_sem_ir_ao_banco(
        self, api_client: TestClient
    ) -> None:
        comandos: list[str] = []

        def _contar(*args: object) -> None:
            comandos.append(str(args[2]))

        mesmo_id = {"X-Request-ID": "acompanhamento-invalido"}
        nao_encontrada = api_client.post(
            self._ROTA,
            json={"placa": "ZZZ9Z99", "documento": "52998224725"},
            headers=mesmo_id,
        )
        event.listen(Engine, "before_cursor_execute", _contar)
        try:
            respostas = [
                api_client.post(self._ROTA, json=corpo, headers=mesmo_id)
                for corpo in (
                    {"placa": "PUB1A23", "documento": "529.982.247-26"},
                    {"placa": "PUB1A23", "documento": "11.111.111/0001-11"},
                    {"placa": "!!!!!!!", "documento": "52998224725"},
                )
            ]
        finally:
            event.remove(Engine, "before_cursor_execute", _contar)

        assert comandos == []
        for resposta in respostas:
            assert resposta.status_code == nao_encontrada.status_code == 404
            assert resposta.content == nao_encontrada.content


class TestFalhaDeCredencialUniforme:
    """ADR-039: toda falha de credencial responde 401 com a mesma mensagem."""

    def test_gate_responde_o_mesmo_401_para_qualquer_falha(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        valido = claims(sub=str(admin_user.id))
        expirado = assinar({**valido, "exp": datetime.now(UTC) - timedelta(1)})
        # Assinatura, type e exp validos; o claim `papel` e que nao e de quem
        # emite aqui (ausente, desconhecido ou de tipo errado).
        papel_invalido = [
            assinar(corpo)
            for corpo in (
                {k: v for k, v in valido.items() if k != "papel"},
                {**valido, "papel": "cliente"},
                {**valido, "papel": 42},
            )
        ]
        login = api_client.post(
            "/api/v1/autenticacao/login",
            json={"email": admin_user.email, "senha": SENHA_PADRAO},
        ).json()
        revogado = login["access_token"]
        sair = api_client.post(
            "/api/v1/autenticacao/logout",
            headers={"Authorization": f"Bearer {revogado}"},
        )
        assert sair.status_code == 200

        respostas = [
            api_client.get(_OS, headers=headers)
            for headers in (
                {},
                {"Authorization": "Bearer lixo"},
                {"Authorization": f"Bearer {expirado}"},
                {"Authorization": f"Bearer {forjar_hmac_com_a_chave_publica()}"},
                {"Authorization": f"Bearer {assinar(valido, chave=OUTRA_CHAVE)}"},
                {"Authorization": f"Bearer {login['refresh_token']}"},
                {"Authorization": f"Bearer {revogado}"},
                *({"Authorization": f"Bearer {token}"} for token in papel_invalido),
            )
        ]

        for resposta in respostas:
            assert resposta.status_code == 401
            assert resposta.json() == {
                "erro": {
                    "codigo": "NAO_AUTENTICADO",
                    "mensagem": "Credencial ausente, invalida ou expirada",
                    "id_requisicao": resposta.headers["X-Request-ID"],
                }
            }
            assert resposta.headers["WWW-Authenticate"] == "Bearer"

    def test_login_e_refresh_usam_a_mesma_mensagem(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        senha_errada = api_client.post(
            "/api/v1/autenticacao/login",
            json={"email": admin_user.email, "senha": "outra-senha-qualquer"},
        )
        email_desconhecido = api_client.post(
            "/api/v1/autenticacao/login",
            json={"email": "ninguem@test.com", "senha": SENHA_PADRAO},
        )
        refresh_invalido = api_client.post(
            "/api/v1/autenticacao/refresh", json={"refresh_token": "lixo"}
        )

        for resposta in (senha_errada, email_desconhecido, refresh_invalido):
            assert resposta.status_code == 401
            assert resposta.json()["erro"]["codigo"] == "NAO_AUTENTICADO"
            assert (
                resposta.json()["erro"]["mensagem"]
                == "Credencial ausente, invalida ou expirada"
            )


class TestTokenValidadoPeloJwks:
    """ADR-039: Billing e Execucao validam o token so com o JWKS publico."""

    def test_access_token_do_login_vale_no_validador_independente(
        self, url_base_da_app: str, admin_user: Usuario
    ) -> None:
        login = httpx.post(
            f"{url_base_da_app}/api/v1/autenticacao/login",
            json={"email": admin_user.email, "senha": SENHA_PADRAO},
            timeout=10,
        )
        assert login.status_code == 200
        tokens = login.json()

        resultado = validar_access_token(url_base_da_app, tokens["access_token"])

        assert (resultado["sub"], resultado["papel"]) == (str(admin_user.id), "admin")
        assert "email" not in resultado
        # O refresh tem a mesma assinatura, mas nao passa como access.
        with pytest.raises(jwt.InvalidTokenError, match="access"):
            validar_access_token(url_base_da_app, tokens["refresh_token"])


class TestCadastroComDocumentoInvalido:
    def test_dv_errado_da_422_sem_ecoar_o_numero(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        resp = api_client.post(
            "/api/v1/clientes",
            headers=_login(api_client, admin_user.email),
            json={
                "nome": "Maria Silva",
                "documento": "529.982.247-26",
                "tipo_documento": "cpf",
                "contato": "maria@exemplo.com",
            },
        )

        assert resp.status_code == 422
        assert resp.json()["erro"]["codigo"] == "VALOR_INVALIDO"
        assert resp.json()["erro"]["mensagem"] == "CPF invalido"
        assert "247" not in resp.text


class TestRegrasEntreContextos:
    def test_cliente_com_os_ativa_nao_desativa_ate_encerrar(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="ATV1A23"
        )
        ordem_id = _abrir(api_client, headers, cliente_id, veiculo_id)["id"]

        bloqueado = api_client.delete(f"/api/v1/clientes/{cliente_id}", headers=headers)
        assert bloqueado.status_code == 409
        erasure = api_client.delete(
            f"/api/v1/clientes/{cliente_id}/dados-pessoais", headers=headers
        )
        assert erasure.status_code == 409

        api_client.post(
            f"{_OS}/{ordem_id}/cancelamento", headers=headers, json={"motivo": "x"}
        )
        liberado = api_client.delete(f"/api/v1/clientes/{cliente_id}", headers=headers)
        assert liberado.status_code == 204

    def test_erasure_anonimiza_o_texto_livre_das_os(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="LGP1A23"
        )
        resp = api_client.post(
            _OS,
            headers=headers,
            json={
                "cliente_id": cliente_id,
                "veiculo_id": veiculo_id,
                "descricao_problema": "Cliente Maria, tel 11 99999-0000, motor",
            },
        )
        ordem_id = resp.json()["id"]
        api_client.post(
            f"{_OS}/{ordem_id}/cancelamento",
            headers=headers,
            json={"motivo": "Maria ligou do 11 99999-0000 desistindo"},
        )

        erasure = api_client.delete(
            f"/api/v1/clientes/{cliente_id}/dados-pessoais", headers=headers
        )

        assert erasure.status_code == 204
        ordem = api_client.get(f"{_OS}/{ordem_id}", headers=headers).json()
        assert ordem["descricao_problema"] == "ANONIMIZADO"
        assert ordem["motivo_cancelamento"] == "ANONIMIZADO"
        assert ordem["versao"] == 3  # abertura, cancelamento e erasure
        mudancas = api_client.get(
            f"{_OS}/{ordem_id}/historico", headers=headers
        ).json()["mudancas"]
        # A linha do tempo continua inteira; so o texto livre sai.
        assert [(m["para"], m["motivo"]) for m in mudancas] == [
            ("recebida", None),
            ("cancelada", "ANONIMIZADO"),
        ]

    def test_veiculo_com_qualquer_os_nao_e_removido(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="VEI1A23"
        )
        ordem_id = _abrir(api_client, headers, cliente_id, veiculo_id)["id"]
        api_client.post(
            f"{_OS}/{ordem_id}/cancelamento", headers=headers, json={"motivo": "x"}
        )

        resp = api_client.delete(
            f"/api/v1/clientes/{cliente_id}/veiculos/{veiculo_id}", headers=headers
        )

        assert resp.status_code == 409
        assert resp.json()["erro"]["mensagem"] == (
            "Veiculo possui ordem de servico vinculada e nao pode ser removido"
        )


class TestConsentimentoEPortabilidade:
    """Ciclo LGPD de consentimento e export pela app real (repositorio e DI)."""

    def test_conceder_revogar_e_conceder_de_novo(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, _ = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="LGP1D23"
        )
        url = f"/api/v1/clientes/{cliente_id}/consentimento"

        concedido = api_client.post(url, headers=headers, json={"tipo": "Marketing"})
        assert concedido.status_code == 201
        assert (concedido.json()["tipo"], concedido.json()["ativo"]) == (
            "marketing",
            True,
        )
        assert (
            api_client.post(
                url, headers=headers, json={"tipo": "marketing"}
            ).status_code
            == 409
        )

        revogado = api_client.delete(url, headers=headers, params={"tipo": "MARKETING"})
        assert revogado.status_code == 204

        # Depois da revogacao vale o registro mais recente: concede de novo e o
        # novo fica ativo (um segundo pedido volta a dar 409), e a revogacao
        # alcanca esse novo registro, nao o antigo ja revogado.
        assert (
            api_client.post(
                url, headers=headers, json={"tipo": "marketing"}
            ).status_code
            == 201
        )
        assert (
            api_client.post(
                url, headers=headers, json={"tipo": "marketing"}
            ).status_code
            == 409
        )
        assert (
            api_client.delete(
                url, headers=headers, params={"tipo": "marketing"}
            ).status_code
            == 204
        )

    def test_revogar_sem_consentimento_404(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, _ = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="LGP2D34"
        )
        resp = api_client.delete(
            f"/api/v1/clientes/{cliente_id}/consentimento",
            headers=headers,
            params={"tipo": "marketing"},
        )
        assert resp.status_code == 404

    def test_exportar_dados_pessoais(
        self, api_client: TestClient, admin_user: Usuario
    ) -> None:
        headers = _login(api_client, admin_user.email)
        cliente_id, veiculo_id = _cliente_com_veiculo(
            api_client, headers, documento="21249722519", placa="EXP1D23"
        )

        resp = api_client.get(
            f"/api/v1/clientes/{cliente_id}/dados-pessoais/exportar", headers=headers
        )

        assert resp.status_code == 200
        dados = resp.json()
        assert dados["id"] == cliente_id
        assert dados["nome"] == "Maria Silva"
        assert dados["documento_formatado"] == "212.497.225-19"
        assert dados["contato"] == "maria@exemplo.com"
        assert [v["id"] for v in dados["veiculos"]] == [veiculo_id]
        assert dados["veiculos"][0]["placa"] == "EXP1D23"
