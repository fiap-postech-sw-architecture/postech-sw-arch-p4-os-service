"""Casos de uso de OS com repositorio em memoria e UoW fake (sem banco)."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from src.compartilhado.dominio.cnpj import CNPJ
from src.compartilhado.dominio.cpf import CPF
from src.compartilhado.dominio.exceptions import (
    ConflitoDeConcorrenciaException,
    TransicaoStatusInvalidaException,
    ValorInvalidoException,
)
from src.compartilhado.dominio.placa import Placa
from src.ordem_servico.aplicacao.dtos import (
    AbrirOrdemDTO,
    AcompanhamentoDTO,
    ComandoEmVooDTO,
    SagaDTO,
)
from src.ordem_servico.aplicacao.saga.modelo import SagaNaoEncontradaException
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.use_cases import (
    CANCELAMENTO_RECUSADO,
    ORDEM_JA_CANCELADA,
    AbrirOrdem,
    CancelarOrdem,
    ConsultarAcompanhamento,
    ListarOrdens,
    ObterOrdem,
    ObterSaga,
    RegistrarEntrega,
)
from src.ordem_servico.dominio.exceptions import (
    ClienteNaoEncontradoException,
    OrdemNaoEncontradaException,
    VeiculoNaoEncontradoException,
)
from src.ordem_servico.dominio.ordem_de_servico import OrdemDeServico
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import (
    ATOR_ATENDENTE,
    CHECKOUT_URL,
    EXPIRA_EM,
    LINK_DECISAO,
    VALIDO_ATE,
    ordem_em,
)
from tests.unitarios.fakes import (
    RETRATO,
    ClientePortFake,
    ConsultaAcompanhamentoEspia,
    ConsultaDaOrdemEmMemoria,
    FakeUnitOfWork,
    RepoEmMemoria,
    SagasEmMemoria,
)
from tests.unitarios.ordem_servico.cenario_da_saga import (
    COMPENSACOES,
    PRAZO,
    CenarioDaSaga,
)


def _dto() -> AbrirOrdemDTO:
    return AbrirOrdemDTO(
        cliente_id=uuid4(),
        veiculo_id=uuid4(),
        descricao_problema="Freio rangendo",
        ator=ATOR_ATENDENTE,
    )


class TestAbrirOrdem:
    def test_abre_a_os_e_a_saga_e_grava_o_solicitar_diagnostico(self) -> None:
        repo, uow, sagas = RepoEmMemoria(), FakeUnitOfWork(), SagasEmMemoria()
        dto = _dto()

        resultado = AbrirOrdem(repo, uow, ClientePortFake(), sagas).executar(dto)

        assert uow.committed
        (salva,) = repo.salvas
        assert resultado.id == salva.id
        assert (resultado.cliente_id, resultado.veiculo_id) == (
            dto.cliente_id,
            dto.veiculo_id,
        )
        assert resultado.status == "recebida"
        assert resultado.descricao_problema == "Freio rangendo"
        assert resultado.orcamento is None
        assert resultado.pagamento is None
        assert resultado.versao == 1
        (abertura,) = resultado.historico
        assert (abertura.de, abertura.para, abertura.origem, abertura.ator) == (
            None,
            "recebida",
            "atendimento",
            ATOR_ATENDENTE,
        )
        # Comando com o retrato do veiculo; causa e a requisicao (sem causation).
        (comando,) = uow.comandos
        assert comando == (
            "SolicitarDiagnostico",
            {
                "ordem_id": salva.id,
                "veiculo_id": dto.veiculo_id,
                "veiculo": {
                    "placa": RETRATO.placa,
                    "marca": RETRATO.marca,
                    "modelo": RETRATO.modelo,
                    "ano": RETRATO.ano,
                },
                "descricao_problema": "Freio rangendo",
            },
            salva.id,
            None,
        )
        (saga,) = sagas.salvas
        assert saga.ordem_id == salva.id
        assert saga.etapa.value == "aguardando_diagnostico"
        assert saga.iniciada_em == salva.criado_em
        (passo,) = saga.passos
        assert (passo["gatilho"], passo["comando"], passo["ator"]) == (
            "abertura",
            "SolicitarDiagnostico",
            ATOR_ATENDENTE,
        )
        assert passo["comando_id"] == uow.envelopes[0]["id"]
        # Sem resposta automatica: nada em voo e nada da placa ou do texto livre.
        assert (saga.comando_em_voo, saga.prazo_resposta_em) == (None, None)
        assert "BRA2E19" not in str(saga.passos)

    def test_cliente_inexistente_ou_inativo_levanta_sem_persistir(self) -> None:
        repo, uow, sagas = RepoEmMemoria(), FakeUnitOfWork(), SagasEmMemoria()
        uc = AbrirOrdem(repo, uow, ClientePortFake(cliente_ok=False), sagas)

        with pytest.raises(ClienteNaoEncontradoException):
            uc.executar(_dto())

        assert (repo.salvas, sagas.salvas, uow.comandos) == ([], [], [])
        assert not uow.committed

    def test_veiculo_de_outro_cliente_levanta_sem_persistir(self) -> None:
        repo, uow, sagas = RepoEmMemoria(), FakeUnitOfWork(), SagasEmMemoria()
        uc = AbrirOrdem(repo, uow, ClientePortFake(veiculo_ok=False), sagas)

        with pytest.raises(VeiculoNaoEncontradoException):
            uc.executar(_dto())

        assert (repo.salvas, sagas.salvas, uow.comandos) == ([], [], [])
        assert not uow.committed

    def test_descricao_invalida_levanta_valor_invalido(self) -> None:
        dto = AbrirOrdemDTO(
            cliente_id=uuid4(),
            veiculo_id=uuid4(),
            descricao_problema="   ",
            ator=ATOR_ATENDENTE,
        )
        uow, sagas = FakeUnitOfWork(), SagasEmMemoria()
        with pytest.raises(ValorInvalidoException, match="descricao do problema"):
            AbrirOrdem(RepoEmMemoria(), uow, ClientePortFake(), sagas).executar(dto)
        assert (sagas.salvas, uow.comandos) == ([], [])

    def test_conflito_na_saga_desfaz_tudo(self) -> None:
        repo, uow, sagas = RepoEmMemoria(), FakeUnitOfWork(), SagasEmMemoria()
        sagas.provocar_conflito()

        with pytest.raises(ConflitoDeConcorrenciaException):
            AbrirOrdem(repo, uow, ClientePortFake(), sagas).executar(_dto())

        assert not uow.committed
        assert uow.rolled_back


class TestObterOrdem:
    def test_projeta_resumos_e_historico(self) -> None:
        ordem = ordem_em(StatusOrdem.AGUARDANDO_PAGAMENTO)

        dto = ObterOrdem(
            ConsultaDaOrdemEmMemoria(RepoEmMemoria(ordem), SagasEmMemoria())
        ).executar(ordem.id)

        assert dto.status == "aguardando_pagamento"
        assert dto.orcamento is not None
        assert dto.orcamento.total == Decimal("350.00")
        assert dto.orcamento.moeda == "BRL"
        assert dto.orcamento.link_decisao == LINK_DECISAO
        assert dto.orcamento.valido_ate == VALIDO_ATE
        assert dto.pagamento is not None
        assert dto.pagamento.status == "solicitado"
        assert (dto.pagamento.valor, dto.pagamento.moeda) == (Decimal("350.00"), "BRL")
        assert dto.pagamento.checkout_url == CHECKOUT_URL
        assert dto.pagamento.expira_em == EXPIRA_EM
        assert [m.para for m in dto.historico] == [
            "recebida",
            "em_diagnostico",
            "aguardando_aprovacao",
            "aguardando_pagamento",
        ]
        assert [m.origem for m in dto.historico] == [
            "atendimento",
            "execucao",
            "billing",
            "execucao",
        ]

    def test_inexistente_levanta_404(self) -> None:
        with pytest.raises(OrdemNaoEncontradaException):
            ObterOrdem(
                ConsultaDaOrdemEmMemoria(RepoEmMemoria(), SagasEmMemoria())
            ).executar(uuid4())

    def test_projeta_a_etapa_e_os_passos_da_saga(self) -> None:
        cenario = CenarioDaSaga()
        cenario.receber("DiagnosticoIniciado")

        dto = ObterOrdem(
            ConsultaDaOrdemEmMemoria(cenario.ordens, cenario.sagas)
        ).executar(cenario.ordem_id)

        assert dto.etapa == "aguardando_diagnostico"
        assert dto.passos == cenario.saga.passos
        assert [p["gatilho"] for p in dto.passos] == [
            "abertura",
            "DiagnosticoIniciado",
        ]

    def test_ordem_sem_saga_sai_sem_etapa(self) -> None:
        ordem = ordem_em(StatusOrdem.RECEBIDA)

        dto = ObterOrdem(
            ConsultaDaOrdemEmMemoria(RepoEmMemoria(ordem), SagasEmMemoria())
        ).executar(ordem.id)

        assert (dto.etapa, dto.passos) == (None, ())


class TestObterSaga:
    def test_projeta_o_estado_da_saga_com_o_comando_em_voo(self) -> None:
        cenario = CenarioDaSaga()
        cenario.receber("DiagnosticoIniciado")
        cenario.receber("DiagnosticoConcluido")
        saga = cenario.saga

        dto = ObterSaga(cenario.sagas).executar(cenario.ordem_id)

        assert dto == SagaDTO(
            ordem_id=cenario.ordem_id,
            etapa="aguardando_orcamento",
            motivo=None,
            falha=None,
            plano_compensacao=(),
            comando_em_voo=ComandoEmVooDTO(
                tipo="GerarOrcamento", enviado_em=cenario.relogio.agora
            ),
            reenvios=0,
            prazo_resposta_em=saga.prazo_resposta_em,
            passos=saga.passos,
        )

    def test_sem_comando_em_voo(self) -> None:
        cenario = CenarioDaSaga()

        dto = ObterSaga(cenario.sagas).executar(cenario.ordem_id)

        assert (dto.etapa, dto.comando_em_voo, dto.prazo_resposta_em) == (
            "aguardando_diagnostico",
            None,
            None,
        )

    def test_inexistente_levanta_404(self) -> None:
        with pytest.raises(SagaNaoEncontradaException):
            ObterSaga(SagasEmMemoria()).executar(uuid4())


def _cancelar_ordem(
    repo: RepoEmMemoria, uow: FakeUnitOfWork, sagas: SagasEmMemoria
) -> CancelarOrdem:
    return CancelarOrdem(repo, uow, sagas, prazo_resposta=PRAZO)


def _foto(cenario: CenarioDaSaga) -> tuple[object, ...]:
    saga, ordem = cenario.saga, cenario.ordem
    return (
        saga.etapa,
        saga.passos,
        saga.plano_compensacao,
        saga.comando_em_voo,
        ordem.status,
        ordem.historico,
        ordem.motivo_cancelamento,
        tuple(cenario.publicador.comandos),
    )


# (perfil da OS, plano): as linhas de cancelamento da tabela da RFC-004 secao
# 4.4, com o passo em voo no plano.
CANCELAMENTOS = [
    pytest.param("aguardando_diagnostico", COMPENSACOES[4:], id="diagnostico"),
    pytest.param("aguardando_orcamento", COMPENSACOES[3:], id="orcamento-em-voo"),
    pytest.param("aguardando_decisao", COMPENSACOES[3:], id="decisao"),
    pytest.param("aguardando_reserva", COMPENSACOES[2:], id="reserva-em-voo"),
    pytest.param("aguardando_pagamento", COMPENSACOES[1:], id="pagamento-em-voo"),
    pytest.param("com_checkout", COMPENSACOES[1:], id="checkout-aberto"),
    pytest.param("aguardando_agendamento", COMPENSACOES, id="agendamento-em-voo"),
    pytest.param("aguardando_inicio", COMPENSACOES, id="inicio"),
]


def _cenario_no_perfil(perfil: str) -> CenarioDaSaga:
    if perfil == "com_checkout":
        cenario = CenarioDaSaga.em("aguardando_pagamento")
        cenario.receber("PagamentoSolicitado")
        return cenario
    return CenarioDaSaga.em(perfil)


class TestCancelarOrdem:
    @pytest.mark.parametrize("status", [StatusOrdem.RECEBIDA, StatusOrdem.EM_EXECUCAO])
    def test_os_sem_saga_levanta_404_sem_persistir(self, status: StatusOrdem) -> None:
        # Toda OS tem saga desde a abertura: nao ha cancelamento direto.
        ordem = ordem_em(status)
        repo, uow = RepoEmMemoria(ordem), FakeUnitOfWork()

        with pytest.raises(SagaNaoEncontradaException):
            _cancelar_ordem(repo, uow, SagasEmMemoria()).executar(
                ordem.id, "x", ator=ATOR_ATENDENTE
            )

        assert repo.salvas == []
        assert (uow.comandos, uow.committed, uow.rolled_back) == ([], False, True)
        assert ordem.status is status

    def test_conflito_que_volta_nas_tres_leituras_levanta_sem_commit(self) -> None:
        cenario = CenarioDaSaga.em("aguardando_decisao")

        class _SempreEmConflito(RepoEmMemoria):
            """Cada leitura traz a OS do banco: o rollback desfez a anterior."""

            leituras = 0

            def obter_por_id(self, ordem_id: UUID) -> OrdemDeServico | None:
                self.leituras += 1
                return deepcopy(super().obter_por_id(ordem_id))

        class _SagasDoBanco(SagasEmMemoria):
            def obter(self, ordem_id: UUID) -> Saga | None:
                return deepcopy(super().obter(ordem_id))

        repo, uow = _SempreEmConflito(cenario.ordem, conflito=True), FakeUnitOfWork()
        sagas = _SagasDoBanco(cenario.saga)

        with pytest.raises(ConflitoDeConcorrenciaException):
            _cancelar_ordem(repo, uow, sagas).executar(
                cenario.ordem_id, "x", ator=ATOR_ATENDENTE
            )

        # Reread value (RFC-004 secao 4.5): tres leituras, e o 409 na ultima.
        assert repo.leituras == 3
        assert not uow.committed
        assert uow.rolled_back

    def test_inexistente_levanta_404(self) -> None:
        with pytest.raises(OrdemNaoEncontradaException):
            _cancelar_ordem(
                RepoEmMemoria(), FakeUnitOfWork(), SagasEmMemoria()
            ).executar(uuid4(), "x", ator=ATOR_ATENDENTE)

    @pytest.mark.parametrize(("perfil", "plano"), CANCELAMENTOS)
    def test_antes_do_pivot_a_saga_compensa_e_a_os_guarda_o_pedido(
        self, perfil: str, plano: tuple[str, ...]
    ) -> None:
        cenario = _cenario_no_perfil(perfil)
        status, enviados = cenario.ordem.status, len(cenario.publicador.comandos)

        dto = cenario.cancelar("  Cliente vendeu o carro  ")

        # A OS guarda o pedido e mantem o status ate a saga compensar.
        assert (dto.etapa, dto.status) == ("compensando", status.value)
        assert dto.motivo_cancelamento == "Cliente vendeu o carro"
        assert dto.historico[-1].para == status.value
        # A primeira compensacao do plano, sem causation_id: a causa e a
        # requisicao (RFC-004 secao 5.2).
        ((tipo, dados, correlation_id, causation_id),) = cenario.publicador.comandos[
            enviados:
        ]
        assert (tipo, correlation_id, causation_id) == (
            plano[0],
            cenario.ordem_id,
            None,
        )
        assert (dados["ordem_id"], dados["motivo"]) == (
            str(cenario.ordem_id),
            "cancelamento",
        )
        saga = cenario.saga
        assert (saga.motivo, saga.plano_compensacao) == ("cancelamento", plano)
        assert saga.prazo_resposta_em == cenario.relogio.agora + PRAZO
        passo = saga.passos[-1]
        assert (passo["gatilho"], passo["mensagem_id"], passo["ator"]) == (
            "cancelamento_solicitado",
            None,
            ATOR_ATENDENTE,
        )
        assert passo["comando_id"] == cenario.publicador.envelopes[-1]["id"]
        assert cenario.publicador.committed
        # O texto livre fica na OS: a saga guarda so codigos.
        assert "vendeu" not in str(saga.passos) + str(saga.comando_em_voo)

    @pytest.mark.parametrize("etapa", ["compensando", "falha_na_compensacao"])
    def test_pedido_repetido_na_compensacao_devolve_a_etapa_sem_efeito(
        self, etapa: str
    ) -> None:
        cenario = CenarioDaSaga.em_compensacao(
            COMPENSACOES[1:],
            falha="estorno_recusado" if etapa != "compensando" else None,
        )
        antes = _foto(cenario)
        cenario.publicador.committed = False

        dto = cenario.cancelar("outro motivo")

        assert dto.etapa == etapa
        assert _foto(cenario) == antes
        # A compensacao ja comecou: nada a gravar.
        assert not cenario.publicador.committed

    @pytest.mark.parametrize(
        ("etapa", "mensagem"),
        [
            pytest.param("em_execucao", CANCELAMENTO_RECUSADO, id="em-execucao"),
            pytest.param("concluida", CANCELAMENTO_RECUSADO, id="concluida"),
        ],
    )
    def test_depois_do_inicio_da_execucao_409(self, etapa: str, mensagem: str) -> None:
        cenario = CenarioDaSaga.em(etapa)
        antes = _foto(cenario)

        with pytest.raises(TransicaoStatusInvalidaException) as exc:
            cenario.cancelar()

        assert (exc.value.codigo, exc.value.mensagem) == (
            "TRANSICAO_STATUS_INVALIDA",
            mensagem,
        )
        assert _foto(cenario) == antes
        assert cenario.publicador.rolled_back

    def test_ordem_ja_cancelada_pela_saga_409(self) -> None:
        cenario = CenarioDaSaga.em("aguardando_diagnostico")
        cenario.cancelar()
        cenario.receber("DiagnosticoDescartado")
        assert cenario.saga.etapa.value == "compensada"
        antes = _foto(cenario)

        with pytest.raises(TransicaoStatusInvalidaException) as exc:
            cenario.cancelar()

        assert exc.value.mensagem == ORDEM_JA_CANCELADA
        assert _foto(cenario) == antes

    @pytest.mark.parametrize(
        "motivo",
        [
            pytest.param("   ", id="espacos"),
            pytest.param("desistiu\x00", id="nul"),
            pytest.param("x" * 501, id="longo"),
        ],
    )
    def test_motivo_invalido_422_sem_mudar_nada(self, motivo: str) -> None:
        cenario = CenarioDaSaga.em("aguardando_decisao")
        antes = _foto(cenario)

        with pytest.raises(ValorInvalidoException):
            cenario.cancelar(motivo)

        assert _foto(cenario) == antes


class TestRegistrarEntrega:
    def test_entrega_finalizada(self) -> None:
        ordem = ordem_em(StatusOrdem.FINALIZADA)
        repo, uow = RepoEmMemoria(ordem), FakeUnitOfWork()

        dto = RegistrarEntrega(repo, uow, SagasEmMemoria()).executar(
            ordem.id, ator=ATOR_ATENDENTE
        )

        assert uow.committed
        assert dto.status == "entregue"
        assert (dto.historico[-1].origem, dto.historico[-1].ator) == (
            "atendimento",
            ATOR_ATENDENTE,
        )

    @pytest.mark.parametrize(
        "estado",
        [s for s in StatusOrdem if s is not StatusOrdem.FINALIZADA],
    )
    def test_fora_de_finalizada_levanta_409(self, estado: StatusOrdem) -> None:
        ordem = ordem_em(estado)
        repo, uow = RepoEmMemoria(ordem), FakeUnitOfWork()

        with pytest.raises(TransicaoStatusInvalidaException):
            RegistrarEntrega(repo, uow, SagasEmMemoria()).executar(
                ordem.id, ator=ATOR_ATENDENTE
            )

        assert repo.salvas == []
        assert not uow.committed


class TestListarOrdens:
    def test_repassa_paginacao_e_filtro(self) -> None:
        ordem = ordem_em(StatusOrdem.EM_DIAGNOSTICO)
        repo = RepoEmMemoria(ordem)
        uc = ListarOrdens(repo)

        itens = uc.executar(offset=5, limit=7, incluir_encerradas=True)

        assert repo.args_listar == (5, 7, True)
        (item,) = itens
        assert (item.id, item.status) == (ordem.id, "em_diagnostico")
        assert uc.contar(incluir_encerradas=True) == 42
        assert repo.args_contar is True


def _arabe_indico(digitos: str) -> str:
    return "".join(chr(0x0660 + int(d)) for d in digitos)


_CPF = "52998224725"
_ACOMPANHAMENTO = AcompanhamentoDTO(
    status="em_diagnostico",
    criado_em=datetime(2026, 10, 1, 12, tzinfo=UTC),
    atualizado_em=datetime(2026, 10, 1, 13, tzinfo=UTC),
)


class TestConsultarAcompanhamento:
    def test_par_valido_consulta_com_os_vos_normalizados(self) -> None:
        espia = ConsultaAcompanhamentoEspia(resultado=_ACOMPANHAMENTO)

        dto = ConsultarAcompanhamento(espia).executar("abc-1d23", "529.982.247-25")

        assert dto is _ACOMPANHAMENTO
        assert espia.chamadas == [(Placa(valor="ABC1D23"), CPF(numero=_CPF))]

    @pytest.mark.parametrize(
        ("documento", "esperado"),
        [
            pytest.param(
                "11.222.333/0001-81", CNPJ(numero="11222333000181"), id="cnpj"
            ),
            pytest.param(
                "12.abc.345/01de-35", CNPJ(numero="12ABC34501DE35"), id="cnpj-alfanum"
            ),
            pytest.param(
                " 11.222.333/0001-81\n",
                CNPJ(numero="11222333000181"),
                id="cnpj-com-espaco-e-quebra-de-linha",
            ),
        ],
    )
    def test_documento_de_14_caracteres_vira_cnpj(
        self, documento: str, esperado: CNPJ
    ) -> None:
        espia = ConsultaAcompanhamentoEspia()

        assert ConsultarAcompanhamento(espia).executar("ABC1D23", documento) is None
        assert espia.chamadas == [(Placa(valor="ABC1D23"), esperado)]

    @pytest.mark.parametrize(
        ("placa", "documento"),
        [
            pytest.param("ABC1D23", "52998224726", id="cpf-dv-errado"),
            pytest.param("ABC1D23", "123.456.789-00", id="cpf-mascarado-dv-errado"),
            pytest.param("ABC1D23", "111.111.111-11", id="cpf-digitos-iguais"),
            pytest.param("ABC1D23", "11.111.111/0001-11", id="cnpj-dv-errado"),
            pytest.param("ABC1D23", "12ABC34501DE36", id="cnpj-alfanum-dv-errado"),
            pytest.param("ABC1D23", "abcdefghijk", id="documento-sem-digito"),
            pytest.param("ABC1D23", _arabe_indico(_CPF), id="cpf-arabe-indico"),
            pytest.param("!!!!!!!", _CPF, id="placa-com-simbolos"),
            pytest.param("1234ABC", _CPF, id="placa-fora-do-padrao"),
            pytest.param("ABC" + _arabe_indico("1234"), _CPF, id="placa-arabe-indica"),
        ],
    )
    def test_entrada_invalida_devolve_none_sem_consultar_o_banco(
        self, placa: str, documento: str
    ) -> None:
        espia = ConsultaAcompanhamentoEspia(resultado=_ACOMPANHAMENTO)

        assert ConsultarAcompanhamento(espia).executar(placa, documento) is None
        assert espia.chamadas == []

    def test_par_valido_sem_ordem_devolve_none(self) -> None:
        espia = ConsultaAcompanhamentoEspia()

        assert ConsultarAcompanhamento(espia).executar("ABC1D23", _CPF) is None
        assert len(espia.chamadas) == 1
