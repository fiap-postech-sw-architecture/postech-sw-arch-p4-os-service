from __future__ import annotations

import logging
import os
import re
import sys
from typing import TYPE_CHECKING, Any

import structlog
from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import MutableMapping
    from typing import TextIO

# git_sha/git_date sao injetadas em build args -> ENV pelas pipelines
# (Makefile + Dockerfiles). Lidas uma vez no import e adicionadas a todo
# log structlog via processor -- assim ficam visiveis mesmo apos
# `clear_contextvars()` que o SecurityHeadersMiddleware faz a cada
# request. `[:12]` casa com o curto exibido no banner de boot.
_GIT_SHA = os.environ.get("PYTSTOP_GIT_SHA", "unknown")[:12]
_GIT_DATE = os.environ.get("PYTSTOP_GIT_DATE", "unknown")


def adicionar_versao_imagem(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Injeta git_sha/git_date em todo evento (sem sobrescrever explicit)."""
    event_dict.setdefault("git_sha", _GIT_SHA)
    event_dict.setdefault("git_date", _GIT_DATE)
    return event_dict


def adicionar_contexto_de_trace(
    _logger: object,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Injeta ``trace_id``/``span_id`` do span OpenTelemetry corrente (ADR-043).

    Sem span valido (laco ocioso, teste, OTel da API desligado) nada muda: a
    linha de log so ganha os ids quando ha trace para correlacionar.
    """
    contexto = trace.get_current_span().get_span_context()
    if contexto.is_valid:
        event_dict.setdefault("trace_id", format(contexto.trace_id, "032x"))
        event_dict.setdefault("span_id", format(contexto.span_id, "016x"))
    return event_dict


_CPF_PATTERN = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")
# CNPJ numerico ou alfanumerico (IN RFB 2.229/2024): letras maiusculas e digitos
# nas 12 primeiras posicoes, so digitos nas 2 ultimas. Sem minusculas de
# proposito: `normalizar_cnpj` entrega maiusculas, e minusculas tambem casariam
# com enderecos de memoria dos reprs (`0x7f3a9c2b1d10`).
_CNPJ_PATTERN = re.compile(
    r"\b[0-9A-Z]{2}\.?[0-9A-Z]{3}\.?[0-9A-Z]{3}/?[0-9A-Z]{4}-?\d{2}\b"
)
# Dominio casado label a label (`.` fora da classe) -- correcao do hotspot S5852
# (backtracking polinomial): o scrubber roda sobre o event_dict inteiro,
# tracebacks inclusos, sem cap de tamanho.
# O `[A-Z|a-z]` antigo ainda embutia um `|` literal na classe do TLD.
_EMAIL_PATTERN = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)

# Chave privada em PEM solta no texto (mensagem de erro, traceback, repr, JSON):
# do BEGIN ao END ou, se a mensagem foi truncada, ate onde o corpo base64
# termina. O corpo aceita a quebra de linha real e a escapada (`\n` literal, como
# no `repr` e no `json.dumps`). A parte publica (`PUBLIC KEY`) sai no JWKS e nao e
# segredo. O quantificador possessivo (`*+`) evita o backtracking: o scrubber roda
# sobre qualquer string do log.
_PEM_PRIVADA_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----(?:[A-Za-z0-9+/=\s]|\\[nrt])*+"
    r"(?:-----END [A-Z ]{0,20}PRIVATE KEY-----)?"
)
# JWT solto no texto (header Authorization, query da URL, mensagem de erro):
# cabecalho e corpo comecam em `eyJ` (o JSON `{"` em base64url) e a assinatura
# pode ser vazia (alg=none). Possessivo e so a partir do inicio de uma sequencia
# de caracteres base64url (o lookbehind): sem isso, uma linha longa de `eyJ-eyJ-`
# faria o scrubber reler o resto da linha a cada `eyJ` (tempo quadratico). O
# token logo depois de uma quebra de linha escapada (`\n` literal) tambem conta
# como inicio.
_JWT_PATTERN = re.compile(
    r"(?:(?<![A-Za-z0-9_-])|(?<=\\[nrt]))"
    r"eyJ[A-Za-z0-9_-]++\.[A-Za-z0-9_-]++\.[A-Za-z0-9_-]*+"
)

