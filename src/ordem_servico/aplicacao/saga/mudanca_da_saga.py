"""Mecanica da saga: aplica a transicao ja conferida (RFC-004 secoes 4.1 e 4.4).

Fica fora da ``Saga`` para ela ter so as regras (os mutadores publicos, que
conferem a transicao, o envio e o prazo antes de mudar). Aqui a transicao
conferida vira etapa, registro, comando em voo, prazo, fatos de dominio e
invariantes, sempre pela mesma ordem; listas e dicts sao trocados por novos
(a coluna JSONB nao detecta mutacao no lugar).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from src.compartilhado.dominio.aggregate_root import AggregateRoot
from src.ordem_servico.aplicacao.saga.leitura_da_saga import LeituraDaSaga
from src.ordem_servico.aplicacao.saga.modelo import (
    CompensacaoIniciadaEvent,
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    MotivoCompensacao,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import ANTES_DO_PIVOT
from src.ordem_servico.aplicacao.saga.transicoes import (
    conferir_estado,
    conferir_gatilho,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.ordem_servico.aplicacao.saga.modelo import Envio, Gatilho, Transicao


@dataclass(frozen=True, slots=True)
class Complemento:
    """O que a transicao nao diz e o registro ou o comando em voo levam.

    ``anteriores`` sao os ids dos envios que o comando novo herda (o reenvio e a
    retomada); ``reenvio``, o numero do reenvio por prazo; ``posicao``, a da fila
    de execucao no registro do ``ExecucaoAgendada``.
    """

    anteriores: Sequence[str] = ()
    reenvio: int | None = None
    posicao: int | None = None


class MudancaDaSaga(AggregateRoot, LeituraDaSaga):
    """Base da ``Saga``: aplica a transicao conferida e reaplica as invariantes."""

    def _mudar(
        self,
        transicao: Transicao,
        gatilho: Gatilho,
        envio: Envio | None,
        complemento: Complemento = Complemento(),  # noqa: B008  # VO imutavel
    ) -> None:
        """Aplica a transicao ja conferida e anota o registro dela."""
        conferir_gatilho(transicao, gatilho)
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
        if complemento.posicao is not None:
            passo["posicao_na_fila"] = complemento.posicao
        if complemento.reenvio is not None:
            passo["reenvio"] = complemento.reenvio
        self._passos = [*self._passos, passo]
        if transicao.concluido is not None:
            self._passos_concluidos = [*self._passos_concluidos, transicao.concluido]
        self._esperar_resposta(envio, complemento)
        self._conferir_invariantes()

    def _esperar_resposta(self, envio: Envio | None, complemento: Complemento) -> None:
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
            em_voo = envio.em_voo(self._atualizada_em)
            em_voo["mensagem_ids"] = [*complemento.anteriores, *em_voo["mensagem_ids"]]
            self._comando_em_voo = em_voo
            self._prazo_resposta_em = envio.prazo_resposta_em
            self._reenvios = complemento.reenvio or 0

    def _conferir_invariantes(self) -> None:
        conferir_estado(self)
