"""O texto de cada e-mail e o que fica de fora dele (RFC-004 secao 3; ADR-036)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from src.ordem_servico.aplicacao import emails as modulo_emails
from src.ordem_servico.aplicacao.dtos import ContatoDoCliente
from src.ordem_servico.aplicacao.emails import Email, MontarEmail, extrair_email
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.aplicacao.saga.compensacao import ROTULO_DO_MOTIVO
from src.ordem_servico.aplicacao.saga.modelo import MotivoCompensacao
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import (
    ATOR_ATENDENTE,
    CHECKOUT_URL,
    LINK_DECISAO,
    ordem_em,
    solicitar_pagamento,
)
from tests.unitarios.fakes import RepoEmMemoria

_S = StatusOrdem
_M = ModeloDeNotificacao


class _Clientes:
    def __init__(self, contato: ContatoDoCliente | None) -> None:
        self._contato = contato

    def obter_contato(self, cliente_id: UUID) -> ContatoDoCliente | None:
        return self._contato


def _montar(ordem, contato: str | None = "maria@cliente.com", nome: str = "Maria"):  # type: ignore[no-untyped-def]
    clientes = _Clientes(None if contato is None else ContatoDoCliente(nome, contato))
    return MontarEmail(RepoEmMemoria(ordem), clientes)


@pytest.mark.parametrize(
    ("contato", "endereco"),
    [
        pytest.param("maria@cliente.com", "maria@cliente.com", id="so-o-email"),
        pytest.param(
            "Maria - maria.silva+os@cliente.com.br / (11) 99999-0000",
            "maria.silva+os@cliente.com.br",
            id="texto-livre",
        ),
        pytest.param("(11) 99999-0000", None, id="so-telefone"),
        pytest.param("maria@localhost", None, id="sem-ponto-no-dominio"),
        pytest.param("maria@@cliente.com", None, id="arroba-dobrado"),
        pytest.param(
            "x@y.com\nBcc: alvo@outro.com", "x@y.com", id="nunca-injeta-cabecalho"
        ),
    ],
)
def test_extrai_o_primeiro_email_do_contato_livre(
    contato: str, endereco: str | None
) -> None:
    assert extrair_email(contato) == endereco


def test_situacao_diz_o_rotulo_do_status_do_pedido() -> None:
    ordem = ordem_em(_S.EM_EXECUCAO)
    # O rotulo e o do status no pedido, mesmo se a OS ja andou depois dele.
    pedido = Notificacao(ordem.id, _M.SITUACAO, _S.EM_DIAGNOSTICO)

    email = _montar(ordem).executar(pedido)

    assert email == Email(
        destinatario="maria@cliente.com",
        assunto=f"PytStop — Ordem de Serviço {str(ordem.id)[:8]}: Em diagnóstico",
        corpo=(
            "Olá, Maria!\n\n"
            f"A situação da sua ordem de serviço {str(ordem.id)[:8]} foi atualizada "
            "para: Em diagnóstico.\n\n"
            "Em caso de dúvida, fale com a oficina.\n\n"
            "Equipe PytStop\n"
        ),
    )


@pytest.mark.parametrize(
    ("nome", "saudacao"),
    [
        pytest.param(
            "Hostil\r\nBcc: alvo@outro.com Nome",
            "Olá, Hostil Bcc: alvo@outro.com Nome!",
            id="quebra-de-linha-nao-forja-linha-do-corpo",
        ),
        pytest.param(
            "Maria   da\tSilva \u2028 Souza", "Olá, Maria da Silva Souza!", id="espacos"
        ),
        pytest.param("A" * 255, "Olá, " + "A" * 80 + "!", id="tamanho-maximo"),
        pytest.param(" \r\n ", "Olá, cliente!", id="so-espacos"),
    ],
)
def test_saudacao_leva_o_nome_numa_linha_so_e_curto(nome: str, saudacao: str) -> None:
    ordem = ordem_em(_S.EM_EXECUCAO)

    email = _montar(ordem, nome=nome).executar(
        Notificacao(ordem.id, _M.SITUACAO, _S.EM_EXECUCAO)
    )

    assert email is not None
    assert email.corpo.splitlines()[0] == saudacao
    # O corpo tem so as linhas do modelo: nenhuma nasce do nome.
    assert len(email.corpo.splitlines()) == 7


def test_orcamento_leva_o_total_a_validade_e_o_link_de_decisao() -> None:
    ordem = ordem_em(_S.AGUARDANDO_APROVACAO)

    email = _montar(ordem).executar(
        Notificacao(ordem.id, _M.ORCAMENTO, _S.AGUARDANDO_APROVACAO)
    )

    assert email is not None
    assert email.assunto.endswith(": orçamento para a sua decisão")
    assert (
        "está pronto: R$ 350,00, válido até 13/10/2026 09:00 (horário de Brasília)."
        in email.corpo
    )
    assert f"Para aprovar ou recusar, acesse:\n{LINK_DECISAO}\n" in email.corpo


def test_pagamento_leva_o_valor_a_validade_e_o_checkout() -> None:
    ordem = ordem_em(_S.AGUARDANDO_PAGAMENTO)

    email = _montar(ordem).executar(
        Notificacao(ordem.id, _M.PAGAMENTO, _S.AGUARDANDO_PAGAMENTO)
    )

    assert email is not None
    assert email.assunto.endswith(": pagamento")
    assert (
        "pague R$ 350,00 pelo link abaixo, que vale até 07/10/2026 09:00 "
        f"(horário de Brasília):\n{CHECKOUT_URL}\n"
    ) in email.corpo


@pytest.mark.parametrize("motivo", list(MotivoCompensacao))
def test_cancelamento_diz_o_rotulo_do_codigo_e_nunca_o_texto_do_atendente(
    motivo: MotivoCompensacao,
) -> None:
    ordem = ordem_em(_S.AGUARDANDO_EXECUCAO)
    ordem.cancelar("Texto livre do atendente", OrigemMudanca.SAGA, ator=ATOR_ATENDENTE)

    email = _montar(ordem).executar(
        Notificacao(ordem.id, _M.CANCELAMENTO, _S.CANCELADA, motivo.value)
    )

    assert email is not None
    assert email.assunto.endswith(": cancelada")
    assert f"Motivo: {ROTULO_DO_MOTIVO[motivo]}." in email.corpo
    assert "Texto livre" not in email.corpo


def test_cancelamento_sem_codigo_nao_diz_motivo() -> None:
    ordem = ordem_em(_S.CANCELADA)

    email = _montar(ordem).executar(
        Notificacao(ordem.id, _M.CANCELAMENTO, _S.CANCELADA)
    )

    assert email is not None
    assert "foi cancelada." in email.corpo
    assert "Motivo" not in email.corpo


def test_o_e_mail_nao_leva_dado_da_ordem_alem_do_necessario() -> None:
    ordem = ordem_em(_S.EM_DIAGNOSTICO, descricao_problema="Barulho na suspensao")

    email = _montar(ordem).executar(
        Notificacao(ordem.id, _M.SITUACAO, _S.EM_DIAGNOSTICO)
    )

    assert email is not None
    assert "Barulho" not in email.corpo
    assert str(ordem.cliente_id) not in email.corpo + email.assunto
    assert str(ordem.veiculo_id) not in email.corpo + email.assunto


def test_o_repr_do_e_mail_nao_mostra_destinatario_nem_corpo() -> None:
    email = Email(destinatario="maria@cliente.com", assunto="Assunto", corpo="Segredo")

    assert "maria" not in repr(email)
    assert "Segredo" not in repr(email)


@pytest.mark.parametrize(
    ("modelo", "status_da_ordem", "contato", "motivo"),
    [
        pytest.param(
            _M.SITUACAO, None, "maria@cliente.com", "ordem_inexistente", id="sem-ordem"
        ),
        pytest.param(
            _M.SITUACAO,
            _S.EM_DIAGNOSTICO,
            None,
            "cliente_sem_contato",
            id="cliente-anonimizado",
        ),
        pytest.param(
            _M.SITUACAO,
            _S.EM_DIAGNOSTICO,
            "(11) 99999-0000",
            "contato_sem_email",
            id="contato-sem-email",
        ),
        pytest.param(
            _M.PAGAMENTO,
            _S.AGUARDANDO_APROVACAO,
            "maria@cliente.com",
            "sem_dados_do_modelo",
            id="sem-checkout",
        ),
        pytest.param(
            _M.ORCAMENTO,
            _S.EM_DIAGNOSTICO,
            "maria@cliente.com",
            "sem_dados_do_modelo",
            id="sem-orcamento",
        ),
    ],
)
def test_sem_o_que_entregar_devolve_nada_e_avisa_sem_dado_pessoal(
    modelo: ModeloDeNotificacao,
    status_da_ordem: StatusOrdem | None,
    contato: str | None,
    motivo: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Um logger ja usado numa suite maior guarda a configuracao da primeira vez:
    # o logger novo e o que o capture_logs enxerga.
    monkeypatch.setattr(modulo_emails, "_log", structlog.get_logger())
    # A OS nasce no teste: o agregado so existe depois do mapeamento do SQLAlchemy.
    ordem = None if status_da_ordem is None else ordem_em(status_da_ordem)
    ordem_id = uuid4() if ordem is None else ordem.id
    repo = RepoEmMemoria() if ordem is None else RepoEmMemoria(ordem)
    clientes = _Clientes(
        None if contato is None else ContatoDoCliente("Maria", contato)
    )

    with capture_logs() as logs:
        email = MontarEmail(repo, clientes).executar(
            Notificacao(ordem_id, modelo, _S.EM_DIAGNOSTICO)
        )

    assert email is None
    assert logs == [
        {
            "event": "notification skipped",
            "log_level": "warning",
            "correlation_id": str(ordem_id),
            "modelo": modelo.value,
            "motivo": motivo,
        }
    ]


def test_checkout_so_existe_depois_de_solicitado() -> None:
    ordem = ordem_em(_S.AGUARDANDO_APROVACAO)
    ordem.registrar_pecas_reservadas(ator="consumidor")
    assert (
        _montar(ordem).executar(
            Notificacao(ordem.id, _M.PAGAMENTO, _S.AGUARDANDO_PAGAMENTO)
        )
        is None
    )

    solicitar_pagamento(ordem)

    assert (
        _montar(ordem).executar(
            Notificacao(ordem.id, _M.PAGAMENTO, _S.AGUARDANDO_PAGAMENTO)
        )
        is not None
    )
