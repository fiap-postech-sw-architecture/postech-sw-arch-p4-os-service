from __future__ import annotations

import io
import json
import logging
import time
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
import structlog
import uvicorn

from src.compartilhado.infraestrutura.logging import (
    _JWT_PATTERN,
    _PEM_PRIVADA_PATTERN,
    adicionar_versao_imagem,
    configurar_logging,
    redigir_pii_erro,
    scrub_pii,
)
from tests.chaves_jwt import jwt_service

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


class TestLogging:
    def test_scrub_cpf(self) -> None:
        event_dict: dict[str, object] = {"event": "CPF 123.456.789-00"}
        result = scrub_pii(None, "info", event_dict)
        assert "123.456.789-00" not in str(result["event"])
        assert "***" in str(result["event"])

    def test_scrub_cnpj(self) -> None:
        event_dict: dict[str, object] = {"event": "CNPJ 12.345.678/0001-90"}
        result = scrub_pii(None, "info", event_dict)
        assert "12.345.678/0001-90" not in str(result["event"])

    def test_mascara_cnpj_preserva_o_terceiro_grupo_correto(self) -> None:
        # NN.NNN.NNN/NNNN-NN: o grupo visivel da mascara e o TERCEIRO
        # (digitos raw[5:8] = "678"), nao um fatiamento deslocado ("567").
        event_dict: dict[str, object] = {"event": "CNPJ 12.345.678/0001-90"}
        result = scrub_pii(None, "info", event_dict)
        assert "**.***.678/****-**" in str(result["event"])

    def test_scrub_email(self) -> None:
        event_dict: dict[str, object] = {"event": "Email user@example.com"}
        result = scrub_pii(None, "info", event_dict)
        assert "user@example.com" not in str(result["event"])
        assert "u***@example.com" in str(result["event"])

    def test_scrub_non_string_values(self) -> None:
        event_dict: dict[str, object] = {"count": 42}
        result = scrub_pii(None, "info", event_dict)
        assert result["count"] == 42

    def test_configurar_logging(self) -> None:
        configurar_logging()

    def test_configurar_logging_deixa_do_pika_so_o_error(self) -> None:
        # O WARNING do pika na devolucao de uma mensagem traz o corpo dela.
        logging.getLogger("pika").setLevel(logging.NOTSET)

        configurar_logging()

        pika = logging.getLogger("pika.adapters.blocking_connection")
        assert not pika.isEnabledFor(logging.WARNING)
        assert pika.isEnabledFor(logging.ERROR)

    def test_adicionar_versao_imagem_injeta_git_sha_e_date(self) -> None:
        # Defaults vem do env do processo (PYTSTOP_GIT_SHA/DATE); em
        # tests sem essas vars setadas, valor esperado e "unknown".
        result = adicionar_versao_imagem(None, "info", {"event": "boot"})
        assert "git_sha" in result
        assert "git_date" in result

    def test_adicionar_versao_imagem_nao_sobrescreve_explicit(self) -> None:
        result = adicionar_versao_imagem(
            None, "info", {"event": "x", "git_sha": "explicit"}
        )
        assert result["git_sha"] == "explicit"

    def test_scrub_cpf_sem_pontuacao(self) -> None:
        event_dict: dict[str, object] = {"event": "CPF 12345678900"}
        result = scrub_pii(None, "info", event_dict)
        assert "12345678900" not in str(result["event"])

    def test_scrub_cnpj_sem_pontuacao(self) -> None:
        event_dict: dict[str, object] = {"event": "CNPJ 12345678000190"}
        result = scrub_pii(None, "info", event_dict)
        assert "12345678000190" not in str(result["event"])

    @pytest.mark.parametrize(
        "cnpj",
        [
            pytest.param("12.ABC.345/01DE-35", id="formatado"),
            pytest.param("12ABC34501DE35", id="sem-pontuacao"),
        ],
    )
    def test_scrub_cnpj_alfanumerico(self, cnpj: str) -> None:
        event_dict: dict[str, object] = {"event": f"CNPJ {cnpj}"}
        result = scrub_pii(None, "info", event_dict)
        assert result["event"] == "CNPJ **.***.345/****-**"

    def test_scrub_nao_mascara_endereco_de_memoria_de_repr(self) -> None:
        # Os repr de objeto trazem 0x + 12 hexa minusculos: nao e CNPJ.
        evento = "<Sessao object at 0x7f3a9c2b1d10>"
        event_dict: dict[str, object] = {"event": evento}
        assert scrub_pii(None, "info", event_dict)["event"] == evento

    def test_scrub_recursivo_em_dict_aninhado(self) -> None:
        event_dict: dict[str, object] = {
            "payload": {
                "cliente": {
                    "cpf": "123.456.789-00",
                    "email": "joao@example.com",
                }
            }
        }
        result = scrub_pii(None, "info", event_dict)
        cliente = result["payload"]["cliente"]  # type: ignore[index]
        assert "123.456.789-00" not in str(cliente["cpf"])
        assert "joao@example.com" not in str(cliente["email"])

    def test_scrub_recursivo_em_lista(self) -> None:
        event_dict: dict[str, object] = {
            "itens": [
                {"cpf": "123.456.789-00"},
                "CNPJ 12.345.678/0001-90",
            ]
        }
        result = scrub_pii(None, "info", event_dict)
        itens = result["itens"]
        assert "123.456.789-00" not in str(itens)
        assert "12.345.678/0001-90" not in str(itens)

    def test_scrub_recursivo_em_tupla(self) -> None:
        event_dict: dict[str, object] = {
            "pair": ("user@example.com", "other-value"),
        }
        result = scrub_pii(None, "info", event_dict)
        pair = result["pair"]
        assert "user@example.com" not in str(pair)
        assert "u***@example.com" in str(pair)

    def test_scrub_recursivo_em_set_e_frozenset(self) -> None:
        event_dict: dict[str, object] = {
            "emails": {"user@example.com"},
            "docs": frozenset({"CPF 123.456.789-00"}),
        }
        result = scrub_pii(None, "info", event_dict)
        assert isinstance(result["emails"], set)
        assert isinstance(result["docs"], frozenset)
        assert "user@example.com" not in str(result["emails"])
        assert "123.456.789-00" not in str(result["docs"])

    def test_scrub_respeita_profundidade_maxima(self) -> None:
        # Deeply nested: 8 levels deep. _MAX_SCRUB_DEPTH=6 means level 7+ is skipped.
        deep: dict[str, object] = {"cpf": "123.456.789-00"}
        for _ in range(8):
            deep = {"next": deep}
        event_dict: dict[str, object] = {"root": deep}
        # Must not raise / hang; output may still contain the PII at the deepest level.
        result = scrub_pii(None, "info", event_dict)
        assert "root" in result


