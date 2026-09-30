-- Execute uma vez no PostgreSQL, antes de publicar a nova versao do CRM.
-- Substitua estes valores pelos dados da empresa e instancia atuais.
\set empresa_nome 'Empresa Existente'
\set instancia_whatsapp 'crm_vendas'

BEGIN;

CREATE TABLE IF NOT EXISTS empresa (
    id SERIAL PRIMARY KEY,
    nome VARCHAR(120) NOT NULL,
    instancia_whatsapp VARCHAR(100) NOT NULL UNIQUE,
    data_criacao TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

INSERT INTO empresa (nome, instancia_whatsapp)
VALUES (:'empresa_nome', :'instancia_whatsapp')
RETURNING id \gset

ALTER TABLE "user" ADD COLUMN empresa_id INTEGER REFERENCES empresa(id);
ALTER TABLE etapa ADD COLUMN empresa_id INTEGER REFERENCES empresa(id);
ALTER TABLE negocio ADD COLUMN empresa_id INTEGER REFERENCES empresa(id);
ALTER TABLE pessoa ADD COLUMN empresa_id INTEGER REFERENCES empresa(id);
ALTER TABLE mensagem ADD COLUMN empresa_id INTEGER REFERENCES empresa(id);
ALTER TABLE configuracao ADD COLUMN empresa_id INTEGER REFERENCES empresa(id);

UPDATE "user" SET empresa_id = :id;
UPDATE etapa SET empresa_id = :id;
UPDATE negocio SET empresa_id = :id;
UPDATE pessoa SET empresa_id = :id;
UPDATE mensagem SET empresa_id = :id;
UPDATE configuracao SET empresa_id = :id;

ALTER TABLE "user" ALTER COLUMN empresa_id SET NOT NULL;
ALTER TABLE etapa ALTER COLUMN empresa_id SET NOT NULL;
ALTER TABLE negocio ALTER COLUMN empresa_id SET NOT NULL;
ALTER TABLE pessoa ALTER COLUMN empresa_id SET NOT NULL;
ALTER TABLE mensagem ALTER COLUMN empresa_id SET NOT NULL;
ALTER TABLE configuracao ALTER COLUMN empresa_id SET NOT NULL;

ALTER TABLE pessoa DROP CONSTRAINT IF EXISTS pessoa_telefone_key;
ALTER TABLE pessoa ADD CONSTRAINT uq_pessoa_empresa_telefone UNIQUE (empresa_id, telefone);
ALTER TABLE configuracao ADD CONSTRAINT uq_configuracao_empresa UNIQUE (empresa_id);

CREATE INDEX ix_user_empresa_id ON "user" (empresa_id);
CREATE INDEX ix_etapa_empresa_id ON etapa (empresa_id);
CREATE INDEX ix_negocio_empresa_id ON negocio (empresa_id);
CREATE INDEX ix_pessoa_empresa_id ON pessoa (empresa_id);
CREATE INDEX ix_mensagem_empresa_id ON mensagem (empresa_id);

COMMIT;
