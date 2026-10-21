"""O ``SmtpEmailAdapter``: o que vai ao servidor e como a falha sai (ADR-036)."""

from __future__ import annotations

import smtplib
from typing import Any, ClassVar

import pytest

from src.ordem_servico.aplicacao.ports import FalhaEnvioEmailError
from src.ordem_servico.infraestrutura.email_adapter import SmtpEmailAdapter


class _SmtpFalso:
    instancias: ClassVar[list[_SmtpFalso]] = []

    def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.mensagens: list[Any] = []
        self.fechado = False
        _SmtpFalso.instancias.append(self)

    def __enter__(self) -> _SmtpFalso:
        return self

    def __exit__(self, *args: object) -> None:
        self.fechado = True

    def send_message(self, mensagem: Any) -> None:
        self.mensagens.append(mensagem)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_SmtpFalso]:
    _SmtpFalso.instancias = []
    monkeypatch.setattr(smtplib, "SMTP", _SmtpFalso)
    return _SmtpFalso


def test_envia_texto_com_remetente_destinatario_assunto_e_prazo(
    smtp: type[_SmtpFalso],
) -> None:
    SmtpEmailAdapter("mailpit", 1025, "oficina@pytstop.local").enviar(
        "maria@cliente.com", "PytStop — Ordem de Serviço abc12345: Entregue", "Olá!\n"
    )

    (conexao,) = smtp.instancias
    assert (conexao.host, conexao.port) == ("mailpit", 1025)
    # Prazo finito: um servidor mudo nao prende o laco do notificador.
    assert conexao.timeout == 5
    assert conexao.fechado
    (mensagem,) = conexao.mensagens
    assert mensagem["From"] == "oficina@pytstop.local"
    assert mensagem["To"] == "maria@cliente.com"
    assert mensagem["Subject"] == "PytStop — Ordem de Serviço abc12345: Entregue"
    assert mensagem.get_content().strip() == "Olá!"


def test_configuracao_vem_do_ambiente(
    smtp: type[_SmtpFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_HOST", "mailpit.pytstop-plataforma.svc.cluster.local")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_FROM", "nao-responda@pytstop.dev")

    SmtpEmailAdapter.do_ambiente().enviar("a@b.com", "assunto", "corpo")

    (conexao,) = smtp.instancias
    assert (conexao.host, conexao.port) == (
        "mailpit.pytstop-plataforma.svc.cluster.local",
        2525,
    )
    assert conexao.mensagens[0]["From"] == "nao-responda@pytstop.dev"


@pytest.mark.parametrize("ambiente", ["development", "test"])
def test_sem_ambiente_vale_o_mailpit_local(
    smtp: type[_SmtpFalso], monkeypatch: pytest.MonkeyPatch, ambiente: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", ambiente)
    for nome in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM"):
        monkeypatch.delenv(nome, raising=False)

    SmtpEmailAdapter.do_ambiente().enviar("a@b.com", "assunto", "corpo")

    (conexao,) = smtp.instancias
    assert (conexao.host, conexao.port) == ("localhost", 1025)
    assert conexao.mensagens[0]["From"] == "oficina@pytstop.local"


@pytest.mark.parametrize("host", [None, ""])
@pytest.mark.parametrize("ambiente", ["production", "staging", "PRODUCTION"])
def test_sem_smtp_host_fora_de_development_aborta_o_boot(
    monkeypatch: pytest.MonkeyPatch, ambiente: str, host: str | None
) -> None:
    # O padrao localhost mandaria todo e-mail ao proprio pod, para o `dead`.
    monkeypatch.setenv("ENVIRONMENT", ambiente)
    if host is None:
        monkeypatch.delenv("SMTP_HOST", raising=False)
    else:
        monkeypatch.setenv("SMTP_HOST", host)

    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        SmtpEmailAdapter.do_ambiente()


def test_com_smtp_host_a_producao_sobe(
    smtp: type[_SmtpFalso], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("SMTP_HOST", "mailpit.pytstop-plataforma.svc.cluster.local")

    SmtpEmailAdapter.do_ambiente().enviar("a@b.com", "assunto", "corpo")

    (conexao,) = smtp.instancias
    assert conexao.host == "mailpit.pytstop-plataforma.svc.cluster.local"


@pytest.mark.parametrize("porta", ["nao-numerica", "0", "-1", ""])
def test_porta_invalida_aborta_o_boot_e_nao_a_entrega(
    porta: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SMTP_PORT", porta)

    with pytest.raises(RuntimeError, match="SMTP_PORT"):
        SmtpEmailAdapter.do_ambiente()


@pytest.mark.parametrize(
    "erro",
    [
        pytest.param(ConnectionRefusedError("recusou maria@cliente.com"), id="recusou"),
        pytest.param(TimeoutError("maria@cliente.com"), id="prazo"),
        pytest.param(
            smtplib.SMTPRecipientsRefused({"maria@cliente.com": (550, b"no")}),
            id="destinatario-recusado",
        ),
        pytest.param(smtplib.SMTPServerDisconnected("caiu"), id="desconectou"),
    ],
)
def test_falha_de_transporte_vira_a_excecao_da_porta_sem_a_mensagem_de_origem(
    erro: Exception, monkeypatch: pytest.MonkeyPatch
) -> None:
    def smtp_que_falha(*_args: object, **_kwargs: object) -> object:
        raise erro

    monkeypatch.setattr(smtplib, "SMTP", smtp_que_falha)

    adapter = SmtpEmailAdapter("mailpit", 1025, "oficina@pytstop.local")
    with pytest.raises(FalhaEnvioEmailError) as capturada:
        adapter.enviar("maria@cliente.com", "assunto", "corpo")

    # O endereco do destinatario nao sai na excecao nem na cadeia dela.
    assert str(capturada.value) == type(erro).__name__
    assert capturada.value.__cause__ is None
    assert "maria@cliente.com" not in repr(capturada.value)
