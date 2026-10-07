"""O que a aplicacao enxerga da mensageria (RFC-004 secao 5, ADR-036).

O consumidor entrega ao handler do ``tipo`` uma ``MensagemRecebida`` ja
conferida (produtor, envelope e schema do contrato) e a transacao da mensagem,
e espera um ``Desfecho``. O handler grava o efeito e os comandos
(``PublicadorDeComandos.publicar_comando``) nessa transacao, e quem comita e o
consumidor, uma vez, junto com ``mensagens_processadas``: efeito, comandos e o
registro da mensagem entram juntos ou nao entram. Voltam pela fila de retry:
``FalhaTransitoriaError``, conflito de versao (``ConflitoDeConcorrenciaException``),
erro de banco (``SQLAlchemyError``), ``TimeoutError`` e ``ConnectionError``;
qualquer outra excecao do handler e erro permanente e manda a mensagem para a
DLQ. Fora do consumidor (API, prazos), o caso de uso publica pela
``UnitOfWork``, que comita. Comando fora do contrato levanta
``ContratoInvalidoError``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Any
from uuid import UUID

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class MensagemRecebida:
    """Envelope de um comando ou evento recebido, com os tipos ja convertidos.

    ``dados`` fica fora do ``repr`` (o de uma excecao ou de um log o mostraria):
    traz texto livre e tokens (``observacoes``, ``link_decisao``, ``checkout_url``,
    RFC-004 secao 5.3). E so leitura e nao entra no ``hash``.
    """

    id: UUID
    tipo: str
    versao: int
    origem: str
    correlation_id: UUID
    causation_id: UUID | None
    ocorrido_em: datetime
    dados: Mapping[str, Any] = field(repr=False, hash=False)

    @classmethod
    def do_envelope(cls, envelope: dict[str, Any]) -> MensagemRecebida:
        """Converte o envelope JSON, que o chamador ja validou contra o contrato."""
        causa = envelope["causation_id"]
        return cls(
            id=UUID(envelope["id"]),
            tipo=envelope["tipo"],
            versao=envelope["versao"],
            origem=envelope["origem"],
            correlation_id=UUID(envelope["correlation_id"]),
            causation_id=UUID(causa) if causa is not None else None,
            ocorrido_em=datetime.fromisoformat(envelope["ocorrido_em"]),
            dados=MappingProxyType(envelope["dados"]),
        )


class Comando(StrEnum):
    """Os comandos que o OS publica (RFC-004 secao 5.3); o valor e o ``tipo``."""

    SOLICITAR_DIAGNOSTICO = "SolicitarDiagnostico"
    DESCARTAR_DIAGNOSTICO = "DescartarDiagnostico"
    GERAR_ORCAMENTO = "GerarOrcamento"
    CANCELAR_ORCAMENTO = "CancelarOrcamento"
    RESERVAR_PECAS = "ReservarPecas"
    LIBERAR_RESERVA = "LiberarReserva"
    SOLICITAR_PAGAMENTO = "SolicitarPagamento"
    ESTORNAR_PAGAMENTO = "EstornarPagamento"
    AGENDAR_EXECUCAO = "AgendarExecucao"
    CANCELAR_EXECUCAO = "CancelarExecucao"
    ANONIMIZAR_VEICULO = "AnonimizarVeiculo"


class Desfecho(StrEnum):
    """Resultado de um handler; vira o label ``resultado`` da metrica de consumo."""

    PROCESSADA = "processada"
    # A mensagem nao corresponde ao estado atual (ex.: evento de etapa ja
    # passada): recebe ack sem efeito e nunca vai para a DLQ por isso.
    IGNORADA = "ignorada"


class FalhaTransitoriaError(Exception):
    """Falha que tende a passar sozinha: a mensagem volta pela fila de retry.

    Banco ou dependencia fora do ar e evento adiantado em relacao a etapa da
    saga (RFC-004 secao 4) sao os casos previstos.
    """


class ContratoInvalidoError(Exception):
    """Mensagem fora do contrato (RFC-004 secao 5.5).

    ``caminho`` (JSON path) e ``regra`` (palavra-chave do schema) dizem onde e
    por que, sem o valor recusado, que pode ser dado pessoal (placa) ou texto
    livre.
    """

    def __init__(self, motivo: str, *, caminho: str = "$", regra: str = "") -> None:
        super().__init__(f"{motivo} ({regra} em {caminho})" if regra else motivo)
        self.motivo = motivo
        self.caminho = caminho
        self.regra = regra
