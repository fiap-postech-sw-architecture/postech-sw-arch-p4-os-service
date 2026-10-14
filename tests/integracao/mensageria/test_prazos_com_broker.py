"""O processo dos prazos com relay, consumidor, PostgreSQL e RabbitMQ reais.

Participantes falsos leem ``billing.comandos`` e ``execucao.comandos`` e respondem
(ou calam) como o Billing e a Execucao. O prazo de resposta e de 1 s e o ciclo do
``prazos`` roda a cada 0,2 s, de modo que as sete esperas da RFC-004 secao 4.6
cabem em segundos. O relogio e o real, o banco e o broker tambem.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
import structlog
from prometheus_client import CollectorRegistry
from sqlalchemy import text
from structlog.testing import capture_logs

from src.compartilhado.infraestrutura.mensageria.processo import Sinalizador
from src.compartilhado.infraestrutura.unit_of_work import SQLAlchemyUnitOfWork
from src.consumidor import montar_despachante
from src.ordem_servico.aplicacao.saga import vencer_prazos
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.vencer_prazos import VencerPrazos
from src.ordem_servico.aplicacao.use_cases import RetomarCompensacao
from src.ordem_servico.infraestrutura.metricas_da_saga import ColetorDaSaga
from src.ordem_servico.infraestrutura.presenca_do_consumidor import (
    PresencaPikaDoConsumidor,
)
from src.ordem_servico.infraestrutura.processo_dos_prazos import ProcessoDosPrazos
from src.ordem_servico.infraestrutura.rastreamento_da_saga import span_da_saga
from src.ordem_servico.infraestrutura.repository import SagaSQLAlchemyRepository
from src.ordem_servico.infraestrutura.transacao_dos_prazos import (
    TransacaoDosPrazosSQLAlchemy,
)
from src.ordem_servico.interfaces.dependencies import obter_cancelar_ordem
from tests.eventos import envelope_de_evento
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.mensageria.test_saga_com_broker import (
    _ATENDENTE,
    Atendimento,
    _amostra,
    _consumidor,
    _relay,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.broker import Broker
    from tests.rastreamento import Rastreador

PRAZO = timedelta(seconds=1)
INTERVALO_S = 0.2
MAXIMO = 5
_DLQ = "os.eventos.dlq"


def _processo_dos_prazos(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    pasta: Path,
    *,
    prazo: timedelta = PRAZO,
    maximo: int = MAXIMO,
) -> ProcessoDosPrazos:
    sinal = Sinalizador("prazos", pasta)
    presenca = PresencaPikaDoConsumidor(broker.parametros("os"), sinal)
    vencer = VencerPrazos(
        abrir=lambda: TransacaoDosPrazosSQLAlchemy(session_factory),
        presenca=presenca,
        contexto=ContextoDeEnvio(prazo, span=span_da_saga(rastreador.tracer)),
        max_reenvios=maximo,
    )
    return ProcessoDosPrazos(
        vencer=vencer, presenca=presenca, sinal=sinal, intervalo_s=INTERVALO_S
    )


def _consumidor_com_prazo(
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
    pasta: Path,
    prazo: timedelta,
) -> Any:
    return _consumidor(
        session_factory, broker, rastreador, pasta, montar_despachante(prazo)
    )


class Ordem:
    """Consultas ao banco sobre a saga de uma OS."""

    def __init__(self, atendimento: Atendimento) -> None:
        self.atendimento = atendimento

    @property
    def engine(self) -> Engine:
        return self.atendimento.engine

    def reenvios(self) -> int:
        return self.gatilhos().count("reenvio")

    def gatilhos(self) -> list[str]:
        return self.atendimento.gatilhos()

    def saga(self) -> dict[str, Any]:
        with self.engine.connect() as conexao:
            linha = conexao.execute(
                text(
                    "SELECT etapa, motivo, falha, reenvios, prazo_resposta_em, "
                    "plano_compensacao, comando_em_voo, passos "
                    "FROM sagas WHERE ordem_id = :id"
                ),
                {"id": self.atendimento.ordem_id},
            ).one()
        return dict(linha._mapping)

    def linhas(self, tipo: str) -> list[tuple[str, str]]:
        """(status, id) de cada linha da outbox com o tipo, na ordem."""
        with self.engine.connect() as conexao:
            return [
                (s, str(m))
                for s, m in conexao.execute(
                    text(
                        "SELECT status, mensagem_id FROM outbox "
                        "WHERE correlation_id = :id AND envelope ->> 'tipo' = :tipo "
                        "ORDER BY id"
                    ),
                    {"id": self.atendimento.ordem_id, "tipo": tipo},
                )
            ]


def _ate_gerar_orcamento(atendimento: Atendimento) -> None:
    """Do pedido de diagnostico ao comando ``GerarOrcamento`` na outbox."""
    atendimento.abrir()
    atendimento.receber("SolicitarDiagnostico")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoIniciado")
    atendimento.esperar_passo("DiagnosticoIniciado")
    atendimento.responder("SolicitarDiagnostico", "DiagnosticoConcluido")


def _comandos(broker: Broker, fila: str, tipo: str) -> list[dict[str, Any]]:
    """Os envelopes do ``tipo`` que estao na fila (esvazia a fila)."""
    return [
        e
        for _, corpo in broker.pegar_todas(fila)
        if (e := json.loads(corpo))["tipo"] == tipo
    ]


@pytest.fixture
def atendimento(
    engine: Engine,
    session_factory: sessionmaker[Session],
    broker: Broker,
    rastreador: Rastreador,
) -> Atendimento:
    return Atendimento(engine, session_factory, broker, rastreador)


class TestParticipanteMudo:
    def test_cinco_reenvios_com_id_novo_e_os_mesmos_dados_e_depois_a_compensacao(
        self,
        atendimento: Atendimento,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
        tmp_path: Path,
    ) -> None:
        reenvios = _amostra("pytstop_saga_reenvios_total", comando="GerarOrcamento")
        esgotados = _amostra(
            "pytstop_saga_prazos_esgotados_total", comando="GerarOrcamento"
        )
        compensacoes = _amostra(
            "pytstop_saga_compensacoes_total", motivo="prazo_tecnico"
        )
        relay = _relay(atendimento.engine, broker, rastreador, tmp_path)
        consumidor = _consumidor_com_prazo(
            session_factory, broker, rastreador, tmp_path, PRAZO
        )
        prazos = _processo_dos_prazos(session_factory, broker, rastreador, tmp_path)
        with EmSegundoPlano(relay), EmSegundoPlano(consumidor):
            esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
            with EmSegundoPlano(prazos):
                _ate_gerar_orcamento(atendimento)
                # O Billing nunca responde: o original e cinco reenvios chegam a fila.
                recebidos = [atendimento.receber("GerarOrcamento") for _ in range(6)]
                atendimento.receber("CancelarOrcamento")
                ordem = Ordem(atendimento)
                esperar_ate(lambda: ordem.saga()["etapa"] == "compensando")

                ids = [e["id"] for e in recebidos]
                assert len(set(ids)) == 6
                assert {json_dados(e) for e in recebidos} == {json_dados(recebidos[0])}
                assert {e["causation_id"] for e in recebidos[1:]} == {None}
                # A compensacao e a do passo em voo, na ordem da RFC-004 4.4.
                saga = ordem.saga()
                assert (saga["motivo"], saga["plano_compensacao"]) == (
                    "prazo_tecnico",
                    ["CancelarOrcamento", "DescartarDiagnostico"],
                )
                gatilhos = ordem.gatilhos()
                assert gatilhos.count("reenvio") == MAXIMO
                assert gatilhos.count("prazo_esgotado") == 1
                assert gatilhos[-1] == "prazo_esgotado"
                assert saga["reenvios"] == 0

                # A compensacao e respondida: Billing e Execucao desfazem.
                atendimento.responder("CancelarOrcamento", "OrcamentoCancelado")
                atendimento.receber("DescartarDiagnostico")
                atendimento.responder("DescartarDiagnostico", "DiagnosticoDescartado")
                esperar_ate(lambda: ordem.saga()["etapa"] == "compensada")

        assert (
            _amostra("pytstop_saga_reenvios_total", comando="GerarOrcamento")
            == reenvios + MAXIMO
        )
        assert (
            _amostra("pytstop_saga_prazos_esgotados_total", comando="GerarOrcamento")
            == esgotados + 1
        )
        assert (
            _amostra("pytstop_saga_compensacoes_total", motivo="prazo_tecnico")
            == compensacoes + 1
        )
        assert broker.contar(_DLQ) == 0
        with atendimento.engine.connect() as conexao:
            status = conexao.execute(
                text("SELECT status FROM ordens_de_servico")
            ).scalar_one()
        assert status == "cancelada"

    def test_duas_replicas_do_prazos_reenviam_cada_vez_uma_so(
        self,
        atendimento: Atendimento,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
        tmp_path: Path,
    ) -> None:
        relay = _relay(atendimento.engine, broker, rastreador, tmp_path)
        consumidor = _consumidor_com_prazo(
            session_factory, broker, rastreador, tmp_path, PRAZO
        )
        prazos = [
            _processo_dos_prazos(session_factory, broker, rastreador, tmp_path)
            for _ in range(2)
        ]
        with (
            EmSegundoPlano(relay),
            EmSegundoPlano(consumidor),
            EmSegundoPlano(prazos[0]),
            EmSegundoPlano(prazos[1]),
        ):
            esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
            _ate_gerar_orcamento(atendimento)
            ordem = Ordem(atendimento)
            esperar_ate(lambda: ordem.saga()["etapa"] == "compensando", prazo_s=40)

        gatilhos = ordem.gatilhos()
        assert (gatilhos.count("reenvio"), gatilhos.count("prazo_esgotado")) == (
            MAXIMO,
            1,
        )
        numeros = [p["reenvio"] for p in ordem.saga()["passos"] if "reenvio" in p]
        assert numeros == list(range(1, MAXIMO + 1))
        envios = ordem.linhas("GerarOrcamento")
        assert len(envios) == MAXIMO + 1
        assert len({i for _, i in envios}) == MAXIMO + 1
        assert len(_comandos(broker, "billing.comandos", "GerarOrcamento")) == (
            MAXIMO + 1
        )


class TestEstornoRecusadoEFalha:
    @pytest.mark.parametrize("responde_ao", ["reenvio-da-retomada", "original"])
    def test_estorno_recusado_para_a_saga_e_a_retomada_chega_a_compensada(
        self,
        atendimento: Atendimento,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
        tmp_path: Path,
        responde_ao: str,
    ) -> None:
        prazo = timedelta(seconds=3)
        relay = _relay(atendimento.engine, broker, rastreador, tmp_path)
        consumidor = _consumidor_com_prazo(
            session_factory, broker, rastreador, tmp_path, prazo
        )
        prazos = _processo_dos_prazos(
            session_factory, broker, rastreador, tmp_path, prazo=prazo
        )
        with EmSegundoPlano(relay), EmSegundoPlano(consumidor):
            esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
            with EmSegundoPlano(prazos):
                atendimento.abrir()
                atendimento.ate_aguardando_pagamento_com_checkout()
                with session_factory() as sess:
                    obter_cancelar_ordem(sess).executar(
                        atendimento.ordem_id, "Cliente desistiu", ator=_ATENDENTE
                    )
                original = atendimento.receber("EstornarPagamento")
                dados_do_original = atendimento.comandos["EstornarPagamento"]
                atendimento.responder("EstornarPagamento", "EstornoDePagamentoFalhou")
                ordem = Ordem(atendimento)
                esperar_ate(lambda: ordem.saga()["etapa"] == "falha_na_compensacao")

                # Parada: o prazo nao reenvia, mesmo depois de varios ciclos.
                time.sleep(2 * prazo.total_seconds())
                parada = ordem.saga()
                assert (parada["etapa"], parada["falha"]) == (
                    "falha_na_compensacao",
                    "estorno_recusado",
                )
                assert parada["prazo_resposta_em"] is None
                assert broker.contar("billing.comandos") == 0
                assert ordem.gatilhos().count("reenvio") == 0

                # O operador resolveu a causa e retoma (admin).
                with session_factory() as sess:
                    retomar = RetomarCompensacao(
                        SQLAlchemyUnitOfWork(lambda: sess),
                        SagaSQLAlchemyRepository(sess),
                        ContextoDeEnvio(prazo, span=span_da_saga(rastreador.tracer)),
                    )
                    dto = retomar.executar(atendimento.ordem_id, ator=_ATENDENTE)
                assert (dto.etapa, dto.falha, dto.reenvios) == ("compensando", None, 0)
                retomado = atendimento.receber("EstornarPagamento")
                assert retomado["id"] != original["id"]
                assert json_dados(retomado) == json_dados(original)
                if responde_ao == "original":
                    # A resposta tardia ao original ainda casa depois da retomada.
                    atendimento.comandos["EstornarPagamento"] = dados_do_original
                atendimento.responder("EstornarPagamento", "PagamentoEstornado")
                atendimento.receber("LiberarReserva")
                atendimento.responder("LiberarReserva", "ReservaLiberada")
                atendimento.receber("CancelarOrcamento")
                atendimento.responder("CancelarOrcamento", "OrcamentoCancelado")
                atendimento.receber("DescartarDiagnostico")
                atendimento.responder("DescartarDiagnostico", "DiagnosticoDescartado")
                esperar_ate(lambda: ordem.saga()["etapa"] == "compensada", prazo_s=30)

        passos = ordem.saga()["passos"]
        retomada = next(p for p in passos if p["gatilho"] == "retomada")
        assert (retomada["ator"], retomada["comando"]) == (
            _ATENDENTE,
            "EstornarPagamento",
        )
        assert retomada["comando_id"] == retomado["id"]
        assert broker.contar(_DLQ) == 0


class TestSemConsumidorEComRelayParado:
    def test_sem_consumidor_em_os_eventos_nao_reenvia_nem_esgota_e_o_gauge_acusa(
        self,
        monkeypatch: pytest.MonkeyPatch,
        atendimento: Atendimento,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
        tmp_path: Path,
    ) -> None:
        # O logger do modulo, ja em cache, escaparia do `capture_logs`.
        monkeypatch.setattr(vencer_prazos, "_log", structlog.get_logger())
        relay = _relay(atendimento.engine, broker, rastreador, tmp_path)
        prazos = _processo_dos_prazos(session_factory, broker, rastreador, tmp_path)
        ordem = Ordem(atendimento)
        with EmSegundoPlano(relay), EmSegundoPlano(prazos):
            with EmSegundoPlano(
                _consumidor_com_prazo(
                    session_factory, broker, rastreador, tmp_path, PRAZO
                )
            ):
                esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
                _ate_gerar_orcamento(atendimento)
                # O comando saiu da outbox (entregue) e o consumidor sai do ar antes
                # de qualquer resposta: nada responde, mas a fila esta sem consumidor.
                esperar_ate(
                    lambda: (
                        [s for s, _ in ordem.linhas("GerarOrcamento")] == ["entregue"]
                    )
                )
            with capture_logs() as logs:
                time.sleep(5 * PRAZO.total_seconds())
                pausado = [e for e in logs if "paused" in e["event"]]
                coletor = CollectorRegistry()
                coletor.register(
                    ColetorDaSaga(atendimento.engine.connect, lambda: PRAZO)
                )
                gauge = coletor.get_sample_value("pytstop_saga_prazo_vencido_segundos")

            saga = ordem.saga()
            assert (saga["etapa"], saga["reenvios"]) == ("aguardando_orcamento", 0)
            assert len(ordem.linhas("GerarOrcamento")) == 1
            assert pausado
            # O alerta de saga parada ve o prazo vencido sem o consumidor.
            assert gauge is not None
            assert gauge >= 2

            # O consumidor volta: a saga segue de onde estava e o prazo e tratado.
            with EmSegundoPlano(
                _consumidor_com_prazo(
                    session_factory, broker, rastreador, tmp_path, PRAZO
                )
            ):
                esperar_ate(lambda: ordem.reenvios() >= 1)
                assert len(_comandos(broker, "billing.comandos", "GerarOrcamento")) >= 2

    def test_linha_pendente_na_outbox_nao_gasta_reenvio_enquanto_o_relay_esta_parado(
        self,
        atendimento: Atendimento,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
        tmp_path: Path,
    ) -> None:
        consumidor = _consumidor_com_prazo(
            session_factory, broker, rastreador, tmp_path, PRAZO
        )
        prazos = _processo_dos_prazos(session_factory, broker, rastreador, tmp_path)
        ordem = Ordem(atendimento)
        with EmSegundoPlano(consumidor), EmSegundoPlano(prazos):
            esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
            ordem_id = atendimento.abrir()
            (diagnostico,) = [i for _, i in ordem.linhas("SolicitarDiagnostico")]
            # A Execucao responde sem o relay: os eventos entram direto no broker.
            for tipo in ("DiagnosticoIniciado", "DiagnosticoConcluido"):
                broker.publicar_evento(
                    envelope_de_evento(
                        tipo, correlation_id=ordem_id, causation_id=diagnostico
                    )
                )
                atendimento.esperar_passo(tipo)

            time.sleep(5 * PRAZO.total_seconds())

            # O GerarOrcamento espera na outbox (o relay esta parado): o prazo nao
            # corre, e o gauge de prazo vencido nao o conta.
            assert [s for s, _ in ordem.linhas("GerarOrcamento")] == ["pendente"]
            assert ordem.reenvios() == 0
            coletor = CollectorRegistry()
            coletor.register(ColetorDaSaga(atendimento.engine.connect, lambda: PRAZO))
            assert coletor.get_sample_value("pytstop_saga_prazo_vencido_segundos") == 0

            # O relay volta, entrega, e o prazo passa a correr dai.
            with EmSegundoPlano(
                _relay(atendimento.engine, broker, rastreador, tmp_path)
            ):
                esperar_ate(lambda: ordem.reenvios() >= 1)
                assert ordem.linhas("GerarOrcamento")[0][0] == "entregue"


class TestDoisConsumidoresEOPrazos:
    def test_resposta_tardia_ao_original_e_repetida_ao_reenvio_deixam_a_saga_valida(
        self,
        atendimento: Atendimento,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
        tmp_path: Path,
    ) -> None:
        relay = _relay(atendimento.engine, broker, rastreador, tmp_path)
        consumidores = [
            _consumidor_com_prazo(session_factory, broker, rastreador, tmp_path, PRAZO)
            for _ in range(2)
        ]
        prazos = _processo_dos_prazos(session_factory, broker, rastreador, tmp_path)
        ordem = Ordem(atendimento)
        with (
            EmSegundoPlano(relay),
            EmSegundoPlano(consumidores[0]),
            EmSegundoPlano(consumidores[1]),
            EmSegundoPlano(prazos),
        ):
            esperar_ate(lambda: (tmp_path / "consumidor-pronto").exists())
            _ate_gerar_orcamento(atendimento)
            original = atendimento.receber("GerarOrcamento")
            dados_do_original = atendimento.comandos["GerarOrcamento"]
            reenvio = atendimento.receber("GerarOrcamento")
            assert reenvio["id"] != original["id"]
            ignoradas = _amostra(
                "pytstop_mensagens_consumidas_total",
                tipo="OrcamentoGerado",
                resultado="ignorada",
            )

            # O Billing responde ao original, atrasado, e republica o desfecho ao
            # reenvio: a etapa avanca uma vez, e a repeticao e ignorada.
            atendimento.comandos["GerarOrcamento"] = dados_do_original
            atendimento.responder("GerarOrcamento", "OrcamentoGerado")
            atendimento.esperar_passo("OrcamentoGerado")
            atendimento.comandos["GerarOrcamento"] = (dados_do_original[0], reenvio)
            atendimento.responder("GerarOrcamento", "OrcamentoGerado")
            esperar_ate(
                lambda: (
                    _amostra(
                        "pytstop_mensagens_consumidas_total",
                        tipo="OrcamentoGerado",
                        resultado="ignorada",
                    )
                    > ignoradas
                )
            )

        saga = ordem.saga()
        assert saga["etapa"] == "aguardando_decisao"
        assert saga["prazo_resposta_em"] is None
        assert saga["comando_em_voo"] is None
        assert ordem.gatilhos().count("OrcamentoGerado") == 1
        assert broker.contar(_DLQ) == 0


class TestPresencaDoConsumidor:
    def test_conta_o_consumidor_de_os_eventos_e_marca_o_processo_pronto(
        self,
        session_factory: sessionmaker[Session],
        broker: Broker,
        rastreador: Rastreador,
        tmp_path: Path,
    ) -> None:
        sinal = Sinalizador("prazos", tmp_path)
        presenca = PresencaPikaDoConsumidor(broker.parametros("os"), sinal)
        consumidor = _consumidor_com_prazo(
            session_factory, broker, rastreador, tmp_path, PRAZO
        )
        try:
            assert presenca.ha_consumidor() is False
            # Conectado ao broker, de pe; sem consumidor, mas pronto: broker fora e
            # que tira o processo de pronto.
            assert sinal.pronto.exists()
            with EmSegundoPlano(consumidor):
                esperar_ate(presenca.ha_consumidor)
            esperar_ate(lambda: presenca.ha_consumidor() is False)
        finally:
            presenca.fechar()
        assert not sinal.pronto.exists()

    def test_broker_fora_do_ar_nao_levanta_e_o_processo_fica_fora_de_pronto(
        self, tmp_path: Path
    ) -> None:
        from src.compartilhado.infraestrutura.mensageria import amqp

        sinal = Sinalizador("prazos", tmp_path)
        # Porta sem servidor: a conexao e recusada.
        parametros = amqp.parametros("amqp://os:x@127.0.0.1:1/%2F", "teste")
        presenca = PresencaPikaDoConsumidor(parametros, sinal)

        assert presenca.ha_consumidor() is False
        assert presenca.ha_consumidor() is False
        assert not sinal.pronto.exists()
        # O laco do processo toca o heartbeat mesmo assim.
        presenca.manter()
        presenca.fechar()

    def test_a_queda_da_conexao_no_meio_do_ciclo_vira_false_e_reconecta(
        self, broker: Broker, tmp_path: Path
    ) -> None:
        sinal = Sinalizador("prazos", tmp_path)
        presenca = PresencaPikaDoConsumidor(broker.parametros("os"), sinal)
        try:
            assert presenca.ha_consumidor() is False
            presenca._broker.conexao.close()  # a conexao cai por baixo do adaptador

            # A queda vira False, sem levantar, e tira o processo de pronto...
            assert presenca.ha_consumidor() is False
            assert not sinal.pronto.exists()
            # ...e o ciclo seguinte reconecta e volta a ficar pronto.
            assert presenca.ha_consumidor() is False
            assert sinal.pronto.exists()
        finally:
            presenca.fechar()


def json_dados(envelope: dict[str, Any]) -> str:
    """Os ``dados`` do envelope, em texto estavel, para comparar entre envios."""
    return json.dumps(envelope["dados"], sort_keys=True)
