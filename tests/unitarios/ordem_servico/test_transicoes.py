"""Conferencias da saga antes de mudar: as invariantes do estado e o
vocabulario fechado do gatilho (RFC-004 secoes 4.1, 4.4 e 7.2).
"""

from __future__ import annotations

from typing import Any

import pytest

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga import transicoes
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.modelo import (
    EtapaSaga,
    Gatilho,
    GatilhoDeProcesso,
    InvarianteDaSagaError,
    Transicao,
    TransicaoDaSagaInvalidaError,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    GATILHOS_VALIDOS,
)
from src.ordem_servico.aplicacao.saga.transicoes import conferir_gatilho
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE, ATOR_PROCESSO
from tests.unitarios.ordem_servico.cenario_da_saga import (
    COMPENSACOES,
    ESPERADA,
)
from tests.unitarios.ordem_servico.saga_de_teste import (
    AGORA,
    DEPOIS,
    compensacao_enviada,
    envio_do_comando,
    foto_da_saga,
    marcos_da_ordem,
    saga_compensando,
    saga_em,
)

C = Classificacao
E = EtapaSaga
S = StatusOrdem


class TestInvariantes:
    @pytest.mark.parametrize(
        "campos",
        [
            pytest.param({"_prazo_resposta_em": AGORA}, id="prazo-sem-comando"),
            pytest.param(
                {
                    "_comando_em_voo": {
                        "tipo": "GerarOrcamento",
                        "dados": {},
                        "mensagem_ids": [],
                        "enviado_em": "x",
                    }
                },
                id="comando-sem-prazo",
            ),
            pytest.param({"_reenvios": -7}, id="reenvios-negativo"),
            pytest.param({"_reenvios": 3}, id="reenvios-sem-comando"),
        ],
    )
    def test_estado_contraditorio_nao_monta(self, campos: dict[str, Any]) -> None:
        with pytest.raises(InvarianteDaSagaError, match=r"prazo|reenvios"):
            saga_em("aguardando_orcamento", **campos)

    @pytest.mark.parametrize(
        "campos",
        [
            pytest.param({"_comando_em_voo": None}, id="sem-comando-em-voo"),
            pytest.param({"_prazo_resposta_em": AGORA}, id="com-prazo"),
        ],
    )
    def test_falha_na_compensacao_guarda_o_comando_em_voo_sem_prazo(
        self, campos: dict[str, Any]
    ) -> None:
        # RFC-004 secao 4.7: a retomada reenvia o comando guardado, e o `prazos`
        # nao age sobre a saga parada.
        with pytest.raises(
            InvarianteDaSagaError, match="guarda o comando em voo, sem prazo"
        ):
            saga_em("falha_na_compensacao", **campos)

    def test_todo_mutador_termina_conferindo_as_invariantes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A conferencia roda depois da mudanca: a etapa vista nela e a nova.
        vistas: list[EtapaSaga] = []
        original = Saga._conferir_invariantes

        def espiao(saga: Saga) -> None:
            vistas.append(saga.etapa)
            original(saga)

        monkeypatch.setattr(Saga, "_conferir_invariantes", espiao)
        saga = saga_em("aguardando_diagnostico")
        assert vistas == [E.AGUARDANDO_DIAGNOSTICO]
        vistas.clear()

        saga.avancar(
            evento("DiagnosticoConcluido", saga.ordem_id),
            marcos_da_ordem(),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
            envio=envio_do_comando(Comando.GERAR_ORCAMENTO, saga),
        )
        assert vistas == [E.AGUARDANDO_ORCAMENTO]

        saga.solicitar_cancelamento(
            marcos_da_ordem(),
            agora=DEPOIS,
            ator=ATOR_ATENDENTE,
            envio=compensacao_enviada("CancelarOrcamento", saga, "cancelamento"),
        )
        assert vistas == [E.AGUARDANDO_ORCAMENTO, E.COMPENSANDO]

    def test_falha_na_compensacao_guarda_o_comando_em_voo_e_os_reenvios(self) -> None:
        saga = saga_em("falha_na_compensacao", _reenvios=5)

        assert saga.comando_em_voo is not None
        assert (saga.prazo_resposta_em, saga.reenvios) == (None, 5)

    @pytest.mark.parametrize("etapa", ["compensando", "falha_na_compensacao"])
    @pytest.mark.parametrize(
        "campos",
        [
            pytest.param({"_motivo": None}, id="sem-motivo"),
            pytest.param({"_plano_compensacao": []}, id="sem-plano"),
        ],
    )
    def test_compensacao_exige_motivo_e_plano(
        self, etapa: str, campos: dict[str, Any]
    ) -> None:
        with pytest.raises(InvarianteDaSagaError, match=etapa):
            saga_em(etapa, **campos)

    @pytest.mark.parametrize(
        ("etapa", "campos", "erro"),
        [
            pytest.param(
                "aguardando_inicio",
                {"_plano_compensacao": ["CancelarExecucao"]},
                "plano",
                id="plano-fora-da-compensacao",
            ),
            pytest.param(
                "compensada",
                {"_plano_compensacao": ["DescartarDiagnostico"]},
                "plano",
                id="plano-na-compensada",
            ),
            pytest.param(
                "compensando", {"_falha": "estorno_recusado"}, "falha", id="falha-fora"
            ),
            pytest.param(
                "falha_na_compensacao", {"_falha": None}, "falha", id="sem-falha"
            ),
        ],
    )
    def test_plano_e_falha_so_onde_cabem(
        self, etapa: str, campos: dict[str, Any], erro: str
    ) -> None:
        with pytest.raises(InvarianteDaSagaError, match=erro):
            saga_em(etapa, **campos)


