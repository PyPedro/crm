import os
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from dotenv import load_dotenv, find_dotenv

# Carrega as variáveis do ficheiro .env
load_dotenv(find_dotenv(), override=True)

db = SQLAlchemy()
login_manager = LoginManager()

def create_app(config_overrides=None):
    app = Flask(__name__)
    app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'uma-chave-secreta-muito-segura-123')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///crm.db')
    
    # Injeta as chaves do WhatsApp no ambiente global do Flask
    app.config['EVOLUTION_API_URL'] = os.environ.get('EVOLUTION_API_URL', 'http://195.201.237.138:8080')
    app.config['EVOLUTION_API_KEY'] = os.environ.get('EVOLUTION_API_KEY')
    app.config['EVOLUTION_WEBHOOK_URL'] = os.environ.get('EVOLUTION_WEBHOOK_URL', 'https://crm-qf44.onrender.com/webhook/whatsapp')
    app.config.update(config_overrides or {})
    app.config['INSTANCE_NAME'] = os.environ.get('INSTANCE_NAME')

    db.init_app(app)
    
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

    return app

# Expõe a instância WSGI para o Gunicorn em produção.
app = create_app()