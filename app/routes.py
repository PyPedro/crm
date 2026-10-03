import requests
import json
import re
import secrets
import unicodedata
import math
from dotenv import load_dotenv, find_dotenv
from flask import Blueprint, request, jsonify, render_template, current_app, redirect, url_for, render_template_string, session
from datetime import datetime
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

from app import oauth
from app.models import db, Empresa, Etapa, Negocio, Pessoa, Mensagem, User, Configuracao, Etiqueta
from app.services.whatsapp import enviar_mensagem_whatsapp
from app.tasks import processar_mensagem_whatsapp

load_dotenv(find_dotenv(), override=True)
bp = Blueprint('main', __name__)


@bp.before_app_request
def bloquear_empresa_suspensa():
    if not current_user.is_authenticated:
        return None

    if current_user.is_super_admin and request.endpoint in {
        'main.optmiza_master', 'main.toggle_empresa'
    }:
        return None

    if not current_user.empresa.is_ativa:
        logout_user()
        return 'Conta suspensa. Contacte o suporte.', 403

    return None

# --- FUNÇÃO PARA GERAR TOKENS SEGUROS ---
def get_serializer():
    return URLSafeTimedSerializer(current_app.config.get('SECRET_KEY', 'optmiza-secure-key-2026'))


def slug_instancia(nome_empresa):
    normalizado = unicodedata.normalize('NFKD', nome_empresa).encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[^a-zA-Z0-9]+', '-', normalizado).strip('-').lower() or 'empresa'
    return f"{base[:80]}-{secrets.token_hex(4)}"


def evolution_headers():
    return {"apikey": current_app.config['EVOLUTION_API_KEY']}


def configurar_instancia_whatsapp(nome_empresa):
    if not current_app.config.get('EVOLUTION_API_KEY'):
        raise RuntimeError('A integração WhatsApp não está configurada.')

    instancia = slug_instancia(nome_empresa)
    api_url = current_app.config['EVOLUTION_API_URL'].rstrip('/')
    resposta = requests.post(
        f"{api_url}/instance/create",
        headers=evolution_headers(),
        json={"instanceName": instancia, "qrcode": True, "integration": "WHATSAPP-BAILEYS"},
        timeout=20,
    )
    resposta.raise_for_status()
    resposta_webhook = requests.post(
        f"{api_url}/webhook/set/{instancia}",
        headers=evolution_headers(),
        json={"webhook": {"enabled": True, "url": current_app.config['EVOLUTION_WEBHOOK_URL'], "events": ["MESSAGES_UPSERT"]}},
        timeout=20,
    )
    resposta_webhook.raise_for_status()
    return instancia

# --- TEMPLATE HTML INJETÁVEL COM ASSINATURA (CONVITE E RESET) ---
AUTH_HTML = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Optmiza</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>body { font-family: 'Plus Jakarta Sans', sans-serif; }</style>
</head>
<body class="bg-slate-50 h-screen flex flex-col items-center justify-center">
    <div class="bg-white p-8 rounded-2xl shadow-xl w-[400px] border border-slate-100 mb-6">
        <div class="w-12 h-12 bg-blue-600 rounded-xl mx-auto flex items-center justify-center text-white font-bold text-2xl mb-4 shadow-md">O</div>
        <h2 class="text-2xl font-bold text-center text-slate-800 mb-2">{{ title }}</h2>
        <p class="text-sm text-slate-500 text-center mb-6">{{ subtitle }}</p>
        
        {% if erro %}
        <div class="bg-red-50 border border-red-100 text-red-600 p-3 rounded-xl text-sm mb-5 font-semibold text-center">{{ erro }}</div>
        {% endif %}
        
        <form method="POST" class="space-y-4">
            {% if action == 'invite' %}
            <div>
                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">Defina o seu Utilizador</label>
                <input type="text" name="username" required placeholder="Ex: joao.silva" class="w-full bg-slate-50 border border-slate-200 px-4 py-3 rounded-xl text-sm focus:ring-2 focus:ring-blue-500 focus:bg-white outline-none transition-all">
            </div>
            {% endif %}
            
            <div>
                <label class="block text-xs font-bold text-slate-500 mb-1 uppercase tracking-wider">{% if action == 'invite' %}Palavra-passe{% else %}Nova Palavra-passe{% endif %}</label>
                <input type="password" name="password" required placeholder="••••••••" class="w-full bg-slate-50 border border-slate-200 px-4 py-3 rounded-xl text-sm focus:ring-2 focus:ring-blue-500 focus:bg-white outline-none transition-all">
            </div>
            
            <button type="submit" class="w-full bg-blue-600 hover:bg-blue-700 text-white font-bold py-3.5 rounded-xl transition-all shadow-[0_4px_14px_0_rgba(37,99,235,0.39)] hover:shadow-[0_6px_20px_rgba(37,99,235,0.23)] mt-2">
                {{ btn_text }}
            </button>
        </form>
    </div>
    
    <!-- ASSINATURA OPTMIZA -->
    <p class="text-slate-400 text-[10px] tracking-wide text-center">
        Desenvolvido por <strong class="text-slate-500">Pedro Marinho</strong>. Um produto <strong class="text-blue-600">Optmiza</strong>.
    </p>