# Telefone BR: duas formas estruturais, escolhidas para nao gerar falso-positivo
# em precos (`1500.00`), ids (`12345`), anos (`2026`), portas (`8000`) e CEPs
# (`12345-678`) -- nenhum deles tem o split `\d{4,5}-\d{4}` nem prefixo `+55`:
#   1. DDD (com/sem parenteses) + separador OPCIONAL + bloco local com hifen
#      4-4/5-4 -- cobre `(11)99999-0000` e `1199999-0000` alem dos formatados.
#      Colateral aceito (direcao LGPD-safe): ids numericos hifenizados com
#      shape 6+4 (`123456-7890`) tambem sao mascarados.
#   2. `+55` seguido de 10-11 digitos corridos -- cobre `+5511999990000` (o
#      prefixo de pais e estrutura suficiente; nada legitimo em log tem essa
#      forma). `+55 11999990000` (com espaco) e `11999990000` (sem nada) tem
#      11 digitos corridos com shape de CPF e caem no _CPF_PATTERN acima
#      antes desta regex; campos NOMEADOS telefone/celular/contato sao
#      mascarados pela denylist abaixo.
# O numero nao pode comecar colado a um digito hexadecimal: os ids do servico
# (`ordem_id`, `request_id`, `jti` e o ator da auditoria, o `sub`) sao UUID, e o
# v4 traz entre os grupos trechos `dd-dddd-dddd` (`732ffc02-3465-4237-...`) que o
# split 4-4 casaria (cerca de 1,4% dos UUID saiam mascarados). Em todo UUID o
# `dd` desse trecho vem depois de outro digito hexadecimal, entao o lookbehind o
# barra sem deixar de mascarar o telefone colado a hifen, `_` ou a letra que nao
# e hexadecimal (`tel-11 99999-0000`). Telefone que comeca em `(` ou `+` dispensa
# o lookbehind, porque UUID nao tem nenhum dos dois (`fone(11)99999-0000`).
_TELEFONE_PATTERN = re.compile(
    r"(?:(?<![0-9A-Fa-f])|(?=[(+]))"  # nao colado a digito hexadecimal (UUID)
    r"(?:"
    r"(?:\+55[\s.-]?)?"  # codigo do pais opcional
    r"(?:\(\d{2}\)|\d{2})"  # DDD com ou sem parenteses
    r"[\s.-]?"  # separador opcional entre DDD e numero
    r"9?\d{4}-\d{4}"  # 8 ou 9 digitos com hifen 4-4/5-4
    r"|"
    r"\+55[\s.-]?\d{10,11}"  # +55 com numero corrido (sem hifen local)
    r")"
    r"(?!\d)"  # nao seguido de digito (evita capturar parte de numero maior)
)

# Denylist de chaves: quando o NOME do campo indica segredo ou PII, o valor
# inteiro e mascarado -- independente de casar regex. Cobre credenciais sem
# forma fixa (tokens, segredos) e PII cujo valor pode nao ter estrutura
# detectavel (telefone sem formatacao, contato em texto livre) -- p3 #99.
_CHAVES_SENSIVEIS = frozenset(
    {
        "password",
        "senha",
        "senha_hash",
        "token",
        "secret",
        "authorization",
        "refresh_token",
        "access_token",
        "api_key",
        "jwt_private_key",
        "private_key",
        "telefone",
        "celular",
        "phone",
        "contato",
    }
)

_MASCARA = "***"

# Loggers que o uvicorn configura com handler proprio + `propagate=False`.
# `configurar_logging` os religa ao root para passarem pelo scrubber (p3 #86).
_LOGGER_DE_ACESSO = "uvicorn.access"
_LOGGERS_UVICORN = ("uvicorn", "uvicorn.error", _LOGGER_DE_ACESSO)

