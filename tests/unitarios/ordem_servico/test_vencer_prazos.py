"""O ciclo dos prazos (RFC-004 secoes 4.6 e 4.7) sobre a saga e a OS em memoria.

A saga chega a cada etapa pelo orquestrador real; o relogio e manual (o prazo
vence quando o teste diz), os comandos saem por uma outbox fake que valida cada
envelope contra o contrato, e o consumidor do OS esta de pe ou nao. O SQL que
reivindica a saga e a guarda da outbox tem os testes no PostgreSQL.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from sqlalchemy.exc import DataError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from structlog.testing import capture_logs

from src.compartilhado.dominio.exceptions import ConflitoDeConcorrenciaException
from src.compartilhado.infraestrutura.database import BANCO_FORA
from src.ordem_servico.aplicacao.saga import vencer_prazos
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from src.ordem_servico.aplicacao.saga.prazos import (
    ATOR_PRAZOS,
    REENVIOS_ESGOTADOS,
    ComandoReenviadoEvent,
    PrazoEsgotadoEvent,
)
from src.ordem_servico.aplicacao.saga.vencer_prazos import (
    ResultadoDoCiclo,
)
from tests.unitarios.ordem_servico.cenario_da_saga import (
    FEITO,
    PLANO_DA_RFC,
    CenarioDaSaga,
)
from tests.unitarios.ordem_servico.ciclo_dos_prazos import MAXIMO, PRAZO, Prazos

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _logger_do_modulo_sem_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    # O logger de modulo fica em cache depois do primeiro uso (outro teste da
    # suite), e o `capture_logs` nao o alcancaria.
    monkeypatch.setattr(vencer_prazos, "_log", structlog.get_logger())


ETAPAS = (
    "aguardando_orcamento",
    "aguardando_reserva",
    "aguardando_pagamento",
    "aguardando_agendamento",
)


def _em(etapa: str) -> Prazos:
    return Prazos(CenarioDaSaga.em(etapa))


def _em_compensacao(plano: tuple[str, ...], **campos: Any) -> Prazos:
    return Prazos(CenarioDaSaga.em_compensacao(plano, **campos))


class TestReenvio:
    @pytest.mark.parametrize("etapa", ETAPAS)
    def test_reenvia_o_mesmo_comando_com_id_novo_e_sem_causa(self, etapa: str) -> None:
        prazos = _em(etapa)
        saga = prazos.saga
        antes = saga.comando_em_voo
        original = prazos.cenario.publicador.comandos[-1]
        assert antes is not None

        resultado = prazos.vencer()

        assert resultado == ResultadoDoCiclo(reenviadas=1)
        # Mesmo tipo e mesmos dados (a chave de negocio); correlation_id da OS e
        # causation_id nulo (a causa e o prazo, nao um evento).
        (tipo, dados, correlation_id, causation_id) = prazos.comandos[0]
        assert (tipo, dados, correlation_id, causation_id) == (
            antes["tipo"],
            antes["dados"],
            saga.ordem_id,
            None,
        )
        assert (original[0], original[1]) == (tipo, dados)
        assert len(prazos.comandos) == 1
        envelope = prazos.transacao.envelopes[0]
        assert envelope["id"] not in antes["mensagem_ids"]
        assert saga.comando_em_voo["mensagem_ids"] == [
            *antes["mensagem_ids"],
            envelope["id"],
        ]
        assert saga.reenvios == 1
        assert saga.prazo_resposta_em == prazos.relogio.agora + PRAZO
        passo = saga.passos[-1]
        # O ator e o nome do processo, literal: o historico e o runbook o citam.
        assert (passo["gatilho"], passo["ator"], passo["reenvio"]) == (
            "reenvio",
            "prazos",
            1,
        )
        assert (prazos.transacao.commits, prazos.transacao.rollbacks) == (1, 0)

    def test_so_a_saga_vencida_e_tratada(self) -> None:
        prazos = _em("aguardando_reserva")
        prazos.relogio.agora = prazos.saga.prazo_resposta_em - timedelta(seconds=1)

        resultado = prazos.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo()
        assert (prazos.comandos, prazos.transacao.commits) == ([], 0)
        assert prazos.saga.reenvios == 0

    @pytest.mark.parametrize("etapa", ETAPAS)
    def test_cinco_reenvios_e_depois_a_compensacao_por_prazo_tecnico(
        self, etapa: str
    ) -> None:
        prazos = _em(etapa)
        saga = prazos.saga
        comando = FEITO[etapa][1]

        for numero in range(1, MAXIMO + 1):
            assert prazos.vencer() == ResultadoDoCiclo(reenviadas=1)
            assert saga.reenvios == numero
            assert saga.etapa is EtapaSaga(etapa)
        assert [c[0] for c in prazos.comandos] == [comando] * MAXIMO
        assert len(saga.comando_em_voo["mensagem_ids"]) == MAXIMO + 1

        assert prazos.vencer() == ResultadoDoCiclo(esgotadas=1)

        plano = PLANO_DA_RFC[(etapa, "prazo_tecnico")]
        assert (saga.etapa, saga.motivo, saga.plano_compensacao) == (
            EtapaSaga.COMPENSANDO,
            "prazo_tecnico",
            plano,
        )
        # A compensacao pendente sai com o prazo novo e o contador zerado.
        tipo, dados, _, causa = prazos.comandos[-1]
        assert (tipo, causa) == (plano[0], None)
        assert dados["motivo"] == "prazo_tecnico"
        assert saga.reenvios == 0
        assert saga.comando_em_voo["tipo"] == plano[0]
        assert saga.passos[-1]["gatilho"] == "prazo_esgotado"
        assert saga.passos[-1]["ator"] == ATOR_PRAZOS

    def test_a_resposta_ao_original_e_ao_reenvio_continua_valida(self) -> None:
        # O participante respondeu ao original depois do reenvio: a etapa avanca,
        # e a resposta repetida ao reenvio, que chega depois, e ignorada (RFC-004
        # secao 4.5).
        prazos = _em("aguardando_agendamento")
        prazos.vencer()
        # O consumidor trata a resposta logo depois do ciclo.
        prazos.cenario.relogio.agora = prazos.relogio.agora

        primeira = prazos.cenario.orquestrador.tratar(
            prazos.cenario.evento("ExecucaoAgendada")
        )
        repetida = prazos.cenario.orquestrador.tratar(
            prazos.cenario.evento("ExecucaoAgendada")
        )

        assert primeira.etapa_nova is EtapaSaga.AGUARDANDO_INICIO
        assert repetida.desfecho.value == "ignorada"
        assert prazos.saga.etapa is EtapaSaga.AGUARDANDO_INICIO
        # A resposta zera o prazo: o ciclo seguinte nao a reenvia nem a esgota.
        assert prazos.saga.prazo_resposta_em is None

    def test_o_evento_do_dominio_conta_o_reenvio_por_comando(self) -> None:
        prazos = _em("aguardando_pagamento")

        prazos.vencer()

        assert prazos.saga.coletar_eventos()[-1:] == [
            ComandoReenviadoEvent(
                agregado_id=prazos.saga.ordem_id,
                comando="SolicitarPagamento",
                ocorrido_em=prazos.relogio.agora,
            )
        ]


class TestEsgotamentoEmCompensacao:
    def test_esgotada_a_compensacao_a_saga_para_em_falha_na_compensacao(self) -> None:
        plano = ("EstornarPagamento", "LiberarReserva", "DescartarDiagnostico")
        prazos = _em_compensacao(plano)
        saga = prazos.saga
        for _ in range(MAXIMO):
            prazos.vencer()
        assert saga.reenvios == MAXIMO
        ids = saga.comando_em_voo["mensagem_ids"]
        assert len(ids) == MAXIMO + 1

        assert prazos.vencer() == ResultadoDoCiclo(esgotadas=1)

        assert (saga.etapa, saga.falha, saga.plano_compensacao) == (
            EtapaSaga.FALHA_NA_COMPENSACAO,
            REENVIOS_ESGOTADOS,
            plano,
        )
        # Nada novo sai; o comando em voo fica, sem prazo, com todos os ids.
        assert len(prazos.comandos) == MAXIMO
        assert saga.prazo_resposta_em is None
        assert saga.comando_em_voo["mensagem_ids"] == ids
        eventos = saga.coletar_eventos()
        assert any(isinstance(e, PrazoEsgotadoEvent) for e in eventos)

    def test_saga_parada_nao_volta_a_ser_candidata(self) -> None:
        prazos = _em_compensacao(("LiberarReserva",))
        for _ in range(MAXIMO + 1):
            prazos.vencer()
        enviados = len(prazos.comandos)
        prazos.relogio.agora += timedelta(hours=1)

        resultado = prazos.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo()
        assert len(prazos.comandos) == enviados


class TestSemConsumidor:
    @pytest.mark.parametrize(
        "reenvios",
        [
            pytest.param(0, id="antes-do-primeiro-reenvio"),
            pytest.param(MAXIMO, id="com-os-reenvios-esgotados"),
        ],
    )
    def test_sem_consumidor_nao_reenvia_nem_esgota(self, reenvios: int) -> None:
        prazos = _em("aguardando_orcamento")
        prazos.saga._reenvios = reenvios
        prazos.presenca.ha = False
        prazos.relogio.agora = prazos.saga.prazo_resposta_em + timedelta(hours=1)
        antes = (prazos.saga.etapa, prazos.saga.reenvios, prazos.saga.passos)

        with capture_logs() as logs:
            resultado = prazos.caso_de_uso.executar()

        depois = (prazos.saga.etapa, prazos.saga.reenvios, prazos.saga.passos)
        assert resultado == ResultadoDoCiclo(pausado=True)
        assert depois == antes
        assert (prazos.comandos, prazos.transacao.commits) == ([], 0)
        assert [e["event"] for e in logs] == ["saga deadlines paused"]
        assert (logs[0]["log_level"], logs[0]["motivo"]) == (
            "warning",
            "sem_consumidor",
        )

    def test_a_saga_segue_de_onde_estava_quando_o_consumidor_volta(self) -> None:
        prazos = _em("aguardando_reserva")
        prazos.presenca.ha = False
        prazos.relogio.agora = prazos.saga.prazo_resposta_em + timedelta(hours=1)
        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(pausado=True)

        prazos.presenca.ha = True

        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo(reenviadas=1)
        assert prazos.saga.reenvios == 1

    def test_a_fila_so_e_conferida_com_uma_saga_vencida_na_mao(self) -> None:
        prazos = _em("aguardando_orcamento")

        prazos.vencer()
        assert prazos.presenca.consultas == 1  # a saga vencida; a busca vazia nao
        prazos.caso_de_uso.executar()  # o prazo novo ainda nao venceu

        assert prazos.presenca.consultas == 1


class TestIsolamentoDasSagas:
    def _duas_sagas(self) -> tuple[Prazos, Prazos]:
        """Duas OS no mesmo repositorio, com a saga da primeira corrompida."""
        ruim = CenarioDaSaga.em("aguardando_orcamento")
        boa = CenarioDaSaga.em("aguardando_reserva")
        ruim.ordens.ordens.update(boa.ordens.ordens)
        ruim.sagas.sagas.update(boa.sagas.sagas)
        return Prazos(ruim), Prazos(boa)

    def test_saga_que_o_dominio_recusa_fica_de_fora_e_as_outras_seguem(self) -> None:
        ruim, boa = self._duas_sagas()
        # Dados guardados que o contrato nao aceita (bug ou linha de outra versao):
        # o envio falha antes de gravar, e a saga seguinte e tratada.
        ruim.saga._comando_em_voo["dados"] = {"ordem_id": "nao-e-uuid"}
        ruim.saga._prazo_resposta_em = ruim.saga.atualizada_em
        boa.saga._prazo_resposta_em = boa.saga.atualizada_em + timedelta(seconds=1)
        ruim.relogio.agora = boa.saga.prazo_resposta_em + timedelta(minutes=5)
        avisos: list[None] = []

        with capture_logs() as logs:
            resultado = ruim.caso_de_uso.executar(lambda: avisos.append(None))

        assert resultado == ResultadoDoCiclo(reenviadas=1, recusadas=1)
        assert len(avisos) == 2
        assert ruim.saga.reenvios == 0
        assert boa.saga.reenvios == 1
        # A recusa nao deixou comando nem commit; so a saga boa gravou.
        assert (ruim.transacao.commits, ruim.transacao.rollbacks) == (1, 1)
        assert [c[0] for c in ruim.comandos] == ["ReservarPecas"]
        recusa = next(e for e in logs if e["event"] == "saga deadline refused")
        assert recusa["correlation_id"] == str(ruim.saga.ordem_id)
        assert recusa["log_level"] == "error"
        assert "nao-e-uuid" not in str(logs)

    @pytest.mark.parametrize(
        ("estragar", "erro"),
        [
            pytest.param(
                lambda em_voo: em_voo.pop("dados"), "KeyError", id="sem-dados"
            ),
            pytest.param(
                lambda em_voo: em_voo.update(tipo="placa-ABC1D23"),
                "ValueError",
                id="tipo-desconhecido",
            ),
        ],
    )
    def test_comando_guardado_fora_do_contrato_nao_trava_as_outras_sagas(
        self, estragar: Any, erro: str
    ) -> None:
        ruim, boa = self._duas_sagas()
        estragar(ruim.saga._comando_em_voo)
        ruim.saga._prazo_resposta_em = ruim.saga.atualizada_em
        boa.saga._prazo_resposta_em = boa.saga.atualizada_em + timedelta(seconds=1)
        ruim.relogio.agora = boa.saga.prazo_resposta_em + timedelta(minutes=5)

        with capture_logs() as logs:
            resultado = ruim.caso_de_uso.executar()

        # A de prazo mais antigo falha primeiro e a outra, atras dela, e tratada.
        assert resultado == ResultadoDoCiclo(reenviadas=1, recusadas=1)
        assert boa.saga.reenvios == 1
        recusa = next(e for e in logs if e["event"] == "saga deadline refused")
        assert recusa["correlation_id"] == str(ruim.saga.ordem_id)
        assert recusa["erro"] == erro
        # So o tipo e o arquivo:linha, nunca a mensagem da excecao.
        assert re.fullmatch(r"[a-z_]+\.py:\d+", recusa["onde"])
        assert "motivo" not in recusa
        assert "ABC1D23" not in str(logs)

    def test_erro_do_banco_que_se_repetiria_nao_trava_as_outras_sagas(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ruim, boa = self._duas_sagas()
        ruim = Prazos(ruim.cenario, banco_fora=BANCO_FORA)
        ruim.saga._prazo_resposta_em = ruim.saga.atualizada_em
        boa.saga._prazo_resposta_em = boa.saga.atualizada_em + timedelta(seconds=1)
        ruim.relogio.agora = boa.saga.prazo_resposta_em + timedelta(minutes=5)
        sagas = ruim.cenario.sagas
        salvar = sagas.salvar

        def _salvar(saga: Any) -> None:
            if saga.ordem_id == ruim.saga.ordem_id:
                raise DataError("UPDATE sagas ...", {}, Exception("dado-do-cliente"))
            salvar(saga)

        monkeypatch.setattr(sagas, "salvar", _salvar)

        with capture_logs() as logs:
            resultado = ruim.caso_de_uso.executar()

        assert resultado == ResultadoDoCiclo(reenviadas=1, recusadas=1)
        assert boa.saga.reenvios == 1
        recusa = next(e for e in logs if e["event"] == "saga deadline refused")
        assert recusa["erro"] == "DataError"
        assert "dado-do-cliente" not in str(logs)

    @pytest.mark.parametrize(
        "erro",
        [
            pytest.param(OperationalError("COMMIT", {}, Exception("fora")), id="rede"),
            pytest.param(InterfaceError("COMMIT", {}, Exception("fora")), id="conexao"),
            pytest.param(PoolTimeout("sem conexao livre"), id="pool"),
        ],
    )
    def test_banco_fora_depois_de_reivindicar_sobe_e_nao_recusa_a_saga(
        self, erro: Exception, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prazos = Prazos(CenarioDaSaga.em("aguardando_orcamento"), banco_fora=BANCO_FORA)

        def _cai_o_banco(saga: Any) -> None:
            raise erro

        monkeypatch.setattr(prazos.cenario.sagas, "salvar", _cai_o_banco)

        # O banco fora nao e defeito da saga: o ciclo falha, a transacao desfaz e
        # o laco do processo tenta de novo no ciclo seguinte.
        with pytest.raises(type(erro)):
            prazos.vencer()

        assert (prazos.transacao.commits, prazos.transacao.rollbacks) == (0, 1)

    def test_conflito_de_versao_deixa_a_saga_para_o_proximo_ciclo(self) -> None:
        prazos = _em("aguardando_orcamento")
        prazos.cenario.sagas.provocar_conflito()

        resultado = prazos.vencer()

        assert resultado == ResultadoDoCiclo(recusadas=1)
        assert prazos.transacao.commits == 0

    def test_recusa_do_dominio_leva_o_motivo_ao_log_sem_dado_de_cliente(self) -> None:
        prazos = _em("aguardando_orcamento")
        # Sem comando em voo o dominio recusa o reenvio, dizendo a etapa.
        prazos.saga._comando_em_voo = None

        with capture_logs() as logs:
            resultado = prazos.vencer()

        assert resultado == ResultadoDoCiclo(recusadas=1)
        assert prazos.comandos == []
        recusa = next(e for e in logs if e["event"] == "saga deadline refused")
        assert (recusa["erro"], recusa["motivo"]) == (
            "TransicaoDaSagaInvalidaError",
            "reenvio sem comando em voo na etapa aguardando_orcamento",
        )
        assert "onde" not in recusa

    def test_erro_do_banco_sobe_para_o_laco_do_processo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        prazos = _em("aguardando_orcamento")

        class BancoFora(Exception):  # noqa: N818  # excecao de teste
            pass

        def _falha(*_: Any, **__: Any) -> None:
            raise BancoFora

        monkeypatch.setattr(prazos.cenario.sagas, "reivindicar_vencida", _falha)

        with pytest.raises(BancoFora):
            prazos.vencer()


class TestSpanELog:
    def test_o_span_do_prazo_e_filho_do_contexto_da_saga(self) -> None:
        abertos: list[tuple[str, str | None]] = []

        @contextmanager
        def span(nome: str, traceparent: str | None) -> Iterator[None]:
            abertos.append((nome, traceparent))
            yield

        prazos = Prazos(CenarioDaSaga.em("aguardando_orcamento"), span=span)
        prazos.saga.registrar_contexto_de_trace(
            "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
        )

        prazos.vencer()

        assert abertos == [
            ("saga prazo", "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01")
        ]

    def test_ciclo_ocioso_nao_abre_span(self) -> None:
        abertos: list[str] = []

        @contextmanager
        def span(nome: str, traceparent: str | None) -> Iterator[None]:
            abertos.append(nome)
            yield

        prazos = Prazos(CenarioDaSaga.em("aguardando_decisao"), span=span)

        assert prazos.caso_de_uso.executar() == ResultadoDoCiclo()
        assert abertos == []

    def test_o_log_nao_leva_dados_do_comando(self) -> None:
        prazos = _em("aguardando_orcamento")
        for _ in range(MAXIMO):
            prazos.vencer()

        with capture_logs() as logs:
            prazos.vencer()

        esgotada = next(e for e in logs if e["event"] == "saga deadline exhausted")
        assert esgotada["correlation_id"] == str(prazos.saga.ordem_id)
        assert (esgotada["comando"], esgotada["etapa_nova"], esgotada["motivo"]) == (
            "GerarOrcamento",
            "compensando",
            "prazo_tecnico",
        )
        assert "itens" not in str(logs)

    def test_falha_na_compensacao_sai_em_aviso_com_a_causa(self) -> None:
        prazos = _em_compensacao(("LiberarReserva",))
        for _ in range(MAXIMO):
            prazos.vencer()

        with capture_logs() as logs:
            prazos.vencer()

        esgotada = next(e for e in logs if e["event"] == "saga deadline exhausted")
        assert esgotada["log_level"] == "warning"
        assert esgotada["falha"] == REENVIOS_ESGOTADOS


class TestErroQueSobeAoLaco:
    def test_conflito_antes_de_reivindicar_alguma_saga_sobe_sem_recusar_ninguem(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Sem saga reivindicada nao ha a quem recusar: o erro sobe ao laco do
        # processo, que so trata o erro de banco; qualquer outro derruba o
        # processo, e o Kubernetes o reinicia.
        prazos = _em("aguardando_orcamento")

        def _conflita(*_: Any, **__: Any) -> None:
            raise ConflitoDeConcorrenciaException

        monkeypatch.setattr(prazos.cenario.sagas, "reivindicar_vencida", _conflita)

        with pytest.raises(ConflitoDeConcorrenciaException):
            prazos.vencer()
