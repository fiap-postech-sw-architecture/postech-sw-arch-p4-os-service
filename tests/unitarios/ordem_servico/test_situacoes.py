from __future__ import annotations

import pytest

from src.ordem_servico.aplicacao.situacoes import _SITUACAO_POR_STATUS, situacao_de
from src.ordem_servico.dominio.status import StatusOrdem


@pytest.mark.parametrize(
    ("status", "rotulo"),
    [
        (StatusOrdem.RECEBIDA, "Recebida"),
        (StatusOrdem.EM_DIAGNOSTICO, "Em diagnóstico"),
        (StatusOrdem.AGUARDANDO_APROVACAO, "Aguardando aprovação"),
        (StatusOrdem.AGUARDANDO_PAGAMENTO, "Aguardando pagamento"),
        (StatusOrdem.AGUARDANDO_EXECUCAO, "Aguardando execução"),
        (StatusOrdem.EM_EXECUCAO, "Em execução"),
        (StatusOrdem.FINALIZADA, "Finalizada"),
        (StatusOrdem.ENTREGUE, "Entregue"),
        (StatusOrdem.CANCELADA, "Cancelada"),
    ],
)
def test_rotulo_por_status(status: StatusOrdem, rotulo: str) -> None:
    assert situacao_de(status) == rotulo


def test_situacao_cobre_todos_os_status() -> None:
    # Guard de drift: status novo sem rotulo quebraria a serializacao.
    assert set(_SITUACAO_POR_STATUS) == set(StatusOrdem)
