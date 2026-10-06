from __future__ import annotations

from typing import ClassVar


class DomainException(Exception):
    def __init__(self, codigo: str, mensagem: str) -> None:
        self.codigo = codigo
        self.mensagem = mensagem
        super().__init__(mensagem)


class EntidadeNaoEncontradaException(DomainException):
    def __init__(self, mensagem: str = "Entidade nao encontrada") -> None:
        super().__init__(codigo="ENTIDADE_NAO_ENCONTRADA", mensagem=mensagem)


class ViolacaoRegraDeNegocioException(DomainException):
    def __init__(self, mensagem: str = "Violacao de regra de negocio") -> None:
        super().__init__(codigo="VIOLACAO_REGRA_NEGOCIO", mensagem=mensagem)


class TransicaoStatusInvalidaException(DomainException):
    def __init__(self, mensagem: str = "Transicao de status invalida") -> None:
        super().__init__(codigo="TRANSICAO_STATUS_INVALIDA", mensagem=mensagem)


class ConflitoDeConcorrenciaException(DomainException):
    """Escrita concorrente detectada pelo lock otimista (versao do agregado).

    Outra transacao alterou o agregado entre a leitura e a gravacao; o
    chamador deve reler o estado e decidir de novo (contramedida *reread
    value* do material de SAGA).
    """

    def __init__(
        self,
        mensagem: str = "Registro alterado por outra operacao; releia e tente de novo",
    ) -> None:
        super().__init__(codigo="CONFLITO_DE_CONCORRENCIA", mensagem=mensagem)


class ValorInvalidoException(DomainException):
    """Invariante de agregado violada pelos dados recebidos (422).

    Separa a regra de dominio do ``ValueError`` generico, que tambem nasce de
    driver e de bug.
    """

    def __init__(self, mensagem: str = "Valor invalido") -> None:
        super().__init__(codigo="VALOR_INVALIDO", mensagem=mensagem)


class EntidadeDuplicadaException(DomainException):
    def __init__(self, mensagem: str = "Entidade duplicada") -> None:
        super().__init__(codigo="ENTIDADE_DUPLICADA", mensagem=mensagem)


class FalhaAutenticacaoException(DomainException):
    """401 de credencial: a mensagem publica e sempre a mesma (ADR-039).

    Vale para o login, o refresh e o gate de qualquer rota autenticada: o
    codigo (``NAO_AUTENTICADO``) e a mensagem sao os de Billing e Execucao.
    ``motivo`` (identificador em ingles) diz o que falhou e vai so para o
    log: a resposta nao da dica a quem testa credenciais.
    """

    MENSAGEM: ClassVar[str] = "Credencial ausente, invalida ou expirada"

    def __init__(self, motivo: str = "authentication_failed") -> None:
        super().__init__(codigo="NAO_AUTENTICADO", mensagem=self.MENSAGEM)
        self.motivo = motivo


class AcessoNegadoException(DomainException):
    """403: papel valido, mas sem permissao para a rota (ADR-039).

    Papel ausente ou desconhecido no token e falha de credencial
    (``FalhaAutenticacaoException``), nao esta.
    """

    def __init__(self, mensagem: str = "Papel nao autorizado") -> None:
        super().__init__(codigo="ACESSO_NEGADO", mensagem=mensagem)
