import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from authlib.integrations.flask_client import OAuth
from sqlalchemy import inspect, text
from dotenv import load_dotenv, find_dotenv

# Carrega as variáveis do ficheiro .env
load_dotenv(find_dotenv(), override=True)

db = SQLAlchemy()
login_manager = LoginManager()
oauth = OAuth()

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
    app.config['INSTANCE_NAME'] = os.environ.get('INSTANCE_NAME')

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
        columns = {column['name'] for column in inspect(db.engine).get_columns('pessoa')}
        if 'ia_ativa' not in columns:
            with db.engine.begin() as connection:
                connection.execute(text(
                    'ALTER TABLE pessoa ADD COLUMN ia_ativa BOOLEAN NOT NULL DEFAULT TRUE'
                ))

    return app

# Expõe a instância WSGI para o Gunicorn em produção.
app = create_app()