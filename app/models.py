from app import db
from flask_login import UserMixin
from datetime import datetime
from sqlalchemy import UniqueConstraint


class Empresa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    instancia_whatsapp = db.Column(db.String(100), unique=True, nullable=False)
    data_criacao = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    users = db.relationship('User', backref='empresa', cascade='all, delete-orphan', lazy=True)
    etapas = db.relationship('Etapa', backref='empresa', cascade='all, delete-orphan', lazy=True)
    negocios = db.relationship('Negocio', backref='empresa', cascade='all, delete-orphan', lazy=True)
    pessoas = db.relationship('Pessoa', backref='empresa', cascade='all, delete-orphan', lazy=True)
    mensagens = db.relationship('Mensagem', backref='empresa', cascade='all, delete-orphan', lazy=True)
    configuracoes = db.relationship('Configuracao', backref='empresa', cascade='all, delete-orphan', lazy=True)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    negocios = db.relationship('Negocio', backref='atendente', lazy=True)

class Pessoa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nome = db.Column(db.String(100))
    telefone = db.Column(db.String(20))
    mensagens = db.relationship('Mensagem', backref='pessoa', lazy=True)
    negocios = db.relationship('Negocio', backref='pessoa', lazy=True)
    __table_args__ = (UniqueConstraint('empresa_id', 'telefone', name='uq_pessoa_empresa_telefone'),)

    @property
    def qtd_nao_lidas(self):
        return Mensagem.query.filter_by(empresa_id=self.empresa_id, pessoa_id=self.id, tipo='inbound', lida=False).count()

class Etapa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nome = db.Column(db.String(50))
    negocios = db.relationship('Negocio', backref='etapa', lazy=True)

class Negocio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    titulo = db.Column(db.String(100))
    valor = db.Column(db.Float, default=0.0)
    pessoa_id = db.Column(db.Integer, db.ForeignKey('pessoa.id'))
    etapa_id = db.Column(db.Integer, db.ForeignKey('etapa.id'))
    # NOVO: Define de quem é este negócio (Operador)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)

class Mensagem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    pessoa_id = db.Column(db.Integer, db.ForeignKey('pessoa.id'))
    mensagem = db.Column(db.Text)
    tipo = db.Column(db.String(20)) # 'inbound' ou 'outbound'
    lida = db.Column(db.Boolean, default=False)
    data_envio = db.Column(db.DateTime, default=datetime.utcnow)

class Configuracao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, unique=True)
    prompt_ia = db.Column(db.Text, nullable=False, default="Atuas como o assistente virtual de triagem. Faz 1 pergunta curta. Se pedir orçamento ou humano, encerra com [TRANSFERIR].")