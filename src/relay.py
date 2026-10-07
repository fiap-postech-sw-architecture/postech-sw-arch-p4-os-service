"""Processo relay da outbox: ``python -m src.relay``.

Roda na mesma imagem da API, com outro comando (compose e Deployment proprio).
Nao aplica migracoes: no compose quem as roda e a API, no Kubernetes o Job de
migracao antes do rollout.
"""

from __future__ import annotations

import threading

from src.compartilhado.infraestrutura.mensageria.processo import (
    instalar_sinais,
    preparar,
    subir_metricas,
)
from src.compartilhado.infraestrutura.mensageria.relay import ConfigRelay, Relay
from src.compartilhado.infraestrutura.observability import criar_tracer


def main() -> None:
    """Sobe o relay e publica a outbox ate o SIGTERM."""
    parar = threading.Event()
    instalar_sinais(parar)
    engine, parametros = preparar("relay")
    try:
        relay = Relay(
            engine=engine,
            parametros=parametros,
            tracer=criar_tracer("relay"),
            config=ConfigRelay.do_ambiente(),
        )
        subir_metricas()
        relay.executar(parar)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
