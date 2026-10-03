from app import db
from flask_login import UserMixin
from datetime import datetime
import pytz
from sqlalchemy import UniqueConstraint


FUSO_HORARIO_BR = pytz.timezone('America/Recife')


def hora_atual_br():
    return datetime.now(FUSO_HORARIO_BR).replace(tzinfo=None)


pessoa_etiqueta = db.Table(
    'pessoa_etiqueta',
    db.Column('pessoa_id', db.Integer, db.ForeignKey('pessoa.id'), primary_key=True),
    db.Column('etiqueta_id', db.Integer, db.ForeignKey('etiqueta.id'), primary_key=True),
)


class Empresa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(120), nullable=False)
    instancia_whatsapp = db.Column(db.String(100), unique=True, nullable=False)
    data_criacao = db.Column(db.DateTime, nullable=False, default=hora_atual_br)
    is_ativa = db.Column(db.Boolean, default=False, server_default=db.false(), nullable=False)
    usar_menu_inicial = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    mensagem_saudacao = db.Column(db.Text, nullable=False, default='Olá! Como podemos ajudar hoje?', server_default='Olá! Como podemos ajudar hoje?')
    prompt_personalidade = db.Column(db.Text, nullable=False, default='Responda de forma curta, direta e amigável.', server_default='Responda de forma curta, direta e amigável.')
    tom_resposta = db.Column(db.String(50), nullable=False, default='Profissional', server_default='Profissional')
    mensagem_transbordo = db.Column(db.String(500), nullable=False, default='Vou transferir o seu atendimento para um de nossos consultores. Aguarde um momento!', server_default='Vou transferir o seu atendimento para um de nossos consultores. Aguarde um momento!')

    users = db.relationship('User', backref='empresa', cascade='all, delete-orphan', lazy=True)
    etapas = db.relationship('Etapa', backref='empresa', cascade='all, delete-orphan', lazy=True)
    negocios = db.relationship('Negocio', backref='empresa', cascade='all, delete-orphan', lazy=True)
    pessoas = db.relationship('Pessoa', backref='empresa', cascade='all, delete-orphan', lazy=True)
    mensagens = db.relationship('Mensagem', backref='empresa', cascade='all, delete-orphan', lazy=True)
    configuracoes = db.relationship('Configuracao', backref='empresa', cascade='all, delete-orphan', lazy=True)
    menu_opcoes = db.relationship('MenuOpcao', backref='empresa', cascade='all, delete-orphan', lazy=True)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=True)
    google_id = db.Column(db.String(255), unique=True, nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False)
    is_super_admin = db.Column(db.Boolean, default=False, server_default=db.false(), nullable=False)
    negocios = db.relationship('Negocio', backref='atendente', lazy=True)

class Pessoa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nome = db.Column(db.String(100))
    telefone = db.Column(db.String(20))
    ia_ativa = db.Column(db.Boolean, nullable=False, default=True, server_default=db.true())
    status_atendimento = db.Column(db.String(20), nullable=False, default='ia', server_default='ia')
    mensagens = db.relationship('Mensagem', backref='pessoa', lazy=True)
    negocios = db.relationship('Negocio', backref='pessoa', lazy=True)
    etiquetas = db.relationship('Etiqueta', secondary=pessoa_etiqueta, back_populates='pessoas', lazy='selectin')
    __table_args__ = (UniqueConstraint('empresa_id', 'telefone', name='uq_pessoa_empresa_telefone'),)

    @property
    def qtd_nao_lidas(self):
        return Mensagem.query.filter_by(empresa_id=self.empresa_id, pessoa_id=self.id, tipo='inbound', lida=False).count()


class MenuOpcao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    numero = db.Column(db.Integer, nullable=False)
    descricao = db.Column(db.String(120), nullable=False)
    acao_destino = db.Column(db.String(20), nullable=False)
    __table_args__ = (UniqueConstraint('empresa_id', 'numero', name='uq_menu_opcao_empresa_numero'),)


class Etiqueta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nome = db.Column(db.String(40), nullable=False)
    pessoas = db.relationship('Pessoa', secondary=pessoa_etiqueta, back_populates='etiquetas', lazy=True)
    __table_args__ = (UniqueConstraint('empresa_id', 'nome', name='uq_etiqueta_empresa_nome'),)


class Etapa(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, index=True)
    nome = db.Column(db.String(50))
    exibir_no_menu = db.Column(db.Boolean, nullable=False, default=False, server_default=db.false())
    numero_menu = db.Column(db.Integer, nullable=True)
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
    data_envio = db.Column(db.DateTime, default=hora_atual_br)
    __table_args__ = (
        db.Index('ix_mensagem_empresa_pessoa_id', 'empresa_id', 'pessoa_id', 'id'),
    )

class Configuracao(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    empresa_id = db.Column(db.Integer, db.ForeignKey('empresa.id'), nullable=False, unique=True)
    prompt_ia = db.Column(db.Text, nullable=False, default="Atuas como o assistente virtual de triagem. Faz 1 pergunta curta. Se pedir orçamento ou humano, encerra com [TRANSFERIR].")