import os
from dotenv import load_dotenv

# Carrega as variáveis do ficheiro .env
basedir = os.path.abspath(os.path.dirname(__file__))
load_dotenv(os.path.join(basedir, '.env'))

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'chave-padrao')
    
    # A CORREÇÃO ESTÁ AQUI: Se não encontrar o .env, usa o SQLite local por padrão
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///crm.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Adicionamos fallbacks também para a API para garantir que arranca sempre
    EVOLUTION_API_URL = os.environ.get('EVOLUTION_API_URL', 'http://localhost:8080')
    EVOLUTION_API_KEY = os.environ.get('EVOLUTION_API_KEY', 'sua_chave_secreta_aqui')
    INSTANCE_NAME = os.environ.get('INSTANCE_NAME', 'crm_vendas')