class TestScrubTelefone:
    """Telefone BR formatado deve ser mascarado; numero qualquer NAO (LGPD)."""

    @pytest.mark.parametrize(
        "telefone",
        [
            "(11) 99999-0000",
            "(11) 9999-0000",
            "+55 11 99999-0000",
            "+55 (11) 99999-0000",
            "11 99999-0000",
            # Sem espaco apos o DDD (p3 #99): separador agora e opcional.
            "(11)99999-0000",
            "1199999-0000",
            # +55 com numero corrido, sem hifen local (p3 #99).
            "+5511999990000",
            "+55 11999990000",
        ],
    )
    def test_telefone_formatado_mascarado(self, telefone: str) -> None:
        event_dict: dict[str, object] = {"event": f"contato {telefone}"}
        result = scrub_pii(None, "info", event_dict)
        text = str(result["event"])
        assert telefone not in text
        assert "***" in text

    @pytest.mark.parametrize(
        "nao_telefone",
        [
            "id 12345",  # id curto
            "valor R$ 1500.00",  # preco
            "ordem 998877",  # numero de ordem
            "ano 2026",
            "porta 8000",
            "cep 12345-678",  # bloco local 5-3 nao casa o split 4-4/5-4
            "id longo 123456789012345",  # 15 digitos corridos sem +55
        ],
    )
    def test_nao_telefone_preservado(self, nao_telefone: str) -> None:
        # Guard contra falso-positivo: numeros que nao sao telefone ficam intactos.
        event_dict: dict[str, object] = {"event": nao_telefone}
        result = scrub_pii(None, "info", event_dict)
        assert str(result["event"]) == nao_telefone

    def test_telefone_11_digitos_corrido_mascarado(self) -> None:
        # 11 digitos corridos tem o shape de CPF e caem no _CPF_PATTERN --
        # mascarado por valor de qualquer forma (p3 #99). Campos NOMEADOS
        # telefone/celular/contato caem na denylist de chaves.
        event_dict: dict[str, object] = {"event": "retorno 11999990000"}
        result = scrub_pii(None, "info", event_dict)
        assert "11999990000" not in str(result["event"])

    @pytest.mark.parametrize(
        "texto",
        [
            pytest.param("tel (11) 99999-0000, ok", id="virgula-depois"),
            pytest.param("(+55 11 99999-0000)", id="entre-parenteses"),
            pytest.param("contato: 11 99999-0000.", id="ponto-final"),
            pytest.param("Key (contato)=(11 99999-0000) existe", id="detalhe-do-banco"),
        ],
    )
    def test_telefone_cercado_de_pontuacao_mascarado(self, texto: str) -> None:
        result = scrub_pii(None, "info", {"event": texto})
        assert "99999-0000" not in str(result["event"])
        assert "***" in str(result["event"])

    @pytest.mark.parametrize(
        "texto",
        [
            pytest.param("55-11-99999-0000", id="depois-de-hifen"),
            pytest.param("tel-11 99999-0000", id="rotulo-com-hifen"),
            pytest.param("cel_11 99999-0000", id="depois-de-underscore"),
            pytest.param("fone(11)99999-0000", id="parenteses-depois-de-letra"),
            pytest.param("fone+55 11 99999-0000", id="mais-depois-de-letra"),
            pytest.param("11 99999-0000abc", id="letra-depois"),
            pytest.param("tel 11 99999-0000-ramal", id="hifen-depois"),
        ],
    )
    def test_telefone_colado_a_hifen_ou_letra_continua_mascarado(
        self, texto: str
    ) -> None:
        # So o digito hexadecimal antes do numero o poupa (o UUID e feito deles).
        result = scrub_pii(None, "info", {"event": texto})
        assert "99999-0000" not in str(result["event"])
        assert "***" in str(result["event"])


