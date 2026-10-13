"""Instancia da saga: abertura, as tabelas da RFC-004 secoes 4.1 e 4.4 no
agregado e a classificacao dos eventos.

Cada linha da tabela (etapa seguinte, comando, dados, prazo e passo concluido),
o inicio da compensacao por falha de negocio e pelo cancelamento, a resposta
de cada compensacao e a corrida do pivot sao aplicados e recusados no proprio
agregado, alem do handler. As matrizes da classificacao (12 etapas x 10 perfis
da OS x 23 tipos, e cada compensacao pendente x cada resposta, casada ou nao)
sao geradas do oraculo do ``cenario_da_saga``, escrito a partir da RFC e nao
importado do modulo testado.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

import pytest

from src.compartilhado.aplicacao.mensageria import Comando, MensagemRecebida
from src.ordem_servico.aplicacao.saga import transicoes
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.modelo import (
    CompensacaoIniciadaEvent,
    Envio,
    EtapaDaSagaAlteradaEvent,
    EtapaSaga,
    Gatilho,
    GatilhoDeProcesso,
    MotivoCompensacao,
    SagaIniciadaEvent,
    Transicao,
    TransicaoDaSagaInvalidaError,
    itens_do_diagnostico,
    pecas_dos_itens,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    COMANDOS_COM_PRAZO,
    GATILHOS_VALIDOS,
)
from src.ordem_servico.aplicacao.saga.transicoes import conferir_gatilho
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.fabricas import ATOR_ATENDENTE, ATOR_PROCESSO, ordem_em
from tests.unitarios.ordem_servico.cenario_da_saga import (
    COMPENSACOES,
    ESPERADA,
    FEITO,
    PERFIS,
    RESPONDE,
    STATUS_DE_ENTRADA,
    esperado,
    ordem_no_perfil,
)

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)
DEPOIS = AGORA + timedelta(minutes=10)
PRAZO = DEPOIS + timedelta(minutes=2)
C = Classificacao
E = EtapaSaga
if TYPE_CHECKING:
    from collections.abc import Callable

S = StatusOrdem
# Itens do DiagnosticoConcluido de exemplo do platform (um servico e uma peca).
_ITENS = itens_do_diagnostico(evento("DiagnosticoConcluido", uuid4()).dados)
_PECAS = [
    {"sku": i["codigo"], "quantidade": i["quantidade"]}
    for i in _ITENS
    if i["tipo"] == "peca"
]
# Orcamento que a OS guarda nos testes do agregado (o do OrcamentoGerado).
_ORCAMENTO_ID = uuid4()
_PAGAMENTO_ID = uuid4()


def saga_em(etapa: EtapaSaga | str, **campos: Any) -> Saga:
    """Saga como a persistencia a reidrata, ja na ``etapa``.

    Nas etapas da compensacao, com motivo e plano, e na falha dela, com a
    falha (os invariantes delas).
    """
    if EtapaSaga(etapa) in {E.COMPENSANDO, E.FALHA_NA_COMPENSACAO}:
        campos = {
            "_motivo": "cancelamento",
            "_plano_compensacao": ["DescartarDiagnostico"],
            **campos,
        }
    if EtapaSaga(etapa) is E.FALHA_NA_COMPENSACAO:
        # A falha guarda a pendente em voo, sem prazo (RFC-004 secao 4.7).
        pendente = (campos["_plano_compensacao"] or ["DescartarDiagnostico"])[0]
        em_voo = {
            "tipo": pendente,
            "dados": {"ordem_id": str(uuid4()), "motivo": campos["_motivo"]},
            "mensagem_ids": [str(uuid4())],
            "enviado_em": AGORA.isoformat(),
        }
        campos = {"_falha": "estorno_recusado", "_comando_em_voo": em_voo, **campos}
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


def _marcos(
    *,
    diagnostico: bool = True,
    checkout: bool = False,
    orcamento_id: UUID | None = _ORCAMENTO_ID,
) -> MarcosDaOrdem:
    return MarcosDaOrdem(
        diagnostico_iniciado=diagnostico,
        checkout_aberto=checkout,
        encerrada=False,
        orcamento_id=orcamento_id,
        pagamento_id=_PAGAMENTO_ID if checkout else None,
    )


def _dados(comando: Comando, saga: Saga) -> dict[str, Any]:
    """Os ``dados`` certos do comando para a ``saga`` (RFC-004 secao 4.1)."""
    ordem_id = str(saga.ordem_id)
    return {
        Comando.GERAR_ORCAMENTO: {"ordem_id": ordem_id, "itens": _ITENS},
        Comando.RESERVAR_PECAS: {"ordem_id": ordem_id, "pecas": _PECAS},
        Comando.SOLICITAR_PAGAMENTO: {
            "ordem_id": ordem_id,
            "orcamento_id": str(_ORCAMENTO_ID),
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
        em_voo = saga.comando_em_voo
        assert em_voo is not None
        em_voo["mensagem_ids"].append("outro")

        assert saga.passos[0]["ator"] == ATOR_PROCESSO
        assert saga.itens[0]["quantidade"] == _ITENS[0]["quantidade"]
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
            pytest.param({"_comando_em_voo": None}, id="sem-comando-em-voo"),
            pytest.param({"_prazo_resposta_em": AGORA}, id="com-prazo"),
        ],
    )
    def test_falha_na_compensacao_guarda_o_comando_em_voo_sem_prazo(
        self, campos: dict[str, Any]
    ) -> None:
        # RFC-004 secao 4.7: a retomada reenvia o comando guardado, e o `prazos`
        # nao age sobre a saga parada.
        with pytest.raises(ValueError, match="guarda o comando em voo, sem prazo"):
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
            _marcos(),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
            envio=_envio(Comando.GERAR_ORCAMENTO, saga),
        )
        assert vistas == [E.AGUARDANDO_ORCAMENTO]

        saga.cancelar(
            _marcos(),
            agora=DEPOIS,
            ator=ATOR_ATENDENTE,
            envio=_compensacao("CancelarOrcamento", saga, "cancelamento"),
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
        with pytest.raises(ValueError, match=etapa):
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
        with pytest.raises(ValueError, match=erro):
            saga_em(etapa, **campos)


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
        assert pecas_dos_itens(saga.itens) == [{"sku": "PEC-1", "quantidade": 2}]

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

    def test_permanencia_na_compensacao_conta_desde_a_entrada_na_etapa(
        self,
    ) -> None:
        # A compensacao e a primeira etapa com varios registros (um por
        # resposta): a duracao que o histograma recebe e a da etapa inteira.
        saga, enviado = _compensando(COMPENSACOES[3:])
        saga.avancar(
            evento("OrcamentoCancelado", saga.ordem_id, causation_id=enviado),
            _marcos(),
            agora=AGORA + timedelta(minutes=5),
            ator=ATOR_PROCESSO,
            envio=_compensacao("DescartarDiagnostico", saga, "cancelamento"),
        )
        saga.limpar_eventos()
        ultimo = UUID(saga.comando_em_voo["mensagem_ids"][-1])  # type: ignore[index]

        saga.avancar(
            evento("DiagnosticoDescartado", saga.ordem_id, causation_id=ultimo),
            _marcos(),
            agora=AGORA + timedelta(minutes=9),
            ator=ATOR_PROCESSO,
        )

        (mudanca,) = saga.coletar_eventos()
        assert isinstance(mudanca, EtapaDaSagaAlteradaEvent)
        assert (mudanca.etapa_anterior, mudanca.permanencia) == (
            E.COMPENSANDO,
            timedelta(minutes=9),
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

    @pytest.mark.parametrize(
        ("etapa", "tipo", "comando", "dados", "marcos"),
        [
            pytest.param(
                "aguardando_reserva",
                "PecasReservadas",
                Comando.SOLICITAR_PAGAMENTO,
                {"orcamento_id": str(uuid4())},
                _marcos(),
                id="orcamento-que-nao-e-o-da-os",
            ),
            pytest.param(
                "aguardando_reserva",
                "PecasReservadas",
                Comando.SOLICITAR_PAGAMENTO,
                {"orcamento_id": str(_ORCAMENTO_ID)},
                _marcos(orcamento_id=None),
                id="os-sem-orcamento",
            ),
            pytest.param(
                "aguardando_reserva",
                "PecasReservadas",
                Comando.SOLICITAR_PAGAMENTO,
                {"orcamento_id": None},
                _marcos(orcamento_id=None),
                id="pagamento-sem-orcamento-e-os-sem-orcamento",
            ),
            pytest.param(
                "aguardando_pagamento",
                "PagamentoConfirmado",
                Comando.AGENDAR_EXECUCAO,
                {"prioridade": "alta"},
                _marcos(checkout=True),
                id="prioridade-que-nao-e-normal",
            ),
            pytest.param(
                "aguardando_pagamento",
                "PagamentoConfirmado",
                Comando.AGENDAR_EXECUCAO,
                {"prioridade": None},
                _marcos(checkout=True),
                id="agendamento-sem-prioridade",
            ),
        ],
    )
    def test_dados_que_a_os_ou_a_tabela_nao_confirmam_sao_recusados(
        self,
        etapa: str,
        tipo: str,
        comando: Comando,
        dados: dict[str, Any],
        marcos: MarcosDaOrdem,
    ) -> None:
        saga = saga_em(etapa)
        antes = _foto(saga)
        # ``None`` tira a chave dos dados do comando.
        trocados = {**_dados(comando, saga), **dados}
        envio = _envio(
            comando, saga, dados={k: v for k, v in trocados.items() if v is not None}
        )

        with pytest.raises(TransicaoDaSagaInvalidaError, match="dados"):
            saga.avancar(
                evento(tipo, saga.ordem_id),
                marcos,
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
                envio=envio,
            )

        assert _foto(saga) == antes


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
        with pytest.raises(TransicaoDaSagaInvalidaError, match="vocabulario"):
            conferir_gatilho(
                Transicao(E.EM_EXECUCAO), Gatilho(gatilho, None, AGORA, "x")
            )

    def test_o_da_transicao_vale_no_lugar_do_tipo_do_evento(self) -> None:
        # A corrida do pivot grava cancelamento_recusado; o tipo da mensagem
        # (aqui fora do vocabulario) nao conta quando a transicao diz o seu.
        conferir_gatilho(
            Transicao(E.EM_EXECUCAO, gatilho="cancelamento_recusado"),
            Gatilho("qualquer", None, AGORA, "x"),
        )
        with pytest.raises(TransicaoDaSagaInvalidaError, match="inventado"):
            conferir_gatilho(
                Transicao(E.EM_EXECUCAO, gatilho="inventado"),
                Gatilho("abertura", None, AGORA, "x"),
            )

    def test_a_saga_recusa_o_registro_com_gatilho_fora_do_vocabulario(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        saga, _ = _compensando(COMPENSACOES)
        antes = _foto(saga)
        monkeypatch.setattr(
            transicoes,
            "CORRIDA_DO_PIVOT",
            Transicao(E.EM_EXECUCAO, gatilho="inventado"),
        )

        with pytest.raises(TransicaoDaSagaInvalidaError, match="inventado"):
            saga.avancar(
                evento("ExecucaoIniciada", saga.ordem_id),
                _marcos(),
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
            )

        assert _foto(saga) == antes


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


# ----- compensacao no agregado (RFC-004 secoes 4.4, 4.5 e 4.7)

# A resposta de cada compensacao, como o participante a publica.
RESPOSTA = {
    "CancelarExecucao": "ExecucaoCancelada",
    "EstornarPagamento": "PagamentoCancelado",
    "LiberarReserva": "ReservaLiberada",
    "CancelarOrcamento": "OrcamentoCancelado",
    "DescartarDiagnostico": "DiagnosticoDescartado",
}


def _feito(perfil: str, **campos: Any) -> dict[str, Any]:
    """Passos concluidos e comando em voo da saga no ``perfil`` (``FEITO``)."""
    concluidos, em_voo = FEITO[perfil]
    estado: dict[str, Any] = {"_passos_concluidos": list(concluidos), **campos}
    if em_voo is not None:
        estado |= {
            "_comando_em_voo": {
                "tipo": em_voo,
                "dados": {},
                "mensagem_ids": [str(uuid4())],
                "enviado_em": AGORA.isoformat(),
            },
            "_prazo_resposta_em": AGORA + timedelta(minutes=2),
        }
    return estado


def _compensacao(
    comando: str,
    saga: Saga,
    motivo: str,
    marcos: MarcosDaOrdem | None = None,
    **campos: Any,
) -> Envio:
    """A compensacao como o orquestrador a envia: ``{ordem_id, motivo}``, o id do
    recurso que a OS ja conhece (``marcos``) e o prazo."""
    marcos = marcos or _marcos()
    ids = {
        "CancelarOrcamento": ("orcamento_id", marcos.orcamento_id),
        "EstornarPagamento": ("pagamento_id", marcos.pagamento_id),
    }
    chave, valor = ids.get(comando, ("", None))
    return Envio(
        **{
            "tipo": Comando(comando),
            "id": uuid4(),
            "dados": {
                "ordem_id": str(saga.ordem_id),
                "motivo": motivo,
                **({chave: str(valor)} if valor is not None else {}),
            },
            "prazo_resposta_em": PRAZO,
            **campos,
        }
    )


def _compensando(plano: tuple[str, ...], **campos: Any) -> tuple[Saga, UUID]:
    """Saga compensando pelo ``plano``, com a pendente em voo; devolve o id dela."""
    enviado = uuid4()
    saga = saga_em(
        "compensando",
        **{
            "_plano_compensacao": list(plano),
            "_comando_em_voo": {
                "tipo": plano[0],
                "dados": {},
                "mensagem_ids": [str(uuid4()), str(enviado)],
                "enviado_em": AGORA.isoformat(),
            },
            "_prazo_resposta_em": AGORA + timedelta(minutes=2),
            "_reenvios": 1,
            **campos,
        },
    )
    return saga, enviado


# (falha, etapa, perfil do que a saga ja fez, motivo, plano): as linhas de
# falha de negocio da tabela da RFC-004 secao 4.4.
FALHAS = [
    pytest.param(
        "GeracaoDeOrcamentoFalhou",
        "aguardando_orcamento",
        "aguardando_orcamento",
        "geracao_falhou",
        COMPENSACOES[4:],
        id="geracao",
    ),
    pytest.param(
        "OrcamentoRecusado",
        "aguardando_decisao",
        "aguardando_decisao",
        "orcamento_recusado",
        COMPENSACOES[4:],
        id="recusa",
    ),
    pytest.param(
        "OrcamentoExpirado",
        "aguardando_decisao",
        "aguardando_decisao",
        "orcamento_expirado",
        COMPENSACOES[4:],
        id="expiracao",
    ),
    pytest.param(
        "ReservaDePecasFalhou",
        "aguardando_reserva",
        "aguardando_reserva",
        "reserva_falhou",
        COMPENSACOES[3:],
        id="reserva",
    ),
    pytest.param(
        "PagamentoRecusado",
        "aguardando_pagamento",
        "aguardando_pagamento_com_checkout",
        "pagamento_recusado",
        COMPENSACOES[2:],
        id="pagamento-recusado",
    ),
    pytest.param(
        "PagamentoExpirado",
        "aguardando_pagamento",
        "aguardando_pagamento_com_checkout",
        "pagamento_expirado",
        COMPENSACOES[2:],
        id="pagamento-expirado",
    ),
]


class TestFalhaDeNegocio:
    @pytest.mark.parametrize(("falha", "etapa", "perfil", "motivo", "plano"), FALHAS)
    def test_inicia_a_compensacao_pela_primeira_do_plano(
        self,
        falha: str,
        etapa: str,
        perfil: str,
        motivo: str,
        plano: tuple[str, ...],
    ) -> None:
        saga = saga_em(etapa, **_feito(perfil))
        recebido = evento(falha, saga.ordem_id)
        envio = _compensacao(plano[0], saga, motivo)

        saga.avancar(
            recebido,
            _marcos(checkout=perfil.endswith("checkout")),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
            envio=envio,
        )

        assert (saga.etapa, saga.motivo, saga.falha) == (E.COMPENSANDO, motivo, None)
        assert saga.plano_compensacao == plano
        assert saga.comando_em_voo == envio.em_voo(DEPOIS)
        assert (saga.prazo_resposta_em, saga.reenvios) == (PRAZO, 0)
        (passo,) = saga.passos
        assert (passo["gatilho"], passo["mensagem_id"], passo["motivo"]) == (
            falha,
            str(recebido.id),
            motivo,
        )
        assert (passo["de"], passo["para"], passo["comando"]) == (
            etapa,
            "compensando",
            plano[0],
        )
        assert saga.coletar_eventos() == [
            EtapaDaSagaAlteradaEvent(
                agregado_id=saga.ordem_id,
                etapa_anterior=EtapaSaga(etapa),
                etapa_nova=E.COMPENSANDO,
                permanencia=DEPOIS - AGORA,
                ocorrido_em=DEPOIS,
            ),
            CompensacaoIniciadaEvent(
                agregado_id=saga.ordem_id,
                motivo=MotivoCompensacao(motivo),
                ocorrido_em=DEPOIS,
            ),
        ]

    @pytest.mark.parametrize(
        "mudanca",
        [
            pytest.param({"envio": None}, id="sem-envio"),
            pytest.param({"comando": "LiberarReserva"}, id="outra-compensacao"),
            pytest.param({"motivo": "cancelamento"}, id="outro-motivo"),
            pytest.param({"prazo_resposta_em": None}, id="sem-prazo"),
        ],
    )
    def test_recusa_o_envio_que_nao_e_a_primeira_do_plano(
        self, mudanca: dict[str, Any]
    ) -> None:
        saga = saga_em("aguardando_reserva", **_feito("aguardando_reserva"))
        antes = _foto(saga)
        envio = mudanca.get(
            "envio",
            _compensacao(
                mudanca.get("comando", "CancelarOrcamento"),
                saga,
                mudanca.get("motivo", "reserva_falhou"),
                prazo_resposta_em=mudanca.get("prazo_resposta_em", PRAZO),
            ),
        )

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.avancar(
                evento("ReservaDePecasFalhou", saga.ordem_id),
                _marcos(),
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
                envio=envio,
            )

        assert _foto(saga) == antes


class TestRespostaDaCompensacao:
    @pytest.mark.parametrize(
        "plano",
        [pytest.param(COMPENSACOES[i:], id=COMPENSACOES[i]) for i in range(4)],
    )
    def test_resposta_da_pendente_envia_a_seguinte(
        self, plano: tuple[str, ...]
    ) -> None:
        saga, enviado = _compensando(plano)
        recebido = evento(RESPOSTA[plano[0]], saga.ordem_id, causation_id=enviado)
        envio = _compensacao(plano[1], saga, "cancelamento")

        saga.avancar(recebido, _marcos(), agora=DEPOIS, ator=ATOR_PROCESSO, envio=envio)

        assert (saga.etapa, saga.motivo) == (E.COMPENSANDO, "cancelamento")
        assert saga.plano_compensacao == plano[1:]
        # Comando novo: os envios e o contador recomecam (RFC-004 secao 4.6).
        assert saga.comando_em_voo == envio.em_voo(DEPOIS)
        assert (saga.prazo_resposta_em, saga.reenvios) == (PRAZO, 0)
        (passo,) = saga.passos
        assert (passo["gatilho"], passo["comando"], passo["motivo"]) == (
            RESPOSTA[plano[0]],
            plano[1],
            "cancelamento",
        )
        assert saga.coletar_eventos() == []

    def test_resposta_da_ultima_compensa_a_saga(self) -> None:
        saga, enviado = _compensando(("DescartarDiagnostico",))
        recebido = evento("DiagnosticoDescartado", saga.ordem_id, causation_id=enviado)

        saga.avancar(recebido, _marcos(), agora=DEPOIS, ator=ATOR_PROCESSO)

        assert (saga.etapa, saga.motivo, saga.plano_compensacao) == (
            E.COMPENSADA,
            "cancelamento",
            (),
        )
        assert (saga.comando_em_voo, saga.prazo_resposta_em, saga.reenvios) == (
            None,
            None,
            0,
        )
        assert saga.encerrada
        assert saga.coletar_eventos() == [
            EtapaDaSagaAlteradaEvent(
                agregado_id=saga.ordem_id,
                etapa_anterior=E.COMPENSANDO,
                etapa_nova=E.COMPENSADA,
                permanencia=DEPOIS - AGORA,
                ocorrido_em=DEPOIS,
            )
        ]

    @pytest.mark.parametrize("resposta", ["PagamentoCancelado", "PagamentoEstornado"])
    def test_estorno_responde_com_o_cancelamento_ou_o_estorno_casados(
        self, resposta: str
    ) -> None:
        saga, enviado = _compensando(COMPENSACOES[1:])
        recebido = evento(resposta, saga.ordem_id, causation_id=enviado)
        envio = _compensacao("LiberarReserva", saga, "cancelamento")

        saga.avancar(recebido, _marcos(), agora=DEPOIS, ator=ATOR_PROCESSO, envio=envio)

        assert saga.plano_compensacao == COMPENSACOES[2:]

    def test_estorno_recusado_para_a_sequencia(self) -> None:
        # O compensado fica compensado, o resto como estava; a pendente segue
        # no plano para a retomada (RFC-004 secao 4.7).
        saga, enviado = _compensando(COMPENSACOES[1:])
        antes = {"_comando_em_voo": saga.comando_em_voo}
        recebido = evento(
            "EstornoDePagamentoFalhou", saga.ordem_id, causation_id=enviado
        )

        saga.avancar(recebido, _marcos(), agora=DEPOIS, ator=ATOR_PROCESSO)

        assert (saga.etapa, saga.falha, saga.motivo) == (
            E.FALHA_NA_COMPENSACAO,
            "estorno_recusado",
            "cancelamento",
        )
        assert saga.plano_compensacao == COMPENSACOES[1:]
        # O comando em voo fica, com os ids de todos os envios, e sem prazo: uma
        # resposta tardia ainda casa depois da retomada (RFC-004 secoes 4.4/4.7).
        assert saga.comando_em_voo == antes["_comando_em_voo"]
        assert (saga.prazo_resposta_em, saga.reenvios) == (None, 1)
        assert not saga.encerrada
        (passo,) = saga.passos
        assert (passo["gatilho"], passo["para"], passo["comando"]) == (
            "EstornoDePagamentoFalhou",
            "falha_na_compensacao",
            None,
        )

    @pytest.mark.parametrize(
        "resposta",
        ["PagamentoCancelado", "PagamentoEstornado", "EstornoDePagamentoFalhou"],
    )
    def test_resposta_do_estorno_sem_o_causation_id_de_um_envio_e_recusada(
        self, resposta: str
    ) -> None:
        saga, _ = _compensando(COMPENSACOES[1:])
        antes = _foto(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.avancar(
                evento(resposta, saga.ordem_id, causation_id=uuid4()),
                _marcos(),
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
            )

        assert _foto(saga) == antes

    @pytest.mark.parametrize("posicao", [0, 1, 2])
    @pytest.mark.parametrize(
        "resposta",
        ["PagamentoCancelado", "PagamentoEstornado", "EstornoDePagamentoFalhou"],
    )
    def test_resposta_ao_envio_original_ou_a_um_reenvio_casa_pelo_causation_id(
        self, resposta: str, posicao: int
    ) -> None:
        # O comando foi reenviado duas vezes: a resposta de qualquer um dos tres
        # envios (o original inclusive) responde a pendente (RFC-004 secao 4.4).
        ids = [uuid4(), uuid4(), uuid4()]
        saga, _ = _compensando(
            COMPENSACOES[1:],
            _comando_em_voo={
                "tipo": "EstornarPagamento",
                "dados": {},
                "mensagem_ids": [str(i) for i in ids],
                "enviado_em": AGORA.isoformat(),
            },
        )
        falhou = resposta == "EstornoDePagamentoFalhou"
        envio = None if falhou else _compensacao("LiberarReserva", saga, "cancelamento")

        saga.avancar(
            evento(resposta, saga.ordem_id, causation_id=ids[posicao]),
            _marcos(),
            agora=DEPOIS,
            ator=ATOR_PROCESSO,
            envio=envio,
        )

        assert saga.etapa is (E.FALHA_NA_COMPENSACAO if falhou else E.COMPENSANDO)

    def test_resposta_que_envia_outra_compensacao_e_recusada(self) -> None:
        saga, enviado = _compensando(COMPENSACOES[2:])
        antes = _foto(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.avancar(
                evento("ReservaLiberada", saga.ordem_id, causation_id=enviado),
                _marcos(),
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
                envio=_compensacao("DescartarDiagnostico", saga, "cancelamento"),
            )

        assert _foto(saga) == antes


class TestCorridaDoPivot:
    @pytest.mark.parametrize("etapa", ["compensando", "falha_na_compensacao"])
    def test_inicio_com_o_cancelamento_da_execucao_pendente_volta_ao_fluxo(
        self, etapa: str
    ) -> None:
        # Nada foi desfeito: a saga volta a em_execucao, sem motivo nem plano,
        # e o registro diz que o cancelamento foi recusado (RFC-004 4.5).
        saga = saga_em(
            etapa, _plano_compensacao=list(COMPENSACOES), _passos_concluidos=["T7"]
        )
        iniciada = evento("ExecucaoIniciada", saga.ordem_id)

        saga.avancar(iniciada, _marcos(checkout=True), agora=DEPOIS, ator=ATOR_PROCESSO)

        assert (saga.etapa, saga.motivo, saga.falha) == (E.EM_EXECUCAO, None, None)
        # O prazo e o comando em voo somem com a volta ao fluxo.
        assert (saga.plano_compensacao, saga.comando_em_voo) == ((), None)
        assert (saga.prazo_resposta_em, saga.reenvios) == (None, 0)
        (passo,) = saga.passos
        assert (
            passo["gatilho"],
            passo["mensagem_id"],
            passo["de"],
            passo["motivo"],
        ) == ("cancelamento_recusado", str(iniciada.id), etapa, None)
        assert [type(f) for f in saga.coletar_eventos()] == [EtapaDaSagaAlteradaEvent]

    @pytest.mark.parametrize("pendente", COMPENSACOES[1:])
    def test_inicio_com_outra_compensacao_pendente_e_recusado(
        self, pendente: str
    ) -> None:
        saga = saga_em(
            "compensando",
            _plano_compensacao=list(COMPENSACOES[COMPENSACOES.index(pendente) :]),
        )
        antes = _foto(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.avancar(
                evento("ExecucaoIniciada", saga.ordem_id),
                _marcos(checkout=True),
                agora=DEPOIS,
                ator=ATOR_PROCESSO,
            )

        assert _foto(saga) == antes


# (perfil do que a saga ja fez, etapa, plano): as linhas de cancelamento da
# tabela da RFC-004 secao 4.4.
CANCELAMENTOS = [
    pytest.param(perfil, perfil.removesuffix("_com_checkout"), plano, id=perfil)
    for perfil, plano in [
        ("aguardando_diagnostico", COMPENSACOES[4:]),
        ("aguardando_orcamento", COMPENSACOES[3:]),
        ("aguardando_decisao", COMPENSACOES[3:]),
        ("aguardando_reserva", COMPENSACOES[2:]),
        ("aguardando_pagamento", COMPENSACOES[1:]),
        ("aguardando_pagamento_com_checkout", COMPENSACOES[1:]),
        ("aguardando_agendamento", COMPENSACOES),
        ("aguardando_inicio", COMPENSACOES),
    ]
]


class TestCancelar:
    @pytest.mark.parametrize(("perfil", "etapa", "plano"), CANCELAMENTOS)
    def test_antes_do_pivot_compensa_os_passos_e_o_em_voo(
        self, perfil: str, etapa: str, plano: tuple[str, ...]
    ) -> None:
        saga = saga_em(etapa, **_feito(perfil))
        marcos = _marcos(checkout=perfil.endswith("com_checkout"))
        envio = _compensacao(plano[0], saga, "cancelamento", marcos)

        saga.cancelar(marcos, agora=DEPOIS, ator=ATOR_ATENDENTE, envio=envio)

        assert (saga.etapa, saga.motivo) == (E.COMPENSANDO, "cancelamento")
        assert saga.plano_compensacao == plano
        assert saga.comando_em_voo == envio.em_voo(DEPOIS)
        (passo,) = saga.passos
        assert passo == {
            "seq": 1,
            "em": DEPOIS.isoformat(),
            "de": etapa,
            "para": "compensando",
            "gatilho": "cancelamento_solicitado",
            "mensagem_id": None,
            "comando": plano[0],
            "comando_id": str(envio.id),
            "motivo": "cancelamento",
            "ator": ATOR_ATENDENTE,
        }
        assert saga.coletar_eventos()[-1] == CompensacaoIniciadaEvent(
            agregado_id=saga.ordem_id,
            motivo=MotivoCompensacao.CANCELAMENTO,
            ocorrido_em=DEPOIS,
        )

    @pytest.mark.parametrize(
        "etapa",
        [
            "em_execucao",
            "concluida",
            "compensando",
            "compensada",
            "falha_na_compensacao",
        ],
    )
    def test_fora_das_etapas_de_antes_do_pivot_e_recusado(self, etapa: str) -> None:
        saga = saga_em(etapa)
        antes = _foto(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError, match=etapa):
            saga.cancelar(
                _marcos(),
                agora=DEPOIS,
                ator=ATOR_ATENDENTE,
                envio=_compensacao("DescartarDiagnostico", saga, "cancelamento"),
            )

        assert _foto(saga) == antes

    @pytest.mark.parametrize(
        ("mudanca", "agora"),
        [
            pytest.param({"comando": "CancelarOrcamento"}, DEPOIS, id="outra"),
            pytest.param({"motivo": "prazo_tecnico"}, DEPOIS, id="outro-motivo"),
            pytest.param({}, AGORA - timedelta(seconds=1), id="instante-que-volta"),
            pytest.param({}, datetime(2026, 10, 7, 13), id="instante-sem-fuso"),
            pytest.param({"prazo_resposta_em": None}, DEPOIS, id="prazo-ausente"),
            pytest.param({"prazo_resposta_em": DEPOIS}, DEPOIS, id="prazo-no-instante"),
            pytest.param(
                {"prazo_resposta_em": DEPOIS - timedelta(seconds=1)},
                DEPOIS,
                id="prazo-no-passado",
            ),
            pytest.param(
                {"prazo_resposta_em": datetime(2026, 10, 7, 14)},
                DEPOIS,
                id="prazo-sem-fuso",
            ),
        ],
    )
    def test_envio_ou_instante_errado_e_recusado(
        self, mudanca: dict[str, Any], agora: datetime
    ) -> None:
        # A guarda do prazo e da propria ``cancelar``: sem ela o envio errado so
        # seria barrado pelas invariantes, depois de a saga mudar.
        saga = saga_em("aguardando_diagnostico")
        antes = _foto(saga)
        envio = _compensacao(
            mudanca.get("comando", "DescartarDiagnostico"),
            saga,
            mudanca.get("motivo", "cancelamento"),
            **(
                {"prazo_resposta_em": mudanca["prazo_resposta_em"]}
                if "prazo_resposta_em" in mudanca
                else {}
            ),
        )

        with pytest.raises(TransicaoDaSagaInvalidaError):
            saga.cancelar(_marcos(), agora=agora, ator=ATOR_ATENDENTE, envio=envio)

        assert _foto(saga) == antes


class TestIdDoRecursoNaCompensacao:
    # O CancelarOrcamento leva o orcamento_id e o EstornarPagamento o
    # pagamento_id quando a OS ja os tem, e nenhum quando nao (RFC-004 secao
    # 5.3): a saga confere contra o que a OS guarda.
    @pytest.mark.parametrize(
        ("comando", "campo", "perfil"),
        [
            pytest.param("CancelarOrcamento", "orcamento_id", "aguardando_decisao"),
            pytest.param(
                "EstornarPagamento", "pagamento_id", "aguardando_pagamento_com_checkout"
            ),
        ],
    )
    @pytest.mark.parametrize(
        "dados",
        [
            pytest.param(lambda campo: {}, id="sem-o-id-que-a-os-tem"),
            pytest.param(lambda campo: {campo: str(uuid4())}, id="outro-id"),
        ],
    )
    def test_id_ausente_ou_de_outro_recurso_e_recusado(
        self,
        comando: str,
        campo: str,
        perfil: str,
        dados: Callable[[str], dict[str, str]],
    ) -> None:
        saga = saga_em(perfil.removesuffix("_com_checkout"), **_feito(perfil))
        marcos = _marcos(checkout=perfil.endswith("com_checkout"))
        certo = _compensacao(comando, saga, "cancelamento", marcos)
        errado = Envio(
            tipo=certo.tipo,
            id=certo.id,
            dados={
                "ordem_id": str(saga.ordem_id),
                "motivo": "cancelamento",
                **dados(campo),
            },
            prazo_resposta_em=PRAZO,
        )
        antes = _foto(saga)

        with pytest.raises(TransicaoDaSagaInvalidaError, match="nao sao os desta saga"):
            saga.cancelar(marcos, agora=DEPOIS, ator=ATOR_ATENDENTE, envio=errado)

        assert _foto(saga) == antes

    def test_id_que_a_os_nao_tem_nao_vai_no_comando(self) -> None:
        saga = saga_em("aguardando_orcamento", **_feito("aguardando_orcamento"))
        marcos = _marcos(orcamento_id=None)
        com_id = Envio(
            tipo=Comando.CANCELAR_ORCAMENTO,
            id=uuid4(),
            dados={
                "ordem_id": str(saga.ordem_id),
                "motivo": "cancelamento",
                "orcamento_id": str(_ORCAMENTO_ID),
            },
            prazo_resposta_em=PRAZO,
        )

        with pytest.raises(TransicaoDaSagaInvalidaError, match="nao sao os desta saga"):
            saga.cancelar(marcos, agora=DEPOIS, ator=ATOR_ATENDENTE, envio=com_id)


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
            tipo, _marcos(), causation_id=enviado if casada else uuid4()
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

        classificacao = saga.classificar("ExecucaoIniciada", _marcos(checkout=True))

        assert classificacao is esperado(etapa, "ExecucaoIniciada", pendente=pendente)
        assert (classificacao is C.PROCESSAR) is (pendente == "CancelarExecucao")

    def test_estorno_sem_comando_em_voo_e_o_automatico(self) -> None:
        # Parada na falha nada fica em voo: nenhum causation_id casa.
        saga = saga_em(
            "falha_na_compensacao", _plano_compensacao=list(COMPENSACOES[1:])
        )

        classificacao = saga.classificar(
            "PagamentoEstornado", _marcos(checkout=True), causation_id=uuid4()
        )

        assert classificacao is C.ESTORNO_AUTOMATICO
