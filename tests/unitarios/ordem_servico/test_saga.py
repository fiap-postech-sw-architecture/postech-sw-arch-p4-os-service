"""Instancia da saga: abertura, a tabela da RFC-004 secao 4.1 no agregado e a
classificacao dos eventos.

Cada linha da tabela (etapa seguinte, comando, dados, prazo e passo concluido)
e aplicada e recusada no proprio agregado, alem do handler. A matriz da
classificacao (12 etapas x 10 perfis da OS x 23 tipos) e gerada do oraculo do
``cenario_da_saga``, escrito a partir da RFC e nao importado do modulo testado.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.compartilhado.aplicacao.mensageria import Comando, MensagemRecebida
from src.ordem_servico.aplicacao.saga.modelo import (
    Envio,
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    SagaIniciadaEvent,
    TransicaoDaSagaInvalidaError,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    COMANDOS_COM_PRAZO,
    Classificacao,
)
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE, ATOR_PROCESSO, ordem_em
from tests.unitarios.ordem_servico.cenario_da_saga import (
    ESPERADA,
    PERFIS,
    STATUS_DE_ENTRADA,
    esperado,
    ordem_no_perfil,
)

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)
DEPOIS = AGORA + timedelta(minutes=10)
PRAZO = DEPOIS + timedelta(minutes=2)
C = Classificacao
E = EtapaSaga
S = StatusOrdem
# Itens do DiagnosticoConcluido de exemplo do platform (um servico e uma peca).
_ITENS = itens_do_diagnostico(evento("DiagnosticoConcluido", uuid4()).dados)
_PECAS = [
    {"sku": i["codigo"], "quantidade": i["quantidade"]}
    for i in _ITENS
    if i["tipo"] == "peca"
]


def saga_em(etapa: EtapaSaga | str, **campos: Any) -> Saga:
    """Saga como a persistencia a reidrata, ja na ``etapa``.

    Nas etapas da compensacao, com motivo e plano (o invariante delas).
    """
    if EtapaSaga(etapa) is E.COMPENSANDO:
        campos = {
            "_motivo": "cancelamento",
            "_plano_compensacao": ["DescartarDiagnostico"],
            **campos,
        }
    return Saga(
        **{
            "id": uuid4(),
            "_etapa": EtapaSaga(etapa),
            "_iniciada_em": AGORA,
            "_etapa_desde": AGORA,
            "_atualizada_em": AGORA,
            **campos,
        }
    )


def _marcos(*, diagnostico: bool = True, checkout: bool = False) -> MarcosDaOrdem:
    return MarcosDaOrdem(
        diagnostico_iniciado=diagnostico, checkout_aberto=checkout, encerrada=False
    )


def _dados(comando: Comando, saga: Saga) -> dict[str, Any]:
    """Os ``dados`` certos do comando para a ``saga`` (RFC-004 secao 4.1)."""
    ordem_id = str(saga.ordem_id)
    return {
        Comando.GERAR_ORCAMENTO: {"ordem_id": ordem_id, "itens": _ITENS},
        Comando.RESERVAR_PECAS: {"ordem_id": ordem_id, "pecas": _PECAS},
        Comando.SOLICITAR_PAGAMENTO: {
            "ordem_id": ordem_id,
            "orcamento_id": str(uuid4()),
        },
        Comando.AGENDAR_EXECUCAO: {"ordem_id": ordem_id, "prioridade": "normal"},
    }[comando]


def _envio(comando: Comando, saga: Saga, **campos: Any) -> Envio:
    return Envio(
        **{
            "tipo": comando,
            "id": uuid4(),
            "dados": _dados(comando, saga),
            "prazo_resposta_em": PRAZO,
            **campos,
        }
    )


def _em_voo(ordem_id: UUID) -> dict[str, Any]:
    """Comando em voo de antes do evento, ja reenviado duas vezes."""
    return {
        "_comando_em_voo": {
            "tipo": "GerarOrcamento",
            "dados": {"ordem_id": str(ordem_id)},
            "mensagem_ids": [str(uuid4())],
            "enviado_em": AGORA.isoformat(),
        },
        "_prazo_resposta_em": AGORA + timedelta(minutes=2),
        "_reenvios": 2,
    }


def _foto(saga: Saga) -> tuple[object, ...]:
    """Estado observavel completo, para provar que nada mudou."""
    return (
        saga.etapa,
        saga.etapa_desde,
        saga.atualizada_em,
        saga.passos,
        saga.passos_concluidos,
        saga.comando_em_voo,
        saga.prazo_resposta_em,
        saga.reenvios,
        saga.itens,
        tuple(saga.coletar_eventos()),
    )


class TestIniciar:
    def test_abre_em_aguardando_diagnostico_com_o_passo_de_abertura(self) -> None:
        ordem_id = uuid4()
        envio = Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=uuid4())

        saga = Saga.iniciar(ordem_id, envio=envio, ator=ATOR_ATENDENTE, agora=AGORA)

        assert (saga.ordem_id, saga.etapa) == (ordem_id, E.AGUARDANDO_DIAGNOSTICO)
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
        assert not saga.encerrada
        assert saga.coletar_eventos() == [
            SagaIniciadaEvent(agregado_id=ordem_id, ocorrido_em=AGORA)
        ]

    @pytest.mark.parametrize(
        ("envio", "agora"),
        [
            pytest.param(
                Envio(tipo=Comando.GERAR_ORCAMENTO, id=uuid4()), AGORA, id="outro"
            ),
            pytest.param(
                Envio(
                    tipo=Comando.SOLICITAR_DIAGNOSTICO,
                    id=uuid4(),
                    prazo_resposta_em=PRAZO,
                ),
                AGORA,
                id="com-prazo",
            ),
            pytest.param(
                Envio(tipo=Comando.SOLICITAR_DIAGNOSTICO, id=uuid4()),
                datetime(2026, 10, 7, 12),  # sem fuso de proposito
                id="sem-fuso",
            ),
        ],
    )
    def test_abertura_so_com_o_solicitar_diagnostico_sem_prazo(
        self, envio: Envio, agora: datetime
    ) -> None:
        with pytest.raises(TransicaoDaSagaInvalidaError):
            Saga.iniciar(uuid4(), envio=envio, ator=ATOR_ATENDENTE, agora=agora)

    def test_colecoes_devolvidas_sao_copias(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        saga.avancar(
            evento("DiagnosticoConcluido", saga.ordem_id),
            _marcos(),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
            envio=_envio(Comando.GERAR_ORCAMENTO, saga),
        )

        saga.passos[0]["ator"] = "outro"
        saga.itens[0]["quantidade"] = 999
        saga.pecas.clear()
        em_voo = saga.comando_em_voo
        assert em_voo is not None
        em_voo["mensagem_ids"].append("outro")

        assert saga.passos[0]["ator"] == ATOR_PROCESSO
        assert saga.itens[0]["quantidade"] == _ITENS[0]["quantidade"]
        assert saga.pecas == _PECAS
        assert saga.comando_em_voo is not None
        assert len(saga.comando_em_voo["mensagem_ids"]) == 1

    def test_repr_nao_expoe_passos_nem_comando(self) -> None:
        saga = saga_em(
            "aguardando_orcamento",
            _comando_em_voo={
                "tipo": "GerarOrcamento",
                "dados": {"segredo": "nao-aparece"},
                "mensagem_ids": [],
                "enviado_em": "x",
            },
            _prazo_resposta_em=AGORA,
        )
        assert "nao-aparece" not in repr(saga)


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
        with pytest.raises(ValueError, match=r"prazo|reenvios"):
            saga_em("aguardando_orcamento", **campos)

    @pytest.mark.parametrize(
        "campos",
        [
            pytest.param({"_motivo": None}, id="sem-motivo"),
            pytest.param({"_plano_compensacao": []}, id="sem-plano"),
        ],
    )
    def test_compensando_exige_motivo_e_plano(self, campos: dict[str, Any]) -> None:
        with pytest.raises(ValueError, match="compensando"):
            saga_em("compensando", **campos)


# (evento, etapa antes, comando enviado, etapa nova, passo concluido)
LINHAS = [
    pytest.param(
        "DiagnosticoIniciado",
        E.AGUARDANDO_DIAGNOSTICO,
        None,
        E.AGUARDANDO_DIAGNOSTICO,
        None,
        id="DiagnosticoIniciado",
    ),
    pytest.param(
        "DiagnosticoConcluido",
        E.AGUARDANDO_DIAGNOSTICO,
        Comando.GERAR_ORCAMENTO,
        E.AGUARDANDO_ORCAMENTO,
        None,
        id="DiagnosticoConcluido",
    ),
    pytest.param(
        "OrcamentoGerado",
        E.AGUARDANDO_ORCAMENTO,
        None,
        E.AGUARDANDO_DECISAO,
        "T3",
        id="OrcamentoGerado",
    ),
    pytest.param(
        "OrcamentoAprovado",
        E.AGUARDANDO_DECISAO,
        Comando.RESERVAR_PECAS,
        E.AGUARDANDO_RESERVA,
        None,
        id="OrcamentoAprovado",
    ),
    pytest.param(
        "PecasReservadas",
        E.AGUARDANDO_RESERVA,
        Comando.SOLICITAR_PAGAMENTO,
        E.AGUARDANDO_PAGAMENTO,
        "T5",
        id="PecasReservadas",
    ),
    pytest.param(
        "PagamentoSolicitado",
        E.AGUARDANDO_PAGAMENTO,
        None,
        E.AGUARDANDO_PAGAMENTO,
        "T6",
        id="PagamentoSolicitado",
    ),
    pytest.param(
        "PagamentoConfirmado",
        E.AGUARDANDO_PAGAMENTO,
        Comando.AGENDAR_EXECUCAO,
        E.AGUARDANDO_AGENDAMENTO,
        None,
        id="PagamentoConfirmado",
    ),
    pytest.param(
        "ExecucaoAgendada",
        E.AGUARDANDO_AGENDAMENTO,
        None,
        E.AGUARDANDO_INICIO,
        "T7",
        id="ExecucaoAgendada",
    ),
    pytest.param(
        "ExecucaoIniciada",
        E.AGUARDANDO_INICIO,
        None,
        E.EM_EXECUCAO,
        None,
        id="ExecucaoIniciada",
    ),
    pytest.param(
        "ExecucaoFinalizada", E.EM_EXECUCAO, None, E.CONCLUIDA, None, id="Finalizada"
    ),
]


def _marcos_da_linha(tipo: str) -> MarcosDaOrdem:
    """Os marcos com que o evento se classifica para processar na etapa dele."""
    return _marcos(
        diagnostico=tipo != "DiagnosticoIniciado",
        checkout=tipo not in {"DiagnosticoIniciado", "PagamentoSolicitado"}
        and tipo
        not in {
            "DiagnosticoConcluido",
            "OrcamentoGerado",
            "OrcamentoAprovado",
            "PecasReservadas",
        },
    )


class TestAvancar:
    @pytest.mark.parametrize(("tipo", "antes", "comando", "nova", "concluido"), LINHAS)
    def test_cada_linha_da_tabela_no_agregado(
        self,
        tipo: str,
        antes: EtapaSaga,
        comando: Comando | None,
        nova: EtapaSaga,
        concluido: str | None,
    ) -> None:
        saga = saga_em(antes, _itens=list(_ITENS), _passos_concluidos=["T1"])
        saga = saga_em(antes, id=saga.ordem_id, _itens=list(_ITENS), **_em_voo(saga))
        recebido = evento(tipo, saga.ordem_id)
        envio = _envio(comando, saga) if comando else None

        saga.avancar(
            recebido,
            _marcos_da_linha(tipo),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
            envio=envio,
        )

        assert saga.etapa is nova
        assert saga.passos_concluidos == ((concluido,) if concluido else ())
        (passo,) = saga.passos
        assert {k: v for k, v in passo.items() if k != "posicao_na_fila"} == {
            "seq": 1,
            "em": DEPOIS.isoformat(),
            "de": antes.value,
            "para": nova.value,
            "gatilho": tipo,
            "mensagem_id": str(recebido.id),
            "comando": comando.value if comando else None,
            "comando_id": str(envio.id) if envio else None,
            "motivo": None,
            "ator": ATOR_PROCESSO,
        }
        assert saga.atualizada_em == DEPOIS
        mudou = nova is not antes
        assert saga.etapa_desde == (DEPOIS if mudou else AGORA)
        assert saga.coletar_eventos() == (
            [
                EtapaDaSagaAlteradaEvent(
                    agregado_id=saga.ordem_id,
                    etapa_anterior=antes,
                    etapa_nova=nova,
                    permanencia=DEPOIS - AGORA,
                    ocorrido_em=DEPOIS,
                )
            ]
            if mudou
            else []
        )
        # Comando com resposta automatica: em voo, com prazo e reenvios
        # zerados; sem comando, nada fica em voo (RFC-004 secao 4.6).
        if envio is None:
            assert (saga.comando_em_voo, saga.prazo_resposta_em) == (None, None)
        else:
            assert saga.comando_em_voo == {
                "tipo": envio.tipo.value,
                "dados": dict(envio.dados),
                "mensagem_ids": [str(envio.id)],
                "enviado_em": DEPOIS.isoformat(),
            }
            assert saga.prazo_resposta_em == PRAZO
        assert saga.reenvios == 0

    def test_diagnostico_concluido_guarda_so_os_campos_do_contrato(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        itens = [
            {"tipo": "peca", "codigo": "PEC-1", "quantidade": 2, "extra": "ignorado"}
        ]
        guardados = [{"tipo": "peca", "codigo": "PEC-1", "quantidade": 2}]

        saga.avancar(
            evento("DiagnosticoConcluido", saga.ordem_id, itens=itens),
            _marcos(),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
            envio=_envio(
                Comando.GERAR_ORCAMENTO,
                saga,
                dados={"ordem_id": str(saga.ordem_id), "itens": guardados},
            ),
        )

        assert saga.itens == tuple(guardados)
        assert saga.pecas == [{"sku": "PEC-1", "quantidade": 2}]

    def test_execucao_agendada_guarda_a_posicao_so_no_passo(self) -> None:
        saga = saga_em("aguardando_agendamento")

        saga.avancar(
            evento("ExecucaoAgendada", saga.ordem_id, posicao_na_fila=7),
            _marcos(checkout=True),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
        )

        assert saga.passos[-1]["posicao_na_fila"] == 7

    def test_permanencia_e_contada_desde_a_entrada_na_etapa(self) -> None:
        # Duas mudancas: a segunda permanencia (15 min) e desde a entrada em
        # aguardando_orcamento, nao desde a abertura (25 min).
        saga = saga_em("aguardando_diagnostico")
        saga.avancar(
            evento("DiagnosticoConcluido", saga.ordem_id),
            _marcos(),
            agora=AGORA + timedelta(minutes=10),
            ator=ATOR_PROCESSO,
            envio=_envio(Comando.GERAR_ORCAMENTO, saga),
        )
        saga.limpar_eventos()

        saga.avancar(
            evento("OrcamentoGerado", saga.ordem_id),
            _marcos(),
            agora=AGORA + timedelta(minutes=25),
            ator=ATOR_PROCESSO,
        )

        (mudanca,) = saga.coletar_eventos()
        assert isinstance(mudanca, EtapaDaSagaAlteradaEvent)
        assert (mudanca.etapa_anterior, mudanca.permanencia) == (
            E.AGUARDANDO_ORCAMENTO,
            timedelta(minutes=15),
        )

    def test_dado_malformado_levanta_antes_de_mudar(self) -> None:
        # O schema do contrato barra o evento sem a posicao; quem chamar sem
        # ele encontra a saga intacta.
        saga = saga_em("aguardando_agendamento")
        antes = _foto(saga)
        sem_posicao = MensagemRecebida(
            id=uuid4(),
            tipo="ExecucaoAgendada",
            versao=1,
            origem="execution-service",
            correlation_id=saga.ordem_id,
            causation_id=None,
            ocorrido_em=DEPOIS,
            dados={"ordem_id": str(saga.ordem_id)},
        )

        with pytest.raises(KeyError):
            saga.avancar(
                sem_posicao, _marcos(checkout=True), agora=DEPOIS, ator=ATOR_PROCESSO
            )

        assert _foto(saga) == antes


def _recusa(**mudanca: Any) -> dict[str, Any]:
    return mudanca


# Cada conferencia do avancar, com o DiagnosticoConcluido como base (etapa
# aguardando_diagnostico, diagnostico iniciado, GerarOrcamento com prazo).
RECUSAS = [
    pytest.param(_recusa(correlation_id=uuid4()), id="evento-de-outra-ordem"),
    pytest.param(_recusa(tipo="ExecucaoIniciada"), id="adiantado-de-etapa"),
    pytest.param(_recusa(tipo="OrcamentoRecusado"), id="falha-de-negocio"),
    pytest.param(_recusa(marcos=_marcos(diagnostico=False)), id="adiantado-na-etapa"),
    pytest.param(
        _recusa(
            marcos=MarcosDaOrdem(
                diagnostico_iniciado=True, checkout_aberto=False, encerrada=True
            )
        ),
        id="os-encerrada",
    ),
    pytest.param(
        _recusa(agora=datetime(2026, 10, 7, 13)),
        id="instante-sem-fuso",
    ),
    pytest.param(_recusa(agora=AGORA - timedelta(seconds=1)), id="instante-que-volta"),
    pytest.param(_recusa(envio=None), id="sem-envio"),
    pytest.param(_recusa(comando=Comando.AGENDAR_EXECUCAO), id="envio-de-outro-tipo"),
    pytest.param(_recusa(dados={"ordem_id": str(uuid4())}), id="dados-de-outra-os"),
    pytest.param(_recusa(dados={"itens": []}), id="itens-que-nao-sao-os-do-evento"),
    pytest.param(_recusa(prazo_resposta_em=None), id="sem-prazo"),
    pytest.param(
        _recusa(prazo_resposta_em=datetime(2026, 10, 7, 13)),
        id="prazo-sem-fuso",
    ),
    pytest.param(_recusa(prazo_resposta_em=DEPOIS), id="prazo-no-envio"),
]


class TestRecusasDoAvancar:
    @pytest.mark.parametrize("mudanca", RECUSAS)
    def test_recusa_sem_mudar_nada(self, mudanca: dict[str, Any]) -> None:
        saga = saga_em("aguardando_diagnostico")
        antes = _foto(saga)
        tipo = mudanca.get("tipo", "DiagnosticoConcluido")
        recebido = evento(tipo, mudanca.get("correlation_id", saga.ordem_id))
        comando = mudanca.get("comando", Comando.GERAR_ORCAMENTO)
        dados = {**_dados(Comando.GERAR_ORCAMENTO, saga), **mudanca.get("dados", {})}
        envio = (
            mudanca["envio"]
            if "envio" in mudanca
            else Envio(
                tipo=comando,
                id=uuid4(),
                dados=dados,
                prazo_resposta_em=mudanca.get("prazo_resposta_em", PRAZO),
            )
        )

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.avancar(
                recebido,
                mudanca.get("marcos", _marcos()),
                agora=mudanca.get("agora", DEPOIS),
                ator=ATOR_PROCESSO,
                envio=envio,
            )

        assert _foto(saga) == antes

    @pytest.mark.parametrize(
        ("etapa", "tipo", "marcos"),
        [
            pytest.param(
                "aguardando_diagnostico",
                "DiagnosticoIniciado",
                _marcos(),
                id="diagnostico-repetido",
            ),
            pytest.param(
                "aguardando_pagamento",
                "PagamentoSolicitado",
                _marcos(checkout=True),
                id="pagamento-repetido",
            ),
            pytest.param(
                "aguardando_pagamento",
                "PagamentoConfirmado",
                _marcos(checkout=False),
                id="confirmado-sem-checkout",
            ),
            pytest.param(
                "compensando",
                "ReservaLiberada",
                _marcos(),
                id="resposta-de-compensacao",
            ),
        ],
    )
    def test_evento_que_nao_se_classifica_para_processar_e_recusado(
        self, etapa: str, tipo: str, marcos: MarcosDaOrdem
    ) -> None:
        saga = saga_em(etapa)
        antes = _foto(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError, match=tipo):
            saga.avancar(
                evento(tipo, saga.ordem_id), marcos, agora=DEPOIS, ator=ATOR_PROCESSO
            )

        assert _foto(saga) == antes

    def test_envio_onde_a_linha_nao_envia_comando_e_recusado(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        antes = _foto(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError, match="nenhum comando"):
            saga.avancar(
                evento("DiagnosticoIniciado", saga.ordem_id),
                _marcos(diagnostico=False),
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
                envio=_envio(Comando.GERAR_ORCAMENTO, saga),
            )

        assert _foto(saga) == antes

    def test_pecas_que_nao_sao_as_dos_itens_sao_recusadas(self) -> None:
        saga = saga_em("aguardando_decisao", _itens=list(_ITENS))
        servicos = [
            {"sku": i["codigo"], "quantidade": 1}
            for i in _ITENS
            if i["tipo"] == "servico"
        ]

        with pytest.raises(TransicaoDaSagaInvalidaError, match="dados"):
            saga.avancar(
                evento("OrcamentoAprovado", saga.ordem_id),
                _marcos(),
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
                envio=_envio(
                    Comando.RESERVAR_PECAS,
                    saga,
                    dados={"ordem_id": str(saga.ordem_id), "pecas": servicos},
                ),
            )


class TestContextoDeTrace:
    def test_guarda_o_traceparent_w3c(self) -> None:
        saga = saga_em("aguardando_diagnostico")
        valido = f"00-{'a' * 32}-{'b' * 16}-01"

        saga.registrar_contexto_de_trace(valido)

        assert saga.traceparent == valido

    @pytest.mark.parametrize(
        "invalido",
        [
            pytest.param("x" * 500, id="longo"),
            pytest.param(f"01-{'a' * 32}-{'b' * 16}-01", id="outra-versao"),
            pytest.param(f"00-{'A' * 32}-{'b' * 16}-01", id="maiusculas"),
            pytest.param(f"00-{'a' * 32}-{'b' * 16}-01\n", id="quebra-de-linha"),
        ],
    )
    def test_recusa_o_que_nao_e_traceparent_sem_mudar(self, invalido: str) -> None:
        saga = saga_em("aguardando_diagnostico")

        with pytest.raises(ValueError, match="traceparent"):
            saga.registrar_contexto_de_trace(invalido)

        assert saga.traceparent is None


class TestEnvio:
    def test_dados_sao_uma_copia_so_de_leitura(self) -> None:
        dados = {"ordem_id": "x", "itens": [{"codigo": "A"}]}
        envio = Envio(tipo=Comando.GERAR_ORCAMENTO, id=uuid4(), dados=dados)

        dados["ordem_id"] = "outro"
        dados["itens"][0]["codigo"] = "B"

        assert envio.dados == {"ordem_id": "x", "itens": [{"codigo": "A"}]}
        with pytest.raises(TypeError):
            envio.dados["ordem_id"] = "outro"  # type: ignore[index]
        with pytest.raises(FrozenInstanceError):
            envio.tipo = Comando.RESERVAR_PECAS  # type: ignore[misc]

    def test_hash_ignora_os_dados(self) -> None:
        envio_id = uuid4()
        um = Envio(tipo=Comando.GERAR_ORCAMENTO, id=envio_id, dados={"a": 1})
        outro = Envio(tipo=Comando.GERAR_ORCAMENTO, id=envio_id, dados={"a": 2})

        assert hash(um) == hash(outro)
        assert um != outro


def test_comandos_com_prazo_sao_os_nove_com_resposta_automatica() -> None:
    # RFC-004 secoes 4.3 e 4.6: o SolicitarDiagnostico e o AnonimizarVeiculo
    # nao tem resposta automatica.
    assert {c.value for c in COMANDOS_COM_PRAZO} == {
        "GerarOrcamento",
        "ReservarPecas",
        "SolicitarPagamento",
        "AgendarExecucao",
        "CancelarExecucao",
        "EstornarPagamento",
        "LiberarReserva",
        "CancelarOrcamento",
        "DescartarDiagnostico",
    }


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
