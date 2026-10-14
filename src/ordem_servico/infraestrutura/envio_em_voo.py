"""A linha da outbox do envio mais recente de uma saga, para o SQL (RFC-004 4.6).

O ultimo id de ``sagas.comando_em_voo.mensagem_ids`` e o envio mais recente (o
original, um reenvio ou a retomada); a consulta dos prazos e o gauge de prazo
vencido o juntam a ``outbox.mensagem_id``. O ``CAST`` para ``uuid`` falha a
consulta inteira diante de um texto que nao seja uuid, e uma saga com esse dado
derrubaria o ciclo do ``prazos`` e a raspagem do coletor para sempre: so o texto
no formato de uuid e convertido, e a saga com outro valor simplesmente nao casa
com linha nenhuma. O ``CASE`` garante a ordem (o ``CAST`` so roda no ``THEN``) e
o ``=`` com a expressao continua usando o indice unico de ``mensagem_id``.
"""

from __future__ import annotations

from typing import Final

_ULTIMO_ID: Final = "s.comando_em_voo->'mensagem_ids'->>-1"
_FORMATO_UUID: Final = (
    "'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'"
)

# Expressao ``uuid`` (ou nula) do envio mais recente da saga ``s``.
ULTIMO_ENVIO: Final = (
    f"CASE WHEN {_ULTIMO_ID} ~* {_FORMATO_UUID} THEN CAST({_ULTIMO_ID} AS uuid) END"
)
