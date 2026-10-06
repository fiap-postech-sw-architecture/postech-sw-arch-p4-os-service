from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest

from src.autenticacao.dominio.papel import Papel
from src.autenticacao.dominio.usuario import Usuario
from src.cliente_veiculo.dominio.cliente import Cliente
from src.cliente_veiculo.dominio.cnpj import CNPJ
from src.cliente_veiculo.dominio.contato import Contato
from src.cliente_veiculo.dominio.cpf import CPF
from src.cliente_veiculo.dominio.documento_anonimizado import DocumentoAnonimizado
from src.cliente_veiculo.dominio.placa import Placa

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

pytestmark = pytest.mark.integracao


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _criar_cliente_cpf(
    session: Session,
    nome: str = "Maria Silva",
    cpf_numero: str = "21249722519",
    contato: str = "11999990000",
) -> Cliente:
    from src.cliente_veiculo.infraestrutura.repository import (
        ClienteSQLAlchemyRepository,
    )

    repo = ClienteSQLAlchemyRepository(session=session)
    cliente = Cliente(
        _nome=nome,
        _documento=CPF(numero=cpf_numero),
        _contato=Contato(valor=contato),
    )
    repo.salvar(cliente)
    return cliente


def _criar_cliente_cnpj(
    session: Session,
    nome: str = "Oficina Ltda",
    cnpj_numero: str = "11222333000181",
    contato: str = "1133330000",
) -> Cliente:
    from src.cliente_veiculo.infraestrutura.repository import (
        ClienteSQLAlchemyRepository,
    )

    repo = ClienteSQLAlchemyRepository(session=session)
    cliente = Cliente(
        _nome=nome,
        _documento=CNPJ(numero=cnpj_numero),
        _contato=Contato(valor=contato),
    )
    repo.salvar(cliente)
    return cliente


# ===========================================================================
# 1. Autenticacao
# ===========================================================================