# Cap on recursion depth when scrubbing nested structures. Guards against
# pathological or cyclic structured log payloads without sacrificing coverage
# of realistic nesting (events usually nest 2-3 levels deep at most).
_MAX_SCRUB_DEPTH = 6


def _mask_cpf(match: re.Match[str]) -> str:
    raw = match.group().replace(".", "").replace("-", "")
    return f"***.***.{raw[6:9]}-**"


def _mask_cnpj(match: re.Match[str]) -> str:
    # Digitos do CNPJ NN.NNN.NNN/NNNN-NN: o terceiro grupo e raw[5:8].
    raw = match.group().replace(".", "").replace("/", "").replace("-", "")
    return f"**.***.{raw[5:8]}/****-**"


def _mask_email(match: re.Match[str]) -> str:
    email = match.group()
    local, domain = email.split("@", 1)
    masked_local = local[0] + "***" if local else "***"
    return f"{masked_local}@{domain}"


def _mask_string(value: str) -> str:
    # Chave e token primeiro: o corpo em base64 pode ter sequencias de digitos
    # que os padroes de documento e telefone achariam.
    value = _PEM_PRIVADA_PATTERN.sub(_MASCARA, value)
    value = _JWT_PATTERN.sub(_MASCARA, value)
    value = _CPF_PATTERN.sub(_mask_cpf, value)
    value = _CNPJ_PATTERN.sub(_mask_cnpj, value)
    value = _EMAIL_PATTERN.sub(_mask_email, value)
    return _TELEFONE_PATTERN.sub(_MASCARA, value)


def _chave_sensivel(key: Any) -> bool:  # noqa: ANN401  # chaves podem nao ser str
    return isinstance(key, str) and key.lower() in _CHAVES_SENSIVEIS


