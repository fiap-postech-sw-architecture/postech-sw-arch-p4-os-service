"""Instancia da saga de atendimento: o process manager do OS Service.

Uma por OS (``id`` = ``ordem_id`` = ``correlation_id`` das mensagens),
persistida na tabela ``sagas`` como agregado proprio (RFC-004 secao 4; ADR-035
"Estado persistido e eventos fora de ordem"). Aqui ficam so o estado e as
regras, sem I/O: quem le e grava saga e OS e publica os comandos e o
``OrquestradorDaSaga``, pelas portas, numa transacao so. A tabela da RFC e a
classificacao dos eventos ficam em ``tabela_da_saga``; os tipos, em ``modelo``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.ordem_servico.aplicacao.saga.modelo import (
    ComandoEmVoo,
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    ItemDoDiagnostico,
    Passo,
    SagaIniciadaEvent,
    TransicaoDaSagaInvalidaError,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    ETAPA_ESPERADA,
    ETAPA_SEGUINTE,
    ETAPAS_FINAIS,
    GATILHO_ABERTURA,
    PASSO_CONCLUIDO,
    classificar,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.modelo import Envio
    from src.ordem_servico.aplicacao.saga.tabela_da_saga import Classificacao
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


@dataclass(eq=False)
class Saga(AggregateRoot):
    """Instancia da saga de uma OS; ``id`` e o ``ordem_id``.

    Construir com ``Saga.iniciar``; o fluxo normal anda com ``avancar``. A
    ``versao`` e o lock otimista da persistencia, como na OS. Listas e dicts
    sao trocados por novos a cada mudanca (a coluna JSONB nao detecta mutacao
    no lugar).
    """

    _etapa: EtapaSaga = field(kw_only=True)
    _iniciada_em: datetime = field(kw_only=True)
    _etapa_desde: datetime = field(kw_only=True)
    _atualizada_em: datetime = field(kw_only=True)
    _motivo: str | None = field(default=None, kw_only=True)
    _falha: str | None = field(default=None, kw_only=True)
    _passos: list[Passo] = field(default_factory=list, kw_only=True, repr=False)
    _passos_concluidos: list[str] = field(default_factory=list, kw_only=True)
    _comando_em_voo: ComandoEmVoo | None = field(default=None, kw_only=True, repr=False)
    _plano_compensacao: list[str] = field(default_factory=list, kw_only=True)
    _itens: list[ItemDoDiagnostico] = field(
        default_factory=list, kw_only=True, repr=False
    )
    _reenvios: int = field(default=0, kw_only=True)
    _prazo_resposta_em: datetime | None = field(default=None, kw_only=True)
    # Contexto W3C da ultima transicao (ADR-043): a persistencia grava o do
    # span corrente ao salvar.
    traceparent: str | None = field(default=None, kw_only=True, repr=False)
    _versao: int = field(default=1, kw_only=True)

    @classmethod
    def iniciar(
        cls, ordem_id: UUID, *, envio: Envio, ator: str | None, agora: datetime
    ) -> Saga:
        """T1: abre a saga em ``aguardando_diagnostico``, com o passo ``abertura``.

        O ``SolicitarDiagnostico`` do ``envio`` nao tem resposta automatica
        (RFC-004 secao 4.3): a saga fica sem comando em voo e sem prazo.
        """
        saga = cls(
            id=ordem_id,
            _etapa=EtapaSaga.AGUARDANDO_DIAGNOSTICO,
            _iniciada_em=agora,
            _etapa_desde=agora,
            _atualizada_em=agora,
        )
        saga._passos = [
            saga._passo(
                gatilho=GATILHO_ABERTURA,
                de=None,
                mensagem_id=None,
                ator=ator,
                envio=envio,
            )
        ]
        saga._registrar_evento(
            SagaIniciadaEvent(agregado_id=ordem_id, ocorrido_em=agora)
        )
        return saga

    @property
    def ordem_id(self) -> UUID:
        return self.id

    @property
    def etapa(self) -> EtapaSaga:
        return self._etapa

    @property
    def encerrada(self) -> bool:
        """Concluida ou compensada: nenhum evento muda mais a saga."""
        return self._etapa in ETAPAS_FINAIS

    @property
    def motivo(self) -> str | None:
        """Codigo da compensacao (``orcamento_recusado``, ``cancelamento``...)."""
        return self._motivo

    @property
    def falha(self) -> str | None:
        """``reenvios_esgotados`` ou ``estorno_recusado`` em falha_na_compensacao."""
        return self._falha

    @property
    def passos(self) -> tuple[Passo, ...]:
        """Copia: mudar um passo devolvido nao toca o estado da saga."""
        return tuple(deepcopy(self._passos))

    @property
    def passos_concluidos(self) -> tuple[str, ...]:
        return tuple(self._passos_concluidos)

    @property
    def comando_em_voo(self) -> ComandoEmVoo | None:
        return deepcopy(self._comando_em_voo)

    @property
    def plano_compensacao(self) -> tuple[str, ...]:
        """Compensacoes restantes; a primeira e a pendente."""
        return tuple(self._plano_compensacao)

    @property
    def itens(self) -> tuple[ItemDoDiagnostico, ...]:
        return tuple(deepcopy(self._itens))

    @property
    def reenvios(self) -> int:
        return self._reenvios

    @property
    def prazo_resposta_em(self) -> datetime | None:
        """Prazo tecnico do comando em voo; ``None`` sem resposta automatica."""
        return self._prazo_resposta_em

    @property
    def iniciada_em(self) -> datetime:
        return self._iniciada_em

    @property
    def etapa_desde(self) -> datetime:
        return self._etapa_desde

    @property
    def atualizada_em(self) -> datetime:
        return self._atualizada_em

    @property
    def versao(self) -> int:
        return self._versao

    def classificar(self, tipo: str, ordem: OrdemDeServico) -> Classificacao:
        """Classifica o evento ``tipo`` na etapa atual (``tabela_da_saga``)."""
        return classificar(self._etapa, tipo, ordem)

    def avancar(
        self,
        evento: MensagemRecebida,
        *,
        agora: datetime,
        ator: str | None,
        envio: Envio | None = None,
    ) -> None:
        """Aplica um evento do fluxo normal na etapa em que ele e esperado.

        Anota o passo, vai para a etapa seguinte da tabela da RFC-004 secao
        4.1, marca o passo concluido (T3, T5, T6, T7), guarda os itens do
        diagnostico e troca o comando em voo: o ``envio`` com prazo passa a
        esperar resposta, com ``reenvios`` zerado; sem ele, a saga deixa de
        esperar resposta automatica.

        Raises:
            TransicaoDaSagaInvalidaError: o evento nao e do fluxo normal ou a
                saga nao esta na etapa em que ele e esperado.
        """
        tipo = evento.tipo
        seguinte = ETAPA_SEGUINTE.get(tipo)
        if seguinte is None or ETAPA_ESPERADA[tipo] is not self._etapa:
            msg = f"{tipo} nao avanca a saga na etapa {self._etapa.value}"
            raise TransicaoDaSagaInvalidaError(msg)
        de = self._etapa
        if seguinte is not de:
            self._registrar_evento(
                EtapaDaSagaAlteradaEvent(
                    agregado_id=self.id,
                    etapa_anterior=de,
                    etapa_nova=seguinte,
                    permanencia=agora - self._etapa_desde,
                    ocorrido_em=agora,
                )
            )
            self._etapa = seguinte
            self._etapa_desde = agora
        self._atualizada_em = agora
        passo = self._passo(
            gatilho=tipo, de=de, mensagem_id=evento.id, ator=ator, envio=envio
        )
        if tipo == "ExecucaoAgendada":
            passo["posicao_na_fila"] = evento.dados["posicao_na_fila"]
        self._passos = [*self._passos, passo]
        if tipo == "DiagnosticoConcluido":
            self._itens = itens_do_diagnostico(evento.dados)
        if concluido := PASSO_CONCLUIDO.get(tipo):
            self._passos_concluidos = [*self._passos_concluidos, concluido]
        self._esperar_resposta(envio)

    # ----- mecanica interna

    def _passo(
        self,
        *,
        gatilho: str,
        de: EtapaSaga | None,
        mensagem_id: UUID | None,
        ator: str | None,
        envio: Envio | None,
    ) -> Passo:
        """Passo novo no instante da alteracao (``atualizada_em``)."""
        return {
            "seq": len(self._passos) + 1,
            "em": self._atualizada_em.isoformat(),
            "de": de.value if de is not None else None,
            "para": self._etapa.value,
            "gatilho": gatilho,
            "mensagem_id": str(mensagem_id) if mensagem_id is not None else None,
            "comando": envio.tipo.value if envio is not None else None,
            "comando_id": str(envio.id) if envio is not None else None,
            "motivo": self._motivo,
            "ator": ator,
        }

    def _esperar_resposta(self, envio: Envio | None) -> None:
        """Comando com prazo vira o comando em voo; sem ele, nada fica em voo."""
        if envio is None or envio.prazo_resposta_em is None:
            self._comando_em_voo = None
            self._prazo_resposta_em = None
            return
        self._comando_em_voo = {
            "tipo": envio.tipo.value,
            "dados": dict(envio.dados),
            "mensagem_ids": [str(envio.id)],
            "enviado_em": self._atualizada_em.isoformat(),
        }
        self._prazo_resposta_em = envio.prazo_resposta_em
        self._reenvios = 0
