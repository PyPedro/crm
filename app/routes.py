import requests
import json
import re
import secrets
import unicodedata
import math
import base64
from flask import Blueprint, request, jsonify, render_template, current_app, redirect, url_for, render_template_string, session, flash
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

from app import oauth
from app.models import (
    FUSO_HORARIO_BR,
    db,
    Empresa,
    Etapa,
    Negocio,
    Pessoa,
    Mensagem,
    User,
    Configuracao,
    Etiqueta,
    RespostaAutomatica,
    LogotipoGlobal,
    hora_atual_br,
)
from app.services.whatsapp import enviar_mensagem_whatsapp
from app.tasks import processar_mensagem_whatsapp

bp = Blueprint('main', __name__)


@bp.before_app_request
def bloquear_empresa_suspensa():
    if not current_user.is_authenticated:
        return None

    if current_user.is_super_admin and request.endpoint in {
        'main.optmiza_master', 'main.toggle_empresa',
        'main.admin_usuarios_empresa', 'main.admin_gerar_reset_link_usuario',
        'main.admin_upload_logo_global', 'main.admin_apagar_logo_global',
        'main.admin_empresas_pendentes', 'main.admin_ativar_empresa', 'main.admin_nova_empresa',
        'main.admin_gerar_convite_empresa', 'main.admin_config_empresa', 'main.admin_branding_empresa'
    }:
        return None

    if current_user.empresa and not current_user.empresa.is_ativa:
        logout_user()
        return 'Conta suspensa. Contacte o suporte.', 403

    return None


def get_serializer():
    return URLSafeTimedSerializer(current_app.config.get('SECRET_KEY', 'optmiza-secure-key-2026'))


def _roteiro_menu_padrao(etapas):
    etapas_menu = [etapa for etapa in etapas if etapa.exibir_no_menu]
    etapas_menu = etapas_menu or etapas
    opcoes = []
    numeros_usados = set()
    proximo_numero = 1
    for etapa in etapas_menu:
        numero = etapa.numero_menu
        if not isinstance(numero, int) or numero < 1 or numero > 9 or numero in numeros_usados:
            while proximo_numero in numeros_usados:
                proximo_numero += 1
            numero = proximo_numero
        if numero > 9:
            break
        numeros_usados.add(numero)
        opcoes.append({
            "numero": numero,
            "texto": etapa.nome,
            "etapa_id": etapa.id,
            "proximo_no": None,
        })
    return {"no_inicial": "inicio", "nos": [{
        "id": "inicio",
        "mensagem": "Como podemos ajudar?",
        "opcoes": opcoes,
    }]}


