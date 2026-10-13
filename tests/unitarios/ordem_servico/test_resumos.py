from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from src.compartilhado.dominio.dinheiro import Dinheiro
from src.compartilhado.dominio.exceptions import ViolacaoRegraDeNegocioException
from src.ordem_servico.dominio.resumos import (
    TAMANHO_MAXIMO_URL,
    ResumoOrcamento,
    ResumoPagamento,
    StatusPagamento,
    pagamento_confirmado,
    pagamento_exigido,
    pagamento_solicitado,
)
from tests.fabricas import EXPIRA_EM, VALIDO_ATE

_LINK = "https://billing.pytstop.local/publico/orcamentos/tok"
_VALOR = Dinheiro(Decimal("10.00"))


def _orcamento(**kwargs: object) -> ResumoOrcamento:
    campos: dict[str, object] = {
        "orcamento_id": uuid4(),
        "total": _VALOR,
        "link_decisao": _LINK,
        "valido_ate": VALIDO_ATE,
        **kwargs,
    }
    return ResumoOrcamento(**campos)  # type: ignore[arg-type]


def _pagamento(**kwargs: object) -> ResumoPagamento:
    campos: dict[str, object] = {
        "pagamento_id": uuid4(),
        "status": StatusPagamento.SOLICITADO,
        "valor": _VALOR,
        "checkout_url": _LINK,
        "expira_em": EXPIRA_EM,
        **kwargs,
    }
    return ResumoPagamento(**campos)  # type: ignore[arg-type]


_URLS_INVALIDAS = [
    pytest.param("", id="vazia"),
    pytest.param("javascript:alert(1)", id="javascript"),
    pytest.param("//sem-esquema.com", id="sem-esquema"),
    pytest.param("http:///sem-host", id="sem-host"),
    pytest.param("https://billing.local@evil.com/x", id="userinfo"),
    pytest.param("https://user:senha@billing.local/x", id="usuario-e-senha"),
    pytest.param("https://billing.local/a b", id="espaco"),
    pytest.param(" https://billing.local/x", id="espaco-inicial"),
    pytest.param("https://billing.local/x\r\nSet-Cookie:a=b", id="crlf"),
    pytest.param("https://billing.local/x\x00", id="nul"),
    pytest.param("https://billing.local/ç", id="nao-ascii"),
]


class TestResumoOrcamento:
    def test_valido_e_igualdade_estrutural(self) -> None:
        oid = uuid4()
        a = _orcamento(orcamento_id=oid)
        b = _orcamento(orcamento_id=oid)
        assert a == b
        assert hash(a) == hash(b)

    @pytest.mark.parametrize("link", _URLS_INVALIDAS)
    def test_link_invalido_levanta(self, link: str) -> None:
        with pytest.raises(ValueError, match="link de decisao"):
            _orcamento(link_decisao=link)

    def test_link_no_limite_passa_e_acima_levanta(self) -> None:
        base = "https://x.y/"
        _orcamento(link_decisao=base + "a" * (TAMANHO_MAXIMO_URL - len(base)))
        with pytest.raises(ValueError, match="excede"):
            _orcamento(link_decisao=base + "a" * (TAMANHO_MAXIMO_URL - len(base) + 1))

    @pytest.mark.parametrize(
        ("campo", "valor"),
        [
            pytest.param("orcamento_id", None, id="id-none"),
            pytest.param("orcamento_id", "nao-uuid", id="id-texto"),
            pytest.param("total", None, id="total-none"),
            pytest.param("total", Decimal("10.00"), id="total-sem-moeda"),
            pytest.param("link_decisao", None, id="link-none"),
            pytest.param("valido_ate", None, id="validade-none"),
            pytest.param("valido_ate", datetime(2026, 10, 13), id="validade-sem-fuso"),
        ],
    )
    def test_campo_ausente_ou_de_outro_tipo_levanta(
        self, campo: str, valor: object
    ) -> None:
        with pytest.raises(ValueError, match=r"obrigatorio|fuso"):
            _orcamento(**{campo: valor})

    def test_repr_nao_expoe_o_link(self) -> None:
        assert "tok" not in repr(_orcamento())

    def test_imutavel(self) -> None:
        resumo = _orcamento()
        with pytest.raises(FrozenInstanceError):
            resumo.link_decisao = "https://outro"  # type: ignore[misc]


class TestResumoPagamento:
    def test_valido(self) -> None:
        resumo = _pagamento()
        assert resumo.status is StatusPagamento.SOLICITADO
        assert resumo.valor == _VALOR
        assert resumo.expira_em == EXPIRA_EM

    @pytest.mark.parametrize("url", _URLS_INVALIDAS)
    def test_checkout_invalido_levanta(self, url: str) -> None:
        with pytest.raises(ValueError, match="checkout_url"):
            _pagamento(checkout_url=url)

    @pytest.mark.parametrize(
        ("campo", "valor"),
        [
            pytest.param("pagamento_id", None, id="id-none"),
            pytest.param("status", None, id="status-none"),
            pytest.param("status", "confirmado", id="status-texto"),
            pytest.param("valor", None, id="valor-none"),
            pytest.param("expira_em", None, id="expiracao-none"),
            pytest.param("expira_em", datetime(2026, 10, 7), id="expiracao-sem-fuso"),
        ],
    )
    def test_campo_ausente_ou_de_outro_tipo_levanta(
        self, campo: str, valor: object
    ) -> None:
        with pytest.raises(ValueError, match=r"obrigatorio|fuso"):
            _pagamento(**{campo: valor})

    def test_repr_nao_expoe_o_checkout(self) -> None:
        assert "billing.pytstop" not in repr(_pagamento())


