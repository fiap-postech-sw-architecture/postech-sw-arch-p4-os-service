"""Saga e envios de teste: a saga reidratada numa etapa e os comandos que a conferem.

Compartilhado pelos testes do agregado (``test_saga``), da classificacao
(``test_classificacao``) e das conferencias (``test_transicoes``) e do envio
(``test_contexto_de_envio``): cada helper monta o que a persistencia entregaria.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from src.compartilhado.aplicacao.mensageria import Comando
from src.ordem_servico.aplicacao.saga.classificacao import Classificacao
from src.ordem_servico.aplicacao.saga.modelo import (
    Envio,
    EtapaSaga,
    itens_do_diagnostico,
)
from src.ordem_servico.aplicacao.saga.saga import Saga
from src.ordem_servico.dominio.marcos import MarcosDaOrdem
from src.ordem_servico.dominio.status import StatusOrdem
from tests.eventos import evento
from tests.unitarios.ordem_servico.cenario_da_saga import (
    FEITO,
)

C = Classificacao
E = EtapaSaga
S = StatusOrdem

AGORA = datetime(2026, 10, 7, 12, tzinfo=UTC)

DEPOIS = AGORA + timedelta(minutes=10)

PRAZO = DEPOIS + timedelta(minutes=2)

# Itens do DiagnosticoConcluido de exemplo do platform (um servico e uma peca).
ITENS = itens_do_diagnostico(evento("DiagnosticoConcluido", uuid4()).dados)

PECAS = [
    {"sku": i["codigo"], "quantidade": i["quantidade"]}
    for i in ITENS
    if i["tipo"] == "peca"
]

# Orcamento que a OS guarda nos testes do agregado (o do OrcamentoGerado).
ORCAMENTO_ID = uuid4()

PAGAMENTO_ID = uuid4()


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


def marcos_da_ordem(
    *,
    diagnostico: bool = True,
    checkout: bool = False,
    orcamento_id: UUID | None = ORCAMENTO_ID,
) -> MarcosDaOrdem:
    return MarcosDaOrdem(
        diagnostico_iniciado=diagnostico,
        checkout_aberto=checkout,
        encerrada=False,
        orcamento_id=orcamento_id,
        pagamento_id=PAGAMENTO_ID if checkout else None,
    )


def dados_do_comando(comando: Comando, saga: Saga) -> dict[str, Any]:
    """Os ``dados`` certos do comando para a ``saga`` (RFC-004 secao 4.1)."""
    ordem_id = str(saga.ordem_id)
    return {
        Comando.GERAR_ORCAMENTO: {"ordem_id": ordem_id, "itens": ITENS},
        Comando.RESERVAR_PECAS: {"ordem_id": ordem_id, "pecas": PECAS},
        Comando.SOLICITAR_PAGAMENTO: {
            "ordem_id": ordem_id,
            "orcamento_id": str(ORCAMENTO_ID),
        },
        Comando.AGENDAR_EXECUCAO: {"ordem_id": ordem_id, "prioridade": "normal"},
    }[comando]


def envio_do_comando(comando: Comando, saga: Saga, **campos: Any) -> Envio:
    return Envio(
        **{
            "tipo": comando,
            "id": uuid4(),
            "dados": dados_do_comando(comando, saga),
            "prazo_resposta_em": PRAZO,
            **campos,
        }
    )


def comando_em_voo_de(ordem_id: UUID) -> dict[str, Any]:
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


def foto_da_saga(saga: Saga) -> tuple[object, ...]:
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


def feito_no_perfil(perfil: str, **campos: Any) -> dict[str, Any]:
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


def compensacao_enviada(
    comando: str,
    saga: Saga,
    motivo: str,
    marcos: MarcosDaOrdem | None = None,
    **campos: Any,
) -> Envio:
    """A compensacao como o orquestrador a envia: ``{ordem_id, motivo}``, o id do
    recurso que a OS ja conhece (``marcos``) e o prazo."""
    marcos = marcos or marcos_da_ordem()
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


def saga_compensando(plano: tuple[str, ...], **campos: Any) -> tuple[Saga, UUID]:
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