def _validar_roteiro_menu(roteiro, etapas):
    if not isinstance(roteiro, dict):
        return None, "O roteiro deve ser um objeto JSON."
    no_inicial = roteiro.get('no_inicial')
    nos_lista = roteiro.get('nos')
    if not isinstance(no_inicial, str) or not isinstance(nos_lista, list) or not nos_lista:
        return None, "Informe o nó inicial e ao menos um nó no roteiro."
    if len(nos_lista) > 100:
        return None, "O roteiro pode ter no máximo 100 nós."

    etapa_ids = {etapa.id for etapa in etapas}
    nos = {}
    for indice, no in enumerate(nos_lista, 1):
        if not isinstance(no, dict):
            return None, f"O nó {indice} do roteiro é inválido."
        no_id = no.get('id')
        mensagem = no.get('mensagem')
        opcoes = no.get('opcoes')
        if not isinstance(no_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', no_id):
            return None, f"O identificador do nó {indice} é inválido."
        if no_id in nos:
            return None, f"O identificador de nó {no_id} está repetido."
        if not isinstance(mensagem, str) or not mensagem.strip() or len(mensagem) > 2000:
            return None, f"O nó {no_id} precisa de uma mensagem de até 2000 caracteres."
        if not isinstance(opcoes, list) or not 1 <= len(opcoes) <= 9:
            return None, f"O nó {no_id} precisa ter entre 1 e 9 opções."

        opcoes_normalizadas = []
        numeros_usados = set()
        for opcao_indice, opcao in enumerate(opcoes, 1):
            if not isinstance(opcao, dict):
                return None, f"A opção {opcao_indice} do nó {no_id} é inválida."
            numero = opcao.get('numero')
            texto = opcao.get('texto')
            etapa_id = opcao.get('etapa_id')
            proximo_no = opcao.get('proximo_no')
            if isinstance(numero, bool) or not isinstance(numero, int) or not 1 <= numero <= 9 or numero in numeros_usados:
                return None, f"Os números das opções do nó {no_id} devem ser únicos, de 1 a 9."
            if not isinstance(texto, str) or not texto.strip() or len(texto) > 200:
                return None, f"A opção {numero} do nó {no_id} precisa de um texto de até 200 caracteres."
            if isinstance(etapa_id, bool) or not isinstance(etapa_id, int) or etapa_id not in etapa_ids:
                return None, f"A opção {numero} do nó {no_id} aponta para uma etapa inexistente."
            if proximo_no is not None and (
                not isinstance(proximo_no, str)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', proximo_no)
            ):
                return None, f"O próximo nó da opção {numero} do nó {no_id} é inválido."
            numeros_usados.add(numero)
            opcoes_normalizadas.append({
                "numero": numero,
                "texto": texto.strip(),
                "etapa_id": etapa_id,
                "proximo_no": proximo_no,
            })
        nos[no_id] = {
            "id": no_id,
            "mensagem": mensagem.strip(),
            "opcoes": opcoes_normalizadas,
        }

    if no_inicial not in nos:
        return None, "O nó inicial não existe no roteiro."

    pais = {no_id: 0 for no_id in nos}
    for no in nos.values():
        for opcao in no['opcoes']:
            proximo_no = opcao['proximo_no']
            if proximo_no is None:
                continue
            if proximo_no not in nos:
                return None, f"A opção aponta para o nó inexistente {proximo_no}."
            pais[proximo_no] += 1
            if pais[proximo_no] > 1:
                return None, f"O nó {proximo_no} deve ter apenas uma opção de entrada."
    if pais[no_inicial] != 0:
        return None, "O nó inicial não pode ser destino de outra opção."
    if any(pais[no_id] != 1 for no_id in nos if no_id != no_inicial):
        return None, "Todos os nós devem estar ligados ao nó inicial."

    visitados = set()
    em_visita = set()

    def visitar(no_id):
        if no_id in em_visita:
            return False
        if no_id in visitados:
            return True
        em_visita.add(no_id)
        for opcao in nos[no_id]['opcoes']:
            destino = opcao['proximo_no']
            if destino is not None and not visitar(destino):
                return False
        em_visita.remove(no_id)
        visitados.add(no_id)
        return True

    if not visitar(no_inicial):
        return None, "O roteiro não pode conter ciclos."
    if len(visitados) != len(nos):
        return None, "Todos os nós devem estar ligados ao nó inicial."

    return {"no_inicial": no_inicial, "nos": list(nos.values())}, None


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


def gerar_username_unico(base_str):
    limpo = re.sub(r'[^a-zA-Z0-9._]', '', base_str.lower())[:35] or 'usuario'
    candidato = limpo
    contador = 1
    while User.query.filter_by(username=candidato).first():
        candidato = f"{limpo}{contador}"
        contador += 1
    return candidato


AUTH_HTML = """
<!DOCTYPE html>
<html lang="pt">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - Optmiza</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
    <style>
        body { 
            font-family: 'Plus Jakarta Sans', sans-serif; 
            background-color: #F8FAFC;
            {% if bg_b64 %}
            background-image: url('{{ bg_b64 }}');
            background-size: cover;
            background-position: center;
            background-repeat: no-repeat;
            background-attachment: fixed;
            {% endif %}
        }
        :root { --cor-primaria: {{ cor_primaria or '#2563eb' }}; }
    </style>
</head>
<body class="min-h-screen flex flex-col items-center justify-center p-4">
    <div class="bg-white/95 backdrop-blur-md p-8 rounded-2xl shadow-xl w-full max-w-[400px] border border-slate-100 mb-6">
        {% if logo_b64 %}
            <div class="h-16 w-32 mx-auto flex items-center justify-center mb-4">
                <img src="{{ logo_b64 }}" alt="Logo" class="max-h-full max-w-full object-contain">
            </div>
        {% else %}
            <div class="w-12 h-12 rounded-xl mx-auto flex items-center justify-center text-white font-bold text-2xl mb-4 shadow-md" style="background-color: var(--cor-primaria);">O</div>
        {% endif %}
        
        <h2 class="text-2xl font-bold text-center text-slate-800 mb-2">{{ title }}</h2>
        <p class="text-sm text-slate-500 text-center mb-6">{{ subtitle }}</p>
        
        {% if erro %}
        <div class="bg-red-50 border border-red-100 text-red-600 p-3 rounded-xl text-sm mb-5 font-semibold text-center">{{ erro }}</div>
        {% endif %}

        {% if action == 'invite' and token %}
        <a href="{{ url_for('main.invite_google', token=token) }}" class="w-full bg-white hover:bg-slate-50 text-slate-700 font-bold py-3 px-4 rounded-xl border border-slate-200 transition-all shadow-sm flex items-center justify-center gap-3 mb-5 text-sm">
            <svg class="w-5 h-5" viewBox="0 0 24 24">
                <path fill="#4285F4" d="M23.745 12.27c0-.7-.06-1.4-.19-2.07H12v4.51h6.6c-.29 1.52-1.14 2.82-2.4 3.68v3.05h3.88c2.27-2.09 3.665-5.17 3.665-9.17z"/>
                <path fill="#34A853" d="M12 24c3.3 0 6.08-1.09 8.1-2.96l-3.88-3.05c-1.1.74-2.5 1.18-4.22 1.18-3.25 0-6-2.2-6.98-5.15H1.01v3.14C3.02 21.15 7.21 24 12 24z"/>
                <path fill="#FBBC05" d="M5.02 14.02c-.25-.74-.39-1.54-.39-2.37s.14-1.63.39-2.37V6.14H1.01C.37 7.42 0 8.87 0 10.4s.37 2.98 1.01 4.26l4.01-3.14z"/>
                <path fill="#EA4335" d="M12 4.75c1.8 0 3.41.62 4.68 1.84l3.5-3.5C18.07 1.19 15.3 0 12 0 7.21 0 3.02 2.85 1.01 6.84l4.01 3.14c.98-2.95 3.73-5.23 6.98-5.23z"/>
            </svg>
            Continuar com o Google (Gmail)
        </a>

        <div class="relative flex py-2 items-center mb-4">
            <div class="flex-grow border-t border-slate-200"></div>
            <span class="flex-shrink mx-4 text-slate-400 text-xs font-semibold uppercase">ou com utilizador</span>
            <div class="flex-grow border-t border-slate-200"></div>
        </div>
        {% endif %}
        
        {% if action in ['invite', 'reset'] %}
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
            
            <button type="submit" class="w-full text-white font-bold py-3.5 rounded-xl transition-all shadow-md hover:opacity-95 mt-2" style="background-color: var(--cor-primaria);">
                {{ btn_text }}
            </button>
        </form>
        {% endif %}
    </div>
    
    <p class="text-slate-500 text-[10px] tracking-wide text-center bg-white/85 backdrop-blur-sm px-4 py-1.5 rounded-full shadow-sm">
        Desenvolvido por <strong class="text-slate-700">Pedro Marinho</strong>. Um produto <strong style="color: var(--cor-primaria);">Optmiza</strong>.
    </p>
</body>
</html>
"""


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

    empresa = Empresa(nome=nome_empresa, instancia_whatsapp=instancia, is_ativa=False)
    db.session.add(empresa)
    db.session.flush()
    db.session.add(User(empresa_id=empresa.id, username=username, password_hash=generate_password_hash(password), is_admin=True))
    db.session.add_all([Etapa(empresa_id=empresa.id, nome=nome) for nome in ('Qualificação', 'Contato Feito', 'Proposta Enviada', 'Fechamento')])
    db.session.add(Configuracao(empresa_id=empresa.id, prompt_ia=""))
    db.session.commit()
    flash('Cadastro realizado! A sua conta passará por uma análise e será ativada em breve pela nossa equipa.', 'success')
    return redirect(url_for('main.login'))


@bp.route('/login', methods=['GET', 'POST'])
def login():
    msg_sucesso = request.args.get('msg')
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            if user.empresa and not user.empresa.is_ativa:
                flash('Conta pendente de aprovação do administrador', 'warning')
                return redirect(url_for('main.login'))
            login_user(user)
            return redirect(url_for('main.index'))
            
        logos_globais = LogotipoGlobal.query.all()
        return render_template('login.html', erro="Credenciais inválidas", empresas=logos_globais)
        
    logos_globais = LogotipoGlobal.query.all()
    return render_template('login.html', msg=msg_sucesso, empresas=logos_globais)


@bp.route('/login/google')
def login_google():
    if not current_app.config.get('GOOGLE_CLIENT_ID') or not current_app.config.get('GOOGLE_CLIENT_SECRET'):
        return redirect(url_for('main.login', msg='O acesso com Google ainda não está configurado.'))
    callback_url = current_app.config.get('GOOGLE_REDIRECT_URI') or url_for('main.authorize_google', _external=True)
    return oauth.google.authorize_redirect(callback_url)


@bp.route('/invite/<token>/google')
def invite_google(token):
    try:
        data = get_serializer().loads(token, max_age=86400)
        if data.get('action') != 'invite':
            raise ValueError
    except Exception:
        return render_template_string(AUTH_HTML, title="Convite Inválido", subtitle="Este link expirou ou é inválido.", erro="Solicite um novo link.", action="error")

    session['invite_token_google'] = token
    if not current_app.config.get('GOOGLE_CLIENT_ID') or not current_app.config.get('GOOGLE_CLIENT_SECRET'):
        return redirect(url_for('main.processar_convite', token=token))
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
    nome_google = (perfil.get('name') or email.split('@')[0]).strip()
    if not email or not google_id or not perfil.get('email_verified'):
        return redirect(url_for('main.login', msg='O Google não confirmou um endereço de e-mail válido.'))

    # --- FLUXO 1: VEIO DE UM LINK DE CONVITE ---
    invite_token = session.pop('invite_token_google', None)
    if invite_token:
        try:
            data = get_serializer().loads(invite_token, max_age=86400)
            empresa = db.session.get(Empresa, data.get('empresa_id'))
            if empresa:
                user = User.query.filter((User.email == email) | (User.google_id == google_id)).first()
                if not user:
                    novo_username = gerar_username_unico(nome_google or email.split('@')[0])
                    user = User(
                        empresa_id=empresa.id,
                        username=novo_username,
                        email=email,
                        google_id=google_id,
                        password_hash=generate_password_hash(secrets.token_urlsafe(32)),
                        is_admin=data.get('is_admin', True)
                    )
                    db.session.add(user)
                else:
                    user.google_id = google_id
                    if not user.empresa_id:
                        user.empresa_id = empresa.id
                        user.is_admin = data.get('is_admin', True)
                db.session.commit()
                login_user(user)
                return redirect(url_for('main.index'))
        except Exception:
            flash('O link de convite expirou ou é inválido.', 'error')
            return redirect(url_for('main.login'))

    # --- FLUXO 2: LOGIN NORMAL COM GOOGLE ---
    user = User.query.filter_by(email=email).first()
    if not user:
        user = User.query.filter_by(google_id=google_id).first()
    
    if user:
        if user.google_id and user.google_id != google_id:
            return redirect(url_for('main.login', msg='Este e-mail está associado a outra conta Google.'))
        if not user.empresa and not user.is_super_admin:
            session['registro_google_email'] = email
            session['registro_google_google_id'] = google_id
            return redirect(url_for('main.completar_cadastro'))
        if user.empresa and not user.empresa.is_ativa and not user.is_super_admin:
            flash('Conta pendente de aprovação do administrador', 'warning')
            return redirect(url_for('main.login'))
        if not user.google_id:
            user.google_id = google_id
            db.session.commit()
            
        login_user(user)
        return redirect(url_for('main.index'))

    session['registro_google_email'] = email
    session['registro_google_google_id'] = google_id
    return redirect(url_for('main.completar_cadastro'))


@bp.route('/completar_cadastro', methods=['GET', 'POST'])
def completar_cadastro():
    email = session.get('registro_google_email')
    google_id = session.get('registro_google_google_id')
    if not email or not google_id:
        flash('Inicie o cadastro novamente com a sua conta Google.', 'warning')
        return redirect(url_for('main.login'))

    if request.method == 'GET':
        return render_template('completar_cadastro.html', email=email)

    nome_utilizador = (request.form.get('nome_utilizador') or '').strip()
    nome_empresa = (request.form.get('nome_empresa') or '').strip()
    if not nome_utilizador or len(nome_utilizador) > 50:
        return render_template('completar_cadastro.html', email=email, erro='Informe um nome com até 50 caracteres.', nome_utilizador=nome_utilizador, nome_empresa=nome_empresa), 400
    if not nome_empresa or len(nome_empresa) > 120:
        return render_template('completar_cadastro.html', email=email, erro='Informe o nome da empresa com até 120 caracteres.', nome_utilizador=nome_utilizador, nome_empresa=nome_empresa), 400
        
    user_existente = User.query.filter_by(email=email).first()
    if user_existente and user_existente.empresa_id is not None:
        session.pop('registro_google_email', None)
        session.pop('registro_google_google_id', None)
        flash('Esta conta já está registada. Faça login para continuar.', 'warning')
        return redirect(url_for('main.login'))
        
    if User.query.filter_by(username=nome_utilizador).first() and not user_existente:
        return render_template('completar_cadastro.html', email=email, erro='Este nome de utilizador já está em uso.', nome_utilizador=nome_utilizador, nome_empresa=nome_empresa), 409

    try:
        instancia = configurar_instancia_whatsapp(nome_empresa)
    except RuntimeError as erro:
        return render_template('completar_cadastro.html', email=email, erro=str(erro), nome_utilizador=nome_utilizador, nome_empresa=nome_empresa), 503
    except requests.RequestException:
        return render_template('completar_cadastro.html', email=email, erro='Não foi possível preparar o WhatsApp.', nome_utilizador=nome_utilizador, nome_empresa=nome_empresa), 502

    empresa = Empresa(nome=nome_empresa, instancia_whatsapp=instancia, is_ativa=False)
    db.session.add(empresa)
    db.session.flush()
    
    if user_existente:
        user_existente.empresa_id = empresa.id
        user_existente.username = nome_utilizador
        user_existente.google_id = google_id
    else:
        db.session.add(User(empresa_id=empresa.id, username=nome_utilizador, email=email, google_id=google_id, password_hash=generate_password_hash(secrets.token_urlsafe(32)), is_admin=True))
        
    db.session.add_all([Etapa(empresa_id=empresa.id, nome=etapa) for etapa in ('Qualificação', 'Contato Feito', 'Proposta Enviada', 'Fechamento')])
    db.session.add(Configuracao(empresa_id=empresa.id, prompt_ia=""))
    db.session.commit()
    session.pop('registro_google_email', None)
    session.pop('registro_google_google_id', None)
    flash('Cadastro recebido! Aguarde a aprovação do administrador.', 'warning')
    return redirect(url_for('main.login'))


@bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.login'))


# --- PAINEL DO SUPER ADMIN (MASTER) ---
@bp.route('/optmiza-master', methods=['GET'])
def optmiza_master():
    if not current_user.is_authenticated or not current_user.is_super_admin:
        return 'Acesso proibido.', 403

    empresas = Empresa.query.order_by(Empresa.id.desc()).all()
    logos = LogotipoGlobal.query.order_by(LogotipoGlobal.id.desc()).all()
    
    total_usuarios = dict(
        db.session.query(User.empresa_id, db.func.count(User.id))
        .group_by(User.empresa_id)
        .all()
    )
    csrf_token = session.setdefault('master_csrf_token', secrets.token_urlsafe(32))
    return render_template(
        'master.html',
        empresas=empresas,
        logos=logos,
        total_usuarios=total_usuarios,
        csrf_token=csrf_token,
    )


@bp.route('/optmiza-master/empresa/<int:id>/usuarios', methods=['GET'])
@login_required
def admin_usuarios_empresa(id):
    if not current_user.is_super_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    empresa = db.session.get(Empresa, id)
    if not empresa:
        return jsonify({"erro": "Empresa não encontrada"}), 404
    usuarios = User.query.filter_by(empresa_id=empresa.id).order_by(User.id).all()
    return jsonify([{
        "id": usuario.id,
        "username": usuario.username,
        "email": usuario.email,
        "is_admin": usuario.is_admin,
    } for usuario in usuarios])


@bp.route('/optmiza-master/usuarios/<int:user_id>/reset_link', methods=['POST'])
@login_required
def admin_gerar_reset_link_usuario(user_id):
    if not current_user.is_super_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    usuario = db.session.get(User, user_id)
    if not usuario:
        return jsonify({"erro": "Utilizador não encontrado"}), 404
    token = get_serializer().dumps({"user_id": usuario.id, "action": "reset"})
    return jsonify({
        "link": url_for('main.processar_reset', token=token, _external=True)
    })


@bp.route('/optmiza-master/empresa/<int:id>/toggle', methods=['POST'])
def toggle_empresa(id):
    if not current_user.is_authenticated or not current_user.is_super_admin:
        return 'Acesso proibido.', 403
    token_enviado = request.form.get('csrf_token', '')
    token_sessao = session.get('master_csrf_token', '')
    if not token_sessao or not secrets.compare_digest(token_enviado, token_sessao):
        return 'Requisição inválida.', 400

    empresa = db.session.get(Empresa, id)
    if empresa:
        empresa.is_ativa = not empresa.is_ativa
        db.session.commit()
    return redirect(url_for('main.optmiza_master'))


# --- ROTAS DE GESTÃO DIRETA (SUPER ADMIN) ---
@bp.route('/optmiza-master/empresa/<int:id>/invite', methods=['POST'])
@login_required
def admin_gerar_convite_empresa(id):
    if not current_user.is_super_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    empresa = db.session.get(Empresa, id)
    if not empresa:
        return jsonify({"erro": "Empresa não encontrada"}), 404
    
    token = get_serializer().dumps({"is_admin": True, "empresa_id": empresa.id, "action": "invite"})
    return jsonify({"link": url_for('main.processar_convite', token=token, _external=True)})


@bp.route('/optmiza-master/empresa/<int:id>/config', methods=['GET', 'POST'])
@login_required
def admin_config_empresa(id):
    if not current_user.is_super_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    empresa = db.session.get(Empresa, id)
    if not empresa:
        return jsonify({"erro": "Empresa não encontrada"}), 404
    
    if request.method == 'GET':
        regras = RespostaAutomatica.query.filter_by(empresa_id=empresa.id).order_by(RespostaAutomatica.id).all()
        faqs_lista = [{"id": r.id, "pergunta": r.palavra_chave, "resposta": r.resposta} for r in regras]
        etapas = Etapa.query.filter_by(empresa_id=empresa.id).order_by(Etapa.id).all()
        try:
            roteiro_menu = json.loads(empresa.roteiro_menu) if empresa.roteiro_menu else _roteiro_menu_padrao(etapas)
        except ValueError:
            current_app.logger.exception('Roteiro numérico inválido para a empresa %s.', empresa.id)
            return jsonify({"erro": "O roteiro salvo está inválido; corrija-o nas configurações do atendimento."}), 409
        return jsonify({
            "tom_resposta": empresa.tom_resposta or 'Profissional',
            "mensagem_saudacao": empresa.mensagem_saudacao or '',
            "mensagem_transbordo": empresa.mensagem_transbordo or '',
            "prompt_personalidade": empresa.prompt_personalidade or '',
            "faqs": faqs_lista,
            "roteiro_menu": roteiro_menu,
        })
        
    dados = request.get_json(silent=True) or {}
    if 'roteiro_menu' in dados:
        etapas = Etapa.query.filter_by(empresa_id=empresa.id).order_by(Etapa.id).all()
        roteiro_menu, erro = _validar_roteiro_menu(dados['roteiro_menu'], etapas)
        if erro:
            return jsonify({"erro": erro}), 400
        empresa.roteiro_menu = json.dumps(roteiro_menu, ensure_ascii=False)
        empresa.usar_menu_inicial = True
    empresa.tom_resposta = dados.get('tom_resposta', empresa.tom_resposta)
    empresa.mensagem_saudacao = dados.get('mensagem_saudacao', empresa.mensagem_saudacao)
    empresa.mensagem_transbordo = dados.get('mensagem_transbordo', empresa.mensagem_transbordo)
    empresa.prompt_personalidade = dados.get('prompt_personalidade', empresa.prompt_personalidade)

    if 'faqs' in dados:
        faqs = dados.get('faqs') or []
        if not isinstance(faqs, list):
            return jsonify({"erro": "A lista de FAQs é inválida"}), 400

        novas_regras = []
        for item in faqs:
            if not isinstance(item, dict):
                return jsonify({"erro": "Uma FAQ é inválida"}), 400
            pergunta = item.get('pergunta')
            resposta = item.get('resposta')
            if isinstance(pergunta, str) and isinstance(resposta, str):
                pergunta = pergunta.strip()
                resposta = resposta.strip()
                if pergunta and resposta:
                    novas_regras.append(RespostaAutomatica(
                        empresa_id=empresa.id,
                        palavra_chave=pergunta,
                        resposta=resposta
                    ))

        RespostaAutomatica.query.filter_by(empresa_id=empresa.id).delete()
        db.session.add_all(novas_regras)

    db.session.commit()
    return jsonify({"status": "sucesso"})


# --- PERSONALIZAÇÃO VISUAL (LOGO, COR PRIMÁRIA E IMAGEM DE FUNDO DO TERMINAL) ---
@bp.route('/optmiza-master/empresa/<int:id>/branding', methods=['POST'])
@login_required
def admin_branding_empresa(id):
    if not current_user.is_super_admin:
        return 'Acesso proibido.', 403

    empresa = db.session.get(Empresa, id)
    if not empresa:
        flash('Empresa não encontrada.', 'error')
        return redirect(url_for('main.optmiza_master'))

    cor_primaria = (request.form.get('cor_primaria') or '').strip()
    if cor_primaria:
        empresa.cor_primaria = cor_primaria

    # Upload de Logótipo
    file_logo = request.files.get('logo')
    if file_logo and file_logo.filename:
        encoded_logo = base64.b64encode(file_logo.read()).decode('utf-8')
        mimetype_logo = file_logo.mimetype or 'image/png'
        empresa.logo_b64 = f"data:{mimetype_logo};base64,{encoded_logo}"

    if request.form.get('remover_logo') == '1':
        empresa.logo_b64 = ""

    # Upload de Imagem de Fundo (Background)
    file_bg = request.files.get('background')
    if file_bg and file_bg.filename:
        encoded_bg = base64.b64encode(file_bg.read()).decode('utf-8')
        mimetype_bg = file_bg.mimetype or 'image/jpeg'
        empresa.bg_b64 = f"data:{mimetype_bg};base64,{encoded_bg}"

    if request.form.get('remover_bg') == '1':
        empresa.bg_b64 = ""

    db.session.commit()
    flash(f'Identidade visual e imagem de fundo da empresa "{empresa.nome}" atualizadas com sucesso!', 'success')
    return redirect(url_for('main.optmiza_master'))


# --- ROTAS DE LOGÓTIPOS GLOBAIS ---
@bp.route('/optmiza-master/logos', methods=['POST'])
@login_required
def admin_upload_logo_global():
    if not current_user.is_super_admin:
        return 'Acesso proibido.', 403

    file = request.files.get('logo')
    nome_arquivo = request.form.get('nome', 'Logótipo').strip()
    
    if file and file.filename.lower().endswith('.png'):
        encoded_string = base64.b64encode(file.read()).decode('utf-8')
        novo_logo = LogotipoGlobal(
            nome=nome_arquivo or file.filename,
            logo_b64=f"data:image/png;base64,{encoded_string}"
        )
        db.session.add(novo_logo)
        db.session.commit()
        flash('Logótipo adicionado à biblioteca global com sucesso.', 'success')
    else:
        flash('Por favor, selecione um arquivo de imagem em formato PNG.', 'error')
        
    return redirect(url_for('main.optmiza_master'))


@bp.route('/optmiza-master/logos/<int:id>/apagar', methods=['POST'])
@login_required
def admin_apagar_logo_global(id):
    if not current_user.is_super_admin:
        return 'Acesso proibido.', 403

    logo = db.session.get(LogotipoGlobal, id)
    if logo:
        db.session.delete(logo)
        db.session.commit()
        flash('Logótipo removido da biblioteca.', 'success')
    return redirect(url_for('main.optmiza_master'))


# --- CRIAÇÃO DE EMPRESA SEM PRECISAR DE USUÁRIO E SENHA ---
@bp.route('/optmiza-master/empresa/nova', methods=['POST'])
@login_required
def admin_nova_empresa():
    if not current_user.is_super_admin:
        return 'Acesso proibido.', 403

    token_enviado = request.form.get('csrf_token', '')
    token_sessao = session.get('master_csrf_token', '')
    if not token_sessao or not secrets.compare_digest(token_enviado, token_sessao):
        flash('Requisição inválida por segurança.', 'error')
        return redirect(url_for('main.optmiza_master'))

    nome_empresa = (request.form.get('nome_empresa') or '').strip()
    cor_primaria = (request.form.get('cor_primaria') or '#2563eb').strip()

    if not nome_empresa:
        flash('Informe o nome da empresa.', 'error')
        return redirect(url_for('main.optmiza_master'))

    try:
        instancia = configurar_instancia_whatsapp(nome_empresa)
    except Exception as e:
        flash(f'Erro ao conectar à Evolution API: {str(e)}', 'error')
        return redirect(url_for('main.optmiza_master'))

    logo_b64_str = ""
    file_logo = request.files.get('logo')
    if file_logo and file_logo.filename:
        encoded_logo = base64.b64encode(file_logo.read()).decode('utf-8')
        mimetype_logo = file_logo.mimetype or 'image/png'
        logo_b64_str = f"data:{mimetype_logo};base64,{encoded_logo}"

    bg_b64_str = ""
    file_bg = request.files.get('background')
    if file_bg and file_bg.filename:
        encoded_bg = base64.b64encode(file_bg.read()).decode('utf-8')
        mimetype_bg = file_bg.mimetype or 'image/jpeg'
        bg_b64_str = f"data:{mimetype_bg};base64,{encoded_bg}"

    empresa = Empresa(
        nome=nome_empresa,
        instancia_whatsapp=instancia,
        is_ativa=True,
        cor_primaria=cor_primaria,
        logo_b64=logo_b64_str,
        bg_b64=bg_b64_str
    )
    db.session.add(empresa)
    db.session.flush()

    db.session.add_all([Etapa(empresa_id=empresa.id, nome=nome) for nome in ('Qualificação', 'Contato Feito', 'Proposta Enviada', 'Fechamento')])
    db.session.add(Configuracao(empresa_id=empresa.id, prompt_ia=""))
    db.session.commit()

    flash(f'Empresa "{nome_empresa}" criada! Clique no botão "Convite" na linha dela para copiar o link de acesso do cliente.', 'success')
    return redirect(url_for('main.optmiza_master'))


@bp.route('/admin/empresas/pendentes', methods=['GET'])
@login_required
def admin_empresas_pendentes():
    if not current_user.is_super_admin:
        return 'Acesso proibido.', 403
    csrf_token = session.setdefault('admin_empresas_csrf_token', secrets.token_urlsafe(32))
    empresas = Empresa.query.filter_by(is_ativa=False).order_by(Empresa.id).all()
    return render_template('painel_admin.html', empresas=empresas, csrf_token=csrf_token)


@bp.route('/admin/empresas/<int:id>/ativar', methods=['POST'])
@login_required
def admin_ativar_empresa(id):
    if not current_user.is_super_admin:
        return 'Acesso proibido.', 403
    token_enviado = request.form.get('csrf_token', '')
    token_sessao = session.get('admin_empresas_csrf_token', '')
    if not token_sessao or not secrets.compare_digest(token_enviado, token_sessao):
        return 'Requisição inválida.', 400
    empresa = db.session.get(Empresa, id)
    if empresa:
        empresa.is_ativa = True
        db.session.commit()
        flash(f'Empresa "{empresa.nome}" ativada com sucesso.', 'success')
    return redirect(url_for('main.admin_empresas_pendentes'))


@bp.route('/api/invite', methods=['POST'])
@login_required
def gerar_link_convite():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    is_admin = request.json.get('is_admin', False)
    token = get_serializer().dumps({"is_admin": is_admin, "empresa_id": current_user.empresa_id, "action": "invite"})
    return jsonify({"link": url_for('main.processar_convite', token=token, _external=True)})


@bp.route('/invite/<token>', methods=['GET', 'POST'])
def processar_convite(token):
    try:
        data = get_serializer().loads(token, max_age=86400)
        if data.get('action') != 'invite':
            raise ValueError
    except Exception:
        return render_template_string(AUTH_HTML, title="Convite Inválido", subtitle="Este link expirou ou é inválido.", erro="Solicite um novo link.", action="error")

    empresa = db.session.get(Empresa, data.get('empresa_id'))
    if not empresa:
        return render_template_string(AUTH_HTML, title="Convite Inválido", subtitle="A empresa deste convite não existe.", erro="Solicite novo link.", action="error"), 404

    if request.method == 'POST':
        username, password = (request.form.get('username') or '').strip(), request.form.get('password') or ''
        if User.query.filter_by(username=username).first():
            return render_template_string(
                AUTH_HTML, title=f"Entrar em {empresa.nome}", subtitle="Crie o seu acesso ou entre com o Google",
                action="invite", btn_text="Concluir Registo", erro="Este nome de utilizador já está em uso.",
                token=token, logo_b64=empresa.logo_b64, cor_primaria=empresa.cor_primaria, bg_b64=empresa.bg_b64
            )
        novo_user = User(empresa_id=empresa.id, username=username, password_hash=generate_password_hash(password), is_admin=data.get('is_admin', True))
        db.session.add(novo_user)
        db.session.commit()
        login_user(novo_user)
        return redirect(url_for('main.index'))

    return render_template_string(
        AUTH_HTML, title=f"Entrar em {empresa.nome}", subtitle="Crie o seu acesso na plataforma",
        action="invite", btn_text="Concluir Registo", token=token,
        logo_b64=empresa.logo_b64, cor_primaria=empresa.cor_primaria, bg_b64=empresa.bg_b64
    )


@bp.route('/api/usuarios/<int:id>/reset_link', methods=['POST'])
@login_required
def gerar_link_reset(id):
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    user = User.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if not user:
        return jsonify({"erro": "Utilizador não encontrado"}), 404
    token = get_serializer().dumps({"user_id": user.id, "action": "reset"})
    return jsonify({"link": url_for('main.processar_reset', token=token, _external=True)})


@bp.route('/reset/<token>', methods=['GET', 'POST'])
def processar_reset(token):
    try:
        data = get_serializer().loads(token, max_age=3600)
        if data.get('action') != 'reset':
            raise ValueError
    except Exception:
        return render_template_string(AUTH_HTML, title="Link Inválido", subtitle="Este link de segurança expirou.", erro="Solicite novo reset.", action="error")

    user = db.session.get(User, data['user_id'])
    if not user:
        return render_template_string(AUTH_HTML, title="Link Inválido", subtitle="A conta não foi encontrada.", erro="Solicite novo reset.", action="error"), 404
    if request.method == 'POST':
        user.password_hash = generate_password_hash(request.form.get('password'))
        db.session.commit()
        return redirect(url_for('main.login', msg="Palavra-passe atualizada!"))
    return render_template_string(AUTH_HTML, title="Redefinir Palavra-passe", subtitle=f"Acesso para: {user.username}", action="reset", btn_text="Guardar Nova")


@bp.route('/api/usuarios', methods=['GET', 'POST'])
@login_required
def gerir_usuarios():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    if request.method == 'POST':
        username, password, is_admin = request.json.get('username'), request.json.get('password'), request.json.get('is_admin', False)
        if not username or not password:
            return jsonify({"erro": "Preencha todos os campos"}), 400
        if User.query.filter_by(username=username).first():
            return jsonify({"erro": "Nome já existe"}), 400
        db.session.add(User(empresa_id=current_user.empresa_id, username=username, password_hash=generate_password_hash(password), is_admin=is_admin))
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify([{"id": u.id, "username": u.username, "is_admin": u.is_admin} for u in User.query.filter_by(empresa_id=current_user.empresa_id).all()])


@bp.route('/api/usuarios/<int:id>', methods=['DELETE'])
@login_required
def apagar_usuario(id):
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    if id == current_user.id:
        return jsonify({"erro": "Não pode apagar a própria conta"}), 400
    user = User.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if user:
        Negocio.query.filter_by(user_id=id, empresa_id=current_user.empresa_id).update({'user_id': None})
        db.session.delete(user)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Utilizador não encontrado"}), 404


@bp.route('/api/regras', methods=['GET', 'POST'])
@login_required
def gerir_regras():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    if request.method == 'POST':
        dados = request.get_json(silent=True)
        if not isinstance(dados, dict):
            return jsonify({"erro": "O conteúdo enviado é inválido"}), 400

        if 'regras' in dados:
            regras = dados['regras']
            if not isinstance(regras, list) or not regras:
                return jsonify({"erro": "Envie uma lista com pelo menos uma pergunta e resposta"}), 400

            novas_regras = []
            for indice, item in enumerate(regras, start=1):
                if not isinstance(item, dict):
                    return jsonify({"erro": f"A pergunta e resposta da linha {indice} são inválidas"}), 400
                palavra = item.get('palavra_chave')
                resposta = item.get('resposta')
                if not isinstance(palavra, str) or not isinstance(resposta, str):
                    return jsonify({"erro": f"Preencha a pergunta e resposta da linha {indice}"}), 400
                palavra, resposta = palavra.strip(), resposta.strip()
                if not palavra or not resposta or len(palavra) > 100:
                    return jsonify({"erro": f"Verifique a pergunta e resposta da linha {indice}; a pergunta deve ter até 100 caracteres"}), 400
                novas_regras.append(RespostaAutomatica(
                    empresa_id=current_user.empresa_id,
                    palavra_chave=palavra,
                    resposta=resposta,
                ))

            db.session.add_all(novas_regras)
            db.session.commit()
            return jsonify({"status": "sucesso", "quantidade": len(novas_regras)})

        palavra = dados.get('palavra_chave')
        resposta = dados.get('resposta')
        if not isinstance(palavra, str) or not isinstance(resposta, str):
            return jsonify({"erro": "Preencha ambos"}), 400
        palavra, resposta = palavra.strip(), resposta.strip()
        if not palavra or not resposta or len(palavra) > 100:
            return jsonify({"erro": "Preencha ambos"}), 400
        nova_regra = RespostaAutomatica(empresa_id=current_user.empresa_id, palavra_chave=palavra, resposta=resposta)
        db.session.add(nova_regra)
        db.session.commit()
        return jsonify({"status": "sucesso", "id": nova_regra.id})
    return jsonify([{"id": r.id, "palavra_chave": r.palavra_chave, "resposta": r.resposta} for r in RespostaAutomatica.query.filter_by(empresa_id=current_user.empresa_id).all()])


@bp.route('/api/regras/<int:id>', methods=['DELETE'])
@login_required
def apagar_regra(id):
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    regra = RespostaAutomatica.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if regra:
        db.session.delete(regra)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Não encontrada"}), 404


@bp.route('/configuracoes/bot', methods=['GET', 'POST'])
@login_required
def configuracoes_bot():
    if not current_user.is_admin:
        return 'Acesso negado.', 403
    empresa = db.session.get(Empresa, current_user.empresa_id)
    etapas = Etapa.query.filter_by(empresa_id=empresa.id).order_by(Etapa.id).all()
    roteiro_menu_texto = empresa.roteiro_menu or json.dumps(
        _roteiro_menu_padrao(etapas), ensure_ascii=False, indent=2
    )
    erro, form_values = None, {}

    if request.method == 'POST':
        mensagem_saudacao = (request.form.get('mensagem_saudacao') or '').strip()
        prompt_personalidade = (request.form.get('prompt_personalidade') or '').strip() or 'Responda de forma curta, direta e amigável.'
        tom_resposta = (request.form.get('tom_resposta') or '').strip()
        mensagem_transbordo = (request.form.get('mensagem_transbordo') or '').strip()
        roteiro_menu_texto = (request.form.get('roteiro_menu') or '').strip()
        mensagens_etapa = {
            etapa.id: (request.form.get(f'mensagem_etapa_{etapa.id}') or '').strip()
            for etapa in etapas
        }
        roteiro_menu = None

        if not mensagem_saudacao or len(mensagem_saudacao) > 2000:
            erro = 'Saudação inválida.'
        elif len(prompt_personalidade) > 10000:
            erro = 'Orientações grandes demais.'
        elif tom_resposta not in {'Profissional', 'Descontraído', 'Empático'}:
            erro = 'Escolha um tom.'
        elif not mensagem_transbordo or len(mensagem_transbordo) > 500:
            erro = 'Mensagem de transbordo inválida.'
        elif any(len(mensagem) > 2000 for mensagem in mensagens_etapa.values()):
            erro = 'As mensagens das etapas devem ter no máximo 2000 caracteres.'

        if not erro:
            try:
                roteiro_bruto = json.loads(roteiro_menu_texto)
            except (TypeError, ValueError):
                erro = 'O roteiro numérico precisa ser um JSON válido.'
            else:
                roteiro_menu, erro = _validar_roteiro_menu(roteiro_bruto, etapas)

        form_values = {
            'mensagem_saudacao': mensagem_saudacao,
            'prompt_personalidade': prompt_personalidade,
            'tom_resposta': tom_resposta,
            'mensagem_transbordo': mensagem_transbordo,
            'mensagens_etapa': mensagens_etapa,
        }
        if erro:
            return render_template(
                'configuracoes.html',
                empresa=empresa,
                etapas=etapas,
                erro=erro,
                form_values=form_values,
                roteiro_menu_texto=roteiro_menu_texto,
            ), 400

        empresa.usar_menu_inicial = True
        empresa.roteiro_menu = json.dumps(roteiro_menu, ensure_ascii=False)
        empresa.mensagem_saudacao = mensagem_saudacao
        empresa.prompt_personalidade = prompt_personalidade
        empresa.tom_resposta = tom_resposta
        empresa.mensagem_transbordo = mensagem_transbordo
        for etapa in etapas:
            etapa.mensagem_etapa = mensagens_etapa[etapa.id] or None
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return render_template(
                'configuracoes.html',
                empresa=empresa,
                etapas=etapas,
                erro='Erro ao salvar.',
                form_values=form_values,
                roteiro_menu_texto=roteiro_menu_texto,
            ), 409
        return redirect(url_for('main.configuracoes_bot', salvo=1))
    return render_template(
        'configuracoes.html',
        empresa=empresa,
        etapas=etapas,
        salvo=request.args.get('salvo') == '1',
        roteiro_menu_texto=roteiro_menu_texto,
    )


@bp.route('/api/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    config = Configuracao.query.filter_by(empresa_id=current_user.empresa_id).first()
    if request.method == 'POST':
        config.prompt_ia = request.json.get('prompt_ia', config.prompt_ia)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"prompt_ia": config.prompt_ia if config else ""})


