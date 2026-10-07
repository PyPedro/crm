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
    
    # Configurações do Bot de Atendimento
    usar_menu_inicial = db.Column(db.Boolean, default=False)
    mensagem_saudacao = db.Column(db.Text, nullable=True)
    prompt_personalidade = db.Column(db.Text, nullable=True)
    tom_resposta = db.Column(db.String(20), nullable=True)
    mensagem_transbordo = db.Column(db.Text, nullable=True)
    
    # Coluna para o logótipo em Base64
    logo_b64 = db.Column(db.Text, nullable=True)

    # Relacionamentos
    users = db.relationship('User', backref='empresa', lazy=True)
    etapas = db.relationship('Etapa', backref='empresa', lazy=True)
    pessoas = db.relationship('Pessoa', backref='empresa', lazy=True)
    negocios = db.relationship('Negocio', backref='empresa', lazy=True)
    mensagens = db.relationship('Mensagem', backref='empresa', lazy=True)
    configuracao = db.relationship('Configuracao', backref='empresa', uselist=False, lazy=True)
    etiquetas = db.relationship('Etiqueta', backref='empresa', lazy=True)


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=True)
    google_id = db.Column(db.String(120), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_super_admin = db.Column(db.Boolean, default=False)

    negocios = db.relationship('Negocio', backref='responsavel', lazy=True)


class Etapa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    nome = db.Column(db.String(50), nullable=False)
    exibir_no_menu = db.Column(db.Boolean, default=False)
    numero_menu = db.Column(db.Integer, nullable=True)

    negocios = db.relationship('Negocio', backref='etapa', lazy=True)


class Etiqueta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    nome = db.Column(db.String(40), nullable=False)


class Pessoa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    nome = db.Column(db.String(100), nullable=False)
    telefone = db.Column(db.String(20), nullable=False)
    
    # Controle de IA e Bot: 'menu', 'ia', 'humano', 'fechado'
    status_atendimento = db.Column(db.String(20), default='humano')
    ia_ativa = db.Column(db.Boolean, default=False)

    # Relacionamentos
    negocios = db.relationship('Negocio', backref='pessoa', lazy=True)
    mensagens = db.relationship('Mensagem', backref='pessoa', lazy=True)
    etiquetas = db.relationship('Etiqueta', secondary=pessoa_etiqueta, backref=db.backref('pessoas', lazy=True))


class Negocio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    pessoa_id = db.Column(db.Integer, db.ForeignKey('pessoa.id'), nullable=False)
    etapa_id = db.Column(db.Integer, db.ForeignKey('etapa.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    
    titulo = db.Column(db.String(100), nullable=False)
    valor = db.Column(db.Float, default=0.0)


class Mensagem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    pessoa_id = db.Column(db.Integer, db.ForeignKey('pessoa.id'), nullable=False)
    
    mensagem = db.Column(db.Text, nullable=False)
    tipo = db.Column(db.String(20), nullable=False) # 'inbound' (recebida) ou 'outbound' (enviada)
    lida = db.Column(db.Boolean, default=False)
    data_envio = db.Column(db.DateTime(timezone=True), default=hora_atual_br)


class Configuracao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False)
    prompt_ia = db.Column(db.Text, nullable=True)
