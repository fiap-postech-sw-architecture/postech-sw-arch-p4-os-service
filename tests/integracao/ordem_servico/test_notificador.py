"""O ``notificador`` com PostgreSQL e Mailpit reais (RFC-004 secao 7.2; ADR-036).

Um e-mail por pedido, com o texto do modelo; SMTP fora nao perde o pedido nem
atrasa os de outra OS; cliente anonimizado fecha a linha sem envio; e nada de
endereco, nome nem link sai pelo log (LGPD).
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from sqlalchemy import text

from src.cliente_veiculo.dominio.contato import CONTATO_ANONIMIZADO
from src.ordem_servico.aplicacao.notificacoes import ModeloDeNotificacao, Notificacao
from src.ordem_servico.dominio.status import StatusOrdem
from src.ordem_servico.infraestrutura.email_adapter import SmtpEmailAdapter
from src.ordem_servico.infraestrutura.notificacoes_mapping import (
    CANAL_NOTIFICACAO,
    NotificacoesSQLAlchemy,
)
from src.ordem_servico.infraestrutura.notificador import (
    ConfigNotificador,
    Notificador,
)
from src.ordem_servico.infraestrutura.repository import (
    OrdemDeServicoSQLAlchemyRepository,
)
from tests.fabricas import CHECKOUT_URL, LINK_DECISAO, ordem_em
from tests.integracao.broker import EmSegundoPlano, esperar_ate
from tests.integracao.seed_helpers import criar_cliente_com_veiculo

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

    from sqlalchemy import Engine
    from sqlalchemy.orm import Session, sessionmaker

    from tests.integracao.mailpit import Mailpit
    from tests.rastreamento import Rastreador, Saidas

pytestmark = pytest.mark.integracao

_S = StatusOrdem
_M = ModeloDeNotificacao
_CONTATO = "Maria Silva - maria.silva@cliente.com / (11) 99999-0000"
# Atrasos de retry curtos: as cinco tentativas cabem no teste.
_ATRASOS = (0.05, 0.05, 0.05, 0.05)


def _ordem(
    session_factory: sessionmaker[Session],
    status: StatusOrdem,
    *,
    contato: str = _CONTATO,
) -> UUID:
    """Cliente com contato, OS no ``status`` (com os resumos do Billing) e commit."""
    with session_factory() as sessao:
        cliente = criar_cliente_com_veiculo(sessao, nome="Maria Silva", contato=contato)
        ordem = ordem_em(
            status, cliente_id=cliente.id, veiculo_id=cliente.veiculos[0].id
        )
        OrdemDeServicoSQLAlchemyRepository(sessao).salvar(ordem)
        sessao.commit()
        return ordem.id


def _pedir(
    session_factory: sessionmaker[Session],
    ordem_id: UUID,
    modelo: ModeloDeNotificacao,
    status: StatusOrdem,
    motivo: str | None = None,
) -> None:
    with session_factory() as sessao:
        NotificacoesSQLAlchemy(sessao).registrar(
            Notificacao(ordem_id, modelo, status, motivo)
        )
        sessao.commit()


def _situacoes(engine: Engine) -> list[str]:
    with engine.connect() as conexao:
        return [
            linha[0]
            for linha in conexao.execute(
                text("SELECT situacao FROM notificacoes ORDER BY id")
            )
        ]


def _linha(engine: Engine, ordem_id: UUID) -> Any:
    with engine.connect() as conexao:
        return conexao.execute(
            text(
                "SELECT situacao, tentativas, ultimo_erro FROM notificacoes "
                "WHERE ordem_id = :id ORDER BY id"
            ),
            {"id": ordem_id},
        ).first()


def _tentativas(engine: Engine) -> list[tuple[str, int]]:
    """(status da OS, tentativas) de cada pedido, na ordem em que foram feitos."""
    with engine.connect() as conexao:
        return [
            (linha.status_da_ordem, linha.tentativas)
            for linha in conexao.execute(
                text("SELECT status_da_ordem, tentativas FROM notificacoes ORDER BY id")
            )
        ]


def _escutando(engine: Engine) -> bool:
    """Alguma conexao ja terminou o ``LISTEN`` do canal dos pedidos."""
    with engine.connect() as conexao:
        return bool(
            conexao.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE state = 'idle' "
                    "AND query = :listen"
                ),
                {"listen": f"LISTEN {CANAL_NOTIFICACAO}"},
            ).scalar_one()
        )


def _notificador(
    engine: Engine,
    pasta: Path,
    *,
    porta_smtp: int,
    host: str = "127.0.0.1",
    atrasos: tuple[float, ...] = _ATRASOS,
) -> Notificador:
    return Notificador(
        engine=engine,
        email=SmtpEmailAdapter(host, porta_smtp, "oficina@pytstop.local"),
        config=ConfigNotificador(
            poll_s=0.1,
            lease=timedelta(seconds=30),
            atrasos_s=atrasos,
            diretorio_de_saude=pasta,
        ),
    )


def test_um_e_mail_por_pedido_com_o_texto_do_modelo(
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
) -> None:
    situacao = _ordem(session_factory, _S.EM_EXECUCAO)
    orcamento = _ordem(session_factory, _S.AGUARDANDO_APROVACAO)
    pagamento = _ordem(session_factory, _S.AGUARDANDO_PAGAMENTO)
    cancelada = _ordem(session_factory, _S.CANCELADA)
    _pedir(session_factory, situacao, _M.SITUACAO, _S.EM_EXECUCAO)
    _pedir(session_factory, orcamento, _M.ORCAMENTO, _S.AGUARDANDO_APROVACAO)
    _pedir(session_factory, pagamento, _M.PAGAMENTO, _S.AGUARDANDO_PAGAMENTO)
    _pedir(
        session_factory, cancelada, _M.CANCELAMENTO, _S.CANCELADA, "orcamento_expirado"
    )

    with EmSegundoPlano(_notificador(engine, tmp_path, porta_smtp=mailpit.porta_smtp)):
        mensagens = mailpit.esperar(4)
        esperar_ate(lambda: _situacoes(engine) == ["entregue"] * 4)

    por_assunto = {m["Subject"].split(": ", 1)[1]: m for m in mensagens}
    assert set(por_assunto) == {
        "Em execução",
        "orçamento para a sua decisão",
        "pagamento",
        "cancelada",
    }
    for mensagem in mensagens:
        assert [d["Address"] for d in mensagem["To"]] == ["maria.silva@cliente.com"]
        assert mensagem["From"]["Address"] == "oficina@pytstop.local"
    assert LINK_DECISAO in mailpit.texto(por_assunto["orçamento para a sua decisão"])
    assert CHECKOUT_URL in mailpit.texto(por_assunto["pagamento"])
    assert "Orçamento expirado sem decisão do cliente" in mailpit.texto(
        por_assunto["cancelada"]
    )
    assert "Olá, Maria Silva!" in mailpit.texto(por_assunto["Em execução"])


def test_cliente_anonimizado_fecha_a_linha_sem_enviar(
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
) -> None:
    apagado = _ordem(session_factory, _S.ENTREGUE, contato=CONTATO_ANONIMIZADO)
    sem_email = _ordem(session_factory, _S.ENTREGUE, contato="(11) 99999-0000")
    normal = _ordem(session_factory, _S.ENTREGUE)
    for ordem_id in (apagado, sem_email, normal):
        _pedir(session_factory, ordem_id, _M.SITUACAO, _S.ENTREGUE)

    with EmSegundoPlano(_notificador(engine, tmp_path, porta_smtp=mailpit.porta_smtp)):
        esperar_ate(lambda: _situacoes(engine) == ["entregue"] * 3)
        (unica,) = mailpit.esperar(1)

    # Os dois sem destinatario fecharam sem envio; so o cliente normal recebeu.
    assert [d["Address"] for d in unica["To"]] == ["maria.silva@cliente.com"]
    assert len(mailpit.mensagens()) == 1


def test_smtp_fora_nao_trava_outra_os_enquanto_o_primeiro_da_os_esta_em_backoff(
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
) -> None:
    os_a = _ordem(session_factory, _S.EM_DIAGNOSTICO)
    os_b = _ordem(session_factory, _S.EM_DIAGNOSTICO)
    # Os tres pedidos existem antes de o notificador ligar: a ordem so vale dentro
    # da OS A, e o seu segundo e-mail espera o primeiro.
    _pedir(session_factory, os_a, _M.SITUACAO, _S.EM_DIAGNOSTICO)
    _pedir(session_factory, os_a, _M.SITUACAO, _S.EM_EXECUCAO)
    _pedir(session_factory, os_b, _M.SITUACAO, _S.AGUARDANDO_EXECUCAO)
    # Porta fechada (um SMTP fora do ar) e atraso longo: o primeiro da A fica em
    # backoff durante todo o teste.
    sem_smtp = _notificador(engine, tmp_path, porta_smtp=1, atrasos=(60, 60, 60, 60))

    with EmSegundoPlano(sem_smtp):
        esperar_ate(lambda: _tentativas(engine)[2][1] == 1)

    # O da OS B foi tentado e o segundo da A ainda nao: a ordem da A o segura.
    assert _tentativas(engine) == [
        ("em_diagnostico", 1),
        ("em_execucao", 0),
        ("aguardando_execucao", 1),
    ]
    assert _situacoes(engine) == ["pendente"] * 3
    assert mailpit.mensagens() == []


def test_smtp_fora_conta_tentativa_ate_dead_e_o_log_so_leva_o_nome_da_excecao(
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
    saidas: Saidas,
    rastreador: Rastreador,
) -> None:
    os_a = _ordem(session_factory, _S.EM_DIAGNOSTICO)
    os_b = _ordem(session_factory, _S.EM_DIAGNOSTICO)
    _pedir(session_factory, os_a, _M.SITUACAO, _S.EM_DIAGNOSTICO)
    _pedir(session_factory, os_a, _M.SITUACAO, _S.EM_EXECUCAO)
    _pedir(session_factory, os_b, _M.SITUACAO, _S.EM_DIAGNOSTICO)
    # Porta fechada: conexao recusada, como um SMTP fora do ar.
    sem_smtp = _notificador(engine, tmp_path, porta_smtp=1)

    with EmSegundoPlano(sem_smtp):
        # Cada pedido gasta as suas cinco tentativas, um depois do outro na OS A.
        esperar_ate(lambda: _situacoes(engine) == ["dead", "dead", "dead"])

    primeira = _linha(engine, os_a)
    assert (primeira.situacao, primeira.tentativas) == ("dead", 5)
    # So o nome da excecao: a mensagem dela pode trazer o endereco.
    assert primeira.ultimo_erro == "falha ao enviar o e-mail (ConnectionRefusedError)"
    assert mailpit.mensagens() == []
    tudo = saidas.texto(rastreador)
    assert "notification send failed; row dead" in tudo
    for dado in ("maria.silva", "Maria Silva", "cliente.com", LINK_DECISAO):
        assert dado not in tudo


def test_smtp_de_volta_entrega_o_que_ficou_para_tras(
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
) -> None:
    ordem_id = _ordem(session_factory, _S.EM_DIAGNOSTICO)
    _pedir(session_factory, ordem_id, _M.SITUACAO, _S.EM_DIAGNOSTICO)
    # Ate o SMTP voltar, o adapter aponta para a porta fechada; depois, para o Mailpit.
    destino = {"porta": 1}

    class Chaveado:
        def enviar(self, destinatario: str, assunto: str, corpo: str) -> None:
            SmtpEmailAdapter(
                "127.0.0.1", destino["porta"], "oficina@pytstop.local"
            ).enviar(destinatario, assunto, corpo)

    notificador = Notificador(
        engine=engine,
        email=Chaveado(),
        config=ConfigNotificador(
            poll_s=0.1, atrasos_s=_ATRASOS, diretorio_de_saude=tmp_path
        ),
    )

    with EmSegundoPlano(notificador):
        esperar_ate(lambda: (_linha(engine, ordem_id).tentativas or 0) >= 1)
        assert mailpit.mensagens() == []
        destino["porta"] = mailpit.porta_smtp
        mailpit.esperar(1)
        esperar_ate(lambda: _situacoes(engine) == ["entregue"])


def test_o_notify_acorda_o_notificador_sem_esperar_o_poll(
    engine: Engine,
    session_factory: sessionmaker[Session],
    mailpit: Mailpit,
    tmp_path: Path,
) -> None:
    ordem_id = _ordem(session_factory, _S.EM_DIAGNOSTICO)
    notificador = Notificador(
        engine=engine,
        email=SmtpEmailAdapter(
            "127.0.0.1", mailpit.porta_smtp, "oficina@pytstop.local"
        ),
        # Poll de 8 s: o e-mail so sai em 3 s se o NOTIFY acordar o laco.
        config=ConfigNotificador(poll_s=8.0, diretorio_de_saude=tmp_path),
    )

    with EmSegundoPlano(notificador):
        esperar_ate(lambda: (tmp_path / "notificador-pronto").exists())
        esperar_ate(lambda: _escutando(engine))
        inicio = time.monotonic()
        _pedir(session_factory, ordem_id, _M.SITUACAO, _S.EM_DIAGNOSTICO)
        mailpit.esperar(1, prazo_s=3)
        duracao = time.monotonic() - inicio

    assert duracao < 3


def test_saude_heartbeat_pronto_e_encerramento_gracioso(
    engine: Engine, mailpit: Mailpit, tmp_path: Path
) -> None:
    notificador = _notificador(engine, tmp_path, porta_smtp=mailpit.porta_smtp)
    processo = EmSegundoPlano(notificador)

    with processo:
        esperar_ate(lambda: (tmp_path / "notificador-pronto").exists())
        assert (tmp_path / "notificador-heartbeat").exists()
        processo.parar.set()
        esperar_ate(lambda: not processo.vivo)

    assert not (tmp_path / "notificador-pronto").exists()


def test_banco_fora_tira_o_notificador_de_pronto_sem_derruba_lo(
    tmp_path: Path,
) -> None:
    from sqlalchemy import create_engine

    fora = create_engine(
        "postgresql://u:p@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    (tmp_path / "notificador-pronto").touch()
    notificador = Notificador(
        engine=fora,
        email=SmtpEmailAdapter("127.0.0.1", 1, "oficina@pytstop.local"),
        config=ConfigNotificador(poll_s=0.1, diretorio_de_saude=tmp_path),
    )
    processo = EmSegundoPlano(notificador)

    batida = tmp_path / "notificador-heartbeat"
    try:
        with processo:
            esperar_ate(lambda: not (tmp_path / "notificador-pronto").exists())
            esperar_ate(batida.exists)
            # Cada ciclo bate o heartbeat de novo: o processo atravessa varios
            # ciclos com o banco fora, sem cair e sem voltar a pronto.
            for _ in range(3):
                antes = batida.stat().st_mtime_ns
                esperar_ate(lambda antes=antes: batida.stat().st_mtime_ns > antes)
            assert processo.vivo
            assert not (tmp_path / "notificador-pronto").exists()
    finally:
        fora.dispose()