@bp.route('/api/assign_user', methods=['POST'])
@login_required
def assign_user():
    negocio = Negocio.query.filter_by(id=request.json.get('negocio_id'), empresa_id=current_user.empresa_id).first()
    if negocio:
        if current_user.is_admin:
            user_id = request.json.get('user_id')
            if user_id and not User.query.filter_by(id=user_id, empresa_id=current_user.empresa_id).first():
                return jsonify({"erro": "Inválido"}), 404
            negocio.user_id = user_id
        else:
            negocio.user_id = current_user.id
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Não encontrado"}), 404


@bp.route('/api/etapas', methods=['GET', 'POST'])
@login_required
def gerir_etapas():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    if request.method == 'POST':
        if request.json.get('nome'):
            db.session.add(Etapa(empresa_id=current_user.empresa_id, nome=request.json.get('nome')))
            db.session.commit()
            return jsonify({"status": "sucesso"})
        return jsonify({"erro": "Inválido"}), 400
    return jsonify([{"id": e.id, "nome": e.nome, "qtd_negocios": len(e.negocios)} for e in Etapa.query.filter_by(empresa_id=current_user.empresa_id).all()])


@bp.route('/api/etapas/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def editar_etapa(id):
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    etapa = Etapa.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if not etapa:
        return jsonify({"erro": "Não encontrada"}), 404
    if request.method == 'PUT':
        etapa.nome = request.json.get('nome', etapa.nome)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    if len(etapa.negocios) > 0:
        return jsonify({"erro": "Possui negócios."}), 400
    db.session.delete(etapa)
    db.session.commit()
    return jsonify({"status": "sucesso"})


@bp.route('/')
@login_required
def index():
    empresa = db.session.get(Empresa, current_user.empresa_id) if current_user.empresa_id else None
    return render_template(
        'index.html',
        empresa=empresa,
        etapas=Etapa.query.filter_by(empresa_id=current_user.empresa_id).all(),
        users=User.query.filter_by(empresa_id=current_user.empresa_id).all() if current_user.is_admin else []
    )


@bp.route('/api/leads', methods=['POST'])
@login_required
def criar_lead():
    dados = request.get_json(silent=True) or {}
    nome, telefone, titulo = (dados.get('nome') or '').strip(), re.sub(r'\D', '', str(dados.get('telefone') or '')), (dados.get('titulo') or '').strip()
    if not nome or len(nome) > 100 or not telefone or len(telefone) > 20:
        return jsonify({"erro": "Dados inválidos."}), 400
    try:
        valor = float(dados.get('valor') or 0)
    except Exception:
        return jsonify({"erro": "Valor inválido."}), 400
    etapa_id = dados.get('etapa_id')
    etapa = Etapa.query.filter_by(id=int(etapa_id) if etapa_id else None, empresa_id=current_user.empresa_id).first() if etapa_id else Etapa.query.filter_by(empresa_id=current_user.empresa_id).order_by(Etapa.id).first()
    if not etapa:
        return jsonify({"erro": "Etapa não encontrada."}), 400

    pessoa = Pessoa.query.filter_by(empresa_id=current_user.empresa_id, telefone=telefone).first()
    if not pessoa:
        pessoa = Pessoa(
            empresa_id=current_user.empresa_id,
            nome=nome,
            telefone=telefone,
            status_atendimento='menu',
            ia_ativa=False,
        )
        db.session.add(pessoa)
        db.session.flush()

    negocio = Negocio(empresa_id=current_user.empresa_id, titulo=(titulo or f"Lead - {nome}")[:100], valor=valor, pessoa_id=pessoa.id, etapa_id=etapa.id, user_id=current_user.id)
    db.session.add(negocio)
    db.session.commit()
    return jsonify({"status": "sucesso", "lead_id": negocio.id, "pessoa_id": pessoa.id}), 201


@bp.route('/api/get_chat/<int:pessoa_id>')
@login_required
def get_chat(pessoa_id):
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if not pessoa:
        return jsonify({"erro": "Não encontrada"}), 404
    for msg in Mensagem.query.filter_by(pessoa_id=pessoa_id, empresa_id=current_user.empresa_id, tipo='inbound', lida=False).all():
        msg.lida = True
    db.session.commit()
    return jsonify({"nome": pessoa.nome, "telefone": pessoa.telefone, "mensagens": [_formatar_mensagem_chat(m) for m in Mensagem.query.filter_by(pessoa_id=pessoa.id, empresa_id=current_user.empresa_id).order_by(Mensagem.id).all()], "etiquetas": [{"id": e.id, "nome": e.nome} for e in pessoa.etiquetas], "ia_ativa": pessoa.ia_ativa})


@bp.route('/api/encerrar_atendimento/<int:pessoa_id>', methods=['POST'])
@bp.route('/api/atendimento/<int:pessoa_id>/encerrar', methods=['POST'])
@login_required
def encerrar_atendimento(pessoa_id):
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if pessoa:
        pessoa.status_atendimento, pessoa.ia_ativa = 'fechado', False
        db.session.commit()
    return jsonify({"status": "success"})


def _formatar_mensagem_chat(mensagem):
    d = FUSO_HORARIO_BR.localize(mensagem.data_envio) if mensagem.data_envio and mensagem.data_envio.tzinfo is None else (mensagem.data_envio.astimezone(FUSO_HORARIO_BR) if mensagem.data_envio else None)
    return {"id": mensagem.id, "direcao": mensagem.tipo, "conteudo": mensagem.mensagem, "data_envio": d.isoformat() if d else "", "hora": d.strftime("%H:%M") if d else ""}


@bp.route('/api/mensagens/novas/<int:ultimo_id_mensagem>')
@login_required
def mensagens_novas(ultimo_id_mensagem):
    pessoa = Pessoa.query.filter_by(id=request.args.get('pessoa_id', type=int), empresa_id=current_user.empresa_id).first()
    if not pessoa:
        return jsonify({"erro": "Não encontrada."}), 404
    mensagens = Mensagem.query.filter(Mensagem.empresa_id == current_user.empresa_id, Mensagem.pessoa_id == pessoa.id, Mensagem.id > ultimo_id_mensagem).order_by(Mensagem.id).limit(100).all()
    for m in mensagens:
        if m.tipo == 'inbound':
            m.lida = True
    db.session.commit()
    return jsonify([_formatar_mensagem_chat(m) for m in mensagens])


@bp.route('/api/etiquetas', methods=['GET', 'POST'])
@login_required
def gerir_etiquetas():
    if request.method == 'GET':
        return jsonify([{"id": e.id, "nome": e.nome} for e in Etiqueta.query.filter_by(empresa_id=current_user.empresa_id).order_by(Etiqueta.nome).all()])
    nome = (request.get_json(silent=True) or {}).get('nome', '').strip()
    if not nome or len(nome) > 40:
        return jsonify({"erro": "Inválida"}), 400
    if Etiqueta.query.filter(Etiqueta.empresa_id == current_user.empresa_id, db.func.lower(Etiqueta.nome) == nome.lower()).first():
        return jsonify({"erro": "Já existe."}), 409
    e = Etiqueta(empresa_id=current_user.empresa_id, nome=nome)
    db.session.add(e)
    db.session.commit()
    return jsonify({"id": e.id, "nome": e.nome}), 201


@bp.route('/api/conversas/<int:pessoa_id>/etiquetas', methods=['PUT'])
@login_required
def atualizar_etiquetas_conversa(pessoa_id):
    dados = request.get_json(silent=True) or {}
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    etiqueta = Etiqueta.query.filter_by(id=dados.get('etiqueta_id'), empresa_id=current_user.empresa_id).first()
    if pessoa and etiqueta:
        acao = dados.get('acao', 'adicionar')
        if acao == 'adicionar' and etiqueta not in pessoa.etiquetas:
            pessoa.etiquetas.append(etiqueta)
        elif acao == 'remover' and etiqueta in pessoa.etiquetas:
            pessoa.etiquetas.remove(etiqueta)
        db.session.commit()
        return jsonify({"etiquetas": [{"id": i.id, "nome": i.nome} for i in pessoa.etiquetas]})
    return jsonify({"erro": "Não encontrado"}), 404


@bp.route('/api/conversas/<int:pessoa_id>/assistente-ia', methods=['POST'])
@login_required
def atualizar_assistente_conversa(pessoa_id):
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if pessoa:
        dados = request.get_json(silent=True) or {}
        ativa = dados.get('ativa')
        if ativa is True:
            return jsonify({"erro": "A assistência por IA está desativada; use o roteiro numérico."}), 409
        if not isinstance(ativa, bool):
            return jsonify({"erro": "Estado do assistente inválido."}), 400
        pessoa.ia_ativa, pessoa.status_atendimento = False, 'humano'
        db.session.commit()
        return jsonify({"status": "sucesso", "ia_ativa": False})
    return jsonify({"erro": "Não encontrado"}), 404


@bp.route('/api/send_message', methods=['POST'])
@login_required
def send_message():
    dados = request.get_json(silent=True) or {}
    pessoa = Pessoa.query.filter_by(id=dados.get('pessoa_id'), empresa_id=current_user.empresa_id).first()
    if not pessoa:
        return jsonify({"erro": "Não encontrado"}), 404
    texto, media_b64 = dados.get('texto', ''), dados.get('media')
    api_url, api_key, instance_name = current_app.config['EVOLUTION_API_URL'], current_app.config['EVOLUTION_API_KEY'], current_user.empresa.instancia_whatsapp
    if dados.get('voice_note') is True:
        if not isinstance(media_b64, str) or not media_b64.startswith('data:audio/'):
            return jsonify({"erro": "Áudio de voz inválido."}), 400
        try:
            resposta = requests.post(
                f"{api_url.rstrip('/')}/message/sendWhatsAppAudio/{instance_name}",
                headers={"apikey": api_key},
                json={"number": str(pessoa.telefone), "audio": media_b64},
                timeout=30,
            )
            resposta.raise_for_status()
        except requests.RequestException:
            current_app.logger.exception(
                'Falha ao enviar áudio de voz pela Evolution API para pessoa %s.',
                pessoa.id,
            )
            return jsonify({"erro": "Falha ao enviar áudio de voz pela Evolution API."}), 502

        db.session.add(Mensagem(
            empresa_id=current_user.empresa_id,
            pessoa_id=pessoa.id,
            mensagem=json.dumps({
                "type": "audio",
                "content": media_b64,
                "caption": texto,
                "voice_note": True,
            }),
            tipo='outbound',
            data_envio=hora_atual_br(),
        ))
        db.session.commit()
        return jsonify({"status": "sucesso"})

    if media_b64:
        mimetype = media_b64.split(",", 1)[0].split(":")[1].split(";")[0]
        mtype = 'image' if 'image' in mimetype else 'audio' if 'audio' in mimetype else 'video' if 'video' in mimetype else 'document'
        if requests.post(f"{api_url}/message/sendMedia/{instance_name}", headers={"apikey": api_key}, json={"number": pessoa.telefone, "mediatype": mtype, "mimetype": mimetype, "caption": texto, "media": media_b64.split(",", 1)[1], "fileName": dados.get('fileName', 'arquivo')}).status_code in [200, 201]:
            db.session.add(Mensagem(empresa_id=current_user.empresa_id, pessoa_id=pessoa.id, mensagem=json.dumps({"type": mtype, "content": media_b64, "caption": texto}), tipo='outbound', data_envio=hora_atual_br()))
            db.session.commit()
            return jsonify({"status": "sucesso"})
    else:
        if enviar_mensagem_whatsapp(pessoa.telefone, texto, instance_name):
            db.session.add(Mensagem(empresa_id=current_user.empresa_id, pessoa_id=pessoa.id, mensagem=texto, tipo='outbound', data_envio=hora_atual_br()))
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


@bp.route('/webhook/whatsapp', methods=['POST'])
def webhook_whatsapp():
    try:
        processar_mensagem_whatsapp.delay(request.get_json(silent=True))
    except Exception:
        return jsonify({"status": "queue_unavailable"}), 503
    return jsonify({"status": "queued"}), 200


@bp.route('/api/check_updates')
@login_required
def check_updates():
    msg = Mensagem.query.filter_by(empresa_id=current_user.empresa_id).order_by(Mensagem.id.desc()).first()
    if msg:
        txt = msg.mensagem
        try:
            txt = f"📎 [Ficheiro] {json.loads(txt).get('caption', '')}"
        except Exception:
            pass
        return jsonify({"ultima_mensagem_id": msg.id, "remetente": msg.pessoa.nome, "texto": txt, "tipo": msg.tipo})
    return jsonify({"ultima_mensagem_id": 0})


@bp.route('/api/get_qr')
@login_required
def get_qr():
    try:
        res = requests.get(f"{current_app.config['EVOLUTION_API_URL']}/instance/connect/{current_user.empresa.instancia_whatsapp}", headers={"apikey": current_app.config['EVOLUTION_API_KEY']}).json()
        if res.get('instance', {}).get('state') == 'open':
            return jsonify({"status": "connected"}), 200
        b64 = res.get('base64') or (res.get('qrcode', {}).get('base64') if isinstance(res.get('qrcode'), dict) else None)
        return jsonify({"status": "qr", "qr_base64": b64} if b64 else {"status": "pending", "detalhe": "A aguardar QR Code"}), 200
    except Exception as e:
        return jsonify({"status": "error", "erro": str(e)}), 200


@bp.route('/api/disconnect', methods=['POST'])
@login_required
def disconnect_whatsapp():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado."}), 403
    requests.delete(f"{current_app.config['EVOLUTION_API_URL']}/instance/logout/{current_user.empresa.instancia_whatsapp}", headers=evolution_headers())
    return jsonify({"status": "desconectado"})


@bp.route('/api/metrics')
@login_required
def metrics():
    q = Negocio.query.filter_by(empresa_id=current_user.empresa_id)
    if not current_user.is_admin:
        q = q.filter_by(user_id=current_user.id)
    total, funil, fechamentos, tempos = q.count(), [], 0, []
    for e in Etapa.query.filter_by(empresa_id=current_user.empresa_id).all():
        qtd = q.filter_by(etapa_id=e.id).count()
        if 'fechamento' in e.nome.lower() or 'ganho' in e.nome.lower():
            fechamentos += qtd
        funil.append({"nome": e.nome, "quantidade": qtd, "porcentagem": (qtd / total * 100) if total > 0 else 0})
    for p in Pessoa.query.filter_by(empresa_id=current_user.empresa_id).all():
        hin = None
        for m in Mensagem.query.filter_by(empresa_id=current_user.empresa_id, pessoa_id=p.id).order_by(Mensagem.data_envio).all():
            if not m.data_envio:
                continue
            if m.tipo == 'inbound' and not hin:
                hin = m.data_envio
            elif m.tipo == 'outbound' and hin:
                tempos.append((m.data_envio - hin).total_seconds() / 60.0)
                hin = None
    return jsonify({"total_negocios": total, "taxa_conversao": round((fechamentos / total * 100) if total > 0 else 0, 1), "funil": funil, "sla_minutos": round(sum(tempos) / len(tempos) if tempos else 0, 1)})
