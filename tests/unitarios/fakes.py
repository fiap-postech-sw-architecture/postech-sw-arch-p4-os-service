"""Fakes compartilhados dos testes unitarios de casos de uso.

``FakeUnitOfWork`` era copiado em 6 arquivos ``test_use_cases*`` (issue
p3 #173). As copias divergiam apenas no rastreio de ``rolled_back``
(cliente_veiculo); esta versao unificada e o SUPERSET: rastreia ``committed``
E ``rolled_back`` (rollback explicito ou excecao dentro do bloco ``with``,
espelhando o contrato da ``SQLAlchemyUnitOfWork`` real). Quem nao afere
``rolled_back`` simplesmente o ignora — nenhum comportamento divergiu.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException

if TYPE_CHECKING:
    from types import TracebackType
    from uuid import UUID

    from src.compartilhado.dominio.documento import Documento
    from src.compartilhado.dominio.placa import Placa
    from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False

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

    def veiculo_pertence_ao_cliente(self, cliente_id: UUID, veiculo_id: UUID) -> bool:
        return self._veiculo_ok