</body>
</html>
"""

# --- CADASTRO DE EMPRESA E AUTENTICAÇÃO ---
@bp.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template('register.html')

    nome_empresa = (request.form.get('nome_empresa') or '').strip()
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    if not nome_empresa or not username or not password:
        return render_template('register.html', erro="Preencha todos os campos."), 400
    if User.query.filter_by(username=username).first():
        return render_template('register.html', erro="Este nome de utilizador já está em uso."), 409
    try:
        instancia = configurar_instancia_whatsapp(nome_empresa)
    except RuntimeError as erro:
        return render_template('register.html', erro=str(erro)), 503
    except requests.RequestException:
        return render_template('register.html', erro="Não foi possível configurar o WhatsApp. Tente novamente."), 502

    empresa = Empresa(nome=nome_empresa, instancia_whatsapp=instancia)
    db.session.add(empresa)
    db.session.flush()
    db.session.add(User(
        empresa_id=empresa.id,
        username=username,
        password_hash=generate_password_hash(password),
        is_admin=True,
    ))
    db.session.add_all([
        Etapa(empresa_id=empresa.id, nome=nome)
        for nome in ('Qualificação', 'Contato Feito', 'Proposta Enviada', 'Fechamento')
    ])
    db.session.add(Configuracao(empresa_id=empresa.id))
    db.session.commit()
    return redirect(url_for('main.login', msg="Empresa criada. Entre para continuar a configuração do WhatsApp."))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    msg_sucesso = request.args.get('msg')
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            if not user.empresa.is_ativa:
                return render_template('login.html', erro='Conta suspensa. Contacte o suporte.'), 403
            login_user(user)
            return redirect(url_for('main.index'))
        return render_template('login.html', erro="Credenciais inválidas")
        
    return render_template('login.html', msg=msg_sucesso)


@bp.route('/login/google')
def login_google():
    if not current_app.config.get('GOOGLE_CLIENT_ID') or not current_app.config.get('GOOGLE_CLIENT_SECRET'):
        return redirect(url_for('main.login', msg='O acesso com Google ainda não está configurado.'))
    callback_url = current_app.config.get('GOOGLE_REDIRECT_URI') or url_for('main.authorize_google', _external=True)
    return oauth.google.authorize_redirect(callback_url)


@bp.route('/authorize/google')
def authorize_google():
    try:
        token = oauth.google.authorize_access_token()
        perfil = token.get('userinfo') or oauth.google.userinfo() or {}
    except Exception:
        return redirect(url_for('main.login', msg='Não foi possível autenticar com o Google. Tente novamente.'))

    email = (perfil.get('email') or '').strip().lower()
    google_id = perfil.get('sub')
    nome = (perfil.get('name') or email.split('@')[0] or 'Nova empresa').strip()
    if not email or not google_id or not perfil.get('email_verified'):
        return redirect(url_for('main.login', msg='O Google não confirmou um endereço de e-mail válido.'))

    user = User.query.filter_by(google_id=google_id).first()
    if not user:
        user = User.query.filter_by(email=email).first()
    if user:
        if user.google_id and user.google_id != google_id:
            return redirect(url_for('main.login', msg='Este e-mail está associado a outra conta Google.'))
        if not user.empresa.is_ativa:
            return redirect(url_for('main.login', msg='Conta suspensa. Contacte o suporte.'))
        if not user.google_id:
            user.google_id = google_id
            db.session.commit()
        login_user(user)
        return redirect(url_for('main.index'))

    try:
        instancia = configurar_instancia_whatsapp(f'Empresa de {nome}')
    except (RuntimeError, requests.RequestException):
        return redirect(url_for('main.login', msg='Não foi possível preparar o WhatsApp para a nova empresa. Tente novamente.'))

    empresa = Empresa(nome=f'Empresa de {nome}'[:120], instancia_whatsapp=instancia)
    db.session.add(empresa)
    db.session.flush()
    username_base = re.sub(r'[^a-zA-Z0-9_.-]+', '', email.split('@')[0])[:40] or 'google'
    username = f'{username_base}-{secrets.token_hex(3)}'
    novo_user = User(
        empresa_id=empresa.id,
        username=username,
        email=email,
        google_id=google_id,
        password_hash=generate_password_hash(secrets.token_urlsafe(32)),
        is_admin=True,
    )
    db.session.add(novo_user)
    db.session.add_all([
        Etapa(empresa_id=empresa.id, nome=etapa)
        for etapa in ('Qualificação', 'Contato Feito', 'Proposta Enviada', 'Fechamento')
    ])
    db.session.add(Configuracao(empresa_id=empresa.id))
    db.session.commit()
    login_user(novo_user)
    return redirect(url_for('main.index'))

@bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.login'))


@bp.route('/optmiza-master', methods=['GET'])
def optmiza_master():
    if not current_user.is_authenticated or not current_user.is_super_admin:
        return 'Acesso proibido.', 403

    empresas = Empresa.query.all()
    total_usuarios = dict(
        db.session.query(User.empresa_id, db.func.count(User.id))
        .group_by(User.empresa_id)
        .all()
    )
    csrf_token = session.setdefault('master_csrf_token', secrets.token_urlsafe(32))
    return render_template(
        'master.html',
        empresas=empresas,
        total_usuarios=total_usuarios,
        csrf_token=csrf_token,
    )


@bp.route('/optmiza-master/empresa/<int:id>/toggle', methods=['POST'])
def toggle_empresa(id):
    if not current_user.is_authenticated or not current_user.is_super_admin:
        return 'Acesso proibido.', 403

    token_enviado = request.form.get('csrf_token', '')
    token_sessao = session.get('master_csrf_token', '')
    if not token_sessao or not secrets.compare_digest(token_enviado, token_sessao):
        return 'Requisição inválida.', 400

    empresa = db.session.get(Empresa, id)
    if not empresa:
        return 'Empresa não encontrada.', 404

    empresa.is_ativa = not empresa.is_ativa
    db.session.commit()
    return redirect(url_for('main.optmiza_master'))

# --- ROTAS DE SEGURANÇA: CONVITES E RESET DE SENHA ---
@bp.route('/api/invite', methods=['POST'])
@login_required
def gerar_link_convite():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
        
    is_admin = request.json.get('is_admin', False)
    s = get_serializer()
    # Cria um token válido por 24 horas
    token = s.dumps({"is_admin": is_admin, "empresa_id": current_user.empresa_id, "action": "invite"})
    invite_url = url_for('main.processar_convite', token=token, _external=True)
    return jsonify({"link": invite_url})

@bp.route('/invite/<token>', methods=['GET', 'POST'])
def processar_convite(token):
    s = get_serializer()
    try:
        data = s.loads(token, max_age=86400)
        if data.get('action') != 'invite': raise ValueError
    except (SignatureExpired, BadTimeSignature, ValueError):
        return render_template_string(AUTH_HTML, title="Convite Inválido", subtitle="Este link expirou ou é inválido.", erro="Solicite um novo link ao Administrador.")

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if User.query.filter_by(username=username).first():
            return render_template_string(AUTH_HTML, title="Criar Conta", subtitle="Junte-se à equipa Optmiza", action="invite", btn_text="Concluir Registo", erro="Este nome de utilizador já está em uso.")
            
        empresa = db.session.get(Empresa, data.get('empresa_id'))
        if not empresa:
            return render_template_string(AUTH_HTML, title="Convite Inválido", subtitle="A empresa deste convite não existe.", erro="Solicite um novo link ao Administrador."), 404
        novo_user = User(empresa_id=empresa.id, username=username, password_hash=generate_password_hash(password), is_admin=data['is_admin'])
        db.session.add(novo_user)
        db.session.commit()
        return redirect(url_for('main.login', msg="Conta criada! Já pode fazer login."))
        
    return render_template_string(AUTH_HTML, title="Criar Conta", subtitle="Foi convidado para a equipa Optmiza", action="invite", btn_text="Concluir Registo")

@bp.route('/api/usuarios/<int:id>/reset_link', methods=['POST'])
@login_required
def gerar_link_reset(id):
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    user = User.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if not user:
        return jsonify({"erro": "Utilizador não encontrado"}), 404
        
    s = get_serializer()
    # Token válido por 1 hora
    token = s.dumps({"user_id": user.id, "action": "reset"})
    reset_url = url_for('main.processar_reset', token=token, _external=True)
    return jsonify({"link": reset_url})

@bp.route('/reset/<token>', methods=['GET', 'POST'])
def processar_reset(token):
    s = get_serializer()
    try:
        data = s.loads(token, max_age=3600)
        if data.get('action') != 'reset': raise ValueError
    except (SignatureExpired, BadTimeSignature, ValueError):
        return render_template_string(AUTH_HTML, title="Link Inválido", subtitle="Este link de segurança expirou.", erro="Solicite um novo reset ao Administrador.")

    user = db.session.get(User, data['user_id'])
    if not user:
        return render_template_string(AUTH_HTML, title="Link Inválido", subtitle="A conta não foi encontrada.", erro="Solicite um novo reset ao Administrador."), 404
    if request.method == 'POST':
        password = request.form.get('password')
        user.password_hash = generate_password_hash(password)
        db.session.commit()
        return redirect(url_for('main.login', msg="Palavra-passe atualizada com sucesso!"))
        
    return render_template_string(AUTH_HTML, title="Redefinir Palavra-passe", subtitle=f"A redefinir o acesso para: {user.username}", action="reset", btn_text="Guardar Nova Palavra-passe")

@bp.route('/api/usuarios', methods=['GET', 'POST'])
@login_required
def gerir_usuarios():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403

    if request.method == 'POST':
        username = request.json.get('username')
        password = request.json.get('password')
        is_admin = request.json.get('is_admin', False)

        if not username or not password:
            return jsonify({"erro": "Preencha todos os campos"}), 400
        if User.query.filter_by(username=username).first():
            return jsonify({"erro": "Este nome de utilizador já existe"}), 400

        novo_user = User(empresa_id=current_user.empresa_id, username=username, password_hash=generate_password_hash(password), is_admin=is_admin)
        db.session.add(novo_user)
        db.session.commit()
        return jsonify({"status": "sucesso"})

    users = User.query.filter_by(empresa_id=current_user.empresa_id).all()
    return jsonify([{"id": u.id, "username": u.username, "is_admin": u.is_admin} for u in users])

@bp.route('/api/usuarios/<int:id>', methods=['DELETE'])
@login_required
def apagar_usuario(id):
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    if id == current_user.id: return jsonify({"erro": "Não pode apagar a sua própria conta"}), 400
    user = User.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if user:
        Negocio.query.filter_by(user_id=id, empresa_id=current_user.empresa_id).update({'user_id': None})
        db.session.delete(user)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Utilizador não encontrado"}), 404

# --- CONFIGURAÇÕES E KANBAN ---
@bp.route('/configuracoes/bot', methods=['GET', 'POST'])
@login_required
def configuracoes_bot():
    if not current_user.is_admin:
        return 'Acesso negado.', 403

    empresa = db.session.get(Empresa, current_user.empresa_id)
    etapas = Etapa.query.filter_by(empresa_id=empresa.id).order_by(Etapa.id).all()
    erro = None
    form_values = {}

    if request.method == 'POST':
        mensagem_saudacao = (request.form.get('mensagem_saudacao') or '').strip()
        prompt_personalidade = (
            request.form.get('prompt_personalidade') or ''
        ).strip() or 'Responda de forma curta, direta e amigável.'
        tom_resposta = (request.form.get('tom_resposta') or '').strip()
        mensagem_transbordo = (request.form.get('mensagem_transbordo') or '').strip()
        usar_menu = request.form.get('usar_menu_inicial') == 'on'
        etapas_menu = {}
        ids_exibidos = set(request.form.getlist('etapa_exibir_no_menu'))

        if not mensagem_saudacao or len(mensagem_saudacao) > 2000:
            erro = 'A saudação deve ter entre 1 e 2000 caracteres.'
        elif len(prompt_personalidade) > 10000:
            erro = 'As orientações para a assistente devem ter no máximo 10000 caracteres.'
        elif tom_resposta not in {'Profissional', 'Descontraído', 'Empático'}:
            erro = 'Escolha um dos tons de resposta disponíveis.'
        elif not mensagem_transbordo or len(mensagem_transbordo) > 500:
            erro = 'A mensagem de transbordo deve ter entre 1 e 500 caracteres.'
        else:
            numeros_usados = set()
            for etapa in etapas:
                etapa_id = str(etapa.id)
                numero = (request.form.get(f'etapa_numero_{etapa.id}') or '').strip()
                if etapa_id not in ids_exibidos:
                    etapas_menu[etapa.id] = None
                    continue
                if len(numeros_usados) >= 9:
                    erro = 'O menu pode ter no máximo 9 etapas.'
                    break
                try:
                    numero_int = int(numero)
                except ValueError:
                    erro = 'Cada etapa do menu precisa ter um número entre 1 e 9.'
                    break
                if numero_int < 1 or numero_int > 9 or numero_int in numeros_usados:
                    erro = 'Os números das etapas devem ser únicos e ficar entre 1 e 9.'
                    break
                numeros_usados.add(numero_int)
                etapas_menu[etapa.id] = numero_int

            if not erro and usar_menu and not numeros_usados:
                erro = 'Selecione ao menos uma etapa antes de ativar o menu.'

        form_values = {
            'mensagem_saudacao': mensagem_saudacao,
            'prompt_personalidade': prompt_personalidade,
            'tom_resposta': tom_resposta,
            'mensagem_transbordo': mensagem_transbordo,
            'etapas_menu': etapas_menu,
        }
        if erro:
            return render_template(
                'configuracoes.html', empresa=empresa, etapas=etapas, erro=erro,
                form_ativar=usar_menu, form_values=form_values,
            ), 400

        empresa.usar_menu_inicial = usar_menu
        empresa.mensagem_saudacao = mensagem_saudacao
        empresa.prompt_personalidade = prompt_personalidade
        empresa.tom_resposta = tom_resposta
        empresa.mensagem_transbordo = mensagem_transbordo
        for etapa in etapas:
            numero = etapas_menu.get(etapa.id)
            etapa.exibir_no_menu = numero is not None
            etapa.numero_menu = numero
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            current_app.logger.exception('Falha ao salvar as configurações da empresa %s', empresa.id)
            return render_template(
                'configuracoes.html', empresa=empresa, etapas=etapas,
                erro='Não foi possível salvar as configurações. Revise os dados e tente novamente.',
                form_ativar=usar_menu, form_values=form_values,
            ), 409
        return redirect(url_for('main.configuracoes_bot', salvo=1))

    return render_template(
        'configuracoes.html', empresa=empresa, etapas=etapas,
        salvo=request.args.get('salvo') == '1',
    )


@bp.route('/api/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    config = Configuracao.query.filter_by(empresa_id=current_user.empresa_id).first()
    if not config:
        return jsonify({"erro": "Configuração não encontrada"}), 404
    if request.method == 'POST':
        config.prompt_ia = request.json.get('prompt_ia', config.prompt_ia)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"prompt_ia": config.prompt_ia})

@bp.route('/api/assign_user', methods=['POST'])
@login_required
def assign_user():
    dados = request.json
    negocio = Negocio.query.filter_by(id=dados.get('negocio_id'), empresa_id=current_user.empresa_id).first()
    if negocio:
        if current_user.is_admin:
            user_id = dados.get('user_id') or None
            if user_id and not User.query.filter_by(id=user_id, empresa_id=current_user.empresa_id).first():
                return jsonify({"erro": "Utilizador não encontrado"}), 404
            negocio.user_id = user_id
        else:
            negocio.user_id = current_user.id
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Negócio não encontrado"}), 404

@bp.route('/api/etapas', methods=['GET', 'POST'])
@login_required
def gerir_etapas():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    if request.method == 'POST':
        nome = request.json.get('nome')
        if nome:
            db.session.add(Etapa(empresa_id=current_user.empresa_id, nome=nome))
            db.session.commit()
            return jsonify({"status": "sucesso"})
        return jsonify({"erro": "Nome inválido"}), 400
    etapas = Etapa.query.filter_by(empresa_id=current_user.empresa_id).all()
    return jsonify([{"id": e.id, "nome": e.nome, "qtd_negocios": len(e.negocios)} for e in etapas])

@bp.route('/api/etapas/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def editar_etapa(id):
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    etapa = Etapa.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if not etapa: return jsonify({"erro": "Etapa não encontrada"}), 404
    if request.method == 'PUT':
        etapa.nome = request.json.get('nome', etapa.nome)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    if request.method == 'DELETE':
        if len(etapa.negocios) > 0: return jsonify({"erro": "Não é possível apagar uma coluna que contém negócios."}), 400
        db.session.delete(etapa)
        db.session.commit()
        return jsonify({"status": "sucesso"})

# --- ROTAS PRINCIPAIS PROTEGIDAS ---
@bp.route('/')
@login_required
def index():
    etapas = Etapa.query.filter_by(empresa_id=current_user.empresa_id).all()
    users = User.query.filter_by(empresa_id=current_user.empresa_id).all() if current_user.is_admin else []
    return render_template('index.html', etapas=etapas, users=users)


@bp.route('/api/leads', methods=['POST'])
@login_required
def criar_lead():
    dados = request.get_json(silent=True) or {}
    nome = (dados.get('nome') or '').strip()
    telefone = re.sub(r'\D', '', str(dados.get('telefone') or ''))
    titulo = (dados.get('titulo') or '').strip()
    if not nome or len(nome) > 100 or not telefone or len(telefone) > 20:
        return jsonify({"erro": "Informe um nome e um telefone válido."}), 400

    try:
        valor = float(dados.get('valor') or 0)
    except (TypeError, ValueError):
        return jsonify({"erro": "O valor do lead é inválido."}), 400
    if not math.isfinite(valor) or valor < 0:
        return jsonify({"erro": "O valor do lead deve ser zero ou maior."}), 400

    etapa_id = dados.get('etapa_id')
    try:
        etapa_id = int(etapa_id) if etapa_id else None
    except (TypeError, ValueError):
        return jsonify({"erro": "Etapa inválida."}), 400
    etapa = Etapa.query.filter_by(id=etapa_id, empresa_id=current_user.empresa_id).first() if etapa_id else Etapa.query.filter_by(empresa_id=current_user.empresa_id).order_by(Etapa.id).first()
    if not etapa:
        return jsonify({"erro": "Etapa não encontrada. Cadastre uma etapa antes de criar leads."}), 400

    pessoa = Pessoa.query.filter_by(empresa_id=current_user.empresa_id, telefone=telefone).first()
    if not pessoa:
        pessoa = Pessoa(
            empresa_id=current_user.empresa_id,
            nome=nome,
            telefone=telefone,
            status_atendimento='menu' if current_user.empresa.usar_menu_inicial else 'ia',
        )
        db.session.add(pessoa)
        db.session.flush()

    negocio = Negocio(
        empresa_id=current_user.empresa_id,
        titulo=(titulo or f"Lead - {nome}")[:100],
        valor=valor,
        pessoa_id=pessoa.id,
        etapa_id=etapa.id,
        user_id=current_user.id,
    )
    db.session.add(negocio)
    db.session.commit()
    return jsonify({"status": "sucesso", "lead_id": negocio.id, "pessoa_id": pessoa.id}), 201


@bp.route('/api/get_chat/<int:pessoa_id>')
@login_required
def get_chat(pessoa_id):
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if not pessoa:
        return jsonify({"erro": "Pessoa não encontrada"}), 404
    mensagens_nao_lidas = Mensagem.query.filter_by(pessoa_id=pessoa_id, empresa_id=current_user.empresa_id, tipo='inbound', lida=False).all()
    for msg in mensagens_nao_lidas: msg.lida = True
    db.session.commit()
    mensagens = Mensagem.query.filter_by(pessoa_id=pessoa.id, empresa_id=current_user.empresa_id).order_by(Mensagem.id).all()
    formatadas = [_formatar_mensagem_chat(mensagem) for mensagem in mensagens]
    etiquetas = [{"id": etiqueta.id, "nome": etiqueta.nome} for etiqueta in pessoa.etiquetas]
    return jsonify({"nome": pessoa.nome, "telefone": pessoa.telefone, "mensagens": formatadas, "etiquetas": etiquetas, "ia_ativa": pessoa.ia_ativa})


def _formatar_mensagem_chat(mensagem):
    return {
        "id": mensagem.id,
        "direcao": mensagem.tipo,
        "conteudo": mensagem.mensagem,
        "hora": mensagem.data_envio.strftime("%H:%M") if mensagem.data_envio else "",
    }


@bp.route('/api/mensagens/novas/<int:ultimo_id_mensagem>')
@login_required
def mensagens_novas(ultimo_id_mensagem):
    pessoa_id = request.args.get('pessoa_id', type=int)
    if not pessoa_id:
        return jsonify({"erro": "Informe a conversa ativa."}), 400
    pessoa = Pessoa.query.filter_by(
        id=pessoa_id, empresa_id=current_user.empresa_id
    ).first()
    if not pessoa:
        return jsonify({"erro": "Conversa não encontrada."}), 404

    mensagens = Mensagem.query.filter(
        Mensagem.empresa_id == current_user.empresa_id,
        Mensagem.pessoa_id == pessoa.id,
        Mensagem.id > ultimo_id_mensagem,
    ).order_by(Mensagem.id).limit(100).all()
    if any(mensagem.tipo == 'inbound' and not mensagem.lida for mensagem in mensagens):
        for mensagem in mensagens:
            if mensagem.tipo == 'inbound':
                mensagem.lida = True
        db.session.commit()
    return jsonify([_formatar_mensagem_chat(mensagem) for mensagem in mensagens])


@bp.route('/api/etiquetas', methods=['GET', 'POST'])
@login_required
def gerir_etiquetas():
    if request.method == 'GET':
        etiquetas = Etiqueta.query.filter_by(empresa_id=current_user.empresa_id).order_by(Etiqueta.nome).all()
        return jsonify([{"id": etiqueta.id, "nome": etiqueta.nome} for etiqueta in etiquetas])

    dados = request.get_json(silent=True) or {}
    nome = (dados.get('nome') or '').strip()
    if not nome or len(nome) > 40:
        return jsonify({"erro": "A etiqueta deve ter entre 1 e 40 caracteres."}), 400
    existente = Etiqueta.query.filter(
        Etiqueta.empresa_id == current_user.empresa_id,
        db.func.lower(Etiqueta.nome) == nome.lower(),
    ).first()
    if existente:
        return jsonify({"erro": "Já existe uma etiqueta com esse nome."}), 409

    etiqueta = Etiqueta(empresa_id=current_user.empresa_id, nome=nome)
    db.session.add(etiqueta)
    db.session.commit()
    return jsonify({"id": etiqueta.id, "nome": etiqueta.nome}), 201


@bp.route('/api/conversas/<int:pessoa_id>/etiquetas', methods=['PUT'])
@login_required
def atualizar_etiquetas_conversa(pessoa_id):
    dados = request.get_json(silent=True) or {}
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if not pessoa:
        return jsonify({"erro": "Conversa não encontrada."}), 404

    etiqueta = Etiqueta.query.filter_by(
        id=dados.get('etiqueta_id'), empresa_id=current_user.empresa_id
    ).first()
    if not etiqueta:
        return jsonify({"erro": "Etiqueta não encontrada."}), 404

    acao = dados.get('acao', 'adicionar')
    if acao == 'adicionar' and etiqueta not in pessoa.etiquetas:
        pessoa.etiquetas.append(etiqueta)
    elif acao == 'remover':
        pessoa.etiquetas.remove(etiqueta) if etiqueta in pessoa.etiquetas else None
    elif acao != 'adicionar':
        return jsonify({"erro": "Ação inválida."}), 400
    db.session.commit()
    etiquetas = [{"id": item.id, "nome": item.nome} for item in pessoa.etiquetas]
    return jsonify({"etiquetas": etiquetas})


@bp.route('/api/conversas/<int:pessoa_id>/assistente-ia', methods=['POST'])
@login_required
def atualizar_assistente_conversa(pessoa_id):
    dados = request.get_json(silent=True) or {}
    ativa = dados.get('ativa')
    if not isinstance(ativa, bool):
        return jsonify({"erro": "Informe se o assistente deve ficar ativo."}), 400
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if not pessoa:
        return jsonify({"erro": "Conversa não encontrada."}), 404
    pessoa.ia_ativa = ativa
    pessoa.status_atendimento = 'ia' if ativa else 'humano'
    db.session.commit()
    return jsonify({"status": "sucesso", "ia_ativa": pessoa.ia_ativa})

@bp.route('/api/send_message', methods=['POST'])
@login_required
def send_message():
    dados = request.json
    pessoa_id = dados.get('pessoa_id') 
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if not pessoa:
        return jsonify({"erro": "Pessoa não encontrada"}), 404
    numero = pessoa.telefone
    texto = dados.get('texto', '')
    media_b64 = dados.get('media')
    api_url = current_app.config['EVOLUTION_API_URL']
    api_key = current_app.config['EVOLUTION_API_KEY']
    instance_name = current_user.empresa.instancia_whatsapp

    if media_b64:
        header, encoded = media_b64.split(",", 1)
        mimetype = header.split(":")[1].split(";")[0]
        mtype = 'image' if 'image' in mimetype else 'audio' if 'audio' in mimetype else 'video' if 'video' in mimetype else 'document'
        res = requests.post(f"{api_url}/message/sendMedia/{instance_name}", headers={"apikey": api_key}, json={"number": numero, "mediatype": mtype, "mimetype": mimetype, "caption": texto, "media": encoded, "fileName": dados.get('fileName', 'arquivo')})
        if res.status_code in [200, 201]:
            db.session.add(Mensagem(empresa_id=current_user.empresa_id, pessoa_id=pessoa_id, mensagem=json.dumps({"type": mtype, "content": media_b64, "caption": texto}), tipo='outbound', data_envio=datetime.now()))
            db.session.commit()
            return jsonify({"status": "sucesso"})
    else:
        if enviar_mensagem_whatsapp(numero, texto, instance_name):
            db.session.add(Mensagem(empresa_id=current_user.empresa_id, pessoa_id=pessoa_id, mensagem=texto, tipo='outbound', data_envio=datetime.now()))
            db.session.commit()
            return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Falha no envio"}), 500

@bp.route('/api/update_deal_stage', methods=['POST'])
@login_required
def update_deal_stage():
    negocio = Negocio.query.filter_by(id=request.json.get('negocio_id'), empresa_id=current_user.empresa_id).first()
    etapa = Etapa.query.filter_by(id=request.json.get('nova_etapa_id'), empresa_id=current_user.empresa_id).first()
    if negocio and etapa:
        negocio.etapa_id = etapa.id
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Não encontrado"}), 404

# --- WEBHOOK WHATSAPP: RECEBE E ENFILEIRA ---
@bp.route('/webhook/whatsapp', methods=['POST'])
def webhook_whatsapp():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"erro": "Payload JSON inválido."}), 400

    try:
        processar_mensagem_whatsapp.delay(payload)
    except Exception:
        current_app.logger.exception('Não foi possível enfileirar mensagem WhatsApp.')
        return jsonify({"status": "queue_unavailable"}), 503
    return jsonify({"status": "queued"}), 200

# --- ROTAS AUXILIARES E MÉTRICAS ---
@bp.route('/api/check_updates')
@login_required
def check_updates():
    ultima_msg = Mensagem.query.filter_by(empresa_id=current_user.empresa_id).order_by(Mensagem.id.desc()).first()
    if ultima_msg:
        txt = ultima_msg.mensagem
        try: txt = f"📎 [Ficheiro] {json.loads(txt).get('caption', '')}"
        except: pass
        return jsonify({"ultima_mensagem_id": ultima_msg.id, "remetente": ultima_msg.pessoa.nome, "texto": txt, "tipo": ultima_msg.tipo})
    return jsonify({"ultima_mensagem_id": 0})

@bp.route('/api/get_qr')
@login_required
def get_qr():
    try:
        res = requests.get(
            f"{current_app.config['EVOLUTION_API_URL']}/instance/connect/{current_user.empresa.instancia_whatsapp}",
            headers={"apikey": current_app.config['EVOLUTION_API_KEY']}
        ).json()
        
        # Isto vai imprimir a resposta real nos Logs do Render!
        print("RESPOSTA HETZNER:", res)

        if res.get('instance', {}).get('state') == 'open': 
            return jsonify({"status": "connected"}), 200
            
        b64 = res.get('base64') or (res.get('qrcode', {}).get('base64') if isinstance(res.get('qrcode'), dict) else None)
        
        if b64:
            return jsonify({"status": "qr", "qr_base64": b64}), 200
        else:
            # Em vez de Erro 400, dizemos ao navegador para apenas aguardar pacificamente
            return jsonify({"status": "pending", "detalhe": "A aguardar QR Code da API"}), 200
            
    except Exception as e:
        print("ERRO GET_QR:", e)
        return jsonify({"status": "error", "erro": str(e)}), 200

@bp.route('/api/disconnect', methods=['POST'])
@login_required
def disconnect_whatsapp():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado."}), 403
    requests.delete(f"{current_app.config['EVOLUTION_API_URL']}/instance/logout/{current_user.empresa.instancia_whatsapp}", headers=evolution_headers())
    return jsonify({"status": "desconectado"})

@bp.route('/api/metrics')
@login_required
def metrics():
    q_negocios = Negocio.query.filter_by(empresa_id=current_user.empresa_id)
    if not current_user.is_admin:
        q_negocios = q_negocios.filter_by(user_id=current_user.id)
    total = q_negocios.count()
    funil, fechamentos = [], 0
    
    etapas = Etapa.query.filter_by(empresa_id=current_user.empresa_id).all()
    for e in etapas:
        qtd = q_negocios.filter_by(etapa_id=e.id).count()
        if 'fechamento' in e.nome.lower() or 'ganho' in e.nome.lower(): fechamentos += qtd
        funil.append({"nome": e.nome, "quantidade": qtd, "porcentagem": (qtd/total*100) if total>0 else 0})

    tempos = []
    pessoas = Pessoa.query.filter_by(empresa_id=current_user.empresa_id).all()
    for pessoa in pessoas:
        msgs = Mensagem.query.filter_by(empresa_id=current_user.empresa_id, pessoa_id=pessoa.id).order_by(Mensagem.data_envio).all()
        hin = None
        for m in msgs:
            if not m.data_envio: continue
            if m.tipo == 'inbound' and not hin: hin = m.data_envio
            elif m.tipo == 'outbound' and hin:
                tempos.append((m.data_envio - hin).total_seconds()/60.0)
                hin = None
    
    return jsonify({"total_negocios": total, "taxa_conversao": round((fechamentos/total*100) if total>0 else 0, 1), "funil": funil, "sla_minutos": round(sum(tempos)/len(tempos) if tempos else 0, 1)})