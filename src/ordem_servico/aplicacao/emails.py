"""Os e-mails ao cliente: o texto de cada modelo, montado na hora da entrega.

O pedido (``Notificacao``) so diz qual OS e qual modelo. Aqui a OS e o cadastro
do cliente completam o texto: o nome, o endereco (extraido do ``contato``, que
e texto livre), o total e o link de decisao do orcamento, o link do checkout.
Sao dados pessoais e links com token: vivem so no e-mail enviado, nunca em log,
em span nem em metrica. O texto leva so o que o cliente precisa para agir.

Nada a entregar (OS sem o resumo que o modelo pede, cliente anonimizado ou sem
e-mail no contato) nao e falha: retentar nao resolve, entao a linha e dada como
entregue, sem envio, com um aviso no log (RFC-004 secao 3).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import TYPE_CHECKING, Final

import structlog

from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao
from src.ordem_servico.aplicacao.saga.compensacao import ROTULO_DO_MOTIVO
from src.ordem_servico.aplicacao.saga.modelo import MotivoCompensacao
from src.ordem_servico.aplicacao.situacoes import situacao_de

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from src.compartilhado.dominio.dinheiro import Dinheiro
    from src.ordem_servico.aplicacao.notificacoes import Notificacao
    from src.ordem_servico.aplicacao.ports import ClientePort
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
    from src.ordem_servico.dominio.repository import OrdemDeServicoRepository

_log = structlog.get_logger(__name__)

# Forma minima local@dominio.tld; extrai o primeiro candidato do texto livre do
# contato ("Maria - maria@x.com / (11) 9..."). O dominio e casado label a label
# (`.` fora da classe, so como separador) para nao dar backtracking polinomial;
# o contato tem no maximo 255 caracteres (VO ``Contato``). A classe de
# caracteres nao admite espaco nem quebra de linha: o endereco nunca injeta
# cabecalho.
_EMAIL: Final = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
# Horario de Brasilia (UTC-3, sem horario de verao desde 2019), sem depender do
# banco de fusos da imagem.
_BRASILIA: Final = timezone(timedelta(hours=-3), "BRT")


@dataclass(frozen=True, slots=True)
class Email:
    """Um e-mail pronto para o SMTP; destinatario e corpo sao dados pessoais."""

    destinatario: str = field(repr=False)
    assunto: str
    corpo: str = field(repr=False)


def extrair_email(contato: str) -> str | None:
    """O primeiro e-mail do campo livre de contato, ou ``None``."""
    achado = _EMAIL.search(contato)
    return None if achado is None else achado.group()


def _dinheiro(valor: Dinheiro) -> str:
    return f"R$ {valor.valor:.2f}".replace(".", ",")


def _instante(quando: datetime) -> str:
    return quando.astimezone(_BRASILIA).strftime("%d/%m/%Y %H:%M (horário de Brasília)")


def _corpo(nome: str, *paragrafos: str) -> str:
    return "\n\n".join([f"Olá, {nome}!", *paragrafos, "Equipe PytStop"]) + "\n"


type _Texto = tuple[str, list[str]]


def _situacao(notificacao: Notificacao, ordem: OrdemDeServico, curto: str) -> _Texto:
    situacao = situacao_de(notificacao.status)
    return situacao, [
        f"A situação da sua ordem de serviço {curto} foi atualizada para: {situacao}.",
        "Em caso de dúvida, fale com a oficina.",
    ]


def _orcamento(
    notificacao: Notificacao, ordem: OrdemDeServico, curto: str
) -> _Texto | None:
    resumo = ordem.resumo_orcamento
    if resumo is None:
        return None
    return "orçamento para a sua decisão", [
        f"O orçamento da sua ordem de serviço {curto} está pronto: "
        f"{_dinheiro(resumo.total)}, válido até {_instante(resumo.valido_ate)}.",
        f"Para aprovar ou recusar, acesse:\n{resumo.link_decisao}",
    ]


def _pagamento(
    notificacao: Notificacao, ordem: OrdemDeServico, curto: str
) -> _Texto | None:
    resumo = ordem.resumo_pagamento
    if resumo is None:
        return None
    return "pagamento", [
        f"Para seguir com o serviço da ordem {curto}, pague "
        f"{_dinheiro(resumo.valor)} pelo link abaixo, que vale até "
        f"{_instante(resumo.expira_em)}:\n{resumo.checkout_url}",
    ]


def _cancelamento(
    notificacao: Notificacao, ordem: OrdemDeServico, curto: str
) -> _Texto:
    paragrafos = [f"A sua ordem de serviço {curto} foi cancelada."]
    if notificacao.motivo is not None:
        # O rotulo do codigo da compensacao: nunca o texto que o atendente digitou.
        rotulo = ROTULO_DO_MOTIVO[MotivoCompensacao(notificacao.motivo)]
        paragrafos.append(f"Motivo: {rotulo}.")
    return "cancelada", paragrafos


_TEXTOS: Final[
    Mapping[
        ModeloDeNotificacao, Callable[[Notificacao, OrdemDeServico, str], _Texto | None]
    ]
] = {
    ModeloDeNotificacao.SITUACAO: _situacao,
    ModeloDeNotificacao.ORCAMENTO: _orcamento,
    ModeloDeNotificacao.PAGAMENTO: _pagamento,
    ModeloDeNotificacao.CANCELAMENTO: _cancelamento,
}


class MontarEmail:
    """Do pedido ao e-mail: le a OS e o contato do cliente e escreve o texto.

    Le e nao grava; quem chama fecha a transacao de leitura antes do envio, para
    nao segurar uma transacao aberta durante o SMTP.
    """

    def __init__(self, ordens: OrdemDeServicoRepository, clientes: ClientePort) -> None:
        self._ordens = ordens
        self._clientes = clientes

    def executar(self, notificacao: Notificacao) -> Email | None:
        """O e-mail do pedido, ou ``None`` quando nao ha o que entregar."""
        ordem = self._ordens.obter_por_id(notificacao.ordem_id)
        if ordem is None:
            return self._nada("ordem_inexistente", notificacao)
        contato = self._clientes.obter_contato(ordem.cliente_id)
        if contato is None:
            return self._nada("cliente_sem_contato", notificacao)
        destinatario = extrair_email(contato.contato)
        if destinatario is None:
            return self._nada("contato_sem_email", notificacao)
        curto = str(ordem.id)[:8]
        texto = _TEXTOS[notificacao.modelo](notificacao, ordem, curto)
        if texto is None:
            return self._nada("sem_dados_do_modelo", notificacao)
        assunto, paragrafos = texto
        return Email(
            destinatario=destinatario,
            assunto=f"PytStop — Ordem de Serviço {curto}: {assunto}",
            corpo=_corpo(contato.nome, *paragrafos),
        )

    @staticmethod
    def _nada(motivo: str, notificacao: Notificacao) -> None:
        _log.warning(
            "notification skipped",
            correlation_id=str(notificacao.ordem_id),
            modelo=notificacao.modelo.value,
            motivo=motivo,
        )