# UUID v4 de verdade cujo trecho "02-3465-4237" (dd-dddd-dddd) casava com o telefone.
_UUID_COM_SPLIT_DE_TELEFONE = "732ffc02-3465-4237-a5f6-12fd4a2b3be0"


class TestScrubUuid:
    """Os ids do servico (``ordem_id``, ``request_id``, ator e ``jti``) sao UUID."""

    def test_uuid_com_trecho_dd_dddd_dddd_fica_intacto(self) -> None:
        texto = f"ordem {_UUID_COM_SPLIT_DE_TELEFONE}"
        assert scrub_pii(None, "info", {"event": texto})["event"] == texto

    def test_dez_mil_uuid4_ficam_intactos(self) -> None:
        # Antes da correcao cerca de 1,4% dos UUID v4 saiam mascarados do log.
        ids = [str(uuid4()) for _ in range(10_000)]
        mascarados = [
            valor
            for valor in ids
            if scrub_pii(None, "info", {"id": valor})["id"] != valor
        ]
        assert mascarados == []

    def test_dez_mil_uuid4_em_maiusculas_ficam_intactos(self) -> None:
        ids = [str(uuid4()).upper() for _ in range(10_000)]
        mascarados = [
            valor
            for valor in ids
            if scrub_pii(None, "info", {"id": valor})["id"] != valor
        ]
        assert mascarados == []

    @pytest.mark.parametrize(
        "uuid",
        [
            pytest.param("12345678-1234-1234-1234-123456789012", id="so-digitos"),
            pytest.param("00000000-0000-0000-0000-000000000000", id="zeros"),
            pytest.param("99999999-9999-9999-9999-999999999999", id="noves"),
        ],
    )
    def test_uuid_so_com_digitos_fica_intacto(self, uuid: str) -> None:
        # O pior caso do split 4-4: todo grupo e numerico.
        texto = f"ordem {uuid} ok"
        assert scrub_pii(None, "info", {"event": texto})["event"] == texto

    def test_ator_e_alvo_saem_intactos_no_log_json(
        self, logging_pipeline: io.StringIO
    ) -> None:
        # Pelo pipeline real: o ator da auditoria (o sub) e o ordem_id sao UUID.
        structlog.get_logger("test.uuid").info(
            "evento", ator=_UUID_COM_SPLIT_DE_TELEFONE, ordem_id=str(uuid4())
        )
        registro = json.loads(logging_pipeline.getvalue().splitlines()[-1])
        assert registro["ator"] == _UUID_COM_SPLIT_DE_TELEFONE


