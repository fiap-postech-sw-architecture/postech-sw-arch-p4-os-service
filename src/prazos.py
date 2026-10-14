"""Processo dos prazos tecnicos da saga: ``python -m src.prazos``.

Roda na mesma imagem da API, com outro comando (Deployment proprio). Nao aplica
migracoes: no compose quem as roda e a API, no Kubernetes o Job de migracao antes
do rollout. A cada ``PRAZOS_INTERVALO_SEGUNDOS`` reenvia o comando da saga sem
resposta e, esgotados os ``SAGA_MAX_REENVIOS``, inicia a compensacao ou para a
saga em ``falha_na_compensacao`` (RFC-004 secoes 4.6 e 4.7).
"""

from __future__ import annotations

import threading

from src.compartilhado.infraestrutura.database import BANCO_FORA, criar_session_factory
from src.compartilhado.infraestrutura.mensageria.processo import (
    Sinalizador,
    instalar_sinais,
    preparar,
    subir_metricas,
)
from src.compartilhado.infraestrutura.observability import criar_tracer
from src.ordem_servico.aplicacao.saga.contexto_de_envio import ContextoDeEnvio
from src.ordem_servico.aplicacao.saga.vencer_prazos import VencerPrazos
from src.ordem_servico.infraestrutura.prazo_da_saga import (
    intervalo_dos_prazos,
    maximo_de_reenvios_da_saga,
    prazo_de_resposta_da_saga,
)
from src.ordem_servico.infraestrutura.presenca_do_consumidor import (
    PresencaPikaDoConsumidor,
)
from src.ordem_servico.infraestrutura.processo_dos_prazos import ProcessoDosPrazos
from src.ordem_servico.infraestrutura.rastreamento_da_saga import span_da_saga
from src.ordem_servico.infraestrutura.transacao_dos_prazos import (
    TransacaoDosPrazosSQLAlchemy,
)


def main() -> None:
    """Sobe o processo e vence os prazos da saga ate o SIGTERM."""
    parar = threading.Event()
    instalar_sinais(parar)
    # Valores invalidos abortam o boot antes de abrir banco e broker.
    prazo = prazo_de_resposta_da_saga()
    maximo = maximo_de_reenvios_da_saga()
    intervalo = intervalo_dos_prazos()
    engine, parametros = preparar("prazos")
    try:
        sinal = Sinalizador("prazos")
        presenca = PresencaPikaDoConsumidor(parametros, sinal)
        session_factory = criar_session_factory(engine)
        processo = ProcessoDosPrazos(
            vencer=VencerPrazos(
                abrir=lambda: TransacaoDosPrazosSQLAlchemy(session_factory),
                presenca=presenca,
                contexto=ContextoDeEnvio(
                    prazo, span=span_da_saga(criar_tracer("prazos", engine))
                ),
                max_reenvios=maximo,
                banco_fora=BANCO_FORA,
            ),
            presenca=presenca,
            sinal=sinal,
            intervalo_s=intervalo,
        )
        subir_metricas()
        processo.executar(parar)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
