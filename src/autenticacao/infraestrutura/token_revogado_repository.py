from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert

from src.autenticacao.dominio.token_revogado import TokenRevogado
from src.autenticacao.infraestrutura.mapping import tokens_revogados_table

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class TokenRevogadoSQLAlchemyRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def revogar(self, jti: str) -> bool:
        """Revoga o jti. True se revogou agora; False se ja estava revogado.

        Idempotente (p3 #121): logout duplo ou retry nao estoura o UNIQUE do jti
        (IntegrityError -> 500 num fluxo trivial). O retorno bool (p3 #167)
        distingue "revoguei agora" de "ja estava revogado": o fluxo de refresh
        usa False para negar o segundo uso do mesmo token (uso unico).

        Seguro em corrida: o ``INSERT ... ON CONFLICT (jti) DO NOTHING`` deixa o
        banco decidir quem revoga primeiro. Quem perde, porque outra transacao
        revogou o mesmo jti (commitada ou ainda em curso), recebe False em vez
        do erro de UNIQUE que um check-then-insert deixava escapar como 500.
        """
        token = TokenRevogado.criar(jti=jti)
        # Pela conexao da transacao da session: o INSERT e Core, e so o resultado
        # de Connection.execute traz o ``rowcount`` tipado.
        resultado = self._session.connection().execute(
            insert(tokens_revogados_table)
            .values(id=token.id, jti=token.jti, revogado_em=token.revogado_em)
            .on_conflict_do_nothing(index_elements=["jti"])
        )
        return resultado.rowcount == 1

    def esta_revogado(self, jti: str) -> bool:
        stmt = select(exists().where(tokens_revogados_table.c.jti == jti))
        return bool(self._session.scalar(stmt))
