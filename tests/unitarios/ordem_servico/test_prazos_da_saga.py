"""Prazos da saga no agregado: reenvio, esgotamento e retomada (RFC-004 4.6 e 4.7).

Cada etapa com resposta automatica, cada reenvio de 1 a 5, o esgotamento em
etapa linear (o plano da tabela da RFC 4.4 com o passo em voo) e em
``compensando``, a retomada e o que cada uma recusa sem mudar nada.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.modelo import (
    CompensacaoIniciadaEvent,
    Envio,
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    MotivoCompensacao,
    TransicaoDaSagaInvalidaError,
)
from src.ordem_servico.aplicacao.saga.prazos import (
    ATOR_PRAZOS,
    REENVIOS_ESGOTADOS,
    ComandoReenviadoEvent,
    PrazoEsgotadoEvent,
    comando_a_reenviar,
    compensacao_do_esgotamento,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from tests.fabricas import ATOR_ATENDENTE
from tests.unitarios.ordem_servico.cenario_da_saga import FEITO, PLANO_DA_RFC
from tests.unitarios.ordem_servico.saga_de_teste import (
    AGORA,
    ORCAMENTO_ID,
    dados_do_comando,
    foto_da_saga,
    marcos_da_ordem,
    saga_em,
)

E = EtapaSaga
PRAZO_S = timedelta(seconds=120)
MAXIMO = 5
# O instante de cada reenvio ou esgotamento, depois do prazo, e o prazo novo.
VENCIDO = AGORA + PRAZO_S + timedelta(seconds=10)
# As quatro etapas lineares com resposta automatica e o comando em voo de cada
# uma (``FEITO``, que a tabela da RFC 4.4 usa).
ETAPAS_TECNICAS = (
    "aguardando_orcamento",
    "aguardando_reserva",
    "aguardando_pagamento",
    "aguardando_agendamento",
)
PLANO_DA_COMPENSACAO = ("EstornarPagamento", "LiberarReserva", "CancelarOrcamento")


def _saga_linear(etapa: str, *, reenvios: int = 0) -> Saga:
    """Saga na ``etapa`` com o comando em voo dela, ja reenviado ``reenvios`` vezes."""
    concluidos, comando = FEITO[etapa]
    assert comando is not None
    ordem_id = uuid4()
    saga = saga_em(etapa, id=ordem_id)
    ids = [str(uuid4()) for _ in range(reenvios + 1)]
    return saga_em(
        etapa,
        id=ordem_id,
        _passos_concluidos=list(concluidos),
        _comando_em_voo={
            "tipo": comando,
            "dados": dados_do_comando(Comando(comando), saga),
            "mensagem_ids": ids,
            "enviado_em": AGORA.isoformat(),
        },
        _prazo_resposta_em=AGORA + PRAZO_S,
        _reenvios=reenvios,
    )


def _saga_compensando(plano: tuple[str, ...], *, reenvios: int = 0) -> Saga:
    ordem_id = uuid4()
    return saga_em(
        "compensando",
        id=ordem_id,
        _plano_compensacao=list(plano),
        _comando_em_voo={
            "tipo": plano[0],
            "dados": {"ordem_id": str(ordem_id), "motivo": "cancelamento"},
            "mensagem_ids": [str(uuid4()) for _ in range(reenvios + 1)],
            "enviado_em": AGORA.isoformat(),
        },
        _prazo_resposta_em=AGORA + PRAZO_S,
        _reenvios=reenvios,
    )


def _saga_parada(plano: tuple[str, ...], *, falha: str, ids: int = 1) -> Saga:
    ordem_id = uuid4()
    return saga_em(
        "falha_na_compensacao",
        id=ordem_id,
        _plano_compensacao=list(plano),
        _falha=falha,
        _comando_em_voo={
            "tipo": plano[0],
            "dados": {"ordem_id": str(ordem_id), "motivo": "cancelamento"},
            "mensagem_ids": [str(uuid4()) for _ in range(ids)],
            "enviado_em": AGORA.isoformat(),
        },
    )


def _id_do_envio(saga: Saga, indice: int) -> UUID:
    """O id do ``indice``-esimo envio do comando em voo."""
    em_voo = saga.comando_em_voo
    assert em_voo is not None
    return UUID(em_voo["mensagem_ids"][indice])


def _reenvio_de(saga: Saga, **campos: Any) -> Envio:
    """O comando em voo da ``saga`` de novo: id novo e prazo depois de ``VENCIDO``."""
    em_voo = saga.comando_em_voo
    assert em_voo is not None
    return Envio(
        **{
            "tipo": Comando(em_voo["tipo"]),
            "id": uuid4(),
            "dados": em_voo["dados"],
            "prazo_resposta_em": VENCIDO + PRAZO_S,
            **campos,
        }
    )


class TestReenviar:
    @pytest.mark.parametrize("etapa", ETAPAS_TECNICAS)
    @pytest.mark.parametrize("reenvios", range(5))
    def test_reenvio_de_1_a_5_em_cada_etapa_tecnica(
        self, etapa: str, reenvios: int
    ) -> None:
        saga = _saga_linear(etapa, reenvios=reenvios)
        antes = saga.comando_em_voo
        assert antes is not None
        envio = _reenvio_de(saga)

        saga.reenviar(envio, agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

        em_voo = saga.comando_em_voo
        assert em_voo is not None
        # Mesmo tipo, mesmos dados (a chave de negocio); o id novo no fim dos ids.
        assert (em_voo["tipo"], em_voo["dados"]) == (antes["tipo"], antes["dados"])
        assert em_voo["mensagem_ids"] == [*antes["mensagem_ids"], str(envio.id)]
        assert em_voo["enviado_em"] == VENCIDO.isoformat()
        assert saga.reenvios == reenvios + 1
        assert saga.prazo_resposta_em == envio.prazo_resposta_em
        assert (saga.etapa, saga.atualizada_em) == (EtapaSaga(etapa), VENCIDO)
        assert saga.etapa_desde == AGORA
        assert saga.passos[-1] == {
            "seq": 1,
            "em": VENCIDO.isoformat(),
            "de": etapa,
            "para": etapa,
            "gatilho": "reenvio",
            "mensagem_id": None,
            "comando": antes["tipo"],
            "comando_id": str(envio.id),
            "motivo": None,
            "ator": ATOR_PRAZOS,
            "reenvio": reenvios + 1,
        }
        assert saga.coletar_eventos() == [
            ComandoReenviadoEvent(
                agregado_id=saga.id, comando=antes["tipo"], ocorrido_em=VENCIDO
            )
        ]

    def test_reenvio_em_compensacao_guarda_o_motivo_e_o_plano(self) -> None:
        saga = _saga_compensando(("LiberarReserva", "DescartarDiagnostico"), reenvios=2)
        envio = _reenvio_de(saga)

        saga.reenviar(envio, agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

        assert (saga.etapa, saga.motivo, saga.plano_compensacao) == (
            E.COMPENSANDO,
            "cancelamento",
            ("LiberarReserva", "DescartarDiagnostico"),
        )
        assert saga.reenvios == 3
        passo = saga.passos[-1]
        assert (passo["gatilho"], passo["motivo"], passo["reenvio"]) == (
            "reenvio",
            "cancelamento",
            3,
        )

    def test_resposta_a_qualquer_envio_continua_casando(self) -> None:
        saga = _saga_compensando(("EstornarPagamento", "DescartarDiagnostico"))
        original = _id_do_envio(saga, 0)
        saga.reenviar(_reenvio_de(saga), agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)
        reenviado = _id_do_envio(saga, 1)

        marcos = marcos_da_ordem(checkout=True)
        for id_do_envio in (original, reenviado):
            assert (
                saga.classificar("PagamentoEstornado", marcos, causation_id=id_do_envio)
                is Classificacao.PROCESSAR
            )
        assert (
            saga.classificar("PagamentoEstornado", marcos, causation_id=uuid4())
            is Classificacao.ESTORNO_AUTOMATICO
        )

    @pytest.mark.parametrize(
        ("campos", "motivo"),
        [
            pytest.param({"tipo": Comando.RESERVAR_PECAS}, "outro_comando", id="tipo"),
            pytest.param({"dados": {"ordem_id": "x"}}, "outros_dados", id="dados"),
            pytest.param({"prazo_resposta_em": None}, "sem_prazo", id="sem-prazo"),
            pytest.param(
                {"prazo_resposta_em": VENCIDO}, "prazo_no_passado", id="prazo-vencido"
            ),
            pytest.param(
                {"prazo_resposta_em": VENCIDO.replace(tzinfo=None) + PRAZO_S},
                "prazo_sem_fuso",
                id="prazo-sem-fuso",
            ),
        ],
    )
    def test_envio_que_nao_e_o_comando_em_voo_e_recusado(
        self, campos: dict[str, Any], motivo: str
    ) -> None:
        saga = _saga_linear("aguardando_orcamento")
        antes = foto_da_saga(saga)

        reenvio = _reenvio_de(saga, **campos)
        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.reenviar(
                reenvio,
                agora=VENCIDO,
                ator=ATOR_PRAZOS,
                maximo=MAXIMO,
            )

        assert foto_da_saga(saga) == antes, motivo

    def test_id_de_envio_que_a_saga_ja_usou_e_recusado(self) -> None:
        saga = _saga_linear("aguardando_reserva", reenvios=1)
        antes = foto_da_saga(saga)
        usado = _id_do_envio(saga, 0)

        reenvio = _reenvio_de(saga, id=usado)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="repete o id"):
            saga.reenviar(
                reenvio,
                agora=VENCIDO,
                ator=ATOR_PRAZOS,
                maximo=MAXIMO,
            )

        assert foto_da_saga(saga) == antes

    @pytest.mark.parametrize(
        "instante",
        [
            pytest.param(AGORA - timedelta(seconds=1), id="antes-do-ultimo-registro"),
            pytest.param(VENCIDO.replace(tzinfo=None), id="sem-fuso"),
        ],
    )
    def test_instante_invalido_e_recusado(self, instante: datetime) -> None:
        saga = _saga_linear("aguardando_pagamento")
        antes = foto_da_saga(saga)

        reenvio = _reenvio_de(saga)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="instante"):
            saga.reenviar(reenvio, agora=instante, ator=ATOR_PRAZOS, maximo=MAXIMO)

        assert foto_da_saga(saga) == antes

    @pytest.mark.parametrize(
        "etapa", ["aguardando_decisao", "aguardando_inicio", "em_execucao"]
    )
    def test_esperas_sem_comando_em_voo_nao_reenviam(self, etapa: str) -> None:
        saga = saga_em(etapa)
        envio = Envio(
            tipo=Comando.GERAR_ORCAMENTO,
            id=uuid4(),
            dados={"ordem_id": str(saga.id)},
            prazo_resposta_em=VENCIDO + PRAZO_S,
        )

        with pytest.raises(TransicaoDaSagaInvalidaError, match="sem comando em voo"):
            saga.reenviar(envio, agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

    def test_saga_parada_nao_reenvia_por_prazo(self) -> None:
        # O comando em voo fica, mas sem prazo: quem o reenvia e a retomada.
        saga = _saga_parada(("EstornarPagamento",), falha="estorno_recusado")
        antes = foto_da_saga(saga)

        reenvio = _reenvio_de(saga)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="sem prazo"):
            saga.reenviar(reenvio, agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

        assert foto_da_saga(saga) == antes


class TestEsgotarEmEtapaLinear:
    @pytest.mark.parametrize("etapa", ETAPAS_TECNICAS)
    def test_inicia_a_compensacao_por_prazo_tecnico_com_o_passo_em_voo(
        self, etapa: str
    ) -> None:
        saga = _saga_linear(etapa, reenvios=5)
        comando = Comando(FEITO[etapa][1] or "")
        plano = PLANO_DA_RFC[(etapa, "prazo_tecnico")]
        primeira, dados = (
            compensacao_do_esgotamento(saga, VENCIDO, MAXIMO) or (None,) * 2
        )
        assert primeira is Comando(plano[0])
        envio = Envio(
            tipo=primeira,
            id=uuid4(),
            dados=dados or {},
            prazo_resposta_em=VENCIDO + PRAZO_S,
        )

        saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO, envio=envio)

        assert (saga.etapa, saga.motivo, saga.falha) == (
            E.COMPENSANDO,
            "prazo_tecnico",
            None,
        )
        assert saga.plano_compensacao == plano
        # O contador zera no primeiro comando da compensacao (RFC-004 secao 4.6).
        assert saga.reenvios == 0
        em_voo = saga.comando_em_voo
        assert em_voo is not None
        assert (em_voo["tipo"], em_voo["mensagem_ids"]) == (plano[0], [str(envio.id)])
        assert saga.prazo_resposta_em == envio.prazo_resposta_em
        passo = saga.passos[-1]
        assert (passo["gatilho"], passo["ator"], passo["motivo"]) == (
            "prazo_esgotado",
            ATOR_PRAZOS,
            "prazo_tecnico",
        )
        assert (passo["comando"], passo["comando_id"], passo["mensagem_id"]) == (
            plano[0],
            str(envio.id),
            None,
        )
        # O fato do prazo so entra depois da transicao que o agregado conferiu.
        assert saga.coletar_eventos() == [
            EtapaDaSagaAlteradaEvent(
                agregado_id=saga.id,
                etapa_anterior=EtapaSaga(etapa),
                etapa_nova=E.COMPENSANDO,
                permanencia=VENCIDO - AGORA,
                ocorrido_em=VENCIDO,
            ),
            CompensacaoIniciadaEvent(
                agregado_id=saga.id,
                motivo=MotivoCompensacao.PRAZO_TECNICO,
                ocorrido_em=VENCIDO,
            ),
            PrazoEsgotadoEvent(
                agregado_id=saga.id, comando=comando.value, ocorrido_em=VENCIDO
            ),
        ]

    @pytest.mark.parametrize("etapa", ETAPAS_TECNICAS)
    def test_a_primeira_compensacao_leva_so_o_ordem_id_e_o_motivo(
        self, etapa: str
    ) -> None:
        # No prazo esgotado a OS ainda nao conhece o recurso do passo em voo (o id
        # nasce na resposta que apagaria o prazo): nenhum id de recurso vai junto.
        saga = _saga_linear(etapa, reenvios=5)

        primeira = compensacao_do_esgotamento(saga, VENCIDO, MAXIMO)

        assert primeira is not None
        assert primeira[1] == {"ordem_id": str(saga.id), "motivo": "prazo_tecnico"}

    def test_envio_com_o_id_de_um_recurso_que_a_os_nao_conhece_e_recusado(
        self,
    ) -> None:
        saga = _saga_linear("aguardando_orcamento", reenvios=5)
        antes = foto_da_saga(saga)
        com_id = Envio(
            tipo=Comando.CANCELAR_ORCAMENTO,
            id=uuid4(),
            dados={
                "ordem_id": str(saga.id),
                "motivo": "prazo_tecnico",
                "orcamento_id": str(ORCAMENTO_ID),
            },
            prazo_resposta_em=VENCIDO + PRAZO_S,
        )

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.esgotar_prazo(
                agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO, envio=com_id
            )

        assert foto_da_saga(saga) == antes

    def test_envio_que_nao_e_a_primeira_compensacao_e_recusado(self) -> None:
        saga = _saga_linear("aguardando_reserva", reenvios=5)
        antes = foto_da_saga(saga)
        errado = Envio(
            tipo=Comando.CANCELAR_ORCAMENTO,
            id=uuid4(),
            dados={"ordem_id": str(saga.id), "motivo": "prazo_tecnico"},
            prazo_resposta_em=VENCIDO + PRAZO_S,
        )

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.esgotar_prazo(
                agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO, envio=errado
            )

        assert foto_da_saga(saga) == antes

    def test_sem_a_primeira_compensacao_e_recusado(self) -> None:
        saga = _saga_linear("aguardando_agendamento", reenvios=5)
        antes = foto_da_saga(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

        assert foto_da_saga(saga) == antes


class TestEsgotarEmCompensacao:
    def test_vai_a_falha_na_compensacao_sem_enviar_nada(self) -> None:
        plano = ("LiberarReserva", "CancelarOrcamento", "DescartarDiagnostico")
        saga = _saga_compensando(plano, reenvios=5)
        antes = saga.comando_em_voo

        assert compensacao_do_esgotamento(saga, VENCIDO, MAXIMO) is None
        saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

        assert (saga.etapa, saga.falha, saga.motivo) == (
            E.FALHA_NA_COMPENSACAO,
            REENVIOS_ESGOTADOS,
            "cancelamento",
        )
        # A pendente segue primeiro no plano e em voo, sem prazo, com os ids de
        # todos os envios: a resposta tardia ao original ainda casa.
        assert saga.plano_compensacao == plano
        assert saga.comando_em_voo == antes
        assert saga.prazo_resposta_em is None
        passo = saga.passos[-1]
        assert (passo["gatilho"], passo["ator"], passo["comando"]) == (
            "prazo_esgotado",
            ATOR_PRAZOS,
            None,
        )
        assert saga.coletar_eventos() == [
            EtapaDaSagaAlteradaEvent(
                agregado_id=saga.id,
                etapa_anterior=E.COMPENSANDO,
                etapa_nova=E.FALHA_NA_COMPENSACAO,
                permanencia=VENCIDO - AGORA,
                ocorrido_em=VENCIDO,
            ),
            PrazoEsgotadoEvent(
                agregado_id=saga.id, comando="LiberarReserva", ocorrido_em=VENCIDO
            ),
        ]

    def test_na_ultima_compensacao_tambem_para(self) -> None:
        saga = _saga_compensando(("DescartarDiagnostico",), reenvios=5)

        saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

        assert (saga.etapa, saga.plano_compensacao) == (
            E.FALHA_NA_COMPENSACAO,
            ("DescartarDiagnostico",),
        )

    def test_envio_na_falha_da_compensacao_e_recusado(self) -> None:
        saga = _saga_compensando(("LiberarReserva",), reenvios=5)
        antes = foto_da_saga(saga)

        envio = _reenvio_de(saga)
        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.esgotar_prazo(
                agora=VENCIDO,
                ator=ATOR_PRAZOS,
                maximo=MAXIMO,
                envio=envio,
            )

        assert foto_da_saga(saga) == antes


class TestMaximoDeReenvios:
    """O agregado confere o limite de reenvios, nao so o caso de uso."""

    @pytest.mark.parametrize(
        ("maximo", "reenvios"),
        [
            pytest.param(0, 0, id="sem-reenvio-permitido"),
            pytest.param(1, 1, id="o-segundo-com-maximo-1"),
            pytest.param(5, 5, id="o-sexto-com-maximo-5"),
            pytest.param(3, 5, id="muito-alem-do-maximo"),
        ],
    )
    @pytest.mark.parametrize(
        "compensando", [False, True], ids=["linear", "compensando"]
    )
    def test_reenvio_alem_do_maximo_e_recusado(
        self, maximo: int, reenvios: int, compensando: bool
    ) -> None:
        saga = (
            _saga_compensando(("LiberarReserva",), reenvios=reenvios)
            if compensando
            else _saga_linear("aguardando_orcamento", reenvios=reenvios)
        )
        antes = foto_da_saga(saga)

        reenvio = _reenvio_de(saga)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="alem do maximo"):
            saga.reenviar(reenvio, agora=VENCIDO, ator=ATOR_PRAZOS, maximo=maximo)

        assert foto_da_saga(saga) == antes

    @pytest.mark.parametrize(
        ("maximo", "reenvios"),
        [
            pytest.param(1, 0, id="o-primeiro-com-maximo-1"),
            pytest.param(5, 4, id="o-quinto-com-maximo-5"),
        ],
    )
    def test_reenvio_dentro_do_maximo_e_aceito(
        self, maximo: int, reenvios: int
    ) -> None:
        saga = _saga_linear("aguardando_orcamento", reenvios=reenvios)

        saga.reenviar(_reenvio_de(saga), agora=VENCIDO, ator=ATOR_PRAZOS, maximo=maximo)

        assert saga.reenvios == reenvios + 1

    @pytest.mark.parametrize(
        ("maximo", "reenvios"),
        [
            pytest.param(1, 0, id="nenhum-reenvio-feito"),
            pytest.param(5, 0, id="so-o-original"),
            pytest.param(5, 4, id="falta-um-reenvio"),
        ],
    )
    @pytest.mark.parametrize(
        "compensando", [False, True], ids=["linear", "compensando"]
    )
    def test_esgotamento_antes_dos_reenvios_e_recusado(
        self, maximo: int, reenvios: int, compensando: bool
    ) -> None:
        saga = (
            _saga_compensando(("LiberarReserva",), reenvios=reenvios)
            if compensando
            else _saga_linear("aguardando_orcamento", reenvios=reenvios)
        )
        antes = foto_da_saga(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError, match="reenvios feitos"):
            saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=maximo)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="reenvios feitos"):
            compensacao_do_esgotamento(saga, VENCIDO, maximo)

        assert foto_da_saga(saga) == antes

    @pytest.mark.parametrize(
        ("maximo", "reenvios"),
        [
            pytest.param(0, 0, id="sem-reenvio-permitido"),
            pytest.param(5, 5, id="todos-os-reenvios-feitos"),
        ],
    )
    def test_esgotamento_depois_dos_reenvios_e_aceito(
        self, maximo: int, reenvios: int
    ) -> None:
        saga = _saga_compensando(("LiberarReserva",), reenvios=reenvios)

        saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=maximo)

        assert saga.etapa is E.FALHA_NA_COMPENSACAO


class TestEsgotarSemPrazo:
    @pytest.mark.parametrize(
        "etapa",
        ["aguardando_decisao", "aguardando_inicio", "em_execucao", "concluida"],
    )
    def test_espera_humana_ou_etapa_final_nao_esgota(self, etapa: str) -> None:
        saga = saga_em(etapa)

        with pytest.raises(TransicaoDaSagaInvalidaError, match="sem prazo"):
            saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

    def test_prazo_gravado_numa_etapa_que_nao_compensa_e_recusado(self) -> None:
        # Estado que o agregado nao produz (prazo em `em_execucao`): recusa em vez
        # de compensar uma etapa que ja passou do pivot.
        saga = saga_em(
            "em_execucao",
            _comando_em_voo={
                "tipo": "GerarOrcamento",
                "dados": {},
                "mensagem_ids": [str(uuid4())],
                "enviado_em": AGORA.isoformat(),
            },
            _prazo_resposta_em=AGORA + PRAZO_S,
            _reenvios=MAXIMO,
        )
        antes = foto_da_saga(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError, match="em_execucao"):
            saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

        assert foto_da_saga(saga) == antes

    def test_saga_parada_nao_esgota_de_novo(self) -> None:
        saga = _saga_parada(("EstornarPagamento",), falha=REENVIOS_ESGOTADOS)

        with pytest.raises(TransicaoDaSagaInvalidaError, match="sem prazo"):
            saga.esgotar_prazo(agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)

    def test_instante_que_volta_e_recusado(self) -> None:
        saga = _saga_compensando(("LiberarReserva",), reenvios=5)

        agora = AGORA - timedelta(seconds=1)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="instante"):
            saga.esgotar_prazo(agora=agora, ator="x", maximo=MAXIMO)

    def test_sem_comando_em_voo_nao_ha_o_que_reenviar(self) -> None:
        saga = saga_em("aguardando_decisao")
        with pytest.raises(TransicaoDaSagaInvalidaError, match="sem comando em voo"):
            comando_a_reenviar(saga)


class TestRetomar:
    @pytest.mark.parametrize("falha", ["estorno_recusado", REENVIOS_ESGOTADOS])
    @pytest.mark.parametrize("ids", [1, 3, 7])
    def test_volta_a_compensando_com_a_pendente_de_novo(
        self, falha: str, ids: int
    ) -> None:
        plano = ("EstornarPagamento", "LiberarReserva", "DescartarDiagnostico")
        saga = _saga_parada(plano, falha=falha, ids=ids)
        antes = saga.comando_em_voo
        assert antes is not None
        envio = _reenvio_de(saga)

        saga.retomar(envio, agora=VENCIDO, ator=ATOR_ATENDENTE)

        assert (saga.etapa, saga.falha, saga.motivo) == (
            E.COMPENSANDO,
            None,
            "cancelamento",
        )
        assert saga.plano_compensacao == plano
        # Os ids anteriores ficam: a resposta tardia ao original ainda casa.
        em_voo = saga.comando_em_voo
        assert em_voo is not None
        assert em_voo["mensagem_ids"] == [*antes["mensagem_ids"], str(envio.id)]
        assert (em_voo["tipo"], em_voo["dados"]) == (antes["tipo"], antes["dados"])
        assert saga.reenvios == 0
        assert saga.prazo_resposta_em == envio.prazo_resposta_em
        passo = saga.passos[-1]
        assert (passo["gatilho"], passo["ator"], passo["de"], passo["para"]) == (
            "retomada",
            ATOR_ATENDENTE,
            "falha_na_compensacao",
            "compensando",
        )
        assert (passo["comando"], passo["comando_id"]) == (
            "EstornarPagamento",
            str(envio.id),
        )
        assert "reenvio" not in passo
        assert saga.coletar_eventos() == [
            EtapaDaSagaAlteradaEvent(
                agregado_id=saga.id,
                etapa_anterior=E.FALHA_NA_COMPENSACAO,
                etapa_nova=E.COMPENSANDO,
                permanencia=VENCIDO - AGORA,
                ocorrido_em=VENCIDO,
            )
        ]

    def test_resposta_ao_envio_original_ainda_conclui_a_compensacao(self) -> None:
        saga = _saga_parada(
            ("EstornarPagamento", "DescartarDiagnostico"),
            falha="estorno_recusado",
            ids=2,
        )
        original = _id_do_envio(saga, 0)
        saga.retomar(_reenvio_de(saga), agora=VENCIDO, ator=ATOR_ATENDENTE)

        classificacao = saga.classificar(
            "PagamentoEstornado",
            marcos_da_ordem(checkout=True),
            causation_id=original,
        )

        assert classificacao is Classificacao.PROCESSAR

    @pytest.mark.parametrize(
        "etapa",
        [
            "aguardando_orcamento",
            "aguardando_decisao",
            "em_execucao",
            "concluida",
            "compensada",
        ],
    )
    def test_fora_da_falha_na_compensacao_e_recusada(self, etapa: str) -> None:
        saga = saga_em(etapa)
        antes = foto_da_saga(saga)
        envio = Envio(
            tipo=Comando.DESCARTAR_DIAGNOSTICO,
            id=uuid4(),
            dados={"ordem_id": str(saga.id), "motivo": "cancelamento"},
            prazo_resposta_em=VENCIDO + PRAZO_S,
        )

        with pytest.raises(TransicaoDaSagaInvalidaError, match="retomada"):
            saga.retomar(envio, agora=VENCIDO, ator=ATOR_ATENDENTE)

        assert foto_da_saga(saga) == antes

    def test_compensando_nao_e_retomada(self) -> None:
        saga = _saga_compensando(("LiberarReserva",))

        reenvio = _reenvio_de(saga)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="retomada"):
            saga.retomar(reenvio, agora=VENCIDO, ator=ATOR_ATENDENTE)

    @pytest.mark.parametrize(
        "campos",
        [
            pytest.param({"tipo": Comando.LIBERAR_RESERVA}, id="tipo"),
            pytest.param({"dados": {"ordem_id": "x"}}, id="dados"),
            pytest.param({"prazo_resposta_em": None}, id="sem-prazo"),
            pytest.param({"prazo_resposta_em": VENCIDO}, id="prazo-vencido"),
        ],
    )
    def test_envio_que_nao_e_a_pendente_de_novo_e_recusado(
        self, campos: dict[str, Any]
    ) -> None:
        saga = _saga_parada(
            ("EstornarPagamento", "DescartarDiagnostico"), falha="estorno_recusado"
        )
        antes = foto_da_saga(saga)

        reenvio = _reenvio_de(saga, **campos)
        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.retomar(reenvio, agora=VENCIDO, ator=ATOR_ATENDENTE)

        assert foto_da_saga(saga) == antes

    def test_instante_que_volta_e_recusado(self) -> None:
        saga = _saga_parada(("EstornarPagamento",), falha="estorno_recusado")

        reenvio = _reenvio_de(saga)
        agora = AGORA - timedelta(seconds=1)
        with pytest.raises(TransicaoDaSagaInvalidaError, match="instante"):
            saga.retomar(reenvio, agora=agora, ator="x")

    def test_a_saga_retomada_pode_esgotar_e_ser_retomada_de_novo(self) -> None:
        saga = _saga_parada(("EstornarPagamento",), falha="estorno_recusado")
        saga.retomar(_reenvio_de(saga), agora=VENCIDO, ator=ATOR_ATENDENTE)
        depois = VENCIDO + PRAZO_S + timedelta(seconds=1)
        saga.reenviar(
            _reenvio_de(saga, prazo_resposta_em=depois + PRAZO_S),
            agora=depois,
            ator=ATOR_PRAZOS,
            maximo=1,
        )
        saga.esgotar_prazo(
            agora=depois + timedelta(seconds=1), ator=ATOR_PRAZOS, maximo=1
        )
        assert (saga.etapa, saga.falha) == (E.FALHA_NA_COMPENSACAO, REENVIOS_ESGOTADOS)
        ids_antes = (saga.comando_em_voo or {}).get("mensagem_ids", [])
        assert len(ids_antes) == 3

        retomada_em = depois + timedelta(seconds=2)
        saga.retomar(
            _reenvio_de(saga, prazo_resposta_em=retomada_em + PRAZO_S),
            agora=retomada_em,
            ator=ATOR_ATENDENTE,
        )

        assert saga.etapa is E.COMPENSANDO
        assert len((saga.comando_em_voo or {}).get("mensagem_ids", [])) == 4
        assert [p["gatilho"] for p in saga.passos][-4:] == [
            "retomada",
            "reenvio",
            "prazo_esgotado",
            "retomada",
        ]
        assert [p["seq"] for p in saga.passos] == list(range(1, len(saga.passos) + 1))


def test_o_reenvio_nao_perde_o_envio_original_no_registro() -> None:
    saga = _saga_linear("aguardando_orcamento")
    original = (saga.comando_em_voo or {}).get("mensagem_ids", [])[0]
    envio = _reenvio_de(saga)

    saga.reenviar(envio, agora=VENCIDO, ator=ATOR_PRAZOS, maximo=MAXIMO)
    saga.reenviar(
        replace(envio, id=uuid4(), prazo_resposta_em=VENCIDO + 2 * PRAZO_S),
        agora=VENCIDO + PRAZO_S,
        ator=ATOR_PRAZOS,
        maximo=MAXIMO,
    )

    ids = (saga.comando_em_voo or {}).get("mensagem_ids", [])
    assert ids[0] == original
    assert len(ids) == 3
    assert saga.reenvios == 2