def _scrub_value(value: Any, depth: int) -> Any:  # noqa: ANN401
    # Recursivamente normaliza qualquer estrutura JSON-like; o tipo de entrada
    # nao e conhecivel a priori, dai o Any.
    if depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, str):
        return _mask_string(value)
    if isinstance(value, dict):
        # Mascara o valor inteiro quando a chave esta na denylist; senao desce.
        return {
            k: (_MASCARA if _chave_sensivel(k) else _scrub_value(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(item, depth + 1) for item in value]
    if isinstance(value, tuple):
        return tuple(_scrub_value(item, depth + 1) for item in value)
    if isinstance(value, (set, frozenset)):
        limpo = {_scrub_value(item, depth + 1) for item in value}
        return limpo if isinstance(value, set) else frozenset(limpo)
    return value


def scrub_pii(
    _logger: Any,  # noqa: ANN401  # structlog bound logger; nao inspecionado
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Structlog processor que mascara PII e segredos em todo o event_dict.

    Mascara CPF, CNPJ, email, telefone BR formatado, chave privada em PEM e JWT
    por regex de VALOR; e mascara o valor inteiro quando o NOME do campo esta na
    denylist `_CHAVES_SENSIVEIS` (password/token/secret/jwt_private_key/...).
    Percorre recursivamente strings, dicts, listas e
    tuplas ate `_MAX_SCRUB_DEPTH` para pegar PII em payloads estruturados. Aplicado
    automaticamente pelo pipeline de logging (inclusive na chave `exception` do
    traceback, que `format_exc_info` monta ANTES deste processor) para impedir
    vazamento de PII em logs -- structlog e stdlib (LGPD).
    """
    for key, value in event_dict.items():
        event_dict[key] = _MASCARA if _chave_sensivel(key) else _scrub_value(value, 0)
    return event_dict


_MAX_ERRO_LEN = 200


def redigir_pii_erro(erro: str) -> str:
    """Remove PII e segredos (CPF, CNPJ, e-mail, telefone, chave PEM e JWT) de erros.

    Complementa o scrubber de log (``scrub_pii``): aquele atua no pipeline de
    structlog em memoria; esta funcao atua nas strings de erro que saem do
    processo por outro caminho, como a mensagem do 422 de ``ValueError``
    devolvida ao cliente (LGPD: o scrubber de log nao a alcanca).

    Trunca o resultado em ``_MAX_ERRO_LEN`` caracteres para evitar que
    mensagens de excepcao excessivamente longas ocupem espaco excessivo.
    """
    redacted = _mask_string(erro)
    if len(redacted) > _MAX_ERRO_LEN:
        redacted = redacted[:_MAX_ERRO_LEN] + "…"
    return redacted


# Cadeia COMPARTILHADA entre logs structlog e logs stdlib estrangeiros (uvicorn,
# bibliotecas, handler 500). ORDEM CRITICA (p3 #86): `format_exc_info` monta a
# chave `exception` a partir do `exc_info` e DEVE vir ANTES de `scrub_pii`, senao
# o traceback (com possivel PII no repr da excecao) escapa do mascaramento.
# `StackInfoRenderer` (stack_info -> string) tambem precede o scrub, que entao
# mascara ambas as strings. Nenhum renderer final aqui: o `ProcessorFormatter`
# (abaixo) renderiza para JSON tanto os logs structlog quanto os stdlib.
def _cadeia_compartilhada() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        adicionar_contexto_de_trace,
        adicionar_versao_imagem,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        scrub_pii,
    ]


def configurar_logging(stream: TextIO | None = None) -> None:
    """Configura structlog + roteia o logging stdlib pelo mesmo scrubber de PII.

    JSON output, timestamps ISO e scrub automatico de PII/segredos. O
    ``ProcessorFormatter`` instalado no root logger faz com que TODO log stdlib
    (handler 500 em ``error_handler.py``, ``exc_info`` do ValueError handler,
    access/error logs do uvicorn, logs de bibliotecas) passe pela
    ``_cadeia_compartilhada`` -- inclusive ``scrub_pii`` -- via ``foreign_pre_chain``,
    sem reprocessar os logs ja-structlog (estes pulam o pre-chain). Fecha a brecha
    LGPD da p3 #86: traceback cru com PII fora do pipeline do structlog.

    ``stream`` permite direcionar a saida (default ``sys.stdout``); usado em testes
    para capturar o output renderizado.
    """
    compartilhada = _cadeia_compartilhada()
    structlog.configure(
        processors=[
            *compartilhada,
            # Handoff para o ProcessorFormatter do root handler (nao renderiza aqui).
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        # Logs estrangeiros (stdlib) passam por esta cadeia ANTES da renderizacao;
        # logs ja-structlog ja a percorreram e a pulam (sem duplo processamento).
        foreign_pre_chain=compartilhada,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Substitui handlers existentes (ex.: o basicConfig do relay ou um handler
    # de uma chamada anterior) para garantir que o scrubber seja o unico caminho
    # de saida -- idempotente em warm restarts/testes e sem handler cru remanescente.
    root.handlers = [handler]
    if root.level == logging.NOTSET or root.level > logging.INFO:
        root.setLevel(logging.INFO)

    # uvicorn (lancado por CLI no container) instala os PROPRIOS handlers nos
    # loggers `uvicorn`/`uvicorn.access` com `propagate=False` -- seus logs (inclui
    # access logs, que podem trazer PII em path/query) NAO chegariam ao handler de
    # scrub do root. `configurar_logging` roda na fabrica do app, que o uvicorn
    # importa DEPOIS de montar seus loggers e antes da primeira linha do servidor;
    # aqui removemos os handlers crus de uvicorn e religamos `propagate=True` para
    # que tudo flua pelo ProcessorFormatter do root (scrubado, JSON unico).
    # Idempotente. p3 #86.
    for nome in _LOGGERS_UVICORN:
        uvlog = logging.getLogger(nome)
        if nome == _LOGGER_DE_ACESSO and not uvlog.handlers and not uvlog.propagate:
            # `--no-access-log` deixa o logger assim, e o uvicorn decide por
            # `hasHandlers()`, a cada conexao, se escreve o acesso. Religar a
            # propagacao o ligaria de novo ao handler do root: a linha de acesso
            # voltaria mesmo com a flag.
            continue
        uvlog.handlers = []
        uvlog.propagate = True
