"""O ciclo dos prazos sobre a saga e a OS em memoria, com relogio manual.

Compartilhado pelos testes do ciclo (``test_vencer_prazos``) e da propriedade
(``test_propriedades_dos_prazos``): o ``VencerPrazos`` real, a presenca do
consumidor que o teste liga e desliga e uma transacao fake que valida cada
envelope contra o contrato.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.vencer_prazos import (
    ResultadoDoCiclo,
    VencerPrazos,
)
from tests.unitarios.fakes import PresencaFake, TransacaoDosPrazosFake

if TYPE_CHECKING:
    from tests.unitarios.ordem_servico.cenario_da_saga import CenarioDaSaga

PRAZO = timedelta(seconds=120)
MAXIMO = 5


class RelogioManual:
    """O relogio do ``prazos``: so anda quando o teste manda."""

    def __init__(self, agora: datetime) -> None:
        self.agora = agora

    def __call__(self) -> datetime:
        return self.agora


class Prazos:
    """O ``VencerPrazos`` real sobre o cenario, com o que o teste observa."""

    def __init__(
        self,
        cenario: CenarioDaSaga,
        *,
        ha_consumidor: bool = True,
        maximo: int = MAXIMO,
        span: Any = None,
        na_outbox_do_cenario: bool = False,
    ) -> None:
        self.cenario = cenario
        self.relogio = RelogioManual(cenario.saga.atualizada_em)
        # O relogio do banco acompanha o do processo; ``adiantamento`` e quanto o
        # do processo esta na frente dele (negativo: atrasado).
        self.adiantamento = timedelta(0)
        self.transacao = TransacaoDosPrazosFake(
            cenario.ordens,
            cenario.sagas,
            cenario.publicador if na_outbox_do_cenario else None,
            banco=lambda: self.relogio.agora - self.adiantamento,
        )
        self.presenca = PresencaFake(ha_consumidor=ha_consumidor)
        contexto = ContextoDeEnvio(PRAZO, self.relogio)
        if span is not None:
            contexto = ContextoDeEnvio(PRAZO, self.relogio, span)
        self.caso_de_uso = VencerPrazos(
            abrir=lambda: self.transacao,
            presenca=self.presenca,
            contexto=contexto,
            max_reenvios=maximo,
        )

    @property
    def saga(self) -> Any:
        return self.cenario.saga

    def vencer(self, ao_tratar: Any = None) -> ResultadoDoCiclo:
        """Um ciclo no instante em que o prazo da saga vence."""
        if self.saga.prazo_resposta_em is not None:
            self.relogio.agora = max(self.relogio.agora, self.saga.prazo_resposta_em)
        return (
            self.caso_de_uso.executar(ao_tratar)
            if ao_tratar
            else self.caso_de_uso.executar()
        )

    @property
    def comandos(self) -> list[tuple[str, dict[str, Any], UUID, UUID | None]]:
        return self.transacao.comandos