class TestCicloDoPagamento:
    def test_confirmado_e_o_mesmo_pagamento_com_outro_estado(self) -> None:
        solicitado = _pagamento()

        confirmado = solicitado.confirmado()

        assert confirmado == _pagamento(
            pagamento_id=solicitado.pagamento_id, status=StatusPagamento.CONFIRMADO
        )
        assert solicitado.status is StatusPagamento.SOLICITADO

    def test_o_primeiro_resumo_e_o_solicitado(self) -> None:
        novo = _pagamento()

        assert pagamento_solicitado(None, novo) is novo

    def test_segundo_pedido_levanta(self) -> None:
        with pytest.raises(ViolacaoRegraDeNegocioException, match="ja solicitado"):
            pagamento_solicitado(_pagamento(), _pagamento())

    @pytest.mark.parametrize(
        "status", [s for s in StatusPagamento if s is not StatusPagamento.SOLICITADO]
    )
    def test_primeiro_resumo_em_outro_estado_levanta(
        self, status: StatusPagamento
    ) -> None:
        with pytest.raises(ViolacaoRegraDeNegocioException, match=status.value):
            pagamento_solicitado(None, _pagamento(status=status))

    def test_confirmado_parte_do_solicitado(self) -> None:
        solicitado = _pagamento()

        assert pagamento_confirmado(solicitado) == solicitado.confirmado()

    def test_confirmado_sem_solicitado_levanta(self) -> None:
        with pytest.raises(ViolacaoRegraDeNegocioException, match="sem pagamento"):
            pagamento_confirmado(None)


_S = StatusPagamento
_ORIGENS = (
    _S.SOLICITADO,
    _S.CONFIRMADO,
    _S.RECUSADO,
    _S.EXPIRADO,
    _S.CANCELADO,
    _S.ESTORNADO,
)
# RFC-004 secao 4.4 e 7.1: o cancelamento so atinge a cobranca em aberto; em
# outro estado a resposta chegou depois de um mais novo (ou repetida) e o resumo
# segue como esta, sem recusar a resposta do Billing; o estorno serve a
# compensacao (origem confirmado) e ao estorno tardio do Billing, que aceita
# qualquer origem: seis origens por duas transicoes, 12 celulas.
_CANCELADO = {o: _S.CANCELADO if o is _S.SOLICITADO else o for o in _ORIGENS}
_ESTORNADO = dict.fromkeys(_ORIGENS, _S.ESTORNADO)


class TestTransicoesDoPagamento:
    @pytest.mark.parametrize(
        ("origem", "destino"),
        [pytest.param(o, d, id=f"{o.value}-cancelado") for o, d in _CANCELADO.items()],
    )
    def test_cancelado_pela_tabela(
        self, origem: StatusPagamento, destino: StatusPagamento
    ) -> None:
        resumo = _pagamento(status=origem)

        assert resumo.cancelado() == _pagamento(
            pagamento_id=resumo.pagamento_id, status=destino
        )

    @pytest.mark.parametrize(
        ("origem", "destino"),
        [pytest.param(o, d, id=f"{o.value}-estornado") for o, d in _ESTORNADO.items()],
    )
    def test_estornado_pela_tabela(
        self, origem: StatusPagamento, destino: StatusPagamento
    ) -> None:
        resumo = _pagamento(status=origem)

        assert resumo.estornado() == _pagamento(
            pagamento_id=resumo.pagamento_id, status=destino
        )

    def test_repetir_a_transicao_no_destino_devolve_o_mesmo_valor(self) -> None:
        cancelado = _pagamento(status=_S.CANCELADO)
        estornado = _pagamento(status=_S.ESTORNADO)

        assert cancelado.cancelado() is cancelado
        assert estornado.estornado() == estornado

    @pytest.mark.parametrize("origem", _ORIGENS)
    def test_recusado_e_expirado_so_partem_do_solicitado(
        self, origem: StatusPagamento
    ) -> None:
        # O estado antigo que chega depois de um mais novo (o recusado depois do
        # estorno da aprovacao tardia) nao o desfaz.
        resumo = _pagamento(status=origem)
        solicitado = origem is _S.SOLICITADO

        assert resumo.recusado().status is (_S.RECUSADO if solicitado else origem)
        assert resumo.expirado().status is (_S.EXPIRADO if solicitado else origem)

    def test_exigido_sem_resumo_levanta(self) -> None:
        with pytest.raises(ViolacaoRegraDeNegocioException, match="sem pagamento"):
            pagamento_exigido(None)

    def test_exigido_devolve_o_resumo(self) -> None:
        resumo = _pagamento()

        assert pagamento_exigido(resumo) is resumo


def test_status_de_pagamento_cobre_o_ciclo_do_billing() -> None:
    assert {s.value for s in StatusPagamento} == {
        "solicitado",
        "confirmado",
        "recusado",
        "expirado",
        "cancelado",
        "estornado",
    }