class TestVocabularioDoGatilho:
    # O gatilho de um registro e o tipo da mensagem ou uma das seis palavras do
    # processo; a lista e fechada, e o GET /sagas a documenta.
    PALAVRAS = frozenset(
        {
            "abertura",
            "cancelamento_solicitado",
            "cancelamento_recusado",
            "reenvio",
            "prazo_esgotado",
            "retomada",
        }
    )

    def test_vocabulario_e_os_23_tipos_mais_as_6_palavras(self) -> None:
        # Escrito a partir do oraculo do cenario, nao do modulo testado.
        assert set(ESPERADA) | self.PALAVRAS == GATILHOS_VALIDOS
        assert len(GATILHOS_VALIDOS) == 29
        assert {g.value for g in GatilhoDeProcesso} == self.PALAVRAS

    @pytest.mark.parametrize("gatilho", sorted(set(ESPERADA) | PALAVRAS))
    def test_gatilho_do_vocabulario_passa(self, gatilho: str) -> None:
        conferir_gatilho(Transicao(E.EM_EXECUCAO), Gatilho(gatilho, None, AGORA, "x"))

    @pytest.mark.parametrize(
        "gatilho",
        ["", "evento", "Abertura", "abertura ", "diagnosticoiniciado", "retomar"],
    )
    def test_gatilho_fora_do_vocabulario_e_recusado(self, gatilho: str) -> None:
        transicao = Transicao(E.EM_EXECUCAO)
        cabecalho = Gatilho(gatilho, None, AGORA, "x")
        with pytest.raises(TransicaoDaSagaInvalidaError, match="vocabulario"):
            conferir_gatilho(transicao, cabecalho)

    def test_o_da_transicao_vale_no_lugar_do_tipo_do_evento(self) -> None:
        # A corrida do pivot grava cancelamento_recusado; o tipo da mensagem
        # (aqui fora do vocabulario) nao conta quando a transicao diz o seu.
        conferir_gatilho(
            Transicao(E.EM_EXECUCAO, gatilho="cancelamento_recusado"),
            Gatilho("qualquer", None, AGORA, "x"),
        )
        transicao = Transicao(E.EM_EXECUCAO, gatilho="inventado")
        cabecalho = Gatilho("abertura", None, AGORA, "x")
        with pytest.raises(TransicaoDaSagaInvalidaError, match="inventado"):
            conferir_gatilho(transicao, cabecalho)

    def test_a_saga_recusa_o_registro_com_gatilho_fora_do_vocabulario(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saga, _ = saga_compensando(COMPENSACOES)
        antes = foto_da_saga(saga)
        monkeypatch.setattr(
            transicoes,
            "CORRIDA_DO_PIVOT",
            Transicao(E.EM_EXECUCAO, gatilho="inventado"),
        )

        recebido = evento("ExecucaoIniciada", saga.ordem_id)
        marcos = marcos_da_ordem()
        with pytest.raises(TransicaoDaSagaInvalidaError, match="inventado"):
            saga.avancar(
                recebido,
                marcos,
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
            )

        assert foto_da_saga(saga) == antes
