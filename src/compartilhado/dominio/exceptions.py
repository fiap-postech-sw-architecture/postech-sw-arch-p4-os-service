from __future__ import annotations


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


class EntidadeDuplicadaException(DomainException):
    def __init__(self, mensagem: str = "Entidade duplicada") -> None:
        super().__init__(codigo="ENTIDADE_DUPLICADA", mensagem=mensagem)


class FalhaAutenticacaoException(DomainException):
    def __init__(self, mensagem: str = "Falha na autenticacao") -> None:
        super().__init__(codigo="FALHA_AUTENTICACAO", mensagem=mensagem)