class TestScrubChavesSensiveis:
    """Denylist de chaves: o VALOR e mascarado pelo nome do campo, nao por regex."""

    @pytest.mark.parametrize(
        "chave",
        [
            "password",
            "senha",
            "senha_hash",
            "token",
            "secret",
            "authorization",
            "refresh_token",
            "access_token",
            "api_key",
            # Chave RSA do JWT (PEM em texto): o valor nao tem forma fixa de log.
            "jwt_private_key",
            "private_key",
            # PII sem forma detectavel por regex (p3 #99): mascara por nome.
            "telefone",
            "celular",
            "phone",
            "contato",
        ],
    )
    def test_chave_sensivel_mascara_valor(self, chave: str) -> None:
        event_dict: dict[str, object] = {chave: "super-secreto-xyz"}
        result = scrub_pii(None, "info", event_dict)
        assert "super-secreto-xyz" not in str(result[chave])
        assert result[chave] == "***"

    def test_chave_sensivel_case_insensitive(self) -> None:
        event_dict: dict[str, object] = {"Authorization": "Bearer abc.def.ghi"}
        result = scrub_pii(None, "info", event_dict)
        assert "abc.def.ghi" not in str(result["Authorization"])

    def test_chave_sensivel_aninhada(self) -> None:
        event_dict: dict[str, object] = {
            "payload": {"user": "joao", "password": "hunter2"}
        }
        result = scrub_pii(None, "info", event_dict)
        inner = result["payload"]
        assert inner["user"] == "joao"  # type: ignore[index]
        assert "hunter2" not in str(inner["password"])  # type: ignore[index]

    def test_chave_nao_sensivel_preservada(self) -> None:
        event_dict: dict[str, object] = {"username": "joao", "count": 3}
        result = scrub_pii(None, "info", event_dict)
        assert result["username"] == "joao"
        assert result["count"] == 3


_CORPO_PEM = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQCrnHu8kBQQGe7e"


def _cabecalho_pem(rotulo: str = "PRIVATE KEY") -> str:
    # Montado em tempo de execucao: a linha literal acionaria o detector de
    # chave privada do gitleaks, que olha o texto do arquivo.
    return f"-----BEGIN {rotulo}-----"


