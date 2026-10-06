"""Porta de persistencia da ``OrdemDeServico`` (Protocol)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from uuid import UUID

    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


class OrdemDeServicoRepository(Protocol):
    """Contrato de persistencia do agregado ``OrdemDeServico`` (sob a UoW)."""

    # corpos `pass` (nao `...`) evitam o FP CodeQL py/ineffectual-statement
    def obter_por_id(self, ordem_id: UUID) -> OrdemDeServico | None:
        """Retorna a ordem (com o historico) pelo id, ou ``None``."""
        pass

    def salvar(self, ordem: OrdemDeServico) -> None:
        """Persiste a ordem com lock otimista pela ``versao``.

        Raises:
            ConflitoDeConcorrenciaException: outra transacao gravou a mesma
                ordem desde a leitura (versao divergente).
        """
        pass

    def listar(
        self, offset: int = 0, limit: int = 20, *, incluir_encerradas: bool = False
    ) -> list[OrdemDeServico]:
        """Pagina por prioridade de status e antiguidade (mais antiga primeiro).

        Por padrao exclui FINALIZADA, ENTREGUE e CANCELADA;
        ``incluir_encerradas=True`` as devolve ao final da ordenacao.
        """
        pass

    def contar(self, *, incluir_encerradas: bool = False) -> int:
        """Total do mesmo universo de ``listar`` (paginacao consistente)."""
        pass

    def obter_mais_recente_por_placa_e_documento(
        self, placa: str, documento: str
    ) -> OrdemDeServico | None:
        """Ordem mais recente do veiculo ``placa`` do cliente ``documento``."""
        pass
