"""Instancia da saga de atendimento: o process manager do OS Service.

Uma por OS (``id`` = ``ordem_id`` = ``correlation_id`` das mensagens),
persistida na tabela ``sagas`` como agregado proprio (RFC-004 secao 4; ADR-035
"Estado persistido e eventos fora de ordem"). Aqui ficam so o estado e as
regras, sem I/O: quem le e grava saga e OS e publica os comandos e o
``OrquestradorDaSaga`` (ou o caso de uso do cancelamento), pelas portas, numa
transacao so. A tabela da RFC esta em ``tabela_da_saga``, a classificacao dos
eventos em ``classificacao``, as transicoes e conferencias em ``transicoes``,
o plano e as respostas da compensacao em ``compensacao``, o reenvio, o
esgotamento e a retomada em ``prazos``, a aplicacao da transicao conferida em
``mudanca_da_saga`` e os tipos em ``modelo``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final, cast

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    Gatilho,
    GatilhoDeProcesso,
    SagaIniciadaEvent,
    TransicaoDaSagaInvalidaError,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.mudanca_da_saga import (
    Complemento,
    MudancaDaSaga,
)
from src.ordem_servico.aplicacao.saga.prazos import (
    ComandoReenviadoEvent,
    PrazoEsgotadoEvent,
    conferir_reenvio,
    transicao_da_retomada,
    transicao_do_esgotamento,
    transicao_do_reenvio,
)
from src.ordem_servico.aplicacao.saga.transicoes import (
    conferir_envio,
    conferir_prazo,
    transicao_do_cancelamento,
    transicao_do_evento,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.modelo import (
        ComandoEmVoo,
        Envio,
        ItemDoDiagnostico,
        RegistroDaSaga,
    )
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem


# traceparent W3C da versao 00 (55 caracteres, o tamanho da coluna).
_TRACEPARENT: Final = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")


@dataclass(eq=False)
class Saga(MudancaDaSaga):
    """Instancia da saga de uma OS; ``id`` e o ``ordem_id``.

    Construir com ``Saga.iniciar``; os eventos andam com ``avancar``, o pedido
    de cancelamento com ``solicitar_cancelamento`` e os prazos com ``reenviar``,
    ``esgotar_prazo`` e ``retomar``, que impoem as tabelas da RFC-004 (secoes
    4.1, 4.4, 4.6 e 4.7) antes de mudar. A ``versao`` e o lock otimista da
    persistencia, como na OS. Listas e dicts sao trocados por novos a cada
    mudanca (a coluna JSONB nao detecta mutacao no lugar).
    """

    _etapa: EtapaSaga = field(kw_only=True)
    _iniciada_em: datetime = field(kw_only=True)
    _etapa_desde: datetime = field(kw_only=True)
    _atualizada_em: datetime = field(kw_only=True)
    _motivo: str | None = field(default=None, kw_only=True)
    _falha: str | None = field(default=None, kw_only=True)
    _passos: list[RegistroDaSaga] = field(
        default_factory=list, kw_only=True, repr=False
    )
    _passos_concluidos: list[str] = field(default_factory=list, kw_only=True)
    _comando_em_voo: ComandoEmVoo | None = field(default=None, kw_only=True, repr=False)
    _plano_compensacao: list[str] = field(default_factory=list, kw_only=True)
    _itens: list[ItemDoDiagnostico] = field(
        default_factory=list, kw_only=True, repr=False
    )
    _reenvios: int = field(default=0, kw_only=True)
    _prazo_resposta_em: datetime | None = field(default=None, kw_only=True)
    # Contexto W3C da ultima transicao (ADR-043): a persistencia registra o do
    # span corrente antes de gravar.
    _traceparent: str | None = field(default=None, kw_only=True, repr=False)
    _versao: int = field(default=1, kw_only=True)

    def __post_init__(self) -> None:
        super().__post_init__()
        self._conferir_invariantes()

    @classmethod
    def iniciar(
        cls, ordem_id: UUID, *, envio: Envio, ator: str, agora: datetime
    ) -> Saga:
        """T1: abre a saga em ``aguardando_diagnostico``, com o passo ``abertura``.

        O ``envio`` e o ``SolicitarDiagnostico``, sem resposta automatica nem
        prazo (RFC-004 secao 4.3): a saga fica sem comando em voo.

        Raises:
            TransicaoDaSagaInvalidaError: outro comando, com prazo, ou instante
                sem fuso horario.
        """
        if (
            envio.tipo is not Comando.SOLICITAR_DIAGNOSTICO
            or envio.prazo_resposta_em is not None
            or agora.tzinfo is None
        ):
            msg = f"a abertura envia SolicitarDiagnostico sem prazo, nao {envio.tipo}"
            raise TransicaoDaSagaInvalidaError(msg)
        saga = cls(
            id=ordem_id,
            _etapa=EtapaSaga.AGUARDANDO_DIAGNOSTICO,
            _iniciada_em=agora,
            _etapa_desde=agora,
            _atualizada_em=agora,
        )
        abertura = Gatilho(GatilhoDeProcesso.ABERTURA.value, None, agora, ator)
        saga._passos = [abertura.registro(1, None, saga._etapa, envio, None)]
        saga._registrar_evento(
            SagaIniciadaEvent(agregado_id=ordem_id, ocorrido_em=agora)
        )
        return saga

    def registrar_contexto_de_trace(self, traceparent: str) -> None:
        """Guarda o ``traceparent`` W3C (versao 00) da transicao em curso.

        Raises:
            ValueError: texto fora do formato ``00-<32 hex>-<16 hex>-<2 hex>``.
        """
        if not _TRACEPARENT.fullmatch(traceparent):
            msg = "traceparent fora do formato W3C da versao 00"
            raise ValueError(msg)
        self._traceparent = traceparent

    def avancar(
        self,
        evento: MensagemRecebida,
        marcos: MarcosDaOrdem,
        *,
        agora: datetime,
        ator: str,
        envio: Envio | None = None,
    ) -> None:
        """Aplica um evento pela transicao dele (RFC-004 secoes 4.1, 4.4 e 4.5).

        A linha do fluxo normal, o inicio da compensacao pela falha de negocio,
        a resposta da compensacao pendente ou a corrida do pivot, conferida
        antes de mudar (``transicao_do_evento`` e ``conferir_envio``, com os
        ``marcos`` da OS de antes do fato). O ``envio`` passa a esperar resposta.

        Raises:
            TransicaoDaSagaInvalidaError: alguma conferencia falhou; nada muda.
        """
        transicao = transicao_do_evento(self, evento, marcos, agora)
        # Calculados antes de mudar: dado malformado levanta com a saga intacta.
        diagnostico = evento.tipo == "DiagnosticoConcluido"
        itens = itens_do_diagnostico(evento.dados) if diagnostico else self._itens
        agendada = evento.tipo == "ExecucaoAgendada"
        posicao = evento.dados["posicao_na_fila"] if agendada else None
        conferir_envio(transicao, envio, ordem_id=self.id, itens=itens, marcos=marcos)
        conferir_prazo(envio, agora)
        self._itens = itens
        gatilho = Gatilho(evento.tipo, evento.id, agora, ator)
        self._mudar(transicao, gatilho, envio, Complemento(posicao=posicao))

    def solicitar_cancelamento(
        self, marcos: MarcosDaOrdem, *, agora: datetime, ator: str, envio: Envio
    ) -> None:
        """Pedido de cancelamento antes do pivot: a compensacao comeca.

        O plano leva os passos concluidos e o em voo (RFC-004 secao 4.4), e o
        ``envio`` e a primeira compensacao dele, com prazo. O registro leva o
        gatilho ``cancelamento_solicitado`` e o ``ator`` que pediu. A OS so fica
        cancelada quando a ultima compensacao responde.

        Raises:
            TransicaoDaSagaInvalidaError: etapa que nao e de antes do pivot, ou
                o envio nao e a primeira compensacao do plano; nada muda.
        """
        transicao = transicao_do_cancelamento(self, agora)
        conferir_envio(
            transicao, envio, ordem_id=self.id, itens=self._itens, marcos=marcos
        )
        conferir_prazo(envio, agora)
        pedido = Gatilho(
            GatilhoDeProcesso.CANCELAMENTO_SOLICITADO.value, None, agora, ator
        )
        self._mudar(transicao, pedido, envio)

    def reenviar(self, envio: Envio, *, agora: datetime, ator: str) -> None:
        """O prazo do comando em voo venceu: ele sai de novo, com id novo.

        O ``envio`` e o mesmo comando com os mesmos ``dados`` (a mesma chave de
        negocio). O id novo passa a casar a resposta, o prazo recomeca e o
        contador sobe (RFC-004 secao 4.6); a etapa nao muda.

        Raises:
            TransicaoDaSagaInvalidaError: sem comando em voo, envio que nao e o
                dele, prazo invalido ou instante que volta; nada muda.
        """
        transicao = transicao_do_reenvio(self)
        anteriores = conferir_reenvio(self, envio, agora)["mensagem_ids"]
        conferir_prazo(envio, agora)
        tipo = envio.tipo.value
        self._registrar_evento(
            ComandoReenviadoEvent(agregado_id=self.id, comando=tipo, ocorrido_em=agora)
        )
        reenvio = Gatilho(GatilhoDeProcesso.REENVIO.value, None, agora, ator)
        complemento = Complemento(anteriores, reenvio=self._reenvios + 1)
        self._mudar(transicao, reenvio, envio, complemento)

    def esgotar_prazo(
        self,
        marcos: MarcosDaOrdem,
        *,
        agora: datetime,
        ator: str,
        envio: Envio | None = None,
    ) -> None:
        """O ultimo reenvio ficou sem resposta (RFC-004 secoes 4.6 e 4.7).

        Em etapa linear a compensacao comeca por ``prazo_tecnico``, com o passo
        em voo no plano, e o ``envio`` e a primeira compensacao dele; em
        ``compensando`` a saga vai a ``falha_na_compensacao`` (``reenvios_esgotados``)
        e nenhum comando sai.

        Raises:
            TransicaoDaSagaInvalidaError: sem prazo de resposta, envio que nao e
                o da transicao ou instante que volta; nada muda.
        """
        transicao = transicao_do_esgotamento(self, agora)
        conferir_envio(
            transicao, envio, ordem_id=self.id, itens=self._itens, marcos=marcos
        )
        conferir_prazo(envio, agora)
        self._registrar_evento(
            PrazoEsgotadoEvent(
                agregado_id=self.id,
                comando=cast("str", self.tipo_do_comando_em_voo),
                ocorrido_em=agora,
            )
        )
        esgotado = Gatilho(GatilhoDeProcesso.PRAZO_ESGOTADO.value, None, agora, ator)
        self._mudar(transicao, esgotado, envio)

    def retomar(self, envio: Envio, *, agora: datetime, ator: str) -> None:
        """O operador retoma a compensacao parada (RFC-004 secao 4.7).

        A saga volta a ``compensando`` e o ``envio`` e a pendente de novo, com id
        novo, prazo novo e contador zerado; os ids dos envios anteriores ficam
        em ``mensagem_ids``, e a resposta tardia ao original ainda casa.

        Raises:
            TransicaoDaSagaInvalidaError: etapa que nao e ``falha_na_compensacao``,
                envio que nao e a pendente ou instante que volta; nada muda.
        """
        transicao = transicao_da_retomada(self, agora)
        anteriores = conferir_reenvio(self, envio, agora)["mensagem_ids"]
        conferir_prazo(envio, agora)
        retomada = Gatilho(GatilhoDeProcesso.RETOMADA.value, None, agora, ator)
        self._mudar(transicao, retomada, envio, Complemento(anteriores))
