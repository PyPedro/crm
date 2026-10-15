from datetime import datetime
import pytz
from flask_login import UserMixin

# IMPORTANTE: Importa a instância do banco de dados já inicializada pela sua aplicação
from app import db

# Fuso horário padrão do sistema
FUSO_HORARIO_BR = pytz.timezone('America/Sao_Paulo')

def hora_atual_br():
    return datetime.now(FUSO_HORARIO_BR)

# --- TABELAS DE ASSOCIAÇÃO (Muitos-para-Muitos) ---
pessoa_etiqueta = db.Table('pessoa_etiqueta',
    db.Column('pessoa_id', db.Integer, db.ForeignKey('pessoa.id'), primary_key=True),
    db.Column('etiqueta_id', db.Integer, db.ForeignKey('etiqueta.id'), primary_key=True)
)

# --- MODELOS PRINCIPAIS ---

class Empresa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    instancia_whatsapp = db.Column(db.String(100), unique=True, nullable=False)
    is_ativa = db.Column(db.Boolean, default=False)
    
    # Data de criação (Evita NotNullViolation)
    data_criacao = db.Column(db.DateTime(timezone=True), default=hora_atual_br, nullable=False)
    
    # Configurações do Bot de Atendimento com valores padrão (Evita NotNullViolation)
    usar_menu_inicial = db.Column(db.Boolean, default=False)
    mensagem_saudacao = db.Column(db.Text, nullable=False, default="Olá! Bem-vindo ao nosso atendimento. Como podemos ajudar hoje?")
    prompt_personalidade = db.Column(db.Text, nullable=False, default="Você é um assistente virtual prestativo. Responda de forma curta, direta e amigável.")
    tom_resposta = db.Column(db.String(20), nullable=False, default="Profissional")
    mensagem_transbordo = db.Column(db.Text, nullable=False, default="Vou transferir o seu atendimento para um de nossos consultores. Aguarde um momento.")
    
    # Coluna legada
    logo_b64 = db.Column(db.Text, nullable=True, default="")

    # Relacionamentos
    users = db.relationship('User', backref='empresa', lazy=True)
    etapas = db.relationship('Etapa', backref='empresa', lazy=True)
    pessoas = db.relationship('Pessoa', backref='empresa', lazy=True)
    negocios = db.relationship('Negocio', backref='empresa', lazy=True)
    mensagens = db.relationship('Mensagem', backref='empresa', lazy=True)
    configuracao = db.relationship('Configuracao', backref='empresa', uselist=False, lazy=True)
    etiquetas = db.relationship('Etiqueta', backref='empresa', lazy=True)
    respostas_automaticas = db.relationship('RespostaAutomatica', backref='empresa', lazy=True)


class LogotipoGlobal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    logo_b64 = db.Column(db.Text, nullable=False)
    data_envio = db.Column(db.DateTime(timezone=True), default=hora_atual_br)


class RespostaAutomatica(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    palavra_chave = db.Column(db.String(100), nullable=False)
    resposta = db.Column(db.Text, nullable=False)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=True)
