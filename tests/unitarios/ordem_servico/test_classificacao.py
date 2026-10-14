"""Classificacao do evento na etapa da saga (RFC-004 secoes 4.4 e 4.5).

As matrizes (12 etapas x 10 perfis da OS x 23 tipos, e cada compensacao
pendente x cada resposta, casada ou nao) sao geradas do oraculo do
``cenario_da_saga``, escrito a partir da RFC e nao importado do modulo testado.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
)
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.status import StatusOrdem
from tests.fabricas import ordem_em
from tests.unitarios.ordem_servico.cenario_da_saga import (
    COMPENSACOES,
    ESPERADA,
    PERFIS,
    RESPONDE,
    STATUS_DE_ENTRADA,
    esperado,
    ordem_no_perfil,
)
from tests.unitarios.ordem_servico.saga_de_teste import (
    AGORA,
    PRAZO,
    marcos_da_ordem,
    saga_em,
)

C = Classificacao
E = EtapaSaga
S = StatusOrdem


# Marcos de cada perfil da OS, calculados uma vez para a matriz.
_MARCOS = {perfil: MarcosDaOrdem.da_ordem(ordem_no_perfil(perfil)) for perfil in PERFIS}


class TestClassificar:
    @pytest.mark.parametrize(
        ("etapa", "perfil", "tipo"),
        [
            pytest.param(etapa, perfil, tipo, id=f"{etapa}-{perfil}-{tipo}")
            for etapa in STATUS_DE_ENTRADA
            for perfil in PERFIS
            for tipo in ESPERADA
        ],
    )
    def test_matriz_etapa_por_perfil_da_os_por_tipo(
        self, etapa: str, perfil: str, tipo: str
    ) -> None:
        saga = saga_em(etapa)

        assert saga.classificar(tipo, _MARCOS[perfil]) is esperado(etapa, tipo, perfil)

    def test_matriz_cobre_as_12_etapas_os_10_perfis_e_os_23_tipos(self) -> None:
        assert set(STATUS_DE_ENTRADA) == {e.value for e in EtapaSaga}
        assert len(PERFIS) == len(S) + 1
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
    def test_dentro_da_mesma_etapa_os_marcos_da_os_desempatam(
        self,
        etapa: str,
        status: StatusOrdem,
        pagamento: bool,
        tipo: str,
        classificacao: Classificacao,
    ) -> None:
        ordem = ordem_em(status)
        assert (ordem.resumo_pagamento is not None) is pagamento

        marcos = MarcosDaOrdem.da_ordem(ordem)
        assert saga_em(etapa).classificar(tipo, marcos) is classificacao

    def test_os_cancelada_antes_do_diagnostico_recusa_o_evento(self) -> None:
        # Nem repetido nem processado: a saga viva com a OS encerrada e o
        # estado que o cancelamento recusa, e nenhum evento a toca.
        marcos = MarcosDaOrdem.da_ordem(ordem_em(S.CANCELADA))

        classificacao = saga_em("aguardando_diagnostico").classificar(
            "DiagnosticoIniciado", marcos
        )

        assert classificacao is C.ORDEM_ENCERRADA

    def test_falha_na_compensacao_ignora_resposta_atrasada(self) -> None:
        marcos = MarcosDaOrdem.da_ordem(ordem_em(S.AGUARDANDO_APROVACAO))
        saga = saga_em("falha_na_compensacao")

        assert saga.classificar("ReservaLiberada", marcos) is C.FORA_DA_COMPENSACAO


class TestClassificarNaCompensacao:
    @pytest.mark.parametrize(
        ("pendente", "tipo", "casada"),
        [
            pytest.param(p, t, c, id=f"{p}-{t}-{'casada' if c else 'outra'}")
            for p in COMPENSACOES
            for t in RESPONDE
            for c in (True, False)
        ],
    )
    def test_cada_compensacao_pendente_por_cada_resposta(
        self, pendente: str, tipo: str, casada: bool
    ) -> None:
        enviado = uuid4()
        saga = saga_em(
            "compensando",
            _plano_compensacao=list(COMPENSACOES[COMPENSACOES.index(pendente) :]),
            _comando_em_voo={
                "tipo": pendente,
                "dados": {},
                "mensagem_ids": [str(enviado)],
                "enviado_em": AGORA.isoformat(),
            },
            _prazo_resposta_em=PRAZO,
        )

        classificacao = saga.classificar(
            tipo, marcos_da_ordem(), causation_id=enviado if casada else uuid4()
        )

        assert classificacao is esperado(
            "compensando", tipo, pendente=pendente, casada=casada
        )

    @pytest.mark.parametrize("etapa", ["compensando", "falha_na_compensacao"])
    @pytest.mark.parametrize("pendente", COMPENSACOES)
    def test_inicio_da_execucao_conforme_a_pendente(
        self, etapa: str, pendente: str
    ) -> None:
        saga = saga_em(
            etapa,
            _plano_compensacao=list(COMPENSACOES[COMPENSACOES.index(pendente) :]),
        )

        classificacao = saga.classificar(
            "ExecucaoIniciada", marcos_da_ordem(checkout=True)
        )

        assert classificacao is esperado(etapa, "ExecucaoIniciada", pendente=pendente)
        assert (classificacao is C.PROCESSAR) is (pendente == "CancelarExecucao")

    def test_estorno_sem_comando_em_voo_e_o_automatico(self) -> None:
        # Parada na falha nada fica em voo: nenhum causation_id casa.
        saga = saga_em(
            "falha_na_compensacao", _plano_compensacao=list(COMPENSACOES[1:])
        )

        classificacao = saga.classificar(
            "PagamentoEstornado", marcos_da_ordem(checkout=True), causation_id=uuid4()
        )

        assert classificacao is C.ESTORNO_AUTOMATICO
