-- Papeis do PostgreSQL do OS Service, um por uso (ADR-042). A imagem postgres
-- roda este script uma vez, no primeiro init do volume, como o superusuario
-- postgres, que depois disso nao serve a ninguem. As senhas vem do ambiente do
-- container do banco (Secret os-postgres) pelo \getenv do psql: nunca em
-- argumento de processo. A variavel fica na linha seguinte ao PASSWORD porque o
-- trivy (KSV-0109) toma a palavra seguida dos dois-pontos da variavel do psql
-- por uma senha gravada no ConfigMap.

-- Um erro aqui nao leva o comando, com a senha, para o log do servidor.
SET log_min_error_statement = 'panic';

\getenv senha_dono POSTGRES_OWNER_PASSWORD
\getenv senha_app POSTGRES_APP_PASSWORD
\getenv senha_exporter POSTGRES_EXPORTER_PASSWORD

-- Dono do banco e das tabelas (DDL): o Job de migracao.
CREATE ROLE os LOGIN PASSWORD
  :'senha_dono';
ALTER DATABASE os OWNER TO os;
ALTER SCHEMA public OWNER TO os;

-- Aplicacao (API, relay e consumidor): so DML nas tabelas que o dono criar.
-- As tabelas nascem depois, no Job, e por isso o privilegio vem por default
-- privileges, que cobrem tambem a alembic_version lida pelo aguarda-migracao.
CREATE ROLE os_app LOGIN PASSWORD
  :'senha_app';
GRANT CONNECT ON DATABASE os TO os_app;
GRANT USAGE ON SCHEMA public TO os_app;
ALTER DEFAULT PRIVILEGES FOR ROLE os IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO os_app;
ALTER DEFAULT PRIVILEGES FOR ROLE os IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO os_app;

-- postgres_exporter, sidecar do banco: estatisticas do servidor, nenhuma tabela.
CREATE ROLE os_exporter LOGIN PASSWORD
  :'senha_exporter';
GRANT pg_monitor TO os_exporter;
