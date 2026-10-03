import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from authlib.integrations.flask_client import OAuth
from celery import Celery
from sqlalchemy import inspect, text
from dotenv import load_dotenv, find_dotenv

# Carrega as variáveis do ficheiro .env
load_dotenv(find_dotenv(), override=True)

db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()
celery = Celery('optmiza')


class FlaskContextTask(celery.Task):
    def __call__(self, *args, **kwargs):
        flask_app = self.app.flask_app
        with flask_app.app_context():
            try:
                return self.run(*args, **kwargs)
            except Exception:
                db.session.rollback()
                raise


celery.Task = FlaskContextTask

def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'uma-chave-secreta-muito-segura-123')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///crm.db')
    
    # Injeta as chaves do WhatsApp no ambiente global do Flask
    app.config['EVOLUTION_API_URL'] = os.environ.get('EVOLUTION_API_URL', 'http://195.201.237.138:8080')
    app.config['EVOLUTION_API_KEY'] = os.environ.get('EVOLUTION_API_KEY')
    app.config['EVOLUTION_WEBHOOK_URL'] = os.environ.get('EVOLUTION_WEBHOOK_URL', 'https://crm-qf44.onrender.com/webhook/whatsapp')
    app.config['GOOGLE_CLIENT_ID'] = os.environ.get('GOOGLE_CLIENT_ID')
    app.config['GOOGLE_CLIENT_SECRET'] = os.environ.get('GOOGLE_CLIENT_SECRET')
    app.config['GOOGLE_REDIRECT_URI'] = os.environ.get('GOOGLE_REDIRECT_URI')
    app.config.update(config_overrides or {})
    engine_options = dict(app.config.get('SQLALCHEMY_ENGINE_OPTIONS') or {})
    engine_options.update(pool_pre_ping=True, pool_recycle=300)
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = engine_options
    redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    app.config.setdefault('CELERY_BROKER_URL', os.environ.get('CELERY_BROKER_URL', redis_url))
    app.config.setdefault('CELERY_RESULT_BACKEND', os.environ.get('CELERY_RESULT_BACKEND', redis_url))
    app.config['INSTANCE_NAME'] = os.environ.get('INSTANCE_NAME')

    celery.conf.update(
        broker_url=app.config['CELERY_BROKER_URL'],
        result_backend=app.config['CELERY_RESULT_BACKEND'],
        broker_connection_retry_on_startup=True,
    )
    celery.flask_app = app
    app.extensions['celery'] = celery

    db.init_app(app)
    oauth.init_app(app)
    oauth.register(
        name='google',
        server_metadata_url='https://accounts.google.com/.well-known/openid-configuration',
        client_id=app.config['GOOGLE_CLIENT_ID'],
        client_secret=app.config['GOOGLE_CLIENT_SECRET'],
        client_kwargs={'scope': 'openid email profile'},
    )
    
    # Configuração do Sistema de Login
    login_manager.init_app(app)
    login_manager.login_view = 'main.login'

    from app.models import User
    
    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.routes import bp
    app.register_blueprint(bp)

    with app.app_context():
        db.create_all()
        migrations = {
            'empresa': {
                'usar_menu_inicial': 'BOOLEAN NOT NULL DEFAULT FALSE',
                'mensagem_saudacao': "TEXT NOT NULL DEFAULT 'Olá! Como podemos ajudar hoje?'",
                'prompt_personalidade': "TEXT NOT NULL DEFAULT 'Responda de forma curta, direta e amigável.'",
                'tom_resposta': "VARCHAR(50) NOT NULL DEFAULT 'Profissional'",
                'mensagem_transbordo': "VARCHAR(500) NOT NULL DEFAULT 'Vou transferir o seu atendimento para um de nossos consultores. Aguarde um momento!'",
            },
            'etapa': {
                'exibir_no_menu': 'BOOLEAN NOT NULL DEFAULT FALSE',
                'numero_menu': 'INTEGER',
            },
            'pessoa': {
                'ia_ativa': 'BOOLEAN NOT NULL DEFAULT TRUE',
                'status_atendimento': "VARCHAR(20) NOT NULL DEFAULT 'ia'",
            },
        }
        existing_columns = {
            table: {column['name'] for column in inspect(db.engine).get_columns(table)}
            for table in migrations
        }
        pending_migrations = [
            (table, column, definition)
            for table, definitions in migrations.items()
            for column, definition in definitions.items()
            if column not in existing_columns[table]
        ]
        if pending_migrations:
            with db.engine.begin() as connection:
                for table, column, definition in pending_migrations:
                    connection.execute(text(
                        f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}'
                    ))

        if db.engine.dialect.name == 'postgresql':
            timestamp_columns = {
                'empresa': ('data_criacao',),
                'mensagem': ('data_envio',),
            }
            pending_timezone_migrations = []
            for table, columns in timestamp_columns.items():
                existing = {
                    column['name']: column
                    for column in inspect(db.engine).get_columns(table)
                }
                for column_name in columns:
                    if not getattr(existing[column_name]['type'], 'timezone', False):
                        pending_timezone_migrations.append((table, column_name))
            if pending_timezone_migrations:
                with db.engine.begin() as connection:
                    for table, column_name in pending_timezone_migrations:
                        connection.execute(text(
                            f'ALTER TABLE "{table}" ALTER COLUMN "{column_name}" '
                            f'TYPE TIMESTAMP WITH TIME ZONE USING "{column_name}" AT TIME ZONE \'UTC\''
                        ))

        with db.engine.begin() as connection:
            connection.execute(text(
                'CREATE INDEX IF NOT EXISTS "ix_mensagem_empresa_pessoa_id" '
                'ON "mensagem" ("empresa_id", "pessoa_id", "id")'
            ))

    return app

# Expõe a instância WSGI para o Gunicorn em produção.
app = create_app()