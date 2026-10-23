"""Metricas da saga (RFC-004 secao 9, ADR-043), no registro do prometheus_client.

Contadores e histograma: a ``Saga`` registra os fatos (``SagaIniciadaEvent``,
``EtapaDaSagaAlteradaEvent``, ``CompensacaoIniciadaEvent``), o repositorio os
entrega a sessao no ``salvar`` e eles so viram metrica no commit da transacao raiz
(``metricas_apos_commit``, a mesma regra da OS criada): rollback, conflito de versao
ou commit que falha os descartam, e a mensagem que volta pela retry nao conta duas
vezes. Quem emite e o processo que tira a saga da etapa (API na abertura e no
cancelamento, consumidor nos eventos).

Contadores do processo ``prazos`` (``pytstop_saga_reenvios_total`` e
``pytstop_saga_prazos_esgotados_total``, por tipo de comando): vem dos fatos
``ComandoReenviadoEvent`` e ``PrazoEsgotadoEvent``, com a mesma regra do commit.

Gauges: a leitura das sagas (``ler_sagas``) e do ``ColetorDaApi``
(``coletor_da_api.py``), registrado so na API: consulta ``sagas`` (e, no prazo
vencido, ``outbox``) na hora da raspagem, numa conexao propria e com prazo curto, e
continua certo com os outros processos fora do ar; as replicas repetem o valor, e
os paineis agregam com ``max``. A serie ``pytstop_saga_coletor_disponivel`` diz se
a leitura das sagas deu certo.

Series de rotulo fechado comecam em zero no boot de cada processo, para o
``increase()`` dos paineis nao perder o primeiro evento.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

from prometheus_client import Counter, Histogram
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import String, func, select, text, type_coerce

from src.ordem_servico.aplicacao.saga.modelo import (
    CompensacaoIniciadaEvent,
    EtapaDaSagaAlteradaEvent,
    MotivoCompensacao,
    SagaIniciadaEvent,
)
from src.ordem_servico.aplicacao.saga.prazos import (
    ComandoReenviadoEvent,
    PrazoEsgotadoEvent,
)
from src.ordem_servico.aplicacao.saga.tabela_da_saga import (
    COMANDOS_COM_PRAZO,
    ETAPAS_FINAIS,
    ETAPAS_NAO_FINAIS,
)
from src.ordem_servico.infraestrutura.envio_em_voo import ULTIMO_ENVIO
from src.ordem_servico.infraestrutura.mapping import sagas_table
from src.ordem_servico.infraestrutura.metricas_apos_commit import registrar

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    from datetime import timedelta

    from sqlalchemy import Connection
    from sqlalchemy.orm import Session

    from src.compartilhado.dominio.events import DomainEvent

SAGAS_INICIADAS: Final = Counter(
    "pytstop_saga_iniciadas_total", "Sagas abertas junto com a OS."
)
FALHAS_DO_COLETOR: Final = Counter(
    "pytstop_saga_coletor_falhas_total",
    "Raspagens em que o coletor dos gauges da saga nao leu as sagas.",
)
SAGAS_FINALIZADAS: Final = Counter(
    "pytstop_saga_finalizadas_total",
    "Sagas encerradas, por resultado (concluida ou compensada).",
    ["resultado"],
)
DURACAO_DA_ETAPA: Final = Histogram(
    "pytstop_saga_etapa_duracao_segundos",
    "Permanencia da saga em cada etapa, observada quando ela sai da etapa.",
    ["etapa"],
    # De 50 ms (resposta automatica) a 7 dias (espera humana), com 120 s (o prazo
    # tecnico) e 300 s (o fim da escada de retry) em baldes proprios. O
    # histogram_quantile interpola linearmente dentro do balde: com o primeiro em
    # 5 s toda etapa automatica saia com p95 de 4,75 s (0,95 x 5), igual em todas.
    buckets=(
        0.05,
        0.1,
        0.25,
        0.5,
        1,
        2.5,
        5,
        10,
        30,
        60,
        120,
        300,
        600,
        3600,
        14400,
        86400,
        259200,
        604800,
    ),
)
COMPENSACOES: Final = Counter(
    "pytstop_saga_compensacoes_total",
    "Compensacoes iniciadas, por motivo (codigo da RFC-004 secao 9).",
    ["motivo"],
)
REENVIOS: Final = Counter(
    "pytstop_saga_reenvios_total",
    "Comandos reenviados pelo prazos depois do prazo de resposta, por tipo.",
    ["comando"],
)
PRAZOS_ESGOTADOS: Final = Counter(
    "pytstop_saga_prazos_esgotados_total",
    "Comandos que ficaram sem resposta depois do ultimo reenvio, por tipo.",
    ["comando"],
)
for _etapa in ETAPAS_FINAIS:
    SAGAS_FINALIZADAS.labels(resultado=_etapa.value)
for _comando in COMANDOS_COM_PRAZO:
    REENVIOS.labels(comando=_comando.value)
    PRAZOS_ESGOTADOS.labels(comando=_comando.value)
for _motivo in MotivoCompensacao:
    COMPENSACOES.labels(motivo=_motivo.value)
for _etapa in ETAPAS_NAO_FINAIS:
    DURACAO_DA_ETAPA.labels(etapa=_etapa.value)


def anotar(sessao: Session, fatos: Iterable[DomainEvent]) -> None:
    """Guarda a metrica de cada fato da saga ate o commit da transacao da ``sessao``.

    Raises:
        TypeError: fato sem metrica (um fato novo da saga sem a observacao
            dele), antes do commit: nada se perde em silencio.
    """
    registrar(sessao, [_observacao(fato) for fato in fatos])


def _observacao(fato: DomainEvent) -> Callable[[], None]:
    """A metrica do fato, para rodar depois do commit."""
    match fato:
        case SagaIniciadaEvent():
            return SAGAS_INICIADAS.inc
        case EtapaDaSagaAlteradaEvent():
            return partial(_etapa_alterada, fato)
        case CompensacaoIniciadaEvent():
            return COMPENSACOES.labels(motivo=fato.motivo.value).inc
        case ComandoReenviadoEvent():
            return REENVIOS.labels(comando=fato.comando).inc
        case PrazoEsgotadoEvent():
            return PRAZOS_ESGOTADOS.labels(comando=fato.comando).inc
    msg = f"fato da saga sem metrica: {type(fato).__name__}"
    raise TypeError(msg)


def _etapa_alterada(fato: EtapaDaSagaAlteradaEvent) -> None:
    DURACAO_DA_ETAPA.labels(etapa=fato.etapa_anterior.value).observe(
        fato.permanencia.total_seconds()
    )
    if fato.etapa_nova in ETAPAS_FINAIS:
        SAGAS_FINALIZADAS.labels(resultado=fato.etapa_nova.value).inc()


# O maior atraso do prazo tecnico (RFC-004 secao 9): a saga com prazo cujo
# envio mais recente (o ultimo de ``mensagem_ids``) a outbox ja entregou, e a
# entrega mais o prazo ja passou. A mesma condicao com que o processo ``prazos``
# reivindica a saga; pendente ou ``dead`` nao conta (quem acusa e a outbox
# parada), nem a saga parada em ``falha_na_compensacao`` (sem prazo). Zero sem
# atraso.
_ATRASO_DO_PRAZO: Final = text(
    "SELECT COALESCE(EXTRACT(EPOCH FROM max(now() - (o.entregue_em + :prazo))), 0) "  # noqa: S608  # nosec B608 - so constantes
    f"FROM sagas s JOIN outbox o ON o.mensagem_id = {ULTIMO_ENVIO} "
    "WHERE s.prazo_resposta_em <= now() AND o.status = 'entregue' "
    "AND o.entregue_em + :prazo <= now()"
)


@dataclass(frozen=True, slots=True)
class LeituraDaSaga:
    """A leitura das sagas: total e idade por etapa, e o atraso do prazo."""

    por_etapa: dict[str, tuple[int, float]]
    atraso_do_prazo: float


def ler_sagas(conexao: Connection, prazo: timedelta) -> LeituraDaSaga:
    """As duas consultas dos gauges da saga, na transacao e nos prazos da ``conexao``.

    ``..._etapa_mais_antiga_segundos`` sai da mesma consulta de ``ativas``, pelo
    indice parcial ``ix_sagas_ativas``; ``..._prazo_vencido_segundos``, de uma
    segunda, que junta ``sagas`` e ``outbox`` pelo indice ``ix_sagas_prazo``. A
    etapa vem como texto: uma etapa que esta versao nao conhece (rollout com
    versoes misturadas) fica fora da contagem, sem derrubar a raspagem.
    """
    t = sagas_table
    consulta = (
        select(
            type_coerce(t.c.etapa, String),
            func.count(),
            func.extract("epoch", func.now() - func.min(t.c.etapa_desde)),
        )
        .where(t.c.etapa.not_in(ETAPAS_FINAIS))
        .group_by(t.c.etapa)
    )
    linhas = conexao.execute(consulta).all()
    atraso = conexao.execute(_ATRASO_DO_PRAZO, {"prazo": prazo}).scalar_one()
    return LeituraDaSaga(
        {etapa: (total, float(idade)) for etapa, total, idade in linhas}, float(atraso)
    )


def familias_da_saga(leitura: LeituraDaSaga) -> Iterator[GaugeMetricFamily]:
    """``pytstop_saga_ativas``, ``..._etapa_mais_antiga_segundos`` e o prazo vencido."""
    ativas, mais_antiga = _gauges()
    for etapa in ETAPAS_NAO_FINAIS:
        total, idade = leitura.por_etapa.get(etapa.value, (0, 0.0))
        ativas.add_metric([etapa.value], total)
        mais_antiga.add_metric([etapa.value], idade)
    yield ativas
    yield mais_antiga
    yield _prazo_vencido(leitura.atraso_do_prazo)


def descrever_a_saga() -> Iterator[GaugeMetricFamily]:
    """Os nomes dos gauges da saga, sem consultar o banco (registro antes do boot)."""
    yield from _gauges()
    yield _prazo_vencido()
    yield disponibilidade()


def _gauges() -> tuple[GaugeMetricFamily, GaugeMetricFamily]:
    return (
        GaugeMetricFamily(
            "pytstop_saga_ativas", "Sagas em cada etapa nao final.", labels=["etapa"]
        ),
        GaugeMetricFamily(
            "pytstop_saga_etapa_mais_antiga_segundos",
            "Idade, na etapa, da saga mais antiga de cada etapa nao final.",
            labels=["etapa"],
        ),
    )


def _prazo_vencido(valor: float | None = None) -> GaugeMetricFamily:
    return GaugeMetricFamily(
        "pytstop_saga_prazo_vencido_segundos",
        "Maior atraso do prazo tecnico de um comando ja entregue (0 sem atraso).",
        value=valor,
    )


def disponibilidade(valor: int | None = None) -> GaugeMetricFamily:
    """``pytstop_saga_coletor_disponivel``: 1 se leu as sagas, 0 se falhou.

    Com 0 os gauges da saga saem ausentes (nunca em zero): o alerta "Saga parada"
    nao fica mudo com o coletor quebrado.
    """
    return GaugeMetricFamily(
        "pytstop_saga_coletor_disponivel",
        "1 se o coletor leu as sagas nesta raspagem; 0 se falhou.",
        value=valor,
    )