def _pem(rotulo: str = "PRIVATE KEY", *, com_fim: bool = True) -> str:
    fim = f"\n-----END {rotulo}-----" if com_fim else ""
    return f"{_cabecalho_pem(rotulo)}\n{_CORPO_PEM}\n{_CORPO_PEM}{fim}"


class TestScrubChaveEJwtEmTextoLivre:
    """Chave privada em PEM e JWT soltos no texto (mensagem, traceback, URL)."""

    @pytest.mark.parametrize(
        "rotulo",
        [
            pytest.param("PRIVATE KEY", id="pkcs8"),
            pytest.param("RSA PRIVATE KEY", id="pkcs1"),
            pytest.param("EC PRIVATE KEY", id="ec"),
            pytest.param("ENCRYPTED PRIVATE KEY", id="cifrada"),
        ],
    )
    def test_pem_privado_e_mascarado_inteiro(self, rotulo: str) -> None:
        texto = f"falha ao ler a chave: {_pem(rotulo)} (fim)"

        mascarado = scrub_pii(None, "error", {"event": texto})["event"]

        assert mascarado == "falha ao ler a chave: *** (fim)"

    def test_pem_privado_sem_o_fim_e_mascarado_ate_o_fim_do_corpo(self) -> None:
        # Mensagem de erro truncada: sem o END, o corpo base64 nao pode sobrar.
        mascarado = scrub_pii(None, "error", {"event": _pem(com_fim=False)})["event"]

        assert _CORPO_PEM not in mascarado
        assert "BEGIN" not in mascarado

    @pytest.mark.parametrize(
        "formato",
        [
            pytest.param(repr, id="repr"),
            pytest.param(json.dumps, id="json"),
        ],
    )
    def test_pem_com_quebra_de_linha_escapada_e_mascarado_inteiro(
        self, formato: Callable[[str], str]
    ) -> None:
        # No repr e no JSON a quebra de linha vira `\n` literal.
        mascarado = scrub_pii(None, "error", {"event": formato(_pem())})["event"]

        assert _CORPO_PEM not in mascarado
        assert "BEGIN" not in mascarado
        assert mascarado in ("'***'", '"***"')

    def test_pem_publico_nao_e_mascarado(self) -> None:
        # A parte publica sai no JWKS: nao e segredo.
        publico = _pem("PUBLIC KEY")
        assert scrub_pii(None, "info", {"event": publico})["event"] == publico

    def test_pem_no_traceback_sai_mascarado_pelo_pipeline(
        self, logging_pipeline: io.StringIO
    ) -> None:
        try:
            raise ValueError(f"chave invalida: {_pem()}")
        except ValueError:
            structlog.get_logger("test.pem").exception("falha")

        assert _CORPO_PEM not in logging_pipeline.getvalue()

    @pytest.mark.parametrize(
        "token",
        [
            pytest.param(
                "eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJzdWIiOiJ1In0.c2lnbmF0dXJl",
                id="rs256",
            ),
            pytest.param(
                "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ1In0.", id="alg-none-sem-assinatura"
            ),
            pytest.param(
                "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1In0.a-b_c-d_e-f_g", id="base64url"
            ),
        ],
    )
    @pytest.mark.parametrize(
        "moldura",
        [
            pytest.param("{}", id="sozinho"),
            pytest.param("Authorization: Bearer {}", id="header-no-texto"),
            pytest.param("GET /x?token={}&a=1", id="query-da-url"),
            pytest.param("refresh {} reaproveitado", id="no-meio-da-frase"),
        ],
    )
    def test_jwt_e_mascarado(self, token: str, moldura: str) -> None:
        mascarado = scrub_pii(None, "info", {"event": moldura.format(token)})["event"]

        assert mascarado == moldura.format("***")

    def test_jwt_logo_depois_de_quebra_de_linha_escapada_e_mascarado(self) -> None:
        token = jwt_service().gerar_access_token(uuid4(), "admin")

        mascarado = scrub_pii(None, "info", {"event": repr(f"token:\n{token}")})

        assert mascarado["event"] == "'token:\\n***'"

    @pytest.mark.parametrize(
        "entrada",
        [
            pytest.param("eyJ-" * 50_000, id="eyj-com-hifen"),
            pytest.param("eyJ.eyJ." * 25_000, id="eyj-com-ponto"),
            pytest.param(_cabecalho_pem() * 5_000, id="begin-repetido"),
            pytest.param(_cabecalho_pem() + "A" * 200_000, id="pem-sem-fim"),
        ],
    )
    def test_padroes_de_chave_e_jwt_nao_tem_tempo_quadratico(
        self, entrada: str
    ) -> None:
        # O scrubber roda sobre toda string do log, inclusive o path de uma
        # requisicao: o tempo quadratico (10 s ou mais nestas entradas) viraria
        # amplificacao de CPU. O teto e folgado: o tempo real e de milissegundos.
        inicio = time.perf_counter()
        _PEM_PRIVADA_PATTERN.sub("***", entrada)
        _JWT_PATTERN.sub("***", entrada)
        assert time.perf_counter() - inicio < 1.0

    def test_token_de_verdade_do_servico_e_mascarado(self) -> None:
        token = jwt_service().gerar_access_token(uuid4(), "admin")

        mascarado = scrub_pii(None, "info", {"event": f"token {token}"})["event"]

        assert mascarado == "token ***"

    @pytest.mark.parametrize(
        "texto",
        [
            pytest.param("eyJ", id="so-o-prefixo"),
            pytest.param("versao 1.2.3 e a.b.c", id="pontos-comuns"),
            pytest.param("src.compartilhado.infraestrutura.logging", id="modulo"),
            pytest.param("meyJhbGciOiJSUzI1NiJ9.e30.x", id="eyj-no-meio-da-palavra"),
        ],
    )
    def test_texto_que_nao_e_jwt_fica_intacto(self, texto: str) -> None:
        assert scrub_pii(None, "info", {"event": texto})["event"] == texto

    def test_erro_devolvido_ao_cliente_tambem_sai_sem_o_token(self) -> None:
        # redigir_pii_erro usa o mesmo mascaramento (a mensagem do 422 de
        # ValueError). Resultado exato: o teto de 200 caracteres cortaria um token
        # sem mascara e o teste passaria do mesmo jeito.
        token = jwt_service().gerar_refresh_token(uuid4())

        assert redigir_pii_erro(f"consumidor recusou {token}") == (
            "consumidor recusou ***"
        )


