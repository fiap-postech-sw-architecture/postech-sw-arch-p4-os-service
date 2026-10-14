"""Portas de saida (Protocol) que a aplicacao OrdemDeServico consome.

Definidas no contexto consumidor e implementadas na infraestrutura:
``ClientePort`` em ``adapters.py`` (Anti-Corruption Layer: consulta o contexto
Cliente+Veiculo sem importar o agregado vizinho), ``ConsultaAcompanhamento`` e
``ConsultaDaOrdem`` em ``consultas.py`` (query services de leitura, fora do
repositorio do agregado), ``SagaRepository`` em ``repository.py`` e
``PresencaDoConsumidor`` em ``presenca_do_consumidor.py`` (a unica que fala com
o broker, para a camada de aplicacao nao importar o cliente AMQP).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Collection
    from datetime import datetime, timedelta
    from uuid import UUID

    from src.compartilhado.dominio.documento import Documento
    from src.compartilhado.dominio.placa import Placa
    from src.ordem_servico.aplicacao.dtos import (
        AcompanhamentoDTO,
        ContatoDoCliente,
        RetratoDoVeiculo,
    )
    from src.ordem_servico.aplicacao.notificacoes import Notificacao
    from src.ordem_servico.aplicacao.saga.saga import Saga
    from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico


class ClientePort(Protocol):
    """Porta para validar cliente e veiculo no contexto Cliente+Veiculo."""

    # corpos `pass` (nao `...`) evitam o FP CodeQL py/ineffectual-statement
    def cliente_existe(self, cliente_id: UUID) -> bool:
        """Indica se o cliente existe e esta ativo."""
        pass

    def retrato_do_veiculo(
        self, cliente_id: UUID, veiculo_id: UUID
    ) -> RetratoDoVeiculo | None:
        """Placa, marca, modelo e ano do veiculo do cliente.

        ``None`` se o veiculo nao existe ou e de outro cliente (casos que a
        abertura nao distingue).
        """
        pass

    def obter_contato(self, cliente_id: UUID) -> ContatoDoCliente | None:
        """Nome e contato do cliente, por projecao das colunas, sem o agregado.

        Nao carrega o ``Cliente`` nem decifra o ``documento``: o processo do
        e-mail nao recebe a chave de cifragem (ADR-042). ``None`` se o cliente
        nao existe ou foi anonimizado (o contato virou marcador).
        """
        pass


class ConsultaAcompanhamento(Protocol):
    """Query service do acompanhamento publico (projecao, nao o agregado)."""

    def mais_recente(
        self, placa: Placa, documento: Documento
    ) -> AcompanhamentoDTO | None:
        """Status e timestamps da OS mais recente do par, ou ``None``.

        Recebe os VOs ja validados: quem chama garante que documento e placa
        invalidos nunca chegam ao banco.
        """
        pass


class ConsultaDaOrdem(Protocol):
    """Leitura da OS com a saga dela numa consulta so (RFC-004 secao 4).

    O status e a etapa saem do mesmo instante: um commit do consumidor entre
    duas leituras mostraria o status velho com a etapa nova.
    """

    def com_saga(self, ordem_id: UUID) -> tuple[OrdemDeServico, Saga | None] | None:
        """A OS e a saga (``None`` sem saga), ou ``None`` sem a OS."""
        pass


class SagaRepository(Protocol):
    """Persistencia da saga (agregado proprio, RFC-004 secao 4), sob a transacao."""

    def obter(self, ordem_id: UUID) -> Saga | None:
        """A saga da OS ``ordem_id``, ou ``None``."""
        pass

    def salvar(self, saga: Saga) -> None:
        """Persiste a saga com lock otimista pela ``versao``.

        Raises:
            ConflitoDeConcorrenciaException: outra transacao gravou a mesma
                saga desde a leitura.
        """
        pass

    def reivindicar_vencida(
        self, agora: datetime, prazo: timedelta, *, ignorar: Collection[UUID] = ()
    ) -> Saga | None:
        """A saga de prazo vencido mais antiga, travada para esta transacao.

        So vence a saga cujo envio mais recente (o ultimo de
        ``comando_em_voo.mensagem_ids``) ja saiu da outbox: a linha esta
        entregue e ``entregue_em + prazo <= agora``. Envio pendente ou ``dead``
        nao conta (RFC-004 secao 4.6). Duas replicas nunca recebem a mesma saga
        (``FOR UPDATE SKIP LOCKED``): a travada fica de fora da consulta da outra
        ate o commit ou o rollback. ``ignorar`` tira da consulta as sagas que
        falharam neste ciclo.

        Returns:
            A saga travada, ou ``None`` quando nenhuma venceu.
        """
        pass


class PresencaDoConsumidor(Protocol):
    """Se o consumidor do OS esta de pe: a resposta pode estar esperando na fila."""

    def ha_consumidor(self) -> bool:
        """``False`` com ``os.eventos`` sem consumidor ou com o broker fora.

        Nos dois casos o ``prazos`` nao reenvia nem esgota prazo no ciclo
        (RFC-004 secao 4.6): quem acusa a demora e o alerta de saga parada.
        """
        pass


class PublicadorDeNotificacoes(Protocol):
    """Grava o pedido de e-mail na transacao em curso, sem comita-la.

    Ao lado do ``publicar_comando``: a linha entra no commit da transicao (o
    consumidor, com ``mensagens_processadas``; a API, com a UoW) e o processo
    ``notificador`` a entrega depois (RFC-004 secao 7.2).
    """

    def registrar(self, notificacao: Notificacao) -> None:
        """Grava o pedido; o commit e de quem chama."""
        pass


class FalhaEnvioEmailError(Exception):
    """O servidor SMTP nao aceitou o e-mail (conexao, prazo ou protocolo).

    A mensagem da excecao de origem pode trazer o endereco do destinatario:
    quem a trata registra so o nome da classe.
    """


class EmailPort(Protocol):
    """Envio de um e-mail de texto (SMTP; Mailpit na demonstracao)."""

    def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
        """Entrega o e-mail ao servidor SMTP.

        Raises:
            FalhaEnvioEmailError: o servidor nao aceitou a mensagem (conexao,
                prazo ou protocolo); a linha conta uma tentativa.
        """
        pass
