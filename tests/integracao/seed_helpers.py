"""Seeds compartilhados da integracao: cliente + veiculo (+ OS) e usuarios.

CPF (brutils, valido) e placa (contador monotonico) sao UNICOS por chamada:
arquivos que commitam de verdade (threads, TestClient) nao colidem com
residuo de outro teste. O caller controla a transacao — a factory cria e faz
``flush`` na session recebida (savepoint da fixture ``session`` OU session
propria com commit real).
"""

from __future__ import annotations

import itertools
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import text

from src.cliente_veiculo.dominio.cliente import Cliente
from src.cliente_veiculo.dominio.contato import Contato
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.placa import Placa
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from src.autenticacao.dominio.papel import Papel
    from src.autenticacao.dominio.usuario import Usuario

# Senha padrao dos usuarios de teste (compartilhada pelos fixtures/logins).
SENHA_PADRAO = "senhaforte1234"

_contador_placa = itertools.count(1)


def placa_unica(prefixo: str = "ITG") -> str:
    """Placa unica no padrao antigo (3 letras + 4 digitos).

    O ``prefixo`` (3 letras) identifica o arquivo/cenario no banco em caso de
    residuo; o contador (0001-9999) garante unicidade dentro da sessao.
    """
    n = next(_contador_placa) % 10000
    return f"{prefixo}{n:04d}"


def cpf_unico() -> str:
    """CPF valido e unico por chamada (gerador do brutils)."""
    from brutils.cpf import generate as gerar_cpf

    return gerar_cpf()


def criar_cliente_com_veiculo(
    sessao: Session,
    *,
    nome: str = "Cliente Teste",
    contato: str = "11999990000",
    cpf: str | None = None,
    placa: str | None = None,
) -> Cliente:
    """Cria cliente (CPF unico por default) + 1 veiculo (placa unica) e flusha.

    Retorna o agregado; o veiculo fica em ``cliente.veiculos[0]``.
    """
    from src.cliente_veiculo.infraestrutura.repository import (
        ClienteSQLAlchemyRepository,
    )

    cliente = Cliente(
        _nome=nome,
        _documento=CPF(numero=cpf or cpf_unico()),
        _contato=Contato(valor=contato),
    )
    ClienteSQLAlchemyRepository(session=sessao).salvar(cliente)
    cliente.adicionar_veiculo(
        placa=Placa(valor=placa or placa_unica()),
        marca="Fiat",
        modelo="Uno",
        ano=2020,
    )
    sessao.flush()
    return cliente


def criar_ordem_recebida(
    sessao: Session,
    *,
    cliente_id: UUID,
    veiculo_id: UUID,
    limpar_eventos: bool = True,
) -> OrdemDeServico:
    """Abre uma OS RECEBIDA para o par cliente/veiculo e flusha.

    ``limpar_eventos=True`` (default) descarta o evento de abertura — a maioria
    dos cenarios nao quer exercitar a outbox no seed.
    """
    ordem = OrdemDeServico.abrir(
        cliente_id=cliente_id,
        veiculo_id=veiculo_id,
        descricao_problema="Barulho na suspensao dianteira",
        ator="atendente-teste",
    )
    if limpar_eventos:
        ordem.limpar_eventos()
    from src.ordem_servico.infraestrutura.repository import (
        OrdemDeServicoSQLAlchemyRepository,
    )

    OrdemDeServicoSQLAlchemyRepository(session=sessao).salvar(ordem)
    sessao.flush()
    return ordem


def criar_usuario(
    session_factory: sessionmaker[Session], *, email: str, papel: Papel
) -> Usuario:
    """Cria e COMMITA um usuario com ``SENHA_PADRAO`` (para login via API)."""
    from src.autenticacao.dominio.usuario import Usuario
    from src.autenticacao.infraestrutura.password_hasher import hash_senha
    from src.autenticacao.infraestrutura.repository import UsuarioSQLAlchemyRepository

    with session_factory() as sess:
        usuario = Usuario.criar(
            email=email, senha_hash=hash_senha(SENHA_PADRAO), papel=papel
        )
        UsuarioSQLAlchemyRepository(session=sess).salvar(usuario)
        sess.commit()
    return usuario


@contextmanager
def outbox_recusando_insert(engine: Engine) -> Iterator[None]:
    """Trigger que falha todo INSERT na outbox: a transacao inteira tem de cair."""
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "CREATE FUNCTION recusar_outbox() RETURNS trigger LANGUAGE plpgsql "
                "AS $$ BEGIN RAISE EXCEPTION 'outbox indisponivel'; END $$"
            )
        )
        conexao.execute(
            text(
                "CREATE TRIGGER recusar_outbox BEFORE INSERT ON outbox "
                "FOR EACH ROW EXECUTE FUNCTION recusar_outbox()"
            )
        )
    try:
        yield
    finally:
        with engine.begin() as conexao:
            conexao.execute(text("DROP TRIGGER recusar_outbox ON outbox"))
            conexao.execute(text("DROP FUNCTION recusar_outbox()"))


@contextmanager
def outbox_recusando_no_commit(engine: Engine) -> Iterator[None]:
    """Trigger DEFERIDO: o INSERT na outbox passa e o COMMIT falha.

    A falha vem depois de todas as escritas da transacao (OS, saga, historico e
    a propria linha da outbox), entao so a transacao unica desfaz tudo.
    """
    with engine.begin() as conexao:
        conexao.execute(
            text(
                "CREATE FUNCTION recusar_outbox_no_commit() RETURNS trigger "
                "LANGUAGE plpgsql AS $$ BEGIN "
                "RAISE EXCEPTION 'outbox indisponivel no commit'; END $$"
            )
        )
        conexao.execute(
            text(
                "CREATE CONSTRAINT TRIGGER recusar_outbox_no_commit "
                "AFTER INSERT ON outbox DEFERRABLE INITIALLY DEFERRED "
                "FOR EACH ROW EXECUTE FUNCTION recusar_outbox_no_commit()"
            )
        )
    try:
        yield
    finally:
        with engine.begin() as conexao:
            conexao.execute(text("DROP TRIGGER recusar_outbox_no_commit ON outbox"))
            conexao.execute(text("DROP FUNCTION recusar_outbox_no_commit()"))
