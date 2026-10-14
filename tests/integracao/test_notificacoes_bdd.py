"""Passos do BDD dos e-mails ao cliente (``tests/bdd/notificacoes_do_cliente.feature``).

O OS roda inteiro sobre o PostgreSQL de teste, com o barramento em memoria da
saga (``test_saga_atendimento``); o texto do e-mail sai do ``MontarEmail`` real,
sobre as mesmas tabelas, sem SMTP.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pytest_bdd import given, parsers, scenarios, then, when
from sqlalchemy import text

from src.cliente_veiculo.interfaces.dependencies import obter_excluir_dados
from src.ordem_servico.aplicacao.dtos import AbrirOrdemDTO
from src.ordem_servico.aplicacao.emails import Email, MontarEmail
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.adapters import ClienteSQLAlchemyAdapter
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from src.ordem_servico.interfaces.dependencies import (
    obter_abrir_ordem,
    obter_registrar_entrega,
)
from tests.integracao.seed_helpers import criar_cliente_com_veiculo
from tests.integracao.test_saga_atendimento import (
    _ATENDENTE,
    _CAMINHO_FELIZ,
    _RESPOSTA,
    Atendimento,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

scenarios("notificacoes_do_cliente.feature")

_CONTATO = "Maria Silva - maria.silva@cliente.com / (11) 99999-0000"


class Caixa:
    """Os pedidos de e-mail da OS e o e-mail montado do ultimo."""

    def __init__(self, atendimento: Atendimento) -> None:
        self._atendimento = atendimento
        self.antes = 0
        self.email: Email | None = None

    def pedidos(self) -> list[Any]:
        with self._atendimento.session_factory() as sessao:
            return list(
                sessao.execute(
                    text(
                        "SELECT ordem_id, modelo, status_da_ordem, motivo "
                        "FROM notificacoes WHERE ordem_id = :id ORDER BY id"
                    ),
                    {"id": self._atendimento.ordem_id},
                ).all()
            )


@given(
    "um cliente com e-mail e um veículo cadastrados",
    target_fixture="atendimento",
)
def _cliente_com_veiculo(session_factory: sessionmaker[Session]) -> Atendimento:
    atendimento = Atendimento(session_factory)
    with session_factory() as sessao:
        cliente = criar_cliente_com_veiculo(
            sessao, nome="Maria Silva", contato=_CONTATO
        )
        sessao.commit()
        atendimento.veiculo = (cliente.id, cliente.veiculos[0].id)
    return atendimento


@given("uma ordem de serviço aberta pelo atendente", target_fixture="caixa")
def _abrir(atendimento: Atendimento) -> Caixa:
    assert atendimento.veiculo is not None
    cliente_id, veiculo_id = atendimento.veiculo
    with atendimento.session_factory() as sessao:
        atendimento.ordem_id = (
            obter_abrir_ordem(sessao)
            .executar(
                AbrirOrdemDTO(
                    cliente_id=cliente_id,
                    veiculo_id=veiculo_id,
                    descricao_problema="Barulho na suspensao dianteira",
                    ator=_ATENDENTE,
                )
            )
            .id
        )
    atendimento.barramento.levar_a_outbox()
    return Caixa(atendimento)


@given(parsers.parse('os eventos do caminho feliz anteriores a "{evento}"'))
def _caminho_feliz_ate(atendimento: Atendimento, evento: str) -> None:
    for tipo in _CAMINHO_FELIZ[: _CAMINHO_FELIZ.index(evento)]:
        atendimento.publicar(tipo)


@given(parsers.parse('{participante} publica "{tipo}"'))
@when(parsers.parse('{participante} publica "{tipo}"'))
def _publicar(
    atendimento: Atendimento, caixa: Caixa, participante: str, tipo: str
) -> None:
    caixa.antes = len(caixa.pedidos())
    atendimento.publicar(tipo, participante)


@given("o atendente registra a entrega")
@when("o atendente registra a entrega")
def _entregar(atendimento: Atendimento) -> None:
    with atendimento.session_factory() as sessao:
        obter_registrar_entrega(sessao).executar(atendimento.ordem_id, ator=_ATENDENTE)


@when(parsers.parse('o atendente pede o cancelamento com o motivo "{motivo}"'))
def _cancelar(atendimento: Atendimento, caixa: Caixa, motivo: str) -> None:
    caixa.antes = len(caixa.pedidos())
    atendimento.cancelar(motivo)


@when("os participantes respondem a cada compensação")
def _responder_compensacoes(atendimento: Atendimento) -> None:
    while (pendente := atendimento.compensacao_pendente()) is not None:
        atendimento.publicar(_RESPOSTA[pendente])


@given("o cliente pediu a eliminação dos dados pessoais")
def _eliminar(atendimento: Atendimento) -> None:
    assert atendimento.veiculo is not None
    with atendimento.session_factory() as sessao:
        obter_excluir_dados(sessao).executar(atendimento.veiculo[0])


@when("o e-mail do último pedido é montado")
def _montar(atendimento: Atendimento, caixa: Caixa) -> None:
    ultimo = caixa.pedidos()[-1]
    with atendimento.session_factory() as sessao:
        caixa.email = MontarEmail(
            OrdemDeServicoSQLAlchemyRepository(sessao),
            ClienteSQLAlchemyAdapter(sessao),
        ).executar(
            Notificacao(
                ultimo.ordem_id,
                ModeloDeNotificacao(ultimo.modelo),
                StatusOrdem(ultimo.status_da_ordem),
                ultimo.motivo,
            )
        )


@then("os pedidos de e-mail, em ordem, são:")
def _pedidos(caixa: Caixa, datatable: list[list[str]]) -> None:
    _cabecalho, *linhas = datatable
    assert [(p.modelo, p.status_da_ordem, p.motivo or "") for p in caixa.pedidos()] == [
        tuple(linha) for linha in linhas
    ]


@then("nenhum pedido de e-mail novo foi gravado")
def _nenhum_pedido_novo(caixa: Caixa) -> None:
    assert len(caixa.pedidos()) == caixa.antes


@then(parsers.parse('o último pedido de e-mail é "{modelo}" com o motivo "{motivo}"'))
def _ultimo_pedido(caixa: Caixa, modelo: str, motivo: str) -> None:
    ultimo = caixa.pedidos()[-1]
    assert (ultimo.modelo, ultimo.status_da_ordem, ultimo.motivo) == (
        modelo,
        "cancelada",
        motivo,
    )


@then("nenhum pedido de e-mail guarda o texto do atendente")
def _sem_texto_livre(caixa: Caixa) -> None:
    assert "atendente" not in repr([tuple(p) for p in caixa.pedidos()])


@then(
    parsers.parse(
        'o e-mail do último pedido tem o assunto "{assunto}" e o texto "{texto}"'
    )
)
def _email(atendimento: Atendimento, caixa: Caixa, assunto: str, texto: str) -> None:
    _montar(atendimento, caixa)
    assert caixa.email is not None
    assert caixa.email.assunto.endswith(f": {assunto}")
    assert caixa.email.destinatario == "maria.silva@cliente.com"
    assert texto in caixa.email.corpo


@then("não há e-mail a enviar")
def _sem_email(caixa: Caixa) -> None:
    assert caixa.email is None