class TestUsuarioRepository:
    def test_salvar_e_obter_por_id(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.repository import (
            UsuarioSQLAlchemyRepository,
        )

        repo = UsuarioSQLAlchemyRepository(session=session)
        usuario = Usuario.criar(
            email="admin@pytstop.com",
            senha_hash="hashed_password_123",
            papel=Papel.ADMIN,
        )
        repo.salvar(usuario)

        resultado = repo.obter_por_id(usuario.id)

        assert resultado is not None
        assert resultado.email == "admin@pytstop.com"
        assert resultado.senha_hash == "hashed_password_123"
        assert resultado.papel == Papel.ADMIN

    def test_obter_por_email(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.repository import (
            UsuarioSQLAlchemyRepository,
        )

        repo = UsuarioSQLAlchemyRepository(session=session)
        usuario = Usuario.criar(
            email="mecanico@pytstop.com",
            senha_hash="hashed_password_456",
            papel=Papel.MECANICO,
        )
        repo.salvar(usuario)

        resultado = repo.obter_por_email("mecanico@pytstop.com")

        assert resultado is not None
        assert resultado.id == usuario.id
        assert resultado.papel == Papel.MECANICO

    def test_obter_por_email_inexistente(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.repository import (
            UsuarioSQLAlchemyRepository,
        )

        repo = UsuarioSQLAlchemyRepository(session=session)

        resultado = repo.obter_por_email("naoexiste@pytstop.com")

        assert resultado is None

    def test_email_existe(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.repository import (
            UsuarioSQLAlchemyRepository,
        )

        repo = UsuarioSQLAlchemyRepository(session=session)
        usuario = Usuario.criar(
            email="check@pytstop.com",
            senha_hash="hashed_password_789",
            papel=Papel.ADMIN,
        )
        repo.salvar(usuario)

        assert repo.email_existe("check@pytstop.com") is True
        assert repo.email_existe("outro@pytstop.com") is False

    def test_email_unico_constraint(self, session: Session) -> None:
        from sqlalchemy.exc import IntegrityError

        from src.autenticacao.infraestrutura.repository import (
            UsuarioSQLAlchemyRepository,
        )

        repo = UsuarioSQLAlchemyRepository(session=session)
        usuario1 = Usuario.criar(
            email="duplicado@pytstop.com",
            senha_hash="hash_a",
            papel=Papel.ADMIN,
        )
        repo.salvar(usuario1)

        usuario2 = Usuario.criar(
            email="duplicado@pytstop.com",
            senha_hash="hash_b",
            papel=Papel.ADMIN,
        )
        with pytest.raises(IntegrityError):
            repo.salvar(usuario2)
        # IntegrityError rolls back the SAVEPOINT; recover session state.
        session.rollback()

    def test_salvar_com_papel_atendente(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.repository import (
            UsuarioSQLAlchemyRepository,
        )

        repo = UsuarioSQLAlchemyRepository(session=session)
        usuario = Usuario.criar(
            email="atendente@pytstop.com",
            senha_hash="hashed_password_atd",
            papel=Papel.ATENDENTE,
        )
        repo.salvar(usuario)

        resultado = repo.obter_por_id(usuario.id)
        assert resultado is not None
        assert resultado.papel == Papel.ATENDENTE

    def test_obter_por_id_inexistente(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.repository import (
            UsuarioSQLAlchemyRepository,
        )

        repo = UsuarioSQLAlchemyRepository(session=session)

        resultado = repo.obter_por_id(uuid4())

        assert resultado is None


class TestTokenRevogadoRepository:
    def test_revogar_e_verificar(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.token_revogado_repository import (
            TokenRevogadoSQLAlchemyRepository,
        )

        repo = TokenRevogadoSQLAlchemyRepository(session=session)
        jti = "token-jti-abc-123"

        repo.revogar(jti)

        assert repo.esta_revogado(jti) is True

    def test_token_nao_revogado(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.token_revogado_repository import (
            TokenRevogadoSQLAlchemyRepository,
        )

        repo = TokenRevogadoSQLAlchemyRepository(session=session)

        assert repo.esta_revogado("jti-que-nao-existe") is False

    def test_revogar_multiplos_tokens(self, session: Session) -> None:
        from src.autenticacao.infraestrutura.token_revogado_repository import (
            TokenRevogadoSQLAlchemyRepository,
        )

        repo = TokenRevogadoSQLAlchemyRepository(session=session)
        jti_1 = "token-jti-multi-1"
        jti_2 = "token-jti-multi-2"
        jti_3 = "token-jti-multi-3"

        repo.revogar(jti_1)
        repo.revogar(jti_2)

        assert repo.esta_revogado(jti_1) is True
        assert repo.esta_revogado(jti_2) is True
        assert repo.esta_revogado(jti_3) is False


# ===========================================================================
# 2. Cliente + Veiculo
# ===========================================================================


class TestClienteRepository:
    def test_salvar_e_obter_com_cpf(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)

        resultado = repo.obter_por_id(cliente.id)

        assert resultado is not None
        assert resultado.nome == "Maria Silva"
        assert isinstance(resultado.documento, CPF)
        assert resultado.documento.numero == "21249722519"
        assert resultado.contato == Contato(valor="11999990000")
        assert resultado.ativo is True

    def test_salvar_e_obter_com_cnpj(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cnpj(session)

        resultado = repo.obter_por_id(cliente.id)

        assert resultado is not None
        assert resultado.nome == "Oficina Ltda"
        assert isinstance(resultado.documento, CNPJ)
        assert resultado.documento.numero == "11222333000181"

    def test_obter_por_documento_cpf(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)

        resultado = repo.obter_por_documento(CPF(numero="21249722519"))

        assert resultado is not None
        assert resultado.id == cliente.id

    def test_obter_por_documento_inexistente(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)

        resultado = repo.obter_por_documento(CPF(numero="52998224725"))

        assert resultado is None

    def test_documento_unico_constraint(self, session: Session) -> None:
        from sqlalchemy.exc import IntegrityError

        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        _criar_cliente_cpf(session, cpf_numero="21249722519")

        duplicado = Cliente(
            _nome="Outro Nome",
            _documento=CPF(numero="21249722519"),
            _contato=Contato(valor="11888880000"),
        )
        with pytest.raises(IntegrityError):
            repo.salvar(duplicado)
        session.rollback()

    def test_adicionar_veiculo(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        placa = Placa(valor="ABC1D23")
        cliente.adicionar_veiculo(
            placa=placa,
            marca="Fiat",
            modelo="Uno",
            ano=2020,
        )
        session.flush()

        resultado = repo.obter_por_id(cliente.id)

        assert resultado is not None
        assert len(resultado.veiculos) == 1
        veiculo = resultado.veiculos[0]
        assert veiculo.placa == placa
        assert veiculo.marca == "Fiat"
        assert veiculo.modelo == "Uno"
        assert veiculo.ano == 2020

    def test_adicionar_multiplos_veiculos(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        cliente.adicionar_veiculo(
            placa=Placa(valor="AAA1111"),
            marca="Fiat",
            modelo="Uno",
            ano=2020,
        )
        cliente.adicionar_veiculo(
            placa=Placa(valor="BBB2222"),
            marca="VW",
            modelo="Gol",
            ano=2019,
        )
        session.flush()

        resultado = repo.obter_por_id(cliente.id)
        assert resultado is not None
        assert len(resultado.veiculos) == 2

    def test_listar_veiculos_do_cliente(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        cliente.adicionar_veiculo(
            placa=Placa(valor="XYZ9999"),
            marca="Honda",
            modelo="Civic",
            ano=2022,
        )
        cliente.adicionar_veiculo(
            placa=Placa(valor="DEF5678"),
            marca="Toyota",
            modelo="Corolla",
            ano=2021,
        )
        session.flush()

        resultado = repo.obter_por_id(cliente.id)
        assert resultado is not None
        placas = {v.placa.valor for v in resultado.veiculos}
        assert placas == {"XYZ9999", "DEF5678"}

    def test_atualizar_cliente(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        cliente.atualizar(nome="Maria Souza", contato=Contato(valor="11888881111"))
        session.flush()

        resultado = repo.obter_por_id(cliente.id)

        assert resultado is not None
        assert resultado.nome == "Maria Souza"
        assert resultado.contato == Contato(valor="11888881111")

    def test_desativar_cliente(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        assert cliente.ativo is True

        cliente.desativar()
        session.flush()

        resultado = repo.obter_por_id(cliente.id)
        assert resultado is not None
        assert resultado.ativo is False

    def test_anonimizar_cnpj_e_recarregar_retorna_documento_anonimizado(
        self, session: Session
    ) -> None:
        # Caso de aceite do p3 #79: cliente cujo documento original era
        # CNPJ deve voltar do DB como DocumentoAnonimizado, e nao mais como
        # um CPF reconstruido via __new__ (que escondia o tipo original).
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cnpj(session)
        cliente_id = cliente.id
        repo.anonimizar_dados(cliente_id)
        session.expire_all()

        resultado = repo.obter_por_id(cliente_id)
        assert resultado is not None
        assert isinstance(resultado.documento, DocumentoAnonimizado)
        assert not isinstance(resultado.documento, CNPJ)
        assert not isinstance(resultado.documento, CPF)
        assert resultado.documento.cliente_id == cliente_id
        assert resultado.ativo is False

    def test_anonimizar_dois_clientes_preserva_unique_hash(
        self, session: Session
    ) -> None:
        # Apos anonimizacao, ambos viram DocumentoAnonimizado com cliente_id
        # diferente — o tombstone "ANONIMIZADO:{cliente_id}" precisa
        # permanecer unico mesmo se o ORM marcar dirty na proxima flush.
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente_a = _criar_cliente_cpf(session, cpf_numero="21249722519")
        cliente_b = _criar_cliente_cnpj(session, cnpj_numero="11222333000181")
        repo.anonimizar_dados(cliente_a.id)
        repo.anonimizar_dados(cliente_b.id)
        session.flush()
        session.expire_all()

        a = repo.obter_por_id(cliente_a.id)
        b = repo.obter_por_id(cliente_b.id)
        assert a is not None
        assert b is not None
        assert isinstance(a.documento, DocumentoAnonimizado)
        assert isinstance(b.documento, DocumentoAnonimizado)
        assert a.documento != b.documento

    def test_anonimizar_cascateia_para_veiculos_neutraliza_placa(
        self, session: Session
    ) -> None:
        """p3 #72: a anonimizacao neutraliza a placa (PII) dos veiculos do cliente e
        preserva a linha/FK (historico de OS por veiculo_id intacto)."""
        from src.cliente_veiculo.dominio.placa import Placa
        from src.cliente_veiculo.dominio.placa_anonimizada import PlacaAnonimizada
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session, cpf_numero="93214407473")
        cliente.adicionar_veiculo(
            placa=Placa(valor="ABC1234"), marca="Fiat", modelo="Uno", ano=2020
        )
        repo.salvar(cliente)
        veiculo_id = cliente.veiculos[0].id

        repo.anonimizar_dados(cliente.id)
        session.expire_all()

        resultado = repo.obter_por_id(cliente.id)
        assert resultado is not None
        veiculo = resultado.veiculos[0]
        # Placa (PII) neutralizada -> PlacaAnonimizada, sem expor a placa real.
        assert isinstance(veiculo.placa, PlacaAnonimizada)
        assert veiculo.placa.valor == "ANONIMIZADO"
        assert veiculo.marca == "ANONIMIZADO"
        assert veiculo.modelo == "ANONIMIZADO"
        # Linha preservada (id intacto) -> FK ordens_de_servico.veiculo_id valida.
        assert veiculo.id == veiculo_id

    def test_anonimizar_dois_veiculos_preserva_unique_placa(
        self, session: Session
    ) -> None:
        """p3 #72: o tombstone por-veiculo (ANONIMIZADO:{id}) mantem a UNIQUE da
        placa quando dois veiculos (de clientes distintos) sao anonimizados."""
        from src.cliente_veiculo.dominio.placa import Placa
        from src.cliente_veiculo.dominio.placa_anonimizada import PlacaAnonimizada
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente_a = _criar_cliente_cpf(session, cpf_numero="21249722519")
        cliente_a.adicionar_veiculo(
            placa=Placa(valor="AAA1111"), marca="Fiat", modelo="Uno", ano=2020
        )
        repo.salvar(cliente_a)
        cliente_b = _criar_cliente_cnpj(session, cnpj_numero="11222333000181")
        cliente_b.adicionar_veiculo(
            placa=Placa(valor="BBB2222"), marca="VW", modelo="Gol", ano=2021
        )
        repo.salvar(cliente_b)

        repo.anonimizar_dados(cliente_a.id)
        repo.anonimizar_dados(cliente_b.id)
        session.flush()  # tombstones distintos -> nao viola a UNIQUE da placa
        session.expire_all()

        a = repo.obter_por_id(cliente_a.id)
        b = repo.obter_por_id(cliente_b.id)
        assert a is not None
        assert b is not None
        assert isinstance(a.veiculos[0].placa, PlacaAnonimizada)
        assert isinstance(b.veiculos[0].placa, PlacaAnonimizada)
        assert a.veiculos[0].placa != b.veiculos[0].placa

    def test_listar_com_paginacao(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        _criar_cliente_cpf(session, nome="Cliente A", cpf_numero="21249722519")
        _criar_cliente_cpf(session, nome="Cliente B", cpf_numero="52998224725")
        _criar_cliente_cpf(session, nome="Cliente C", cpf_numero="11144477735")

        pagina_1 = repo.listar(offset=0, limit=2)
        pagina_2 = repo.listar(offset=2, limit=2)

        assert len(pagina_1) == 2
        assert len(pagina_2) == 1

    def test_contar(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        assert repo.contar() == 0

        _criar_cliente_cpf(session)
        assert repo.contar() == 1

    def test_placa_existe(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        placa = Placa(valor="PLK8899")
        cliente.adicionar_veiculo(
            placa=placa,
            marca="Ford",
            modelo="Ka",
            ano=2021,
        )
        session.flush()

        assert repo.placa_existe(Placa(valor="PLK8899")) is True
        assert repo.placa_existe(Placa(valor="ZZZ0000")) is False

    def test_placa_existe_excluindo_cliente(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        placa = Placa(valor="EXC1234")
        cliente.adicionar_veiculo(
            placa=placa,
            marca="Chevrolet",
            modelo="Onix",
            ano=2023,
        )
        session.flush()

        assert (
            repo.placa_existe(
                Placa(valor="EXC1234"),
                excluir_cliente_id=cliente.id,
            )
            is False
        )
        assert repo.placa_existe(Placa(valor="EXC1234")) is True

    def test_obter_por_id_inexistente(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)

        resultado = repo.obter_por_id(uuid4())

        assert resultado is None

    def test_remover_veiculo(self, session: Session) -> None:
        from src.cliente_veiculo.infraestrutura.repository import (
            ClienteSQLAlchemyRepository,
        )

        repo = ClienteSQLAlchemyRepository(session=session)
        cliente = _criar_cliente_cpf(session)
        placa = Placa(valor="REM4567")
        veiculo = cliente.adicionar_veiculo(
            placa=placa,
            marca="Fiat",
            modelo="Argo",
            ano=2022,
        )
        session.flush()

        cliente.remover_veiculo(veiculo.id)
        session.flush()

        resultado = repo.obter_por_id(cliente.id)
        assert resultado is not None
        assert len(resultado.veiculos) == 0
