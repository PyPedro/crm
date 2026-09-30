import argparse
import sqlite3
from datetime import datetime
from pathlib import Path


TABLES = ('user', 'etapa', 'pessoa', 'negocio', 'mensagem', 'configuracao')

TABLE_DDL = {
    'user': '''CREATE TABLE "user" (
        id INTEGER NOT NULL PRIMARY KEY,
        empresa_id INTEGER NOT NULL REFERENCES empresa(id),
        username VARCHAR(50) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        is_admin BOOLEAN
    )''',
    'etapa': '''CREATE TABLE etapa (
        id INTEGER NOT NULL PRIMARY KEY,
        empresa_id INTEGER NOT NULL REFERENCES empresa(id),
        nome VARCHAR(50)
    )''',
    'pessoa': '''CREATE TABLE pessoa (
        id INTEGER NOT NULL PRIMARY KEY,
        empresa_id INTEGER NOT NULL REFERENCES empresa(id),
        nome VARCHAR(100),
        telefone VARCHAR(20),
        CONSTRAINT uq_pessoa_empresa_telefone UNIQUE (empresa_id, telefone)
    )''',
    'negocio': '''CREATE TABLE negocio (
        id INTEGER NOT NULL PRIMARY KEY,
        empresa_id INTEGER NOT NULL REFERENCES empresa(id),
        titulo VARCHAR(100),
        valor FLOAT,
        pessoa_id INTEGER REFERENCES pessoa(id),
        etapa_id INTEGER REFERENCES etapa(id),
        user_id INTEGER REFERENCES "user"(id)
    )''',
    'mensagem': '''CREATE TABLE mensagem (
        id INTEGER NOT NULL PRIMARY KEY,
        empresa_id INTEGER NOT NULL REFERENCES empresa(id),
        pessoa_id INTEGER REFERENCES pessoa(id),
        mensagem TEXT,
        tipo VARCHAR(20),
        lida BOOLEAN,
        data_envio DATETIME
    )''',
    'configuracao': '''CREATE TABLE configuracao (
        id INTEGER NOT NULL PRIMARY KEY,
        empresa_id INTEGER NOT NULL UNIQUE REFERENCES empresa(id),
        prompt_ia TEXT NOT NULL
    )''',
}


def migrar(database_path, nome_empresa, instancia):
    database_path = Path(database_path).resolve()
    if not database_path.is_file():
        raise FileNotFoundError(f"Banco SQLite não encontrado: {database_path}")

    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        table_names = {
            row['name']
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        missing_tables = set(TABLES) - table_names
        if missing_tables:
            raise RuntimeError(f"Tabelas esperadas não encontradas: {', '.join(sorted(missing_tables))}")

        tenant_columns = {
            table: {row['name'] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            for table in TABLES
        }
        migrated = [
            'empresa_id' in tenant_columns[table]
            for table in TABLES
        ]
        if all(migrated):
            print('A base já possui empresa_id em todas as tabelas; nada a fazer.')
            return
        if any(migrated):
            raise RuntimeError('Migração parcial detectada; restaure o backup antes de continuar.')

        legacy_names = {f'{table}_legacy_mt' for table in TABLES}
        if legacy_names & table_names:
            raise RuntimeError('Foram encontradas tabelas temporárias de migração anterior.')

        backup_path = database_path.with_name(database_path.name + '.pre_multi_tenant.bak')
        if backup_path.exists():
            raise FileExistsError(f"O backup já existe e não será sobrescrito: {backup_path}")
        backup = sqlite3.connect(backup_path)
        try:
            connection.backup(backup)
        finally:
            backup.close()

        connection.execute('PRAGMA foreign_keys = OFF')
        connection.commit()
        connection.execute('BEGIN')
        if 'empresa' not in table_names:
            connection.execute('''CREATE TABLE empresa (
                id INTEGER NOT NULL PRIMARY KEY,
                nome VARCHAR(120) NOT NULL,
                instancia_whatsapp VARCHAR(100) NOT NULL UNIQUE,
                data_criacao DATETIME NOT NULL
            )''')
        else:
            company_columns = {
                row['name'] for row in connection.execute('PRAGMA table_info(empresa)')
            }
            expected = {'id', 'nome', 'instancia_whatsapp', 'data_criacao'}
            if not expected.issubset(company_columns):
                raise RuntimeError('A tabela empresa existe com um esquema inesperado.')
            if connection.execute('SELECT COUNT(*) FROM empresa').fetchone()[0]:
                raise RuntimeError('A tabela empresa já contém dados; migração interrompida para evitar misturar tenants.')

        cursor = connection.execute(
            'INSERT INTO empresa (nome, instancia_whatsapp, data_criacao) VALUES (?, ?, ?)',
            (nome_empresa, instancia, datetime.utcnow().isoformat()),
        )
        empresa_id = cursor.lastrowid

        for table in TABLES:
            connection.execute(f'ALTER TABLE "{table}" RENAME TO "{table}_legacy_mt"')
        for table in TABLES:
            connection.execute(TABLE_DDL[table])

        for table in TABLES:
            legacy_table = f'{table}_legacy_mt'
            old_columns = {
                row['name']
                for row in connection.execute(f'PRAGMA table_info("{legacy_table}")')
            }
            new_columns = [row['name'] for row in connection.execute(f'PRAGMA table_info("{table}")')]
            copied_columns = [
                column for column in new_columns
                if column != 'empresa_id' and column in old_columns
            ]
            destination = copied_columns + ['empresa_id']
            quoted_destination = ', '.join(f'"{column}"' for column in destination)
            source_values = ', '.join(f'"{column}"' for column in copied_columns) + ', ?'
            connection.execute(
                f'INSERT INTO "{table}" ({quoted_destination}) '
                f'SELECT {source_values} FROM "{legacy_table}"',
                (empresa_id,),
            )

        for table in ('mensagem', 'negocio', 'configuracao', 'pessoa', 'etapa', 'user'):
            connection.execute(f'DROP TABLE "{table}_legacy_mt"')

        for table in TABLES[:-1]:
            connection.execute(f'CREATE INDEX ix_{table}_empresa_id ON "{table}" (empresa_id)')

        violations = connection.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            raise RuntimeError(f"A validação de chaves estrangeiras falhou: {violations}")
        connection.commit()
        connection.execute('PRAGMA foreign_keys = ON')
        print(f"Migração concluída para '{nome_empresa}' ({instancia}). Backup: {backup_path}")
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description='Migra um banco SQLite single-tenant para multi-tenant.')
    parser.add_argument('--database', required=True, help='Caminho do arquivo SQLite a migrar.')
    parser.add_argument('--empresa', required=True, help='Nome da empresa que receberá os dados atuais.')
    parser.add_argument('--instancia', required=True, help='Nome da instância Evolution já existente.')
    args = parser.parse_args()
    migrar(args.database, args.empresa, args.instancia)


if __name__ == '__main__':
    main()
