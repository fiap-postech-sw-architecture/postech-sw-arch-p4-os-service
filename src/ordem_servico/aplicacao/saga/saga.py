"""Instancia da saga de atendimento: o process manager do OS Service.

Uma por OS (``id`` = ``ordem_id`` = ``correlation_id`` das mensagens),
persistida na tabela ``sagas`` como agregado proprio (RFC-004 secao 4; ADR-035
"Estado persistido e eventos fora de ordem"). Aqui ficam so o estado e as
regras, sem I/O: quem le e grava saga e OS e publica os comandos e o
``OrquestradorDaSaga``, pelas portas, numa transacao so. A tabela da RFC e a
classificacao dos eventos ficam em ``tabela_da_saga``; os tipos, em ``modelo``.
"""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from src.compartilhado.aplicacao.mensageria import Comando
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
    COMANDOS_COM_PRAZO,
    ETAPAS_FINAIS,
    FLUXO_NORMAL,
    GATILHO_ABERTURA,
    Classificacao,
    LinhaDoFluxo,
    classificar,
)

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import MensagemRecebida
    from src.ordem_servico.aplicacao.saga.modelo import Envio
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem


# traceparent W3C da versao 00 (55 caracteres, o tamanho da coluna).
_TRACEPARENT: Final = re.compile(r"00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}")


