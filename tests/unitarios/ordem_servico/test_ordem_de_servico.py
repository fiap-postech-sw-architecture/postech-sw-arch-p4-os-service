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
    CHECKOUT_URL,
    EXPIRA_EM,
    LINK_DECISAO,
    TOTAL,
    VALIDO_ATE,
    abrir_ordem,
    ordem_em,
)

S = StatusOrdem
OM = OrigemMudanca


def _orcamento(ordem: OrdemDeServico, link_decisao: str = LINK_DECISAO) -> None:
    ordem.registrar_orcamento_gerado(
        orcamento_id=uuid4(),
        total=TOTAL,
        link_decisao=link_decisao,
        valido_ate=VALIDO_ATE,
    )


def _pagamento(ordem: OrdemDeServico, checkout_url: str = CHECKOUT_URL) -> None:
    ordem.registrar_pagamento_solicitado(
        pagamento_id=uuid4(),
        valor=TOTAL,
        checkout_url=checkout_url,
        expira_em=EXPIRA_EM,
    )


# (nome, fato, estado de origem, estado de destino, origem do historico)
FATOS: list[tuple[str, Callable[[OrdemDeServico], None], S, S, OM]] = [
    (
        "diagnostico_iniciado",
        OrdemDeServico.registrar_diagnostico_iniciado,
        S.RECEBIDA,
        S.EM_DIAGNOSTICO,
        OM.EXECUCAO,
    ),
    (
        "orcamento_gerado",
        _orcamento,
        S.EM_DIAGNOSTICO,
        S.AGUARDANDO_APROVACAO,
        OM.BILLING,
    ),
    (
        "pagamento_solicitado",
        _pagamento,
        S.AGUARDANDO_APROVACAO,
        S.AGUARDANDO_PAGAMENTO,
        OM.BILLING,
    ),
    (
        "aguardando_execucao",
        OrdemDeServico.registrar_aguardando_execucao,
        S.AGUARDANDO_PAGAMENTO,
        S.AGUARDANDO_EXECUCAO,
        OM.EXECUCAO,
    ),
    (
        "execucao_iniciada",
        OrdemDeServico.registrar_execucao_iniciada,
        S.AGUARDANDO_EXECUCAO,
        S.EM_EXECUCAO,
        OM.EXECUCAO,
    ),
    ("finalizar", OrdemDeServico.finalizar, S.EM_EXECUCAO, S.FINALIZADA, OM.EXECUCAO),
    (
        "entrega",
        OrdemDeServico.registrar_entrega,
        S.FINALIZADA,
        S.ENTREGUE,
        OM.ATENDIMENTO,
    ),
]

