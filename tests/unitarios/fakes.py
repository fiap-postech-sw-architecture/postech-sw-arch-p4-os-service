"""Fakes compartilhados dos testes unitarios de casos de uso.

``FakeUnitOfWork`` era copiado em 6 arquivos ``test_use_cases*`` (issue
p3 #173). As copias divergiam apenas no rastreio de ``rolled_back``
(cliente_veiculo); esta versao unificada e o SUPERSET: rastreia ``committed``
E ``rolled_back`` (rollback explicito ou excecao dentro do bloco ``with``,
espelhando o contrato da ``SQLAlchemyUnitOfWork`` real). Quem nao afere
``rolled_back`` simplesmente o ignora — nenhum comportamento divergiu.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.mensageria.contratos import catalogo
from src.ordem_servico.aplicacao.dtos import RetratoDoVeiculo

if TYPE_CHECKING:
    from collections.abc import Mapping
    from types import TracebackType

    from src.compartilhado.dominio.documento import Documento
    from src.compartilhado.dominio.placa import Placa
    from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

# Veiculo que o ClientePortFake devolve (placa no formato do contrato).
RETRATO = RetratoDoVeiculo(placa="BRA2E19", marca="Volkswagen", modelo="Gol", ano=2019)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        # (tipo, dados, correlation_id, causation_id) de cada publicar_comando.
        self.comandos: list[tuple[str, dict[str, Any], UUID, UUID | None]] = []
        # Envelopes montados e validados pelo contrato, como a outbox os grava.
        self.envelopes: list[dict[str, Any]] = []

    def __enter__(self) -> FakeUnitOfWork:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.rolled_back = True

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True

    def publicar_comando(
        self,
        tipo: str,
        dados: Mapping[str, Any],
        *,
        correlation_id: UUID,
        causation_id: UUID | None = None,
    ) -> UUID:
        """Monta o envelope pelo contrato (dados fora do schema levantam)."""
        envelope = catalogo().montar_envelope(
            tipo,
            dados,
            correlation_id=correlation_id,
            causation_id=causation_id,
            ocorrido_em=datetime.now(UTC),
        )
        self.envelopes.append(envelope)
        self.comandos.append((tipo, dict(dados), correlation_id, causation_id))
        return UUID(envelope["id"])


class RepoEmMemoria:
    def __init__(self, *ordens: OrdemDeServico, conflito: bool = False) -> None:
        self.ordens = {o.id: o for o in ordens}
        self.salvas: list[OrdemDeServico] = []
        self._conflito = conflito

    def provocar_conflito(self) -> None:
        """O proximo ``salvar`` ve outra transacao gravar antes (lock otimista)."""
        self._conflito = True

    def obter_por_id(self, ordem_id: UUID) -> OrdemDeServico | None:
        return self.ordens.get(ordem_id)

    def salvar(self, ordem: OrdemDeServico) -> None:
        if self._conflito:
            raise ConflitoDeConcorrenciaException()
        self.ordens[ordem.id] = ordem
        self.salvas.append(ordem)

    def listar(
        self, offset: int = 0, limit: int = 20, *, incluir_encerradas: bool = False
    ) -> list[OrdemDeServico]:
        self.args_listar = (offset, limit, incluir_encerradas)
        return list(self.ordens.values())

    def contar(self, *, incluir_encerradas: bool = False) -> int:
        self.args_contar = incluir_encerradas
        return 42


class ConsultaAcompanhamentoEspia:
    """Registra cada consulta: entrada invalida tem de deixar ``chamadas`` vazia."""

    def __init__(self, resultado: AcompanhamentoDTO | None = None) -> None:
        self.resultado = resultado
        self.chamadas: list[tuple[Placa, Documento]] = []

    def mais_recente(
        self, placa: Placa, documento: Documento
    ) -> AcompanhamentoDTO | None:
        self.chamadas.append((placa, documento))
        return self.resultado


class ClientePortFake:
    def __init__(self, *, cliente_ok: bool = True, veiculo_ok: bool = True) -> None:
        self._cliente_ok = cliente_ok
        self._veiculo_ok = veiculo_ok

    def cliente_existe(self, cliente_id: UUID) -> bool:
        return self._cliente_ok

    def retrato_do_veiculo(
        self, cliente_id: UUID, veiculo_id: UUID
    ) -> RetratoDoVeiculo | None:
        return RETRATO if self._veiculo_ok else None


class SagasEmMemoria:
    """``SagaRepository`` em memoria, com o lock otimista simulado por conflito."""

    def __init__(self, *sagas: Saga) -> None:
        self.sagas = {s.ordem_id: s for s in sagas}
        self.salvas: list[Saga] = []
        self._conflito = False

    def provocar_conflito(self) -> None:
        self._conflito = True

    def obter(self, ordem_id: UUID) -> Saga | None:
        return self.sagas.get(ordem_id)

    def salvar(self, saga: Saga) -> None:
        if self._conflito:
            raise ConflitoDeConcorrenciaException()
        self.sagas[saga.ordem_id] = saga
        self.salvas.append(saga)


class ConsultaDaOrdemEmMemoria:
    """``ConsultaDaOrdem`` sobre os repositorios em memoria."""

    def __init__(self, ordens: RepoEmMemoria, sagas: SagasEmMemoria) -> None:
        self._ordens = ordens
        self._sagas = sagas

    def com_saga(self, ordem_id: UUID) -> tuple[OrdemDeServico, Saga | None] | None:
        ordem = self._ordens.obter_por_id(ordem_id)
        return None if ordem is None else (ordem, self._sagas.obter(ordem_id))