@dataclass(eq=False)
class Saga(AggregateRoot):
    """Instancia da saga de uma OS; ``id`` e o ``ordem_id``.

    Construir com ``Saga.iniciar``; o fluxo normal anda com ``avancar``, que
    impoe a tabela da RFC-004 secao 4.1 (etapa seguinte, comando, dados e
    prazo). A ``versao`` e o lock otimista da persistencia, como na OS. Listas
    e dicts sao trocados por novos a cada mudanca (a coluna JSONB nao detecta
    mutacao no lugar).
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
    # Contexto W3C da ultima transicao (ADR-043): a persistencia registra o do
    # span corrente antes de gravar.
    _traceparent: str | None = field(default=None, kw_only=True, repr=False)
    _versao: int = field(default=1, kw_only=True)

    def __post_init__(self) -> None:
        """Invariantes do estado, para quem monta a saga por fora do ``iniciar``.

        Prazo se e so se ha comando em voo; ``reenvios`` nunca negativo e zero
        sem comando em voo; em compensacao, motivo e plano.
        """
        super().__post_init__()
        if (self._prazo_resposta_em is None) is not (self._comando_em_voo is None):
            msg = "prazo de resposta so com comando em voo, e todo comando em voo tem"
            raise ValueError(msg)
        if self._reenvios < 0 or (self._comando_em_voo is None and self._reenvios):
            msg = f"reenvios {self._reenvios} sem comando em voo ou negativo"
            raise ValueError(msg)
        if self._etapa is EtapaSaga.COMPENSANDO and not (
            self._motivo and self._plano_compensacao
        ):
            msg = "compensando exige o motivo e o plano de compensacao"
            raise ValueError(msg)

    @classmethod
    def iniciar(
        cls, ordem_id: UUID, *, envio: Envio, ator: str | None, agora: datetime
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
        """A OS da saga (o mesmo ``id``, e o ``correlation_id`` das mensagens)."""
        return self.id

    @property
    def etapa(self) -> EtapaSaga:
        """Etapa atual (RFC-004 secao 4.1)."""
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
        """Passos T concluidos (T3, T5, T6, T7), que a compensacao desfaz."""
        return tuple(self._passos_concluidos)

    @property
    def comando_em_voo(self) -> ComandoEmVoo | None:
        """Copia do comando com prazo a espera de resposta, ou ``None``."""
        return deepcopy(self._comando_em_voo)

    @property
    def plano_compensacao(self) -> tuple[str, ...]:
        """Compensacoes restantes; a primeira e a pendente."""
        return tuple(self._plano_compensacao)

    @property
    def itens(self) -> tuple[ItemDoDiagnostico, ...]:
        """Copia dos itens do ``DiagnosticoConcluido`` (vazio antes dele)."""
        return tuple(deepcopy(self._itens))

    @property
    def pecas(self) -> list[dict[str, Any]]:
        """As pecas dos itens como o ``ReservarPecas`` as pede (RFC-004 secao 4.1)."""
        return _pecas(self._itens)

    @property
    def reenvios(self) -> int:
        """Reenvios do comando em voo; zero no envio e sem comando em voo."""
        return self._reenvios

    @property
    def prazo_resposta_em(self) -> datetime | None:
        """Prazo tecnico do comando em voo; ``None`` sem resposta automatica."""
        return self._prazo_resposta_em

    @property
    def iniciada_em(self) -> datetime:
        """Instante da abertura da saga (o da OS)."""
        return self._iniciada_em

    @property
    def etapa_desde(self) -> datetime:
        """Instante da entrada na etapa atual (mede a permanencia nela)."""
        return self._etapa_desde

    @property
    def atualizada_em(self) -> datetime:
        """Instante do ultimo passo (o ``em`` dele)."""
        return self._atualizada_em

    @property
    def versao(self) -> int:
        """Versao do lock otimista: comeca em 1 e sobe a cada gravacao."""
        return self._versao

    @property
    def traceparent(self) -> str | None:
        """Contexto W3C da ultima transicao, pai dos spans que a retomam."""
        return self._traceparent

    def registrar_contexto_de_trace(self, traceparent: str) -> None:
        """Guarda o ``traceparent`` W3C (versao 00) da transicao em curso.

        Raises:
            ValueError: texto fora do formato ``00-<32 hex>-<16 hex>-<2 hex>``.
        """
        if not _TRACEPARENT.fullmatch(traceparent):
            msg = "traceparent fora do formato W3C da versao 00"
            raise ValueError(msg)
        self._traceparent = traceparent

    def classificar(self, tipo: str, marcos: MarcosDaOrdem) -> Classificacao:
        """Classifica o evento ``tipo`` na etapa atual (``tabela_da_saga``)."""
        return classificar(self._etapa, tipo, marcos)

    def avancar(
        self,
        evento: MensagemRecebida,
        marcos: MarcosDaOrdem,
        *,
        agora: datetime,
        ator: str | None,
        envio: Envio | None = None,
    ) -> None:
        """Aplica um evento do fluxo normal pela linha dele na tabela 4.1.

        Confere tudo antes de mudar: o evento e desta OS e se classifica para
        processar (a regra de ``classificar``, com os ``marcos`` da OS de antes
        do fato), o instante tem fuso e nao volta, e o ``envio`` e o comando da
        linha (nenhum onde ela nao envia), com o ``ordem_id`` desta OS, os itens
        ou as pecas que a saga guarda e o prazo de quem tem resposta automatica.
        Depois anota o passo, vai para a etapa seguinte, marca o passo concluido
        (T3, T5, T6, T7), guarda os itens do diagnostico e troca o comando em voo
        (o ``envio`` com prazo passa a esperar resposta, com ``reenvios`` zerado).

        Raises:
            TransicaoDaSagaInvalidaError: alguma conferencia falhou; nada muda.
        """
        linha = self._conferir_evento(evento, marcos, agora)
        # Calculados antes de mudar: dado malformado levanta com a saga intacta.
        diagnostico = evento.tipo == "DiagnosticoConcluido"
        itens = itens_do_diagnostico(evento.dados) if diagnostico else self._itens
        posicao = (
            evento.dados["posicao_na_fila"]
            if evento.tipo == "ExecucaoAgendada"
            else None
        )
        self._conferir_envio(linha, envio, agora, itens)
        de = self._etapa
        if linha.seguinte is not de:
            self._registrar_evento(
                EtapaDaSagaAlteradaEvent(
                    agregado_id=self.id,
                    etapa_anterior=de,
                    etapa_nova=linha.seguinte,
                    permanencia=agora - self._etapa_desde,
                    ocorrido_em=agora,
                )
            )
            self._etapa = linha.seguinte
            self._etapa_desde = agora
        self._atualizada_em = agora
        passo = self._passo(
            gatilho=evento.tipo, de=de, mensagem_id=evento.id, ator=ator, envio=envio
        )
        if posicao is not None:
            passo["posicao_na_fila"] = posicao
        self._passos = [*self._passos, passo]
        self._itens = itens
        if linha.concluido is not None:
            self._passos_concluidos = [*self._passos_concluidos, linha.concluido]
        self._esperar_resposta(envio)

    # ----- mecanica interna

    def _conferir_evento(
        self, evento: MensagemRecebida, marcos: MarcosDaOrdem, agora: datetime
    ) -> LinhaDoFluxo:
        """A linha do evento, se ele e desta OS, do fluxo e do instante certo."""
        tipo = evento.tipo
        linha = FLUXO_NORMAL.get(tipo)
        if evento.correlation_id != self.id:
            msg = f"{tipo} de outra ordem"
        elif linha is None or self.classificar(tipo, marcos) is not (
            Classificacao.PROCESSAR
        ):
            msg = f"{tipo} nao avanca a saga na etapa {self._etapa.value}"
        elif agora.tzinfo is None or agora < self._atualizada_em:
            msg = f"{tipo} num instante sem fuso ou anterior ao ultimo passo"
        else:
            return linha
        raise TransicaoDaSagaInvalidaError(msg)

    def _conferir_envio(
        self,
        linha: LinhaDoFluxo,
        envio: Envio | None,
        agora: datetime,
        itens: list[ItemDoDiagnostico],
    ) -> None:
        """O ``envio`` e o comando da ``linha``, com os dados e o prazo certos."""
        if envio is None and linha.comando is None:
            return
        if envio is None or envio.tipo is not linha.comando:
            enviado = envio.tipo if envio is not None else "nenhum comando"
            msg = f"a linha envia {linha.comando or 'nenhum comando'}, nao {enviado}"
            raise TransicaoDaSagaInvalidaError(msg)
        # Os dados que a saga guarda: os itens do diagnostico e as pecas deles.
        da_saga: dict[Comando, dict[str, Any]] = {
            Comando.GERAR_ORCAMENTO: {"itens": itens},
            Comando.RESERVAR_PECAS: {"pecas": _pecas(itens)},
        }
        esperados = {"ordem_id": str(self.id), **da_saga.get(envio.tipo, {})}
        if any(envio.dados.get(chave) != valor for chave, valor in esperados.items()):
            msg = f"{envio.tipo} com dados que nao sao os desta saga"
            raise TransicaoDaSagaInvalidaError(msg)
        prazo = envio.prazo_resposta_em
        com_prazo = envio.tipo in COMANDOS_COM_PRAZO
        if (prazo is not None) is not com_prazo or (
            prazo is not None and (prazo.tzinfo is None or prazo <= agora)
        ):
            msg = f"{envio.tipo} sem o prazo de resposta depois do envio"
            raise TransicaoDaSagaInvalidaError(msg)

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
        """O envio conferido vira o comando em voo; sem ele, nada fica em voo.

        Todo comando do fluxo normal tem resposta automatica, entao o envio
        conferido sempre traz o prazo.
        """
        if envio is None:
            self._comando_em_voo = None
            self._prazo_resposta_em = None
            self._reenvios = 0
            return
        self._comando_em_voo = {
            "tipo": envio.tipo.value,
            "dados": deepcopy(dict(envio.dados)),
            "mensagem_ids": [str(envio.id)],
            "enviado_em": self._atualizada_em.isoformat(),
        }
        self._prazo_resposta_em = envio.prazo_resposta_em
        self._reenvios = 0


def _pecas(itens: list[ItemDoDiagnostico]) -> list[dict[str, Any]]:
    """Os itens ``peca`` como ``{sku, quantidade}`` (lista vazia vale)."""
    return [
        {"sku": item["codigo"], "quantidade": item["quantidade"]}
        for item in itens
        if item["tipo"] == "peca"
    ]
