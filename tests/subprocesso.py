"""Roda um trecho de codigo do servico num interpretador novo.

O ``REGISTRY`` do ``prometheus_client`` e global ao processo, e as series de
outros testes o sujam: so um interpretador novo mostra o que o processo expoe
logo depois do boot, sem trafego.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def executar_python(codigo: str, *, prazo_s: float = 60.0) -> str:
    """A saida do ``codigo`` num interpretador novo, na raiz do repositorio.

    O ambiente vai sem as variaveis do servico (``DATABASE_URL``, ``OTEL_*`` e
    as demais): o que o processo expoe nao pode depender de quem roda o teste.
    """
    ambiente = {
        chave: valor
        for chave, valor in os.environ.items()
        if not chave.startswith(("OTEL_", "METRICS_", "POSTGRES_", "PYTSTOP_"))
        and chave not in {"DATABASE_URL", "RABBITMQ_URL", "ENVIRONMENT"}
    }
    resultado = subprocess.run(  # noqa: S603  # interpretador do proprio ambiente
        [sys.executable, "-c", codigo],
        cwd=RAIZ,
        env=ambiente,
        capture_output=True,
        text=True,
        timeout=prazo_s,
        check=False,
    )
    assert resultado.returncode == 0, resultado.stderr[-2000:]
    return resultado.stdout
