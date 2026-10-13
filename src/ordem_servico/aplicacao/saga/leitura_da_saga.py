"""Leitura do estado da saga: as propriedades e a consulta do agregado.

Ficam fora da classe ``Saga`` para ela ter so o comportamento (os mutadores e
as regras): nenhuma muda estado, e as listas e dicts saem como copia (a coluna
JSONB nao detecta mutacao no lugar). Os campos ficam na ``Saga``, que os
persiste.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, cast

from src.ordem_servico.aplicacao.saga.classificacao import Classificacao, classificar
from src.ordem_servico.aplicacao.saga.compensacao import dados_da_compensacao
from src.ordem_servico.aplicacao.saga.tabela_da_saga import ETAPAS_FINAIS
from src.ordem_servico.aplicacao.saga.transicoes import transicao_do_cancelamento

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.compartilhado.aplicacao.mensageria import Comando
    from src.ordem_servico.aplicacao.saga.contexto_de_envio import ComandoParaEnviar
    from src.ordem_servico.aplicacao.saga.modelo import (
        ComandoEmVoo,
        EtapaSaga,
        ItemDoDiagnostico,
        RegistroDaSaga,
    )
    from src.ordem_servico.dominio.marcos import MarcosDaOrdem


class LeituraDaSaga:
    """Propriedades de leitura da ``Saga`` sobre os campos dela."""

    id: UUID
    _etapa: EtapaSaga
    _iniciada_em: datetime
    _etapa_desde: datetime
    _atualizada_em: datetime
    _motivo: str | None
    _falha: str | None
    _passos: list[RegistroDaSaga]
    _passos_concluidos: list[str]
    _comando_em_voo: ComandoEmVoo | None
    _plano_compensacao: list[str]
    _itens: list[ItemDoDiagnostico]
    _reenvios: int
    _prazo_resposta_em: datetime | None
    _traceparent: str | None
    _versao: int

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
    def passos(self) -> tuple[RegistroDaSaga, ...]:
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
    def tipo_do_comando_em_voo(self) -> str | None:
        """O tipo do comando com prazo a espera de resposta, ou ``None``."""
        return self._comando_em_voo["tipo"] if self._comando_em_voo else None

    @property
    def plano_compensacao(self) -> tuple[str, ...]:
        """Compensacoes restantes; a primeira e a pendente."""
        return tuple(self._plano_compensacao)

    @property
    def itens(self) -> tuple[ItemDoDiagnostico, ...]:
        """Copia dos itens do ``DiagnosticoConcluido`` (vazio antes dele)."""
        return tuple(deepcopy(self._itens))

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

    def primeira_compensacao_do_cancelamento(
        self, marcos: MarcosDaOrdem, *, agora: datetime
    ) -> ComandoParaEnviar:
        """A primeira compensacao do plano do cancelamento, pronta para enviar.

        E o ``envio`` que ``Saga.solicitar_cancelamento`` pede, com os ``dados``
        que ela confere (RFC-004 secoes 4.4 e 5.3); nada muda.

        Raises:
            TransicaoDaSagaInvalidaError: etapa que nao e de antes do pivot, ou
                instante sem fuso ou que volta antes do ultimo registro.
        """
        transicao = transicao_do_cancelamento(self, agora)
        comando = cast("Comando", transicao.comando)
        return comando, dados_da_compensacao(comando, self.id, transicao.motivo, marcos)

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
