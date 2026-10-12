"""Instancia da saga de atendimento: o process manager do OS Service.

Uma por OS (``id`` = ``ordem_id`` = ``correlation_id`` das mensagens),
persistida na tabela ``sagas`` como agregado proprio (RFC-004 secao 4; ADR-035
"Estado persistido e eventos fora de ordem"). Aqui ficam so o estado e as
regras, sem I/O: quem le e grava saga e OS e publica os comandos e o
``OrquestradorDaSaga`` (ou o caso de uso do cancelamento), pelas portas, numa
transacao so. A tabela da RFC esta em ``tabela_da_saga``, a classificacao dos
eventos em ``classificacao``, as transicoes e conferencias em ``transicoes``,
o plano e as respostas da compensacao em ``compensacao`` e os tipos em
``modelo``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Final, cast

from src.compartilhado.aplicacao.mensageria import Comando
from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao, classificar
from src.ordem_servico.aplicacao.saga.leitura_da_saga import LeituraDaSaga
from src.ordem_servico.aplicacao.saga.modelo import (
    GATILHO_ABERTURA,
    GATILHO_CANCELAMENTO_SOLICITADO,
    CompensacaoIniciadaEvent,
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    Gatilho,
    MotivoCompensacao,
    SagaIniciadaEvent,
    TransicaoDaSagaInvalidaError,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    ANTES_DO_PIVOT,
)
from src.ordem_servico.aplicacao.saga.transicoes import (
    conferir_envio,
    conferir_estado,
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
        Transicao,
    )
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem


# traceparent W3C da versao 00 (55 caracteres, o tamanho da coluna).
_TRACEPARENT: Final = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")


@dataclass(eq=False)
class Saga(AggregateRoot, LeituraDaSaga):
    """Instancia da saga de uma OS; ``id`` e o ``ordem_id``.

    Construir com ``Saga.iniciar``; os eventos andam com ``avancar`` e o
    pedido de cancelamento com ``cancelar``, que impoem as tabelas da RFC-004
    (secoes 4.1 e 4.4) antes de mudar. A ``versao`` e o lock otimista da
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
        abertura = Gatilho(GATILHO_ABERTURA, None, agora, ator)
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

    def classificar(
        self, tipo: str, marcos: MarcosDaOrdem, *, causation_id: UUID | None = None
    ) -> Classificacao:
        """Classifica o evento ``tipo`` na etapa atual (``classificacao``).

        A compensacao pendente e a primeira do plano; o ``causation_id`` casa a
        resposta com um dos envios do comando em voo (RFC-004 secao 4.4).
        """
        em_voo = self._comando_em_voo
        plano = self._plano_compensacao
        return classificar(
            self._etapa,
            tipo,
            marcos,
            pendente=plano[0] if plano else None,
            casada=em_voo is not None and str(causation_id) in em_voo["mensagem_ids"],
        )

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
        self._mudar(transicao, gatilho, envio, posicao)

    def cancelar(
        self, marcos: MarcosDaOrdem, *, agora: datetime, ator: str, envio: Envio
    ) -> None:
        """Pedido de cancelamento antes do pivot: a compensacao comeca.

        O plano leva os passos concluidos e o em voo (RFC-004 secao 4.4), e o
        ``envio`` e a primeira compensacao dele, com prazo. O registro leva o
        gatilho ``cancelamento_solicitado`` e o ``ator`` que pediu.

        Raises:
            TransicaoDaSagaInvalidaError: etapa que nao e de antes do pivot, ou
                o envio nao e a primeira compensacao do plano; nada muda.
        """
        transicao = transicao_do_cancelamento(self, agora)
        conferir_envio(
            transicao, envio, ordem_id=self.id, itens=self._itens, marcos=marcos
        )
        conferir_prazo(envio, agora)
        pedido = Gatilho(GATILHO_CANCELAMENTO_SOLICITADO, None, agora, ator)
        self._mudar(transicao, pedido, envio)

    # ----- mecanica interna

    def _mudar(
        self,
        transicao: Transicao,
        gatilho: Gatilho,
        envio: Envio | None,
        posicao: int | None = None,
    ) -> None:
        """Aplica a transicao ja conferida e anota o registro dela."""
        de, agora = self._etapa, gatilho.em
        if transicao.seguinte is not de:
            self._registrar_evento(
                EtapaDaSagaAlteradaEvent(
                    agregado_id=self.id,
                    etapa_anterior=de,
                    etapa_nova=transicao.seguinte,
                    permanencia=agora - self._etapa_desde,
                    ocorrido_em=agora,
                )
            )
            self._etapa = transicao.seguinte
            self._etapa_desde = agora
        if de in ANTES_DO_PIVOT and transicao.seguinte is EtapaSaga.COMPENSANDO:
            # A transicao que inicia a compensacao sempre traz o motivo.
            motivo = MotivoCompensacao(cast("str", transicao.motivo))
            self._registrar_evento(
                CompensacaoIniciadaEvent(
                    agregado_id=self.id, motivo=motivo, ocorrido_em=agora
                )
            )
        self._atualizada_em = agora
        self._motivo = transicao.motivo
        self._falha = transicao.falha
        self._plano_compensacao = [c.value for c in transicao.plano]
        if transicao.gatilho is not None:
            gatilho = replace(gatilho, tipo=transicao.gatilho)
        passo = gatilho.registro(
            len(self._passos) + 1, de, self._etapa, envio, transicao.motivo
        )
        if posicao is not None:
            passo["posicao_na_fila"] = posicao
        self._passos = [*self._passos, passo]
        if transicao.concluido is not None:
            self._passos_concluidos = [*self._passos_concluidos, transicao.concluido]
        self._esperar_resposta(envio)
        self._conferir_invariantes()

    def _esperar_resposta(self, envio: Envio | None) -> None:
        """O envio conferido vira o comando em voo; sem ele, nada fica em voo.

        Todo comando enviado depois da abertura tem resposta automatica, entao o
        envio conferido sempre traz o prazo. Na falha da compensacao o comando
        em voo permanece, sem prazo, para a retomada reenvia-lo (RFC-004 4.7).
        """
        if self._etapa is EtapaSaga.FALHA_NA_COMPENSACAO:
            self._prazo_resposta_em = None
        elif envio is None:
            self._comando_em_voo = None
            self._prazo_resposta_em = None
            self._reenvios = 0
        else:
            self._comando_em_voo = envio.em_voo(self._atualizada_em)
            self._prazo_resposta_em = envio.prazo_resposta_em
            self._reenvios = 0

    def _conferir_invariantes(self) -> None:
        conferir_estado(self)
