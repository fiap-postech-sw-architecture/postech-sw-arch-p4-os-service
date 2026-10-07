"""Instancia da saga: abertura, passos do fluxo normal e classificacao dos eventos.

A matriz etapa x tipo (12 x 23) e gerada da tabela da RFC-004 secao 4.1,
escrita aqui de novo (e nao importada do modulo testado), mais as excecoes
da secao 4.5 dentro da mesma etapa.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.saga import (
    Classificacao,
    Envio,
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    Saga,
    SagaIniciadaEvent,
    TransicaoDaSagaInvalidaError,
)
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE, ATOR_PROCESSO, ordem_em

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)
C = Classificacao
S = StatusOrdem

# Ordem linear das etapas e etapa esperada de cada tipo (RFC-004 secao 4.1).
LINEAR = [
    "aguardando_diagnostico",
    "aguardando_orcamento",
    "aguardando_decisao",
    "aguardando_reserva",
    "aguardando_pagamento",
    "aguardando_agendamento",
    "aguardando_inicio",
    "em_execucao",
    "concluida",
]
ESPERADA = {
    "DiagnosticoIniciado": "aguardando_diagnostico",
    "DiagnosticoConcluido": "aguardando_diagnostico",
    "OrcamentoGerado": "aguardando_orcamento",
    "GeracaoDeOrcamentoFalhou": "aguardando_orcamento",
    "OrcamentoAprovado": "aguardando_decisao",
    "OrcamentoRecusado": "aguardando_decisao",
    "OrcamentoExpirado": "aguardando_decisao",
    "PecasReservadas": "aguardando_reserva",
    "ReservaDePecasFalhou": "aguardando_reserva",
    "PagamentoSolicitado": "aguardando_pagamento",
    "PagamentoConfirmado": "aguardando_pagamento",
    "PagamentoRecusado": "aguardando_pagamento",
    "PagamentoExpirado": "aguardando_pagamento",
    "ExecucaoAgendada": "aguardando_agendamento",
    "ExecucaoIniciada": "aguardando_inicio",
    "ExecucaoFinalizada": "em_execucao",
    "DiagnosticoDescartado": "compensando",
    "OrcamentoCancelado": "compensando",
    "ReservaLiberada": "compensando",
    "PagamentoCancelado": "compensando",
    "PagamentoEstornado": "compensando",
    "EstornoDePagamentoFalhou": "compensando",
    "ExecucaoCancelada": "compensando",
}
# Status com que a OS entra em cada etapa (o checkout ainda fechado em
# aguardando_pagamento); nas etapas da compensacao, um status anterior ao pivot.
STATUS_DE_ENTRADA = {
    "aguardando_diagnostico": S.RECEBIDA,
    "aguardando_orcamento": S.EM_DIAGNOSTICO,
    "aguardando_decisao": S.AGUARDANDO_APROVACAO,
    "aguardando_reserva": S.AGUARDANDO_APROVACAO,
    "aguardando_pagamento": S.AGUARDANDO_PAGAMENTO,
    "aguardando_agendamento": S.AGUARDANDO_EXECUCAO,
    "aguardando_inicio": S.AGUARDANDO_EXECUCAO,
    "em_execucao": S.EM_EXECUCAO,
    "concluida": S.FINALIZADA,
    "compensando": S.AGUARDANDO_APROVACAO,
    "compensada": S.CANCELADA,
    "falha_na_compensacao": S.AGUARDANDO_EXECUCAO,
}


def esperado(etapa: str, tipo: str) -> Classificacao:
    """A regra da RFC-004 secao 4.5 para a OS no status de entrada da etapa."""
    alvo = ESPERADA[tipo]
    if alvo == "compensando":
        return C.PROCESSAR if etapa == "compensando" else C.FORA_DA_COMPENSACAO
    if etapa not in LINEAR[:-1]:
        return C.FORA_DO_FLUXO
    if LINEAR.index(alvo) < LINEAR.index(etapa):
        return C.OBSOLETO
    if LINEAR.index(alvo) > LINEAR.index(etapa):
        return C.ADIANTADO
    # Mesma etapa, OS no status de entrada: so o DiagnosticoConcluido (OS
    # ainda recebida) e os desfechos do pagamento (checkout fechado) esperam.
    if tipo in {
        "DiagnosticoConcluido",
        "PagamentoConfirmado",
        "PagamentoRecusado",
        "PagamentoExpirado",
    }:
        return C.ADIANTADO
    return C.PROCESSAR


def saga_em(etapa: EtapaSaga | str, **campos: object) -> Saga:
    """Saga como a persistencia a reidrata, ja na ``etapa``."""
    return Saga(
        id=uuid4(),
        _etapa=EtapaSaga(etapa),
        _iniciada_em=AGORA,
        _etapa_desde=AGORA,
        _atualizada_em=AGORA,
        **campos,  # type: ignore[arg-type]
    )


def _envio(tipo: Comando = Comando.GERAR_ORCAMENTO, *, prazo: bool = True) -> Envio:
    return Envio(
        tipo=tipo,
        id=uuid4(),
        dados={"ordem_id": "x"},
        prazo_resposta_em=AGORA + timedelta(minutes=2) if prazo else None,
    )


class TestIniciar:
    def test_abre_em_aguardando_diagnostico_com_o_passo_de_abertura(self) -> None:
        ordem_id = uuid4()
        envio = _envio(Comando.SOLICITAR_DIAGNOSTICO, prazo=False)

        saga = Saga.iniciar(ordem_id, envio=envio, ator=ATOR_ATENDENTE, agora=AGORA)

        assert (saga.ordem_id, saga.etapa) == (
            ordem_id,
            EtapaSaga.AGUARDANDO_DIAGNOSTICO,
        )
        assert saga.iniciada_em == saga.etapa_desde == saga.atualizada_em == AGORA
        assert saga.passos == (
            {
                "seq": 1,
                "em": AGORA.isoformat(),
                "de": None,
                "para": "aguardando_diagnostico",
                "gatilho": "abertura",
                "mensagem_id": None,
                "comando": "SolicitarDiagnostico",
                "comando_id": str(envio.id),
                "motivo": None,
                "ator": ATOR_ATENDENTE,
            },
        )
        # Sem resposta automatica: nem comando em voo nem prazo (RFC-004 4.3).
        assert (saga.comando_em_voo, saga.prazo_resposta_em) == (None, None)
        assert (saga.reenvios, saga.motivo, saga.falha, saga.versao) == (
            0,
            None,
            None,
            1,
        )
        assert (saga.passos_concluidos, saga.plano_compensacao, saga.itens) == (
            (),
            (),
            (),
        )
        assert saga.coletar_eventos() == [
            SagaIniciadaEvent(agregado_id=ordem_id, ocorrido_em=AGORA)
        ]

    def test_repr_nao_expoe_passos_nem_comando(self) -> None:
        saga = saga_em(
            "aguardando_orcamento",
            _comando_em_voo={
                "tipo": "GerarOrcamento",
                "dados": {"segredo": "nao-aparece"},
                "mensagem_ids": [],
                "enviado_em": "x",
            },
        )
        assert "nao-aparece" not in repr(saga)


class TestAvancar:
    def test_mudanca_de_etapa_anota_e_registra_a_permanencia(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        depois = AGORA + timedelta(minutes=30)
        concluido = evento("DiagnosticoConcluido", saga.ordem_id)
        envio = _envio()

        saga.avancar(concluido, agora=depois, ator=ATOR_PROCESSO, envio=envio)

        assert saga.etapa is EtapaSaga.AGUARDANDO_ORCAMENTO
        assert saga.etapa_desde == saga.atualizada_em == depois
        (passo,) = saga.passos
        assert passo == {
            "seq": 1,
            "em": depois.isoformat(),
            "de": "aguardando_diagnostico",
            "para": "aguardando_orcamento",
            "gatilho": "DiagnosticoConcluido",
            "mensagem_id": str(concluido.id),
            "comando": "GerarOrcamento",
            "comando_id": str(envio.id),
            "motivo": None,
            "ator": ATOR_PROCESSO,
        }
        assert saga.coletar_eventos() == [
            EtapaDaSagaAlteradaEvent(
                agregado_id=saga.ordem_id,
                etapa_anterior=EtapaSaga.AGUARDANDO_DIAGNOSTICO,
                etapa_nova=EtapaSaga.AGUARDANDO_ORCAMENTO,
                permanencia=timedelta(minutes=30),
                ocorrido_em=depois,
            )
        ]

    def test_evento_na_mesma_etapa_nao_muda_a_etapa(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        depois = AGORA + timedelta(minutes=5)

        saga.avancar(
            evento("DiagnosticoIniciado", saga.ordem_id), agora=depois, ator=None
        )

        assert (saga.etapa, saga.etapa_desde) == (
            EtapaSaga.AGUARDANDO_DIAGNOSTICO,
            AGORA,
        )
        assert saga.atualizada_em == depois
        assert saga.passos[-1]["de"] == saga.passos[-1]["para"]
        assert saga.coletar_eventos() == []

    def test_comando_com_prazo_vira_o_comando_em_voo(self) -> None:
        saga = saga_em("aguardando_diagnostico", _reenvios=3)
        envio = _envio()

        saga.avancar(
            evento("DiagnosticoConcluido", saga.ordem_id),
            agora=AGORA,
            ator=None,
            envio=envio,
        )

        assert saga.comando_em_voo == {
            "tipo": "GerarOrcamento",
            "dados": {"ordem_id": "x"},
            "mensagem_ids": [str(envio.id)],
            "enviado_em": AGORA.isoformat(),
        }
        assert saga.prazo_resposta_em == envio.prazo_resposta_em
        assert saga.reenvios == 0

    @pytest.mark.parametrize(
        ("etapa", "tipo"),
        [
            pytest.param("aguardando_orcamento", "OrcamentoGerado", id="resposta"),
            pytest.param("aguardando_diagnostico", "DiagnosticoIniciado", id="humano"),
        ],
    )
    def test_passo_sem_comando_com_prazo_limpa_o_comando_em_voo(
        self, etapa: str, tipo: str
    ) -> None:
        saga = saga_em(
            etapa,
            _comando_em_voo={
                "tipo": "GerarOrcamento",
                "dados": {},
                "mensagem_ids": [],
                "enviado_em": "x",
            },
            _prazo_resposta_em=AGORA,
        )

        saga.avancar(evento(tipo, saga.ordem_id), agora=AGORA, ator=None)

        assert (saga.comando_em_voo, saga.prazo_resposta_em) == (None, None)

    def test_diagnostico_concluido_guarda_so_os_campos_do_contrato(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        itens = [
            {"tipo": "peca", "codigo": "PEC-1", "quantidade": 2, "extra": "ignorado"}
        ]

        saga.avancar(
            evento("DiagnosticoConcluido", saga.ordem_id, itens=itens),
            agora=AGORA,
            ator=None,
            envio=_envio(),
        )

        assert saga.itens == ({"tipo": "peca", "codigo": "PEC-1", "quantidade": 2},)

    def test_execucao_agendada_guarda_a_posicao_so_no_passo(self) -> None:
        saga = saga_em("aguardando_agendamento")

        saga.avancar(
            evento("ExecucaoAgendada", saga.ordem_id, posicao_na_fila=7),
            agora=AGORA,
            ator=None,
        )

        assert saga.passos[-1]["posicao_na_fila"] == 7
        assert saga.etapa is EtapaSaga.AGUARDANDO_INICIO

    @pytest.mark.parametrize(
        ("etapa", "tipo", "concluido"),
        [
            pytest.param("aguardando_orcamento", "OrcamentoGerado", "T3", id="T3"),
            pytest.param("aguardando_reserva", "PecasReservadas", "T5", id="T5"),
            pytest.param("aguardando_pagamento", "PagamentoSolicitado", "T6", id="T6"),
            pytest.param("aguardando_agendamento", "ExecucaoAgendada", "T7", id="T7"),
        ],
    )
    def test_marca_o_passo_concluido(
        self, etapa: str, tipo: str, concluido: str
    ) -> None:
        saga = saga_em(etapa, _passos_concluidos=["T1"])

        saga.avancar(evento(tipo, saga.ordem_id), agora=AGORA, ator=None)

        assert saga.passos_concluidos == ("T1", concluido)

    @pytest.mark.parametrize(
        ("etapa", "tipo"),
        [
            pytest.param("aguardando_decisao", "DiagnosticoIniciado", id="obsoleto"),
            pytest.param("aguardando_diagnostico", "ExecucaoIniciada", id="adiantado"),
            pytest.param("aguardando_decisao", "OrcamentoRecusado", id="falha"),
            pytest.param("compensando", "ReservaLiberada", id="compensacao"),
        ],
    )
    def test_evento_fora_da_etapa_ou_do_fluxo_normal_levanta_sem_mutar(
        self, etapa: str, tipo: str
    ) -> None:
        saga = saga_em(etapa)

        with pytest.raises(TransicaoDaSagaInvalidaError, match=tipo):
            saga.avancar(evento(tipo, saga.ordem_id), agora=AGORA, ator=None)

        assert (saga.etapa, saga.passos, saga.coletar_eventos()) == (
            EtapaSaga(etapa),
            (),
            [],
        )


class TestClassificar:
    @pytest.mark.parametrize(
        ("etapa", "tipo"),
        [
            pytest.param(etapa, tipo, id=f"{etapa}-{tipo}")
            for etapa in STATUS_DE_ENTRADA
            for tipo in ESPERADA
        ],
    )
    def test_matriz_etapa_por_tipo(self, etapa: str, tipo: str) -> None:
        saga = saga_em(etapa)
        ordem = ordem_em(STATUS_DE_ENTRADA[etapa])
        if etapa == "aguardando_pagamento":
            # Status de entrada sem o resumo: o checkout ainda nao abriu.
            ordem = ordem_em(S.AGUARDANDO_APROVACAO)
            ordem.registrar_pecas_reservadas(ator=ATOR_PROCESSO)

        assert saga.classificar(tipo, ordem) is esperado(etapa, tipo)

    def test_matriz_cobre_as_12_etapas_e_os_23_tipos(self) -> None:
        assert set(STATUS_DE_ENTRADA) == {e.value for e in EtapaSaga}
        assert len(ESPERADA) == 23

    @pytest.mark.parametrize(
        ("etapa", "status", "pagamento", "tipo", "classificacao"),
        [
            pytest.param(
                "aguardando_diagnostico",
                S.EM_DIAGNOSTICO,
                False,
                "DiagnosticoIniciado",
                C.REPETIDO,
                id="diagnostico-iniciado-repetido",
            ),
            pytest.param(
                "aguardando_diagnostico",
                S.EM_DIAGNOSTICO,
                False,
                "DiagnosticoConcluido",
                C.PROCESSAR,
                id="diagnostico-concluido-depois-do-inicio",
            ),
            pytest.param(
                "aguardando_pagamento",
                S.AGUARDANDO_PAGAMENTO,
                True,
                "PagamentoSolicitado",
                C.REPETIDO,
                id="pagamento-solicitado-repetido",
            ),
            pytest.param(
                "aguardando_pagamento",
                S.AGUARDANDO_PAGAMENTO,
                True,
                "PagamentoConfirmado",
                C.PROCESSAR,
                id="pagamento-confirmado-com-checkout",
            ),
            pytest.param(
                "aguardando_pagamento",
                S.AGUARDANDO_PAGAMENTO,
                True,
                "PagamentoRecusado",
                C.PROCESSAR,
                id="pagamento-recusado-com-checkout",
            ),
            pytest.param(
                "aguardando_pagamento",
                S.AGUARDANDO_PAGAMENTO,
                True,
                "PagamentoExpirado",
                C.PROCESSAR,
                id="pagamento-expirado-com-checkout",
            ),
        ],
    )
    def test_dentro_da_mesma_etapa_o_estado_da_os_desempata(
        self,
        etapa: str,
        status: StatusOrdem,
        pagamento: bool,
        tipo: str,
        classificacao: Classificacao,
    ) -> None:
        ordem = ordem_em(status)
        assert (ordem.resumo_pagamento is not None) is pagamento

        assert saga_em(etapa).classificar(tipo, ordem) is classificacao

    def test_falha_na_compensacao_ignora_resposta_atrasada(self) -> None:
        ordem = ordem_em(S.AGUARDANDO_APROVACAO)
        saga = saga_em("falha_na_compensacao")

        assert saga.classificar("ReservaLiberada", ordem) is C.FORA_DA_COMPENSACAO
