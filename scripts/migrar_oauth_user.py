from sqlalchemy import inspect

from app import app, db


def migrar():
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table('user'):
            raise RuntimeError('A tabela user não existe; inicialize a aplicação antes da migração.')

        colunas = {coluna['name'] for coluna in inspector.get_columns('user')}
        with db.engine.begin() as conexao:
            if 'email' not in colunas:
                conexao.exec_driver_sql('ALTER TABLE "user" ADD COLUMN email VARCHAR(255)')
            if 'google_id' not in colunas:
                conexao.exec_driver_sql('ALTER TABLE "user" ADD COLUMN google_id VARCHAR(255)')
            conexao.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_user_email ON "user" (email)')
            conexao.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS uq_user_google_id ON "user" (google_id)')

        print('Migração OAuth concluída para email e google_id.')


if __name__ == '__main__':
    migrar()