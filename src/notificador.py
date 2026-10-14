"""Processo notificador dos e-mails ao cliente: ``python -m src.notificador``.

Roda na mesma imagem da API, com outro comando (compose e Deployment proprio).
Entrega por SMTP os pedidos da tabela ``notificacoes`` que a transicao da OS
gravou (RFC-004 secao 7.2; ADR-036). Nao fala com o broker nem recebe a chave de
cifragem: le o contato do cliente por projecao de duas colunas. Nao aplica
migracoes: no compose quem as roda e a API, no Kubernetes o Job de migracao.
"""

from __future__ import annotations

import threading

from src.compartilhado.infraestrutura.mensageria.processo import (
    instalar_sinais,
    preparar_sem_broker,
    subir_metricas,
)
from src.ordem_servico.infraestrutura.email_adapter import SmtpEmailAdapter
from src.ordem_servico.infraestrutura.notificador import Notificador


def main() -> None:
    """Sobe o notificador e entrega os e-mails ate o SIGTERM."""
    parar = threading.Event()
    instalar_sinais(parar)
    engine = preparar_sem_broker("notificador")
    try:
        notificador = Notificador(engine=engine, email=SmtpEmailAdapter.do_ambiente())
        subir_metricas()
        notificador.executar(parar)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