@pytest.fixture
def logging_pipeline() -> Iterator[io.StringIO]:
    """Configura o pipeline real e captura o stdout do root logger.

    Restaura o estado anterior do structlog/root logger no teardown para
    nao vazar configuracao entre testes (configurar_logging instala um
    handler no root).
    """
    root = logging.getLogger()
    handlers_anteriores = root.handlers[:]
    nivel_anterior = root.level
    config_anterior = structlog.get_config()

    buffer = io.StringIO()
    configurar_logging(stream=buffer)
    try:
        yield buffer
    finally:
        root.handlers = handlers_anteriores
        root.setLevel(nivel_anterior)
        structlog.configure(**config_anterior)


class TestPipelineMascaraTraceback:
    """O traceback (chave `exception`) deve sair mascarado pelo pipeline real.

    Cobre o bug central da p3 #86: `scrub_pii` rodava ANTES de
    `format_exc_info`, entao a chave `exception` (montada por format_exc_info)
    escapava do mascaramento. Apos o reorder, o traceback e mascarado.
    """

    def test_excecao_com_pii_no_traceback_structlog(
        self, logging_pipeline: io.StringIO
    ) -> None:
        log = structlog.get_logger("test.pipeline")
        try:
            raise RuntimeError(
                "cliente CPF 123.456.789-00 email joao@example.com tel (11) 99999-0000"
            )
        except RuntimeError:
            log.exception("falha no processamento")

        saida = logging_pipeline.getvalue()
        assert saida, "pipeline nao emitiu nada"
        # A chave exception precisa existir (format_exc_info rodou)...
        registro = json.loads(saida.strip().splitlines()[-1])
        assert "exception" in registro
        # ...e o traceback nao pode conter PII crua.
        assert "123.456.789-00" not in saida
        assert "joao@example.com" not in saida
        assert "(11) 99999-0000" not in saida

    def test_excecao_com_pii_no_traceback_stdlib(
        self, logging_pipeline: io.StringIO
    ) -> None:
        # Caminho do handler 500 (error_handler.py): logger STDLIB, nao structlog.
        # Deve passar pelo ProcessorFormatter (foreign_pre_chain) e ser scrubado.
        stdlogger = logging.getLogger("test.stdlib.handler500")
        try:
            raise ValueError("documento 987.654.321-00 contato +55 11 98888-7777")
        except ValueError:
            stdlogger.exception("Erro interno (request_id=abc)")

        saida = logging_pipeline.getvalue()
        assert saida, "pipeline stdlib nao emitiu nada"
        assert "987.654.321-00" not in saida
        assert "+55 11 98888-7777" not in saida
        # Confirma que o traceback foi de fato renderizado (nao so a mensagem).
        registro = json.loads(saida.strip().splitlines()[-1])
        assert "exception" in registro

    def test_stdlib_log_simples_e_scrubado(self, logging_pipeline: io.StringIO) -> None:
        # Log stdlib sem excecao (ex.: uma linha do uvicorn) tambem e scrubado.
        logging.getLogger("uvicorn.error").warning(
            "request de joao@example.com cpf 111.222.333-44"
        )
        saida = logging_pipeline.getvalue()
        assert "joao@example.com" not in saida
        assert "111.222.333-44" not in saida

    def test_uvicorn_loggers_religados_ao_root(self) -> None:
        # uvicorn instala handler proprio + propagate=False; configurar_logging
        # deve limpar o handler cru e religar propagate para o scrubber do root.
        root = logging.getLogger()
        handlers_anteriores = root.handlers[:]
        nivel_anterior = root.level
        config_anterior = structlog.get_config()

        # Simula o estado que o uvicorn deixa: handler proprio + propagate desligado.
        acc = logging.getLogger("uvicorn.access")
        handlers_acc_anteriores = acc.handlers[:]
        propagate_acc_anterior = acc.propagate
        handler_cru = logging.StreamHandler(io.StringIO())
        acc.handlers = [handler_cru]
        acc.propagate = False

        buffer = io.StringIO()
        try:
            configurar_logging(stream=buffer)
            # O handler cru do uvicorn foi removido e propagate religado.
            assert handler_cru not in acc.handlers
            assert acc.handlers == []
            assert acc.propagate is True

            acc.info("GET /clientes?cpf=123.456.789-00")
            saida = buffer.getvalue()
            # Saiu pelo scrubber do root (e nao pelo handler cru do uvicorn).
            assert "123.456.789-00" not in saida
            assert handler_cru.stream.getvalue() == ""  # type: ignore[attr-defined]
        finally:
            root.handlers = handlers_anteriores
            root.setLevel(nivel_anterior)
            acc.handlers = handlers_acc_anteriores
            acc.propagate = propagate_acc_anterior
            structlog.configure(**config_anterior)


