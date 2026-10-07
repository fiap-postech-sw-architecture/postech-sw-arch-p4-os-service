"""Agregado ``OrdemDeServico`` da fase 4: abertura, fatos da saga, historico,
cancelamento e eventos para a outbox."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, tzinfo
from itertools import pairwise
from uuid import uuid4

import pytest

from src.compartilhado.dominio.exceptions import (
    TransicaoStatusInvalidaException,
    ValorInvalidoException,
    ViolacaoRegraDeNegocioException,
)
from src.ordem_servico.dominio.events import (
    OrdemAbertaEvent,
    StatusDaOrdemAlteradoEvent,
)
from src.ordem_servico.dominio.historico import OrigemMudanca
from src.ordem_servico.dominio.ordem_de_servico import (
    TAMANHO_MAXIMO_DESCRICAO,
    TAMANHO_MAXIMO_MOTIVO,
    OrdemDeServico,
)
from src.ordem_servico.dominio.resumos import StatusPagamento
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import (
    ATOR_ATENDENTE,
    ATOR_PROCESSO,
    abrir_ordem,
    ordem_em,
    resumo_do_orcamento,
    resumo_do_pagamento,
)

S = StatusOrdem
OM = OrigemMudanca


def _orcamento(ordem: OrdemDeServico) -> None:
    ordem.registrar_orcamento_gerado(resumo_do_orcamento(), ator=ATOR_PROCESSO)


def _pagamento(ordem: OrdemDeServico) -> None:
    ordem.registrar_pagamento_solicitado(resumo_do_pagamento())


def _aguardando_pagamento_sem_resumo() -> OrdemDeServico:
    """``PecasReservadas`` aplicado e ``PagamentoSolicitado`` ainda nao."""
    ordem = ordem_em(S.AGUARDANDO_APROVACAO)
    ordem.registrar_pecas_reservadas(ator=ATOR_PROCESSO)
    return ordem


# (nome, fato, estado de origem, estado de destino, origem e ator do historico)
FATOS: list[tuple[str, Callable[[OrdemDeServico], None], S, S, OM, str]] = [
    (
        "diagnostico_iniciado",
        lambda o: o.registrar_diagnostico_iniciado(ator=ATOR_PROCESSO),
        S.RECEBIDA,
        S.EM_DIAGNOSTICO,
        OM.EXECUCAO,
        ATOR_PROCESSO,
    ),
    (
        "orcamento_gerado",
        _orcamento,
        S.EM_DIAGNOSTICO,
        S.AGUARDANDO_APROVACAO,
        OM.BILLING,
        ATOR_PROCESSO,
    ),
    (
        "pecas_reservadas",
        lambda o: o.registrar_pecas_reservadas(ator=ATOR_PROCESSO),
        S.AGUARDANDO_APROVACAO,
        S.AGUARDANDO_PAGAMENTO,
        OM.EXECUCAO,
        ATOR_PROCESSO,
    ),
    (
        "pagamento_confirmado",
        lambda o: o.registrar_pagamento_confirmado(ator=ATOR_PROCESSO),
        S.AGUARDANDO_PAGAMENTO,
        S.AGUARDANDO_EXECUCAO,
        OM.BILLING,
        ATOR_PROCESSO,
    ),
    (
        "execucao_iniciada",
        lambda o: o.registrar_execucao_iniciada(ator=ATOR_PROCESSO),
        S.AGUARDANDO_EXECUCAO,
        S.EM_EXECUCAO,
        OM.EXECUCAO,
        ATOR_PROCESSO,
    ),
    (
        "finalizar",
        lambda o: o.finalizar(ator=ATOR_PROCESSO),
        S.EM_EXECUCAO,
        S.FINALIZADA,
        OM.EXECUCAO,
        ATOR_PROCESSO,
    ),
    (
        "entrega",
        lambda o: o.registrar_entrega(ator=ATOR_ATENDENTE),
        S.FINALIZADA,
        S.ENTREGUE,
        OM.ATENDIMENTO,
        ATOR_ATENDENTE,
    ),
]

FATOS_ILEGAIS = [
    pytest.param(fato, estado, id=f"{nome}-em-{estado.value}")
    for nome, fato, de, _para, _origem, _ator in FATOS
    for estado in S
    if estado is not de
]

CANCELAVEIS = [
    S.RECEBIDA,
    S.EM_DIAGNOSTICO,
    S.AGUARDANDO_APROVACAO,
    S.AGUARDANDO_PAGAMENTO,
    S.AGUARDANDO_EXECUCAO,
]
NAO_CANCELAVEIS = [S.EM_EXECUCAO, S.FINALIZADA, S.ENTREGUE, S.CANCELADA]


@pytest.fixture
def relogio_que_avanca(monkeypatch: pytest.MonkeyPatch) -> None:
    """Relogio da OS que anda 1 minuto por leitura.

    A ordem dos instantes e provada sem depender da resolucao do relogio de
    parede: dois fatos no mesmo tique passariam num ">=" com a ordem trocada.
    """

    class _RelogioQueAvanca(datetime):
        _proximo = datetime(2026, 10, 6, 12, tzinfo=UTC)

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _RelogioQueAvanca:
            atual = cls._proximo
            cls._proximo = atual + timedelta(minutes=1)
            return cls.fromtimestamp(atual.timestamp(), tz)

    monkeypatch.setattr(
        "src.ordem_servico.dominio.ordem_de_servico.datetime", _RelogioQueAvanca
    )


def _fotografia(ordem: OrdemDeServico) -> tuple[object, ...]:
    """Estado observavel completo, para provar que nada mudou."""
    return (
        ordem.status,
        ordem.historico,
        ordem.resumo_orcamento,
        ordem.resumo_pagamento,
        ordem.motivo_cancelamento,
        ordem.atualizado_em,
        tuple(ordem.coletar_eventos()),
    )


class TestAbertura:
    def test_abre_em_recebida_com_historico_e_evento(self) -> None:
        cliente_id, veiculo_id = uuid4(), uuid4()
        ordem = OrdemDeServico.abrir(
            cliente_id=cliente_id,
            veiculo_id=veiculo_id,
            descricao_problema="  Motor falhando na partida  ",
            ator=ATOR_ATENDENTE,
        )

        assert ordem.status is S.RECEBIDA
        assert ordem.cliente_id == cliente_id
        assert ordem.veiculo_id == veiculo_id
        assert ordem.descricao_problema == "Motor falhando na partida"
        assert ordem.versao == 1
        assert ordem.criado_em == ordem.atualizado_em
        assert ordem.resumo_orcamento is None
        assert ordem.resumo_pagamento is None
        assert ordem.motivo_cancelamento is None
        (abertura,) = ordem.historico
        assert (abertura.sequencia, abertura.de, abertura.para) == (1, None, S.RECEBIDA)
        assert abertura.origem is OM.ATENDIMENTO
        assert abertura.ator == ATOR_ATENDENTE
        assert abertura.motivo is None
        assert abertura.ocorrido_em == ordem.criado_em
        assert ordem.coletar_eventos() == [
            OrdemAbertaEvent(
                agregado_id=ordem.id,
                cliente_id=cliente_id,
                veiculo_id=veiculo_id,
                ocorrido_em=ordem.criado_em,
            )
        ]

    @pytest.mark.parametrize(
        "descricao",
        [
            pytest.param("", id="vazia"),
            pytest.param("   ", id="espacos"),
            pytest.param("\n\t", id="quebra-e-tab"),
        ],
    )
    def test_descricao_vazia_levanta(self, descricao: str) -> None:
        with pytest.raises(
            ValorInvalidoException, match="descricao do problema e obrigatorio"
        ):
            abrir_ordem(descricao_problema=descricao)

    def test_descricao_no_limite_passa_e_acima_levanta(self) -> None:
        abrir_ordem(descricao_problema="x" * TAMANHO_MAXIMO_DESCRICAO)
        with pytest.raises(ValorInvalidoException, match="excede"):
            abrir_ordem(descricao_problema="x" * (TAMANHO_MAXIMO_DESCRICAO + 1))

    @pytest.mark.parametrize(
        "descricao",
        [
            pytest.param("freio\x00rangendo", id="nul"),
            pytest.param("freio\x1brangendo", id="escape"),
            pytest.param("freio\rrangendo", id="cr-solto"),
            pytest.param("freio\x7frangendo", id="del"),
            pytest.param("freio\x85rangendo", id="c1"),
        ],
    )
    def test_caractere_de_controle_levanta(self, descricao: str) -> None:
        with pytest.raises(ValorInvalidoException, match="caractere de controle"):
            abrir_ordem(descricao_problema=descricao)

    def test_quebra_de_linha_e_tab_passam_e_crlf_vira_lf(self) -> None:
        ordem = abrir_ordem(descricao_problema="freio\r\nrangendo\tforte")
        assert ordem.descricao_problema == "freio\nrangendo\tforte"

    @pytest.mark.parametrize("campo", ["cliente_id", "veiculo_id"])
    def test_ids_none_explicitos_levantam(self, campo: str) -> None:
        kwargs = {
            "cliente_id": uuid4(),
            "veiculo_id": uuid4(),
            "descricao_problema": "x",
            "ator": ATOR_ATENDENTE,
            campo: None,
        }
        with pytest.raises(ValorInvalidoException, match=f"{campo} e obrigatorio"):
            # None de proposito: a guarda de runtime e o que esta sob teste.
            OrdemDeServico.abrir(**kwargs)  # type: ignore[arg-type]

    def test_repr_nao_expoe_texto_livre(self) -> None:
        ordem = abrir_ordem(descricao_problema="cliente Joao 11999990000")
        assert "Joao" not in repr(ordem)


class TestFatosDaSaga:
    @pytest.mark.parametrize(
        ("fato", "de", "para", "origem", "ator"),
        [pytest.param(f, d, p, o, a, id=n) for n, f, d, p, o, a in FATOS],
    )
    def test_fato_legal_transiciona_anota_e_emite(
        self,
        fato: Callable[[OrdemDeServico], None],
        de: StatusOrdem,
        para: StatusOrdem,
        origem: OrigemMudanca,
        ator: str,
    ) -> None:
        ordem = ordem_em(de)
        historico_antes = ordem.historico
        ordem.limpar_eventos()

        fato(ordem)

        assert ordem.status is para
        assert ordem.historico[:-1] == historico_antes
        nova = ordem.historico[-1]
        assert nova.sequencia == len(historico_antes) + 1
        assert (nova.de, nova.para, nova.origem, nova.motivo, nova.ator) == (
            de,
            para,
            origem,
            None,
            ator,
        )
        assert ordem.atualizado_em == nova.ocorrido_em
        assert ordem.coletar_eventos() == [
            StatusDaOrdemAlteradoEvent(
                agregado_id=ordem.id,
                status_anterior=de,
                status_novo=para,
                origem=origem,
                ocorrido_em=nova.ocorrido_em,
            )
        ]

    @pytest.mark.parametrize(("fato", "estado"), FATOS_ILEGAIS)
    def test_fato_fora_de_ordem_levanta_sem_mutar(
        self, fato: Callable[[OrdemDeServico], None], estado: StatusOrdem
    ) -> None:
        ordem = ordem_em(estado)
        antes = _fotografia(ordem)

        with pytest.raises(TransicaoStatusInvalidaException):
            fato(ordem)

        assert _fotografia(ordem) == antes

    def test_orcamento_gerado_guarda_o_resumo(self) -> None:
        ordem = ordem_em(S.EM_DIAGNOSTICO)
        resumo = resumo_do_orcamento()

        ordem.registrar_orcamento_gerado(resumo, ator=ATOR_PROCESSO)

        assert ordem.resumo_orcamento is resumo

    def test_pecas_reservadas_sem_orcamento_levanta_sem_mutar(self) -> None:
        # OS reidratada sem o resumo: o caminho legal grava o resumo no
        # OrcamentoGerado, e o SolicitarPagamento seguinte precisa dele.
        sem_orcamento = OrdemDeServico(
            _cliente_id=uuid4(),
            _veiculo_id=uuid4(),
            _descricao_problema="x",
            _status=S.AGUARDANDO_APROVACAO,
        )
        antes = _fotografia(sem_orcamento)

        with pytest.raises(ViolacaoRegraDeNegocioException, match="sem orcamento"):
            sem_orcamento.registrar_pecas_reservadas(ator=ATOR_PROCESSO)

        assert _fotografia(sem_orcamento) == antes

    @pytest.mark.usefixtures("relogio_que_avanca")
    def test_pagamento_solicitado_so_grava_o_resumo(self) -> None:
        ordem = _aguardando_pagamento_sem_resumo()
        historico = ordem.historico
        ordem.limpar_eventos()
        resumo = resumo_do_pagamento()

        ordem.registrar_pagamento_solicitado(resumo)

        assert ordem.resumo_pagamento is resumo
        assert (ordem.status, ordem.historico) == (S.AGUARDANDO_PAGAMENTO, historico)
        assert ordem.coletar_eventos() == []
        assert ordem.atualizado_em > historico[-1].ocorrido_em

    @pytest.mark.parametrize(
        "estado", [s for s in S if s is not S.AGUARDANDO_PAGAMENTO]
    )
    def test_pagamento_solicitado_fora_de_aguardando_pagamento_levanta_sem_mutar(
        self, estado: StatusOrdem
    ) -> None:
        ordem = ordem_em(estado)
        antes = _fotografia(ordem)

        # O mesmo erro de estado do PagamentoConfirmado: 409 com o status atual.
        with pytest.raises(
            TransicaoStatusInvalidaException, match=f"status atual: {estado.value}"
        ):
            _pagamento(ordem)

        assert _fotografia(ordem) == antes

    def test_pagamento_solicitado_de_novo_levanta_sem_mutar(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_PAGAMENTO)
        antes = _fotografia(ordem)

        with pytest.raises(ViolacaoRegraDeNegocioException, match="ja solicitado"):
            _pagamento(ordem)

        assert _fotografia(ordem) == antes

    def test_pagamento_solicitado_em_outro_estado_levanta_sem_mutar(self) -> None:
        ordem = _aguardando_pagamento_sem_resumo()
        antes = _fotografia(ordem)

        with pytest.raises(ViolacaoRegraDeNegocioException, match="confirmado"):
            ordem.registrar_pagamento_solicitado(
                resumo_do_pagamento(StatusPagamento.CONFIRMADO)
            )

        assert _fotografia(ordem) == antes

    def test_pagamento_confirmado_marca_o_resumo(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_PAGAMENTO)
        solicitado = ordem.resumo_pagamento
        assert solicitado is not None

        ordem.registrar_pagamento_confirmado(ator=ATOR_PROCESSO)

        confirmado = ordem.resumo_pagamento
        assert confirmado is not None
        assert confirmado.status is StatusPagamento.CONFIRMADO
        assert (confirmado.pagamento_id, confirmado.valor) == (
            solicitado.pagamento_id,
            solicitado.valor,
        )

    def test_pagamento_confirmado_sem_resumo_levanta_sem_mutar(self) -> None:
        ordem = _aguardando_pagamento_sem_resumo()
        antes = _fotografia(ordem)

        with pytest.raises(ViolacaoRegraDeNegocioException, match="sem pagamento"):
            ordem.registrar_pagamento_confirmado(ator=ATOR_PROCESSO)

        assert _fotografia(ordem) == antes

    @pytest.mark.usefixtures("relogio_que_avanca")
    def test_fluxo_completo_produz_linha_do_tempo_encadeada(self) -> None:
        ordem = ordem_em(S.ENTREGUE)

        historico = ordem.historico
        assert [m.sequencia for m in historico] == list(range(1, 9))
        assert historico[0].de is None
        for anterior, atual in pairwise(historico):
            assert atual.de is anterior.para
            assert atual.ocorrido_em > anterior.ocorrido_em
        assert historico[-1].para is S.ENTREGUE

    def test_historico_e_vista_imutavel(self) -> None:
        ordem = abrir_ordem()
        assert isinstance(ordem.historico, tuple)


class TestCancelamento:
    @pytest.mark.parametrize("estado", CANCELAVEIS)
    @pytest.mark.parametrize("origem", [OM.ATENDIMENTO, OM.SAGA])
    def test_cancela_antes_da_execucao(
        self, estado: StatusOrdem, origem: OrigemMudanca
    ) -> None:
        ordem = ordem_em(estado)
        ordem.limpar_eventos()

        ordem.cancelar("  orcamento recusado  ", origem, ator=ATOR_ATENDENTE)

        assert ordem.status is S.CANCELADA
        assert ordem.motivo_cancelamento == "orcamento recusado"
        ultima = ordem.historico[-1]
        assert (ultima.de, ultima.para, ultima.origem, ultima.motivo) == (
            estado,
            S.CANCELADA,
            origem,
            "orcamento recusado",
        )
        assert ultima.ator == ATOR_ATENDENTE
        assert ordem.coletar_eventos() == [
            StatusDaOrdemAlteradoEvent(
                agregado_id=ordem.id,
                status_anterior=estado,
                status_novo=S.CANCELADA,
                origem=origem,
                ocorrido_em=ultima.ocorrido_em,
            )
        ]

    @pytest.mark.parametrize("estado", NAO_CANCELAVEIS)
    def test_depois_do_pivot_ou_encerrada_levanta_sem_mutar(
        self, estado: StatusOrdem
    ) -> None:
        ordem = ordem_em(estado)
        antes = _fotografia(ordem)

        with pytest.raises(TransicaoStatusInvalidaException):
            ordem.cancelar("tarde demais", OM.ATENDIMENTO, ator=ATOR_ATENDENTE)

        assert _fotografia(ordem) == antes

    def test_estado_tem_precedencia_sobre_motivo_vazio(self) -> None:
        ordem = ordem_em(S.EM_EXECUCAO)
        with pytest.raises(TransicaoStatusInvalidaException):
            ordem.cancelar("", OM.ATENDIMENTO, ator=ATOR_ATENDENTE)

    @pytest.mark.parametrize(
        "motivo",
        [
            pytest.param("", id="vazio"),
            pytest.param("   ", id="espacos"),
        ],
    )
    def test_motivo_vazio_levanta_sem_mutar(self, motivo: str) -> None:
        ordem = ordem_em(S.RECEBIDA)
        antes = _fotografia(ordem)

        with pytest.raises(
            ValorInvalidoException, match="motivo de cancelamento e obrigatorio"
        ):
            ordem.cancelar(motivo, OM.ATENDIMENTO, ator=ATOR_ATENDENTE)

        assert _fotografia(ordem) == antes

    def test_motivo_com_nul_levanta_sem_mutar(self) -> None:
        ordem = ordem_em(S.RECEBIDA)
        antes = _fotografia(ordem)

        with pytest.raises(ValorInvalidoException, match="caractere de controle"):
            ordem.cancelar("desistiu\x00", OM.ATENDIMENTO, ator=ATOR_ATENDENTE)

        assert _fotografia(ordem) == antes

    def test_motivo_acima_do_limite_levanta(self) -> None:
        ordem = ordem_em(S.RECEBIDA)
        ordem.cancelar(
            "x" * TAMANHO_MAXIMO_MOTIVO, OM.ATENDIMENTO, ator=ATOR_ATENDENTE
        )  # no limite passa
        outra = ordem_em(S.RECEBIDA)
        with pytest.raises(ValorInvalidoException, match="excede"):
            outra.cancelar(
                "x" * (TAMANHO_MAXIMO_MOTIVO + 1), OM.ATENDIMENTO, ator=ATOR_ATENDENTE
            )

    def test_historico_nao_expoe_motivo_no_repr(self) -> None:
        ordem = ordem_em(S.RECEBIDA)
        ordem.cancelar(
            "cpf 12345678900 do cliente", OM.ATENDIMENTO, ator=ATOR_ATENDENTE
        )
        assert "12345678900" not in repr(ordem.historico[-1])


def test_ocorrido_em_e_utc() -> None:
    ordem = abrir_ordem()
    assert isinstance(ordem.criado_em, datetime)
    deslocamento = ordem.criado_em.utcoffset()
    assert deslocamento is not None
    assert deslocamento.total_seconds() == 0
