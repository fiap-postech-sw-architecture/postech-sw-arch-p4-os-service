-- Papeis do PostgreSQL do OS Service, um por uso (ADR-042). A imagem postgres
-- roda este script uma vez, no primeiro init do volume, como o superusuario
-- postgres, que depois disso nao serve a ninguem. As senhas vem do ambiente do
-- container do banco (Secret os-postgres) pelo \getenv do psql: nunca em
-- argumento de processo.

-- O psql troca a variavel pela senha antes de enviar o comando, e o servidor a
-- recebe em claro: a sessao desliga todo log que leva o texto do comando (o
-- log_statement em all ou ddl, a linha STATEMENT de um comando que falha e o
-- log por duracao ou por amostragem).
SET log_statement = 'none';
SET log_min_error_statement = 'panic';
SET log_min_duration_statement = -1;
SET log_min_duration_sample = -1;
SET log_transaction_sample_rate = 0;

\getenv senha_dono POSTGRES_OWNER_PASSWORD
\getenv senha_app POSTGRES_APP_PASSWORD
\getenv senha_exporter POSTGRES_EXPORTER_PASSWORD

-- Dono do banco e das tabelas (DDL): o Job de migracao.
CREATE ROLE os LOGIN PASSWORD :'senha_dono';
ALTER DATABASE os OWNER TO os;
ALTER SCHEMA public OWNER TO os;

-- O PUBLIC tem CONNECT e TEMPORARY em todo banco novo: sem tirar, o GRANT
-- CONNECT abaixo nao diz nada, e qualquer papel entra no banco postgres e cria
-- tabela temporaria. Cada papel recebe so o CONNECT de que precisa.
REVOKE ALL ON DATABASE os FROM PUBLIC;
REVOKE ALL ON DATABASE postgres FROM PUBLIC;

-- Aplicacao (API, relay e consumidor): so DML nas tabelas que o dono criar.
-- As tabelas nascem depois, no Job, e por isso o privilegio vem por default
-- privileges. Elas cobrem tambem a alembic_version, lida pelo aguarda-migracao:
-- o env.py das migracoes tira a escrita dela a cada upgrade.
CREATE ROLE os_app LOGIN PASSWORD :'senha_app';
GRANT CONNECT ON DATABASE os TO os_app;
GRANT USAGE ON SCHEMA public TO os_app;
ALTER DEFAULT PRIVILEGES FOR ROLE os IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO os_app;
ALTER DEFAULT PRIVILEGES FOR ROLE os IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO os_app;

-- postgres_exporter, sidecar do banco: estatisticas do servidor, nenhuma tabela.
CREATE ROLE os_exporter LOGIN PASSWORD :'senha_exporter';
GRANT CONNECT ON DATABASE os TO os_exporter;
GRANT pg_monitor TO os_exporter;
