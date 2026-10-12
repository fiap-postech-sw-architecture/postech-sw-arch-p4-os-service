"""Leitura do estado da saga: as propriedades de consulta do agregado.

Ficam fora da classe ``Saga`` para ela ter so o comportamento (os mutadores e
as regras): nenhuma muda estado, e as listas e dicts saem como copia (a coluna
JSONB nao detecta mutacao no lugar). Os campos ficam na ``Saga``, que os
persiste.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

from src.ordem_servico.aplicacao.saga.tabela_da_saga import ETAPAS_FINAIS

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from src.ordem_servico.aplicacao.saga.modelo import (
        ComandoEmVoo,
        EtapaSaga,
        ItemDoDiagnostico,
        RegistroDaSaga,
    )


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
