"""Transicoes e conferencias da saga: o que a ``Saga`` confere antes de mudar.

``transicao_do_evento`` e ``transicao_do_cancelamento`` dizem a transicao que o
gatilho provoca (a linha do fluxo normal, o inicio da compensacao, a resposta
da pendente ou a corrida do pivot, RFC-004 secoes 4.1, 4.4 e 4.5);
``conferir_envio`` e ``conferir_prazo`` confrontam o comando gravado na outbox
com ela; ``conferir_gatilho`` e ``conferir_estado`` guardam o vocabulario do
registro e a invariante do estado.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.compensacao import (
    CORRIDA_DO_PIVOT,
    MOTIVO_DA_FALHA,
    ORDEM_DAS_COMPENSACOES,
    RESPOSTA_DA_COMPENSACAO,
    dados_da_compensacao,
    inicio_da_compensacao,
    resposta_da_compensacao,
)
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    InvarianteDaSagaError,
    MotivoCompensacao,
    TransicaoDaSagaInvalidaError,
    pecas_dos_itens,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    ANTES_DO_PIVOT,
    COMANDOS_COM_PRAZO,
    ETAPAS_EM_COMPENSACAO,
    FLUXO_NORMAL,
    GATILHOS_VALIDOS,
    PRIORIDADE_DO_AGENDAMENTO,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.leitura_da_saga import LeituraDaSaga
    from src.ordem_servico.aplicacao.saga.modelo import (
        Envio,
        Gatilho,
        ItemDoDiagnostico,
        Transicao,
    )
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem

_E = EtapaSaga


def transicao_do_evento(
    saga: LeituraDaSaga,
    evento: MensagemRecebida,
    marcos: MarcosDaOrdem,
    agora: datetime,
) -> Transicao:
    """A transicao que o ``evento`` provoca na ``saga`` agora, se ele pode.

    O evento e da OS da saga e se classifica para processar na etapa dela (com
    os ``marcos`` da OS de antes do fato e o ``causation_id``), e o instante tem
    fuso e nao volta antes do ultimo registro. A transicao e a linha do fluxo
    normal, o inicio da compensacao pela falha de negocio, a resposta da
    compensacao pendente ou a corrida do pivot (RFC-004 secoes 4.1, 4.4 e 4.5).

    Raises:
        TransicaoDaSagaInvalidaError: alguma das tres conferencias falhou.
    """
    tipo = evento.tipo
    classificacao = saga.classificar(tipo, marcos, causation_id=evento.causation_id)
    if evento.correlation_id != saga.id:
        msg = f"{tipo} de outra ordem"
    elif classificacao is not Classificacao.PROCESSAR:
        msg = f"{tipo} nao avanca a saga na etapa {saga.etapa.value}"
    elif not _instante_valido(saga, agora):
        msg = f"{tipo} num instante sem fuso ou anterior ao ultimo registro"
    elif tipo in MOTIVO_DA_FALHA:
        return inicio_da_compensacao(
            saga.passos_concluidos, saga.tipo_do_comando_em_voo, MOTIVO_DA_FALHA[tipo]
        )
    elif tipo in RESPOSTA_DA_COMPENSACAO:
        return resposta_da_compensacao(tipo, saga.plano_compensacao, saga.motivo)
    elif saga.etapa in ETAPAS_EM_COMPENSACAO:
        # So o ExecucaoIniciada se classifica para processar aqui.
        return CORRIDA_DO_PIVOT
    else:
        return FLUXO_NORMAL[tipo]
    raise TransicaoDaSagaInvalidaError(msg)


def transicao_do_cancelamento(saga: LeituraDaSaga, agora: datetime) -> Transicao:
    """O pedido de cancelamento antes do pivot: a compensacao comeca (RFC-004 4.4).

    Raises:
        TransicaoDaSagaInvalidaError: etapa que nao e de antes do pivot, ou
            instante sem fuso ou que volta antes do ultimo registro.
    """
    if saga.etapa not in ANTES_DO_PIVOT:
        msg = f"cancelamento pela saga na etapa {saga.etapa.value}"
    elif not _instante_valido(saga, agora):
        msg = "cancelamento num instante sem fuso ou anterior ao ultimo registro"
    else:
        return inicio_da_compensacao(
            saga.passos_concluidos,
            saga.tipo_do_comando_em_voo,
            MotivoCompensacao.CANCELAMENTO,
        )
    raise TransicaoDaSagaInvalidaError(msg)


def _instante_valido(saga: LeituraDaSaga, agora: datetime) -> bool:
    return agora.tzinfo is not None and agora >= saga.atualizada_em


def conferir_envio(
    transicao: Transicao,
    envio: Envio | None,
    *,
    ordem_id: UUID,
    itens: Sequence[ItemDoDiagnostico],
    marcos: MarcosDaOrdem,
) -> None:
    """O ``envio`` e o comando da ``transicao``, com os dados que a saga e a OS fixam.

    Nenhum envio onde ela nao envia; nos dados, o ``ordem_id`` da OS e o que a
    saga e a OS guardam: os ``itens`` do diagnostico no ``GerarOrcamento``, as
    pecas deles no ``ReservarPecas``, o orcamento dos ``marcos`` da OS no
    ``SolicitarPagamento``, a prioridade ``normal`` no ``AgendarExecucao`` e o
    ``motivo`` nas compensacoes, com o orcamento e o pagamento da OS no
    ``CancelarOrcamento`` e no ``EstornarPagamento`` (sem eles, nenhum id).

    Raises:
        TransicaoDaSagaInvalidaError: o envio nao e o da transicao, ou seus
            dados nao sao os desta saga.
    """
    if envio is None and transicao.comando is None:
        return
    if envio is None or envio.tipo is not transicao.comando:
        enviado = envio.tipo if envio is not None else "nenhum comando"
        msg = f"a linha envia {transicao.comando or 'nenhum comando'}, nao {enviado}"
        raise TransicaoDaSagaInvalidaError(msg)
    if envio.tipo in ORDEM_DAS_COMPENSACOES:
        # A mesma montagem que o orquestrador e o cancelamento usam: sem o id do
        # recurso na OS, nenhum id vai no comando.
        esperados: dict[str, object] = dict(
            dados_da_compensacao(envio.tipo, ordem_id, transicao.motivo, marcos)
        )
        confere = dict(envio.dados) == esperados
    else:
        # Sem orcamento na OS, str(None), que e "None", nao casa com id nenhum.
        da_saga: dict[Comando, dict[str, object]] = {
            Comando.GERAR_ORCAMENTO: {"itens": list(itens)},
            Comando.RESERVAR_PECAS: {"pecas": pecas_dos_itens(itens)},
            Comando.SOLICITAR_PAGAMENTO: {"orcamento_id": str(marcos.orcamento_id)},
            Comando.AGENDAR_EXECUCAO: {"prioridade": PRIORIDADE_DO_AGENDAMENTO},
        }
        esperados = {"ordem_id": str(ordem_id), **da_saga.get(envio.tipo, {})}
        confere = all(envio.dados.get(k) == v for k, v in esperados.items())
    if not confere:
        msg = f"{envio.tipo} com dados que nao sao os desta saga"
        raise TransicaoDaSagaInvalidaError(msg)


def conferir_gatilho(transicao: Transicao, gatilho: Gatilho) -> None:
    """O gatilho que o registro leva e do vocabulario fechado (``GATILHOS_VALIDOS``).

    E o da transicao quando ela diz um (``cancelamento_recusado``), senao o do
    pedido: o tipo da mensagem ou uma palavra do processo.

    Raises:
        TransicaoDaSagaInvalidaError: gatilho fora do vocabulario.
    """
    registrado = transicao.gatilho or gatilho.tipo
    if registrado not in GATILHOS_VALIDOS:
        msg = f"gatilho {registrado!r} fora do vocabulario da saga"
        raise TransicaoDaSagaInvalidaError(msg)


def conferir_prazo(envio: Envio | None, agora: datetime) -> None:
    """O prazo de resposta do ``envio``, que so os ``COMANDOS_COM_PRAZO`` levam.

    O prazo e depois de ``agora`` e tem fuso horario.

    Raises:
        TransicaoDaSagaInvalidaError: prazo faltando, sobrando, sem fuso horario
            ou que nao e depois do envio.
    """
    if envio is None:
        return
    prazo = envio.prazo_resposta_em
    com_prazo = envio.tipo in COMANDOS_COM_PRAZO
    if (prazo is not None) is not com_prazo or (
        prazo is not None and (prazo.tzinfo is None or prazo <= agora)
    ):
        msg = f"{envio.tipo} sem o prazo de resposta depois do envio"
        raise TransicaoDaSagaInvalidaError(msg)


def conferir_estado(saga: LeituraDaSaga) -> None:
    """Invariantes do estado da saga, para quem a monta por fora do ``iniciar``.

    Prazo se e so se ha comando em voo, salvo em ``falha_na_compensacao``, onde
    o comando em voo permanece e o prazo e nulo; ``reenvios`` nunca negativo e
    zero sem comando em voo; plano so com a compensacao em curso ou parada, e
    nela sempre, com o motivo; a falha so existe em ``falha_na_compensacao``, e
    sempre nela.

    Raises:
        InvarianteDaSagaError: estado contraditorio.
    """
    em_voo = saga.comando_em_voo is not None
    com_prazo = saga.prazo_resposta_em is not None
    parada = saga.etapa is _E.FALHA_NA_COMPENSACAO
    compensacao = saga.etapa in ETAPAS_EM_COMPENSACAO
    if parada and not (em_voo and not com_prazo):
        msg = "falha_na_compensacao guarda o comando em voo, sem prazo"
    elif not parada and com_prazo is not em_voo:
        msg = "prazo de resposta so com comando em voo, e todo comando em voo tem"
    elif saga.reenvios < 0 or (not em_voo and saga.reenvios):
        msg = f"reenvios {saga.reenvios} sem comando em voo ou negativo"
    elif compensacao and not (saga.motivo and saga.plano_compensacao):
        msg = f"{saga.etapa.value} exige o motivo e o plano de compensacao"
    elif saga.plano_compensacao and not compensacao:
        msg = f"plano de compensacao na etapa {saga.etapa.value}"
    elif (saga.falha is not None) is not parada:
        msg = "a falha so existe em falha_na_compensacao, e sempre nela"
    else:
        return
    raise InvarianteDaSagaError(msg)
