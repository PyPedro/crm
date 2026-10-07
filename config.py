import os
from dotenv import load_dotenv

# Carrega as variáveis do ficheiro .env
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

database_url = os.environ.get('DATABASE_URL')
if database_url and database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'chave-padrao')
    
    SQLALCHEMY_DATABASE_URI = database_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Adicionamos fallbacks também para a API para garantir que arranca sempre
    EVOLUTION_API_URL = os.environ.get('EVOLUTION_API_URL', 'http://localhost:8080')
    EVOLUTION_API_KEY = os.environ.get('EVOLUTION_API_KEY', 'sua_chave_secreta_aqui')
    INSTANCE_NAME = os.environ.get('INSTANCE_NAME', 'crm_vendas')