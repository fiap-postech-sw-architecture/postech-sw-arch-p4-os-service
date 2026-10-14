r"""O ``papeis.sql`` e o log do servidor PostgreSQL.

O psql troca o ``\getenv`` pela senha antes de enviar o ``CREATE ROLE``, e o
servidor a recebe em claro. Cada ``SET`` do inicio do script fecha um caminho do
log por onde ela sairia: o log de comandos, a linha ``STATEMENT`` de um comando
que falha e o log por duracao ou por amostragem. O servidor sobe com todos
ligados (o pior caso), e cada teste usa o seu banco: o controle, que tira um
``SET`` de proposito, deixa a senha no log desse banco.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from tests.integracao.banco_do_manifesto import Banco, subir_banco

if TYPE_CHECKING:
    from collections.abc import Iterator

# Roda o script do manifesto de novo, como o superusuario, pelo loopback: o
# CREATE ROLE de cada papel falha ("already exists"), o que gera a linha
# STATEMENT. O ``\getenv`` le o ambiente do container. ``grep -v`` tira do
# script o SET do controle.
_RODAR_O_SCRIPT = (
    "grep -v '{sem}' /docker-entrypoint-initdb.d/papeis.sql "
    '| PGPASSWORD="$POSTGRES_PASSWORD" psql -w -h 127.0.0.1 -U postgres -d os'
)


@pytest.fixture
def banco() -> Iterator[Banco]:
    """O banco recem-iniciado pelo script de init, sem migracao."""
    with subir_banco(migrar=False) as banco:
        yield banco


def _senhas_no_log(banco: Banco) -> list[str]:
    log = banco.log_do_servidor()
    return [chave for chave, senha in banco.senhas.items() if senha in log]


def _rodar_o_script(banco: Banco, *, sem: str = "^$") -> str:
    """O script de novo, sem as linhas que casam ``sem``; devolve a saida do psql."""
    resultado = banco.no_container("sh", "-c", _RODAR_O_SCRIPT.format(sem=sem))
    return str(resultado.output.decode())


def test_nenhuma_senha_chega_ao_log_no_init(banco: Banco) -> None:
    log = banco.log_do_servidor()

    # O proprio SET sai no log: o log de comandos valia na sessao do script.
    assert "statement: SET log_statement = 'none';" in log
    assert _senhas_no_log(banco) == []


def test_comando_do_script_que_falha_nao_leva_a_senha_ao_log(banco: Banco) -> None:
    saida = _rodar_o_script(banco)

    assert 'role "os" already exists' in saida
    # O erro tambem foi para o log do servidor, so que sem a linha STATEMENT.
    assert 'ERROR:  role "os" already exists' in banco.log_do_servidor()
    assert _senhas_no_log(banco) == []


def test_controle_sem_o_set_do_erro_a_senha_vai_na_linha_statement(
    banco: Banco,
) -> None:
    # O teste acima so vale se, sem o SET, a senha aparecesse: o controle roda o
    # mesmo script sem a linha do log_min_error_statement.
    _rodar_o_script(banco, sem="log_min_error_statement")

    log = banco.log_do_servidor()
    assert "STATEMENT:  CREATE ROLE os LOGIN PASSWORD '" in log
    assert _senhas_no_log(banco) != []