FATOS_ILEGAIS = [
    pytest.param(fato, estado, id=f"{nome}-em-{estado.value}")
    for nome, fato, de, _para, _origem in FATOS
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
        ("fato", "de", "para", "origem"),
        [pytest.param(f, d, p, o, id=n) for n, f, d, p, o in FATOS],
    )
    def test_fato_legal_transiciona_anota_e_emite(
        self,
        fato: Callable[[OrdemDeServico], None],
        de: StatusOrdem,
        para: StatusOrdem,
        origem: OrigemMudanca,
    ) -> None:
        ordem = ordem_em(de)
        historico_antes = ordem.historico
        ordem.limpar_eventos()

        fato(ordem)

        assert ordem.status is para
        assert ordem.historico[:-1] == historico_antes
        nova = ordem.historico[-1]
        assert nova.sequencia == len(historico_antes) + 1
        assert (nova.de, nova.para, nova.origem, nova.motivo) == (
            de,
            para,
            origem,
            None,
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
        orcamento_id = uuid4()

        ordem.registrar_orcamento_gerado(
            orcamento_id=orcamento_id,
            total=TOTAL,
            link_decisao=LINK_DECISAO,
            valido_ate=VALIDO_ATE,
        )

        resumo = ordem.resumo_orcamento
        assert resumo is not None
        assert (
            resumo.orcamento_id,
            resumo.total,
            resumo.link_decisao,
            resumo.valido_ate,
        ) == (orcamento_id, TOTAL, LINK_DECISAO, VALIDO_ATE)

    def test_pagamento_solicitado_guarda_o_resumo(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_APROVACAO)
        pagamento_id = uuid4()

        ordem.registrar_pagamento_solicitado(
            pagamento_id=pagamento_id,
            valor=TOTAL,
            checkout_url=CHECKOUT_URL,
            expira_em=EXPIRA_EM,
        )

        resumo = ordem.resumo_pagamento
        assert resumo is not None
        assert resumo.pagamento_id == pagamento_id
        assert resumo.status is StatusPagamento.SOLICITADO
        assert (resumo.valor, resumo.checkout_url, resumo.expira_em) == (
            TOTAL,
            CHECKOUT_URL,
            EXPIRA_EM,
        )

    @pytest.mark.parametrize(
        "link", ["javascript:alert(1)", "/relativo", "ftp://x.y/z", "https://"]
    )
    def test_link_de_decisao_invalido_levanta_sem_mutar(self, link: str) -> None:
        ordem = ordem_em(S.EM_DIAGNOSTICO)
        antes = _fotografia(ordem)

        with pytest.raises(ValueError, match="URL http"):
            _orcamento(ordem, link_decisao=link)

        assert _fotografia(ordem) == antes

    def test_checkout_url_invalida_levanta_sem_mutar(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_APROVACAO)
        antes = _fotografia(ordem)

        with pytest.raises(ValueError, match="checkout_url"):
            _pagamento(ordem, checkout_url="data:text/html,oi")

        assert _fotografia(ordem) == antes

    def test_estado_invalido_tem_precedencia_sobre_dado_invalido(self) -> None:
        ordem = ordem_em(S.RECEBIDA)
        with pytest.raises(TransicaoStatusInvalidaException):
            _orcamento(ordem, link_decisao="nao-e-url")

    def test_fluxo_completo_produz_linha_do_tempo_encadeada(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Relogio que anda 1 minuto por leitura: a ordem dos instantes e
        # provada sem depender da resolucao do relogio de parede (dois fatos
        # no mesmo tique passariam num ">=" mesmo com a ordem trocada).
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


class TestStatusDoPagamento:
    @pytest.mark.parametrize(
        "estado", [S.AGUARDANDO_PAGAMENTO, S.AGUARDANDO_EXECUCAO, S.ENTREGUE]
    )
    def test_so_o_resumo_muda(self, estado: StatusOrdem) -> None:
        ordem = ordem_em(estado)
        antes = ordem.resumo_pagamento
        assert antes is not None
        historico = ordem.historico
        ordem.limpar_eventos()

        ordem.registrar_status_do_pagamento(StatusPagamento.CONFIRMADO)

        depois = ordem.resumo_pagamento
        assert depois is not None
        assert depois.status is StatusPagamento.CONFIRMADO
        assert (depois.pagamento_id, depois.valor) == (antes.pagamento_id, antes.valor)
        assert (ordem.status, ordem.historico) == (estado, historico)
        assert ordem.coletar_eventos() == []
        assert ordem.atualizado_em > historico[-1].ocorrido_em

    def test_estorno_depois_do_cancelamento(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_PAGAMENTO)
        ordem.cancelar("cliente desistiu", OM.ATENDIMENTO)

        ordem.registrar_status_do_pagamento(StatusPagamento.ESTORNADO)

        assert ordem.resumo_pagamento is not None
        assert ordem.resumo_pagamento.status is StatusPagamento.ESTORNADO

    def test_sem_pagamento_solicitado_levanta_sem_mutar(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_APROVACAO)
        antes = _fotografia(ordem)

        with pytest.raises(ViolacaoRegraDeNegocioException, match="sem pagamento"):
            ordem.registrar_status_do_pagamento(StatusPagamento.CONFIRMADO)

        assert _fotografia(ordem) == antes

    def test_status_de_outro_tipo_levanta_sem_mutar(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_PAGAMENTO)
        antes = _fotografia(ordem)

        with pytest.raises(ValueError, match="status do pagamento"):
            # str de proposito: so o StatusPagamento passa na guarda.
            ordem.registrar_status_do_pagamento("confirmado")  # type: ignore[arg-type]

        assert _fotografia(ordem) == antes


class TestCancelamento:
    @pytest.mark.parametrize("estado", CANCELAVEIS)
    @pytest.mark.parametrize("origem", [OM.ATENDIMENTO, OM.SAGA])
    def test_cancela_antes_da_execucao(
        self, estado: StatusOrdem, origem: OrigemMudanca
    ) -> None:
        ordem = ordem_em(estado)
        ordem.limpar_eventos()

        ordem.cancelar("  orcamento recusado  ", origem)

        assert ordem.status is S.CANCELADA
        assert ordem.motivo_cancelamento == "orcamento recusado"
        ultima = ordem.historico[-1]
        assert (ultima.de, ultima.para, ultima.origem, ultima.motivo) == (
            estado,
            S.CANCELADA,
            origem,
            "orcamento recusado",
        )
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
            ordem.cancelar("tarde demais", OM.ATENDIMENTO)

        assert _fotografia(ordem) == antes

    def test_estado_tem_precedencia_sobre_motivo_vazio(self) -> None:
        ordem = ordem_em(S.EM_EXECUCAO)
        with pytest.raises(TransicaoStatusInvalidaException):
            ordem.cancelar("", OM.ATENDIMENTO)

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
            ordem.cancelar(motivo, OM.ATENDIMENTO)

        assert _fotografia(ordem) == antes

    def test_motivo_com_nul_levanta_sem_mutar(self) -> None:
        ordem = ordem_em(S.RECEBIDA)
        antes = _fotografia(ordem)

        with pytest.raises(ValorInvalidoException, match="caractere de controle"):
            ordem.cancelar("desistiu\x00", OM.ATENDIMENTO)

        assert _fotografia(ordem) == antes

    def test_motivo_acima_do_limite_levanta(self) -> None:
        ordem = ordem_em(S.RECEBIDA)
        ordem.cancelar("x" * TAMANHO_MAXIMO_MOTIVO, OM.ATENDIMENTO)  # no limite passa
        outra = ordem_em(S.RECEBIDA)
        with pytest.raises(ValorInvalidoException, match="excede"):
            outra.cancelar("x" * (TAMANHO_MAXIMO_MOTIVO + 1), OM.ATENDIMENTO)

    def test_historico_nao_expoe_motivo_no_repr(self) -> None:
        ordem = ordem_em(S.RECEBIDA)
        ordem.cancelar("cpf 12345678900 do cliente", OM.ATENDIMENTO)
        assert "12345678900" not in repr(ordem.historico[-1])


def test_ocorrido_em_e_utc() -> None:
    ordem = abrir_ordem()
    assert isinstance(ordem.criado_em, datetime)
    deslocamento = ordem.criado_em.utcoffset()
    assert deslocamento is not None
    assert deslocamento.total_seconds() == 0
