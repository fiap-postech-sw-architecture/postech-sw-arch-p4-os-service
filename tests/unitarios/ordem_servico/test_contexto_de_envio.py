"""Envio de comandos: o caminho unico que grava na outbox e a politica do relogio."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import structlog
from structlog.testing import capture_logs

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga import contexto_de_envio
from src.ordem_servico.aplicacao.saga.contexto_de_envio import (
    ContextoDeEnvio,
    RelogioDefasadoError,
    enviar,
)
from src.ordem_servico.aplicacao.saga.modelo import EtapaSaga
from tests.unitarios.fakes import FakeUnitOfWork
from tests.unitarios.ordem_servico.test_saga import AGORA, saga_em

PRAZO = timedelta(seconds=120)
DADOS = {"ordem_id": str(uuid4()), "motivo": "cancelamento"}


class TestEnviar:
    def test_grava_o_comando_e_devolve_o_envio_dele(self) -> None:
        publicador, ordem_id, causa = FakeUnitOfWork(), uuid4(), uuid4()
        prazo = AGORA + PRAZO

        envio = enviar(
            publicador,
            (Comando.DESCARTAR_DIAGNOSTICO, DADOS),
            correlation_id=ordem_id,
            causation_id=causa,
            prazo_resposta_em=prazo,
        )

        assert publicador.comandos == [("DescartarDiagnostico", DADOS, ordem_id, causa)]
        assert str(envio.id) == publicador.envelopes[0]["id"]
        assert (envio.tipo, dict(envio.dados), envio.prazo_resposta_em) == (
            Comando.DESCARTAR_DIAGNOSTICO,
            DADOS,
            prazo,
        )

    def test_sem_causa_nem_prazo(self) -> None:
        publicador = FakeUnitOfWork()

        envio = enviar(
            publicador,
            (Comando.DESCARTAR_DIAGNOSTICO, DADOS),
            correlation_id=uuid4(),
        )

        assert publicador.comandos[0][3] is None
        assert envio.prazo_resposta_em is None

    def test_o_repr_do_envio_nao_expoe_os_dados(self) -> None:
        # A abertura leva a descricao do problema e a placa: dado pessoal.
        dados = {
            "ordem_id": uuid4(),
            "veiculo_id": uuid4(),
            "veiculo": {
                "placa": "BRA2E19",
                "marca": "Volkswagen",
                "modelo": "Gol",
                "ano": 2019,
            },
            "descricao_problema": "texto do cliente",
        }

        envio = enviar(
            FakeUnitOfWork(),
            (Comando.SOLICITAR_DIAGNOSTICO, dados),
            correlation_id=uuid4(),
        )

        assert "texto do cliente" not in repr(envio)
        assert "BRA2E19" not in repr(envio)


class TestContextoDeEnvio:
    def test_envia_com_o_prazo_tecnico_a_partir_do_instante_do_passo(self) -> None:
        publicador = FakeUnitOfWork()

        envio = ContextoDeEnvio(PRAZO).enviar(
            publicador,
            (Comando.DESCARTAR_DIAGNOSTICO, DADOS),
            correlation_id=uuid4(),
            agora=AGORA,
        )

        assert envio.prazo_resposta_em == AGORA + PRAZO

    def test_o_relogio_padrao_e_o_utc_de_agora(self) -> None:
        antes = datetime.now(UTC)

        instante = ContextoDeEnvio(PRAZO).relogio()

        assert antes <= instante <= datetime.now(UTC)
        assert instante.tzinfo is UTC


def _contexto_com_relogio_em(instante: datetime) -> ContextoDeEnvio:
    return ContextoDeEnvio(PRAZO, relogio=lambda: instante)


class TestInstanteDoPasso:
    def test_relogio_a_frente_do_ultimo_registro_e_o_proprio_relogio(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        depois = AGORA + timedelta(minutes=1)

        assert _contexto_com_relogio_em(depois).instante_do_passo(saga, tipo="x") == (
            depois
        )

    @pytest.mark.parametrize(
        ("atras", "avisa"),
        [
            pytest.param(timedelta(milliseconds=5), False, id="5-ms"),
            pytest.param(timedelta(seconds=1), False, id="1-s"),
            pytest.param(timedelta(seconds=1, milliseconds=1), True, id="1-s-e-1-ms"),
            pytest.param(timedelta(seconds=60), True, id="60-s"),
        ],
    )
    def test_atras_do_ultimo_registro_fica_no_instante_dele_e_avisa_acima_de_1_s(
        self, monkeypatch: pytest.MonkeyPatch, atras: timedelta, avisa: bool
    ) -> None:
        monkeypatch.setattr(contexto_de_envio, "_log", structlog.get_logger())
        saga = saga_em("aguardando_diagnostico")

        with capture_logs() as logs:
            instante = _contexto_com_relogio_em(AGORA - atras).instante_do_passo(
                saga, tipo="PedidoX"
            )

        assert instante == AGORA
        assert logs == (
            [
                {
                    "event": "saga clock behind last record",
                    "log_level": "warning",
                    "etapa": "aguardando_diagnostico",
                    "defasagem_segundos": atras.total_seconds(),
                    "correlation_id": str(saga.ordem_id),
                    "tipo": "PedidoX",
                }
            ]
            if avisa
            else []
        )

    def test_mais_de_60_s_atras_levanta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(contexto_de_envio, "_log", structlog.get_logger())
        saga = saga_em(EtapaSaga.AGUARDANDO_DIAGNOSTICO)
        atras = timedelta(seconds=60, milliseconds=1)

        with capture_logs() as logs, pytest.raises(RelogioDefasadoError, match="60"):
            _contexto_com_relogio_em(AGORA - atras).instante_do_passo(saga, tipo="x")

        assert [log["defasagem_segundos"] for log in logs] == [60.001]
