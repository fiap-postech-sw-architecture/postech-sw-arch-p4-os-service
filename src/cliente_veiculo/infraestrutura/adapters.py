from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import case, exists, select, text, update

from src.ordem_servico.dominio.status import ESTADOS_TERMINAIS
from src.ordem_servico.infraestrutura.mapping import (
    historico_status_ordem_table,
    ordens_de_servico_table,
)

if TYPE_CHECKING:
    from uuid import UUID

    from sqlalchemy.orm import Session

# Mesmo sentinela do erasure de clientes e veiculos (repository.py).
_ANONIMIZADO = "ANONIMIZADO"
# Na outbox a placa tem de continuar valida no schema do ``SolicitarDiagnostico``
# (``^[A-Z]{3}[0-9][A-Z0-9][0-9]{2}$``): a linha pendente ainda sera publicada, e
# o relay confere o envelope antes. Um marcador fora do padrao a mataria como
# ``dead``. A descricao e texto livre e aceita qualquer marcador.
PLACA_NA_OUTBOX = "AAA0A00"
DESCRICAO_NA_OUTBOX = "[removido]"
_SQL_ANONIMIZAR_OUTBOX = text(
    "UPDATE outbox SET envelope = jsonb_set(jsonb_set(envelope, "
    "'{dados,veiculo,placa}', to_jsonb(CAST(:placa AS text))), "
    "'{dados,descricao_problema}', to_jsonb(CAST(:descricao AS text))) "
    "WHERE envelope ->> 'tipo' = 'SolicitarDiagnostico' AND correlation_id IN "
    "(SELECT id FROM ordens_de_servico WHERE cliente_id = :cliente_id)"
)


class OrdemDeServicoSQLAlchemyAdapter:
    """Adapta consultas SQLAlchemy para o contrato `OrdemDeServicoPort`.

    Verifica OS ativas para clientes e qualquer OS vinculada a veiculos,
    evitando desativacoes/remocoes que quebrariam regras de negocio ou FKs.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def existe_os_ativa_para_cliente(self, cliente_id: UUID) -> bool:
        stmt = select(
            exists().where(
                ordens_de_servico_table.c.cliente_id == cliente_id,
                ordens_de_servico_table.c.status.notin_(ESTADOS_TERMINAIS),
            )
        )
        return bool(self._session.scalar(stmt))

    def existe_os_para_veiculo(self, veiculo_id: UUID) -> bool:
        """Verifica se existe QUALQUER OS para o veiculo (ativa ou encerrada).

        Usado antes de remover o veiculo do banco para evitar IntegrityError:
        a tabela ordens_de_servico tem FK para veiculos.id sem ON DELETE CASCADE.
        """
        stmt = select(
            exists().where(ordens_de_servico_table.c.veiculo_id == veiculo_id)
        )
        return bool(self._session.scalar(stmt))

    def anonimizar_textos_livres_do_cliente(self, cliente_id: UUID) -> None:
        """UPDATE direto da descricao e dos motivos das OS do cliente (LGPD).

        So roda sem OS ativa e com o cliente travado (guard do erasure), entao
        nenhuma transicao concorre; ``versao`` e ``atualizado_em`` sobem mesmo
        assim para o lock otimista e a leitura enxergarem a escrita. Motivo
        nulo continua nulo (so o texto existente sai).

        O UPDATE em ``historico_status_ordem`` e a unica excecao a regra de
        historico so com insercao (ADR-037): o dever de apagar dado pessoal
        (LGPD Art. 18) vale tambem para o motivo digitado na transicao. So o
        texto sai; sequencia, status, origem e instante ficam.
        """
        ordens = ordens_de_servico_table
        self._session.execute(
            update(ordens)
            .where(ordens.c.cliente_id == cliente_id)
            .values(
                descricao_problema=_ANONIMIZADO,
                motivo_cancelamento=case(
                    (ordens.c.motivo_cancelamento.is_(None), None),
                    else_=_ANONIMIZADO,
                ),
                versao=ordens.c.versao + 1,
                atualizado_em=datetime.now(UTC),
            )
        )
        historico = historico_status_ordem_table
        self._session.execute(
            update(historico)
            .where(
                historico.c.motivo.is_not(None),
                historico.c.ordem_id.in_(
                    select(ordens.c.id).where(ordens.c.cliente_id == cliente_id)
                ),
            )
            .values(motivo=_ANONIMIZADO)
        )

    def anonimizar_comandos_do_cliente_na_outbox(self, cliente_id: UUID) -> None:
        """Placa e descricao do ``SolicitarDiagnostico`` das OS do cliente (LGPD).

        As linhas saem pelo ``correlation_id``, que e o id da OS, em qualquer
        estado (pendente, entregue, ``dead``): marca e modelo do veiculo nao
        identificam o titular e ficam. O relay segue validando o envelope.
        """
        self._session.execute(
            _SQL_ANONIMIZAR_OUTBOX,
            {
                "placa": PLACA_NA_OUTBOX,
                "descricao": DESCRICAO_NA_OUTBOX,
                "cliente_id": cliente_id,
            },
        )
