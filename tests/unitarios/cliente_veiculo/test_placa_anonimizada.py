from __future__ import annotations

from uuid import UUID, uuid4

from src.cliente_veiculo.dominio.placa_anonimizada import (
    PREFIXO_DO_TOMBSTONE,
    PlacaAnonimizada,
    tombstone_da_placa,
)
from src.compartilhado.dominio.placa import Placa


class TestPlacaAnonimizada:
    def test_valor_e_sentinela_sem_pii(self) -> None:
        p = PlacaAnonimizada(veiculo_id=uuid4())
        assert p.valor == "ANONIMIZADO"

    def test_mascarado_devolve_sentinela(self) -> None:
        # Paridade de contrato com Placa.mascarado(): eventos/logs mascaram a
        # placa incondicionalmente, inclusive para veiculos ja anonimizados.
        p = PlacaAnonimizada(veiculo_id=uuid4())
        assert p.mascarado() == "ANONIMIZADO"

    def test_nao_e_placa_real(self) -> None:
        p = PlacaAnonimizada(veiculo_id=uuid4())
        assert not isinstance(p, Placa)

    def test_repr_nao_vaza_placa(self) -> None:
        vid = uuid4()
        p = PlacaAnonimizada(veiculo_id=vid)
        assert "ANONIMIZADO" not in repr(p)  # repr expoe so o veiculo_id (nao PII)
        assert str(vid) in repr(p)

    def test_igualdade_por_veiculo_id(self) -> None:
        vid = uuid4()
        a, b = PlacaAnonimizada(veiculo_id=vid), PlacaAnonimizada(veiculo_id=vid)
        assert a is not b
        assert a == b
        assert a != PlacaAnonimizada(veiculo_id=uuid4())


def test_tombstone_da_placa_tem_o_formato_anonimizado_dois_pontos_e_o_id() -> None:
    # O texto da coluna `placa` e o contrato entre o erasure, o mapeamento do ORM
    # e a consulta dos veiculos a avisar: mudar o formato exige mudar os tres.
    veiculo_id = UUID("00000000-0000-0000-0000-000000000001")

    assert PREFIXO_DO_TOMBSTONE == "ANONIMIZADO:"
    assert (
        tombstone_da_placa(veiculo_id)
        == "ANONIMIZADO:00000000-0000-0000-0000-000000000001"
    )
