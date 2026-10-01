from sqlalchemy import inspect

from app import app, db


def migrar():
    with app.app_context(), db.engine.begin() as connection:
        inspector = inspect(connection)
        alteracoes = (
            ('"user"', 'is_super_admin', 'BOOLEAN NOT NULL DEFAULT FALSE'),
            ('empresa', 'is_ativa', 'BOOLEAN NOT NULL DEFAULT TRUE'),
        )

        for tabela, coluna, definicao in alteracoes:
            colunas_existentes = {item['name'] for item in inspector.get_columns(tabela.strip('"'))}
            if coluna not in colunas_existentes:
                connection.exec_driver_sql(
                    f'ALTER TABLE {tabela} ADD COLUMN {coluna} {definicao}'
                )
                print(f'Coluna {tabela}.{coluna} adicionada.')
            else:
                print(f'Coluna {tabela}.{coluna} já existe.')


if __name__ == '__main__':
    migrar()