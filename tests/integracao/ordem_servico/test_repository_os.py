"""Persistencia da OS contra Postgres real: mapping (VOs, enums, historico),
versao, consultas do repositorio, adapters entre contextos e metricas."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from sqlalchemy import text

from src.cliente_veiculo.infraestrutura.adapters import (
    OrdemDeServicoSQLAlchemyAdapter,
)
from src.cliente_veiculo.infraestrutura.repository import ClienteSQLAlchemyRepository
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.placa import Placa
from src.compartilhado.infraestrutura.metrics import metricas_api
from src.ordem_servico.aplicacao.dtos import AcompanhamentoDTO
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.resumos import StatusPagamento
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.adapters import ClienteSQLAlchemyAdapter
from src.ordem_servico.infraestrutura.consultas import ConsultaAcompanhamentoSQLAlchemy
from src.ordem_servico.infraestrutura.metrics import instrumentar_metricas_de_ordens
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.fabricas import CHECKOUT_URL, FLUXO, LINK_DECISAO, aplicar_fato
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from src.cliente_veiculo.dominio.cliente import Cliente

S = StatusOrdem


def _abrir(session: Session, cliente: Cliente | None = None) -> OrdemDeServico:
    cliente = cliente or criar_cliente_com_veiculo(session)
    ordem = OrdemDeServico.abrir(
        cliente_id=cliente.id,
        veiculo_id=cliente.veiculos[0].id,
        descricao_problema="Vazamento de oleo",
    )
    OrdemDeServicoSQLAlchemyRepository(session).salvar(ordem)
    return ordem


def _avancar(session: Session, ordem: OrdemDeServico, ate: StatusOrdem) -> None:
    repo = OrdemDeServicoSQLAlchemyRepository(session)
    for proximo in FLUXO[FLUXO.index(ordem.status) + 1 : FLUXO.index(ate) + 1]:
        aplicar_fato(ordem, proximo)
        repo.salvar(ordem)


def _recarregar(session: Session, ordem: OrdemDeServico) -> OrdemDeServico:
    session.expire_all()
    recarregada = OrdemDeServicoSQLAlchemyRepository(session).obter_por_id(ordem.id)
    assert recarregada is not None
    return recarregada


class TestMapping:
    def test_round_trip_com_resumos_e_historico(self, session: Session) -> None:
        ordem = _abrir(session)
        _avancar(session, ordem, S.AGUARDANDO_PAGAMENTO)

        lida = _recarregar(session, ordem)

        assert lida.status is S.AGUARDANDO_PAGAMENTO
        assert lida.descricao_problema == "Vazamento de oleo"
        orcamento = lida.resumo_orcamento
        assert orcamento is not None
        assert orcamento.total == Dinheiro(Decimal("350.00"), "BRL")
        assert orcamento.link_decisao == LINK_DECISAO
        assert orcamento.orcamento_id == ordem.resumo_orcamento.orcamento_id  # type: ignore[union-attr]
        pagamento = lida.resumo_pagamento
        assert pagamento is not None
        assert pagamento.status is StatusPagamento.SOLICITADO
        assert pagamento.checkout_url == CHECKOUT_URL
        assert [(m.sequencia, m.de, m.para, m.origem) for m in lida.historico] == [
            (1, None, S.RECEBIDA, OrigemMudanca.ATENDIMENTO),
            (2, S.RECEBIDA, S.EM_DIAGNOSTICO, OrigemMudanca.EXECUCAO),
            (3, S.EM_DIAGNOSTICO, S.AGUARDANDO_APROVACAO, OrigemMudanca.BILLING),
            (4, S.AGUARDANDO_APROVACAO, S.AGUARDANDO_PAGAMENTO, OrigemMudanca.BILLING),
        ]
        assert lida.historico[0].ocorrido_em.utcoffset() is not None

    def test_cancelada_persiste_motivo(self, session: Session) -> None:
        ordem = _abrir(session)
        ordem.cancelar("cliente desistiu", OrigemMudanca.SAGA)
        OrdemDeServicoSQLAlchemyRepository(session).salvar(ordem)

        lida = _recarregar(session, ordem)

        assert lida.motivo_cancelamento == "cliente desistiu"
        assert lida.historico[-1].motivo == "cliente desistiu"
        assert lida.historico[-1].origem is OrigemMudanca.SAGA
        assert lida.resumo_orcamento is None
        assert lida.resumo_pagamento is None

    def test_refresh_reidrata_os_resumos(self, session: Session) -> None:
        ordem = _abrir(session)
        _avancar(session, ordem, S.AGUARDANDO_APROVACAO)
        # Muda a linha por fora do ORM: so o listener de `refresh` traz o
        # valor novo para o VO (o atributo do resumo nao e mapeado).
        session.execute(
            text("UPDATE ordens_de_servico SET orcamento_total = 1 WHERE id = :id"),
            {"id": ordem.id},
        )

        session.refresh(ordem)

        assert ordem.resumo_orcamento is not None
        assert ordem.resumo_orcamento.total.valor == Decimal("1.00")

    def test_ordem_carregada_aceita_novos_fatos(self, session: Session) -> None:
        ordem = _abrir(session)
        lida = _recarregar(session, ordem)

        lida.registrar_diagnostico_iniciado()
        OrdemDeServicoSQLAlchemyRepository(session).salvar(lida)

        assert len(lida.coletar_eventos()) == 1
        assert _recarregar(session, lida).status is S.EM_DIAGNOSTICO

    def test_versao_sobe_a_cada_escrita(self, session: Session) -> None:
        ordem = _abrir(session)
        assert ordem.versao == 1

        _avancar(session, ordem, S.AGUARDANDO_APROVACAO)

        assert ordem.versao == 3
        assert _recarregar(session, ordem).versao == 3

    def test_obter_inexistente(self, session: Session) -> None:
        assert OrdemDeServicoSQLAlchemyRepository(session).obter_por_id(uuid4()) is None


class TestConsultaAcompanhamento:
    @pytest.fixture
    def cliente(self, session: Session) -> Cliente:
        return criar_cliente_com_veiculo(
            session, cpf="52998224725", placa="ABC1D23", contato="x@y.com"
        )

    @staticmethod
    def _consultar(session: Session, placa: str, documento: str) -> object:
        return ConsultaAcompanhamentoSQLAlchemy(session).mais_recente(
            Placa(valor=placa), CPF(numero=documento)
        )

    def test_projeta_so_status_e_timestamps_da_mais_recente(
        self, session: Session, cliente: Cliente
    ) -> None:
        antiga = _abrir(session, cliente)
        mais_nova = _abrir(session, cliente)
        _avancar(session, mais_nova, S.EM_DIAGNOSTICO)
        # criado_em explicito: duas aberturas no mesmo instante cairiam no
        # desempate por id (uuid4 aleatorio) e o teste ficaria instavel.
        session.execute(
            text(
                "UPDATE ordens_de_servico SET criado_em = criado_em - interval "
                "'1 hour' WHERE id = :id"
            ),
            {"id": antiga.id},
        )

        achada = self._consultar(session, "abc-1d23", "529.982.247-25")

        assert achada == AcompanhamentoDTO(
            status="em_diagnostico",
            criado_em=mais_nova.criado_em,
            atualizado_em=mais_nova.atualizado_em,
        )

    @pytest.mark.parametrize(
        ("placa", "documento"),
        [("ZZZ9Z99", "52998224725"), ("ABC1D23", "11144477735")],
        ids=["placa-inexistente", "documento-errado"],
    )
    def test_par_que_nao_casa_devolve_none(
        self, session: Session, cliente: Cliente, placa: str, documento: str
    ) -> None:
        _abrir(session, cliente)
        assert self._consultar(session, placa, documento) is None

    def test_cliente_anonimizado_nao_e_encontrado(
        self, session: Session, cliente: Cliente
    ) -> None:
        ordem = _abrir(session, cliente)
        ordem.cancelar("encerrada", OrigemMudanca.ATENDIMENTO)
        OrdemDeServicoSQLAlchemyRepository(session).salvar(ordem)
        ClienteSQLAlchemyRepository(session).anonimizar_dados(cliente.id)
        session.flush()

        assert self._consultar(session, "ABC1D23", "52998224725") is None


class TestAdaptersEntreContextos:
    def test_cliente_port(self, session: Session) -> None:
        cliente = criar_cliente_com_veiculo(session)
        outro = criar_cliente_com_veiculo(session)
        adapter = ClienteSQLAlchemyAdapter(session)

        assert adapter.cliente_existe(cliente.id) is True
        assert adapter.cliente_existe(uuid4()) is False
        assert adapter.veiculo_pertence_ao_cliente(cliente.id, cliente.veiculos[0].id)
        assert not adapter.veiculo_pertence_ao_cliente(cliente.id, outro.veiculos[0].id)

        cliente.desativar()
        ClienteSQLAlchemyRepository(session).salvar(cliente)
        assert adapter.cliente_existe(cliente.id) is False

    @pytest.mark.parametrize(
        ("estado", "ativa"),
        [
            (S.RECEBIDA, True),
            (S.AGUARDANDO_PAGAMENTO, True),
            (S.FINALIZADA, True),
            (S.ENTREGUE, False),
        ],
    )
    def test_os_ativa_para_o_contexto_de_clientes(
        self, session: Session, estado: StatusOrdem, ativa: bool
    ) -> None:
        ordem = _abrir(session)
        _avancar(session, ordem, estado)
        adapter = OrdemDeServicoSQLAlchemyAdapter(session)

        assert adapter.existe_os_ativa_para_cliente(ordem.cliente_id) is ativa
        # Qualquer OS (ativa ou nao) impede remover o veiculo (FK).
        assert adapter.existe_os_para_veiculo(ordem.veiculo_id) is True

    def test_cancelada_nao_conta_como_ativa(self, session: Session) -> None:
        ordem = _abrir(session)
        ordem.cancelar("x", OrigemMudanca.ATENDIMENTO)
        OrdemDeServicoSQLAlchemyRepository(session).salvar(ordem)

        adapter = OrdemDeServicoSQLAlchemyAdapter(session)
        assert adapter.existe_os_ativa_para_cliente(ordem.cliente_id) is False


class TestMetricasDeNegocio:
    def test_abertura_e_transicao_alimentam_as_metricas(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        criadas: list[int] = []
        duracoes: list[tuple[str, float]] = []
        monkeypatch.setattr(metricas_api, "os_criada", lambda: criadas.append(1))
        monkeypatch.setattr(
            metricas_api,
            "os_duracao_status",
            lambda status, duracao: duracoes.append((status, duracao)),
        )
        instrumentar_metricas_de_ordens()
        instrumentar_metricas_de_ordens()  # idempotente: um listener so

        ordem = _abrir(session)
        assert criadas == [1]
        assert duracoes == []

        ordem.registrar_diagnostico_iniciado()
        OrdemDeServicoSQLAlchemyRepository(session).salvar(ordem)

        assert criadas == [1]
        ((status, duracao),) = duracoes
        assert status == "recebida"
        esperado = (
            ordem.historico[1].ocorrido_em - ordem.historico[0].ocorrido_em
        ).total_seconds()
        assert duracao == pytest.approx(esperado)

    def test_escrita_sem_troca_de_status_nao_mede(
        self, session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        duracoes: list[tuple[str, float]] = []
        monkeypatch.setattr(
            metricas_api,
            "os_duracao_status",
            lambda status, duracao: duracoes.append((status, duracao)),
        )
        instrumentar_metricas_de_ordens()
        ordem = _abrir(session)

        # Mudanca de coluna sem transicao (nao acontece no dominio, mas o
        # listener nao pode medir nada se o status nao mudou).
        ordem._atualizado_em = ordem.atualizado_em
        session.flush()

        assert duracoes == []
