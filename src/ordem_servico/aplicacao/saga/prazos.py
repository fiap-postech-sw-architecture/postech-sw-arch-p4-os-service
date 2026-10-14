"""Prazos tecnicos da saga: reenvio, esgotamento e retomada (RFC-004 secoes 4.6 e 4.7).

Funcoes e fatos puros sobre os dados da saga, como ``compensacao``: o comando
que sai de novo, a transicao do prazo esgotado e a da retomada. Quem le a saga
vencida, grava o comando na outbox e comita e o caso de uso ``VencerPrazos``; a
``Saga`` confere tudo isto antes de mudar.

O reenvio manda o mesmo ``tipo`` com os mesmos ``dados`` do comando em voo (a
mesma chave de negocio), com ``id`` novo: o id novo entra em
``comando_em_voo.mensagem_ids`` e a resposta ao original ou a qualquer reenvio
continua casando pelo ``causation_id``. Esgotados os reenvios, a etapa linear
inicia a compensacao por ``prazo_tecnico``, com o passo em voo no plano, e a
compensacao vai a ``falha_na_compensacao``; a retomada reenvia a pendente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.events import DomainEvent
from src.ordem_servico.aplicacao.saga.compensacao import (
    dados_da_compensacao,
    inicio_da_compensacao,
)
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    MotivoCompensacao,
    Transicao,
    TransicaoDaSagaInvalidaError,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import ANTES_DO_PIVOT
from src.ordem_servico.aplicacao.saga.transicoes import instante_valido

if TYPE_CHECKING:
    from datetime import datetime

    from src.ordem_servico.aplicacao.saga.contexto_de_envio import ComandoParaEnviar
    from src.ordem_servico.aplicacao.saga.leitura_da_saga import LeituraDaSaga
    from src.ordem_servico.aplicacao.saga.modelo import ComandoEmVoo, Envio
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem

# O processo que reenvia e esgota os prazos (``ator`` dos registros dele), e a
# falha da compensacao que ficou sem resposta (RFC-004 secao 4.7).
ATOR_PRAZOS: Final = "prazos"
REENVIOS_ESGOTADOS: Final = "reenvios_esgotados"


@dataclass(frozen=True, slots=True)
class ComandoReenviadoEvent(DomainEvent):
    """O prazo do ``comando`` venceu e ele saiu de novo, com id novo."""

    comando: str = field(kw_only=True)


@dataclass(frozen=True, slots=True)
class PrazoEsgotadoEvent(DomainEvent):
    """O ``comando`` ficou sem resposta depois do ultimo reenvio."""

    comando: str = field(kw_only=True)


def comando_a_reenviar(saga: LeituraDaSaga) -> ComandoParaEnviar:
    """O comando em voo como ele sai de novo: o mesmo ``tipo`` e os mesmos ``dados``.

    Raises:
        TransicaoDaSagaInvalidaError: a saga nao tem comando em voo.
    """
    em_voo = saga.comando_em_voo
    if em_voo is None:
        msg = f"reenvio sem comando em voo na etapa {saga.etapa.value}"
        raise TransicaoDaSagaInvalidaError(msg)
    return Comando(em_voo["tipo"]), em_voo["dados"]


def conferir_reenvio(
    saga: LeituraDaSaga, envio: Envio, agora: datetime
) -> ComandoEmVoo:
    """O ``envio`` e o comando em voo de novo: mesmo tipo, mesmos dados, id novo.

    Vale para o reenvio por prazo e para o da retomada. Devolve o comando em voo
    de antes, de onde o novo herda os ids.

    Raises:
        TransicaoDaSagaInvalidaError: sem comando em voo, outro comando, dados
            diferentes, id que a saga ja usou ou instante sem fuso ou que volta
            antes do ultimo registro.
    """
    comando, dados = comando_a_reenviar(saga)
    em_voo = cast("ComandoEmVoo", saga.comando_em_voo)
    if not instante_valido(saga, agora):
        msg = f"reenvio de {comando} num instante sem fuso ou anterior ao registro"
    elif envio.tipo is not comando or dict(envio.dados) != dict(dados):
        msg = f"{envio.tipo} nao e o {comando} em voo, com os mesmos dados"
    elif str(envio.id) in em_voo["mensagem_ids"]:
        msg = f"o reenvio de {comando} repete o id de um envio anterior"
    else:
        return em_voo
    raise TransicaoDaSagaInvalidaError(msg)


def transicao_do_reenvio(saga: LeituraDaSaga, agora: datetime) -> Transicao:
    """O reenvio: a mesma etapa, o mesmo motivo e o mesmo plano, com o comando de novo.

    Raises:
        TransicaoDaSagaInvalidaError: sem prazo de resposta (esperas humanas,
            saga parada ou encerrada).
    """
    comando, _ = comando_a_reenviar(saga)
    if saga.prazo_resposta_em is None:
        msg = f"reenvio sem prazo de resposta na etapa {saga.etapa.value}"
        raise TransicaoDaSagaInvalidaError(msg)
    return Transicao(
        saga.etapa,
        comando=comando,
        motivo=saga.motivo,
        plano=tuple(Comando(c) for c in saga.plano_compensacao),
    )


def transicao_do_esgotamento(saga: LeituraDaSaga, agora: datetime) -> Transicao:
    """O prazo esgotado: a compensacao por ``prazo_tecnico`` ou a falha dela.

    Em etapa linear com comando em voo, inicia a compensacao com o passo em voo
    no plano (RFC-004 secao 4.4); em ``compensando``, a compensacao pendente
    sem resposta leva a ``falha_na_compensacao`` com ``reenvios_esgotados``, o
    plano intacto e nenhum comando novo (secao 4.7).

    Raises:
        TransicaoDaSagaInvalidaError: sem prazo de resposta (esperas humanas,
            saga parada ou encerrada), ou instante sem fuso ou que volta antes
            do ultimo registro.
    """
    if saga.prazo_resposta_em is None or saga.comando_em_voo is None:
        msg = f"prazo esgotado sem prazo de resposta na etapa {saga.etapa.value}"
        raise TransicaoDaSagaInvalidaError(msg)
    if not instante_valido(saga, agora):
        msg = "prazo esgotado num instante sem fuso ou anterior ao ultimo registro"
        raise TransicaoDaSagaInvalidaError(msg)
    if saga.etapa in ANTES_DO_PIVOT:
        return inicio_da_compensacao(
            saga.passos_concluidos,
            saga.tipo_do_comando_em_voo,
            MotivoCompensacao.PRAZO_TECNICO,
        )
    if saga.etapa is EtapaSaga.COMPENSANDO:
        return Transicao(
            EtapaSaga.FALHA_NA_COMPENSACAO,
            motivo=saga.motivo,
            plano=tuple(Comando(c) for c in saga.plano_compensacao),
            falha=REENVIOS_ESGOTADOS,
        )
    msg = f"prazo esgotado na etapa {saga.etapa.value}"
    raise TransicaoDaSagaInvalidaError(msg)


def compensacao_do_esgotamento(
    saga: LeituraDaSaga, marcos: MarcosDaOrdem, agora: datetime
) -> ComandoParaEnviar | None:
    """A primeira compensacao do prazo esgotado, pronta para enviar.

    ``None`` quando a compensacao ja estava em curso: a saga so para
    (``transicao_do_esgotamento``). E o ``envio`` que ``Saga.esgotar_prazo``
    pede, com os ``dados`` que ela confere; nada muda.
    """
    transicao = transicao_do_esgotamento(saga, agora)
    if transicao.comando is None:
        return None
    dados = dados_da_compensacao(transicao.comando, saga.id, transicao.motivo, marcos)
    return transicao.comando, dados


def transicao_da_retomada(saga: LeituraDaSaga, agora: datetime) -> Transicao:
    """A retomada: a compensacao pendente volta a esperar resposta (RFC-004 4.7).

    A etapa volta a ``compensando`` com o motivo e o plano de antes; o comando
    que sai e a pendente, a primeira do plano, e a ``falha`` some.

    Raises:
        TransicaoDaSagaInvalidaError: a saga nao esta em ``falha_na_compensacao``,
            ou o instante nao tem fuso ou volta antes do ultimo registro.
    """
    if saga.etapa is not EtapaSaga.FALHA_NA_COMPENSACAO:
        msg = f"retomada na etapa {saga.etapa.value}"
        raise TransicaoDaSagaInvalidaError(msg)
    if not instante_valido(saga, agora):
        msg = "retomada num instante sem fuso ou anterior ao ultimo registro"
        raise TransicaoDaSagaInvalidaError(msg)
    plano = tuple(Comando(c) for c in saga.plano_compensacao)
    return Transicao(
        EtapaSaga.COMPENSANDO, comando=plano[0], motivo=saga.motivo, plano=plano
    )