@pytest.fixture
def buffer_com_uvicorn_restaurado() -> Iterator[io.StringIO]:
    """Buffer para o ``configurar_logging``; no fim, desfaz o que ele e o uvicorn mexem.

    O ``uvicorn.Config`` troca handlers e ``propagate`` de tres loggers globais, e
    o ``configurar_logging`` troca o handler do root: nada disso pode vazar.
    """
    nomes = ("uvicorn", "uvicorn.error", "uvicorn.access")
    root = logging.getLogger()
    handlers_anteriores, nivel_anterior = root.handlers[:], root.level
    config_anterior = structlog.get_config()
    estado = {
        nome: (
            logging.getLogger(nome).handlers[:],
            logging.getLogger(nome).propagate,
            logging.getLogger(nome).level,
        )
        for nome in nomes
    }
    yield io.StringIO()
    root.handlers = handlers_anteriores
    root.setLevel(nivel_anterior)
    structlog.configure(**config_anterior)
    for nome, (handlers, propagate, nivel) in estado.items():
        logger = logging.getLogger(nome)
        logger.handlers = handlers
        logger.propagate = propagate
        logger.setLevel(nivel)


class TestLoggersDoUvicorn:
    """O uvicorn monta os loggers antes de importar o app (``--no-access-log``)."""

    def test_sem_access_log_o_uvicorn_continua_sem_access_log(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        # O uvicorn deixa `uvicorn.access` sem handler e sem propagar e le o
        # `hasHandlers()` a cada conexao: religar a propagacao ao root traria de
        # volta a linha de acesso, mesmo com a flag.
        uvicorn.Config("src.main:app", access_log=False)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        acesso = logging.getLogger("uvicorn.access")
        assert not acesso.hasHandlers()
        acesso.info('127.0.0.1:5000 - "GET /api/v1/saude HTTP/1.1" 200')
        assert buffer_com_uvicorn_restaurado.getvalue() == ""

    def test_o_resto_do_uvicorn_sai_em_json_com_o_access_log_desligado(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        uvicorn.Config("src.main:app", access_log=False)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        logging.getLogger("uvicorn.error").info("Started server process [1]")
        registro = json.loads(buffer_com_uvicorn_restaurado.getvalue())
        assert (registro["logger"], registro["event"]) == (
            "uvicorn.error",
            "Started server process [1]",
        )

    def test_com_access_log_ligado_o_acesso_sai_em_json_e_scrubado(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        uvicorn.Config("src.main:app", access_log=True)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        logging.getLogger("uvicorn.access").info("GET /x?email=joao@example.com")
        saida = buffer_com_uvicorn_restaurado.getvalue()
        assert json.loads(saida)["logger"] == "uvicorn.access"
        assert "joao@example.com" not in saida

    def test_configurar_duas_vezes_mantem_o_access_log_ligado(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        # A fabrica do app roda mais de uma vez no mesmo processo (testes, reload):
        # a segunda chamada ve o estado deixado pela primeira.
        uvicorn.Config("src.main:app", access_log=True)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        logging.getLogger("uvicorn.access").info("GET /saude")
        assert json.loads(buffer_com_uvicorn_restaurado.getvalue())["logger"] == (
            "uvicorn.access"
        )

    def test_configurar_duas_vezes_mantem_o_access_log_desligado(
        self, buffer_com_uvicorn_restaurado: io.StringIO
    ) -> None:
        uvicorn.Config("src.main:app", access_log=False)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)
        configurar_logging(stream=buffer_com_uvicorn_restaurado)

        assert not logging.getLogger("uvicorn.access").hasHandlers()


def test_log_dentro_de_um_span_leva_trace_id_e_span_id() -> None:
    from src.compartilhado.infraestrutura.logging import adicionar_contexto_de_trace
    from tests.rastreamento import Rastreador

    rastreador = Rastreador()
    with rastreador.tracer.start_as_current_span("process X") as span:
        dentro = adicionar_contexto_de_trace(None, "info", {"event": "x"})
    fora = adicionar_contexto_de_trace(None, "info", {"event": "y"})

    contexto = span.get_span_context()
    assert dentro == {
        "event": "x",
        "trace_id": f"{contexto.trace_id:032x}",
        "span_id": f"{contexto.span_id:016x}",
    }
    assert fora == {"event": "y"}
