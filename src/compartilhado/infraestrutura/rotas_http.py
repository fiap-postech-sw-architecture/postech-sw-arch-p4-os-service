"""O rotulo das requests que nao casaram rota, igual na metrica HTTP e no span.

Mora fora de ``metrics`` de proposito: ``metrics`` carrega o Starlette e registra o
histograma HTTP ao ser importado, e o relay, o consumidor e o prazos importam o
``observability`` (que usa este rotulo) sem servir HTTP.
"""

from __future__ import annotations

from typing import Final

# Label de rota para requests que nao casaram rota nenhuma (404 de path
# desconhecido): agrega tudo num unico valor em vez de explodir a
# cardinalidade com paths arbitrarios.
ROTA_NAO_ROTEADA: Final = "nao_roteada"
