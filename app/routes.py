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
        'main.admin_upload_logo_global', 'main.admin_apagar_logo_global',
        'main.admin_empresas_pendentes', 'main.admin_ativar_empresa', 'main.admin_nova_empresa'
    }:
        return None

    if current_user.empresa and not current_user.empresa.is_ativa:
        logout_user()
        return 'Conta suspensa. Contacte o suporte.', 403

    return None

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
    
    <p class="text-slate-400 text-[10px] tracking-wide text-center">
        Desenvolvido por <strong class="text-slate-500">Pedro Marinho</strong>. Um produto <strong class="text-blue-600">Optmiza</strong>.
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
    db.session.add(Configuracao(empresa_id=empresa.id))
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

@bp.route('/authorize/google')
def authorize_google():
    try:
        token = oauth.google.authorize_access_token()
        perfil = token.get('userinfo') or oauth.google.userinfo() or {}
    except Exception:
        return redirect(url_for('main.login', msg='Não foi possível autenticar com o Google. Tente novamente.'))

    email = (perfil.get('email') or '').strip().lower()
    google_id = perfil.get('sub')
    if not email or not google_id or not perfil.get('email_verified'):
        return redirect(url_for('main.login', msg='O Google não confirmou um endereço de e-mail válido.'))

    user = User.query.filter_by(email=email).first()
    if not user: user = User.query.filter_by(google_id=google_id).first()
    
    if user:
        if user.google_id and user.google_id != google_id:
            return redirect(url_for('main.login', msg='Este e-mail está associado a outra conta Google.'))
        if not user.empresa:
            session['registro_google_email'] = email
            session['registro_google_google_id'] = google_id
            return redirect(url_for('main.completar_cadastro'))
        if not user.empresa.is_ativa:
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
    db.session.add(Configuracao(empresa_id=empresa.id))
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
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''

    if not nome_empresa or not username or not password:
        flash('Preencha todos os campos do formulário.', 'error')
        return redirect(url_for('main.optmiza_master'))

    if User.query.filter_by(username=username).first():
        flash('Erro: Este nome de utilizador (admin) já está em uso.', 'error')
        return redirect(url_for('main.optmiza_master'))

    try:
        instancia = configurar_instancia_whatsapp(nome_empresa)
    except Exception as e:
        flash(f'Erro ao conectar à Evolution API: {str(e)}', 'error')
        return redirect(url_for('main.optmiza_master'))

    empresa = Empresa(nome=nome_empresa, instancia_whatsapp=instancia, is_ativa=True)
    db.session.add(empresa)
    db.session.flush()

    admin_user = User(empresa_id=empresa.id, username=username, password_hash=generate_password_hash(password), is_admin=True)
    db.session.add(admin_user)
    db.session.add_all([Etapa(empresa_id=empresa.id, nome=nome) for nome in ('Qualificação', 'Contato Feito', 'Proposta Enviada', 'Fechamento')])
    db.session.add(Configuracao(empresa_id=empresa.id))
    db.session.commit()
    flash(f'A empresa "{nome_empresa}" foi cadastrada e ativada com sucesso!', 'success')
    return redirect(url_for('main.optmiza_master'))

@bp.route('/admin/empresas/pendentes', methods=['GET'])
@login_required
def admin_empresas_pendentes():
    if not current_user.is_super_admin: return 'Acesso proibido.', 403
    csrf_token = session.setdefault('admin_empresas_csrf_token', secrets.token_urlsafe(32))
    empresas = Empresa.query.filter_by(is_ativa=False).order_by(Empresa.id).all()
    return render_template('painel_admin.html', empresas=empresas, csrf_token=csrf_token)

@bp.route('/admin/empresas/<int:id>/ativar', methods=['POST'])
@login_required
def admin_ativar_empresa(id):
    if not current_user.is_super_admin: return 'Acesso proibido.', 403
    token_enviado = request.form.get('csrf_token', '')
    token_sessao = session.get('admin_empresas_csrf_token', '')
    if not token_sessao or not secrets.compare_digest(token_enviado, token_sessao): return 'Requisição inválida.', 400
    empresa = db.session.get(Empresa, id)
    if empresa:
        empresa.is_ativa = True
        db.session.commit()
        flash(f'Empresa "{empresa.nome}" ativada com sucesso.', 'success')
    return redirect(url_for('main.admin_empresas_pendentes'))

@bp.route('/api/invite', methods=['POST'])
@login_required
def gerar_link_convite():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    is_admin = request.json.get('is_admin', False)
    token = get_serializer().dumps({"is_admin": is_admin, "empresa_id": current_user.empresa_id, "action": "invite"})
    return jsonify({"link": url_for('main.processar_convite', token=token, _external=True)})

@bp.route('/invite/<token>', methods=['GET', 'POST'])
def processar_convite(token):
    try:
        data = get_serializer().loads(token, max_age=86400)
        if data.get('action') != 'invite': raise ValueError
    except: return render_template_string(AUTH_HTML, title="Convite Inválido", subtitle="Este link expirou ou é inválido.", erro="Solicite um novo link.")

    if request.method == 'POST':
        username, password = request.form.get('username'), request.form.get('password')
        if User.query.filter_by(username=username).first(): return render_template_string(AUTH_HTML, title="Criar Conta", subtitle="Junte-se à equipa Optmiza", action="invite", btn_text="Concluir Registo", erro="Utilizador já em uso.")
        empresa = db.session.get(Empresa, data.get('empresa_id'))
        if not empresa: return render_template_string(AUTH_HTML, title="Convite Inválido", subtitle="A empresa deste convite não existe.", erro="Solicite novo link."), 404
        db.session.add(User(empresa_id=empresa.id, username=username, password_hash=generate_password_hash(password), is_admin=data['is_admin']))
        db.session.commit()
        return redirect(url_for('main.login', msg="Conta criada! Já pode fazer login."))
    return render_template_string(AUTH_HTML, title="Criar Conta", subtitle="Foi convidado para a equipa Optmiza", action="invite", btn_text="Concluir Registo")

@bp.route('/api/usuarios/<int:id>/reset_link', methods=['POST'])
@login_required
def gerar_link_reset(id):
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    user = User.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if not user: return jsonify({"erro": "Utilizador não encontrado"}), 404
    token = get_serializer().dumps({"user_id": user.id, "action": "reset"})
    return jsonify({"link": url_for('main.processar_reset', token=token, _external=True)})

@bp.route('/reset/<token>', methods=['GET', 'POST'])
def processar_reset(token):
    try:
        data = get_serializer().loads(token, max_age=3600)
        if data.get('action') != 'reset': raise ValueError
    except: return render_template_string(AUTH_HTML, title="Link Inválido", subtitle="Este link de segurança expirou.", erro="Solicite novo reset.")

    user = db.session.get(User, data['user_id'])
    if not user: return render_template_string(AUTH_HTML, title="Link Inválido", subtitle="A conta não foi encontrada.", erro="Solicite novo reset."), 404
    if request.method == 'POST':
        user.password_hash = generate_password_hash(request.form.get('password'))
        db.session.commit()
        return redirect(url_for('main.login', msg="Palavra-passe atualizada!"))
    return render_template_string(AUTH_HTML, title="Redefinir Palavra-passe", subtitle=f"Acesso para: {user.username}", action="reset", btn_text="Guardar Nova")

@bp.route('/api/usuarios', methods=['GET', 'POST'])
@login_required
def gerir_usuarios():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    if request.method == 'POST':
        username, password, is_admin = request.json.get('username'), request.json.get('password'), request.json.get('is_admin', False)
        if not username or not password: return jsonify({"erro": "Preencha todos os campos"}), 400
        if User.query.filter_by(username=username).first(): return jsonify({"erro": "Nome já existe"}), 400
        db.session.add(User(empresa_id=current_user.empresa_id, username=username, password_hash=generate_password_hash(password), is_admin=is_admin))
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify([{"id": u.id, "username": u.username, "is_admin": u.is_admin} for u in User.query.filter_by(empresa_id=current_user.empresa_id).all()])

@bp.route('/api/usuarios/<int:id>', methods=['DELETE'])
@login_required
def apagar_usuario(id):
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    if id == current_user.id: return jsonify({"erro": "Não pode apagar a própria conta"}), 400
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
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    if request.method == 'POST':
        palavra, resposta = request.json.get('palavra_chave', '').strip(), request.json.get('resposta', '').strip()
        if not palavra or not resposta: return jsonify({"erro": "Preencha ambos"}), 400
        nova_regra = RespostaAutomatica(empresa_id=current_user.empresa_id, palavra_chave=palavra, resposta=resposta)
        db.session.add(nova_regra)
        db.session.commit()
        return jsonify({"status": "sucesso", "id": nova_regra.id})
    return jsonify([{"id": r.id, "palavra_chave": r.palavra_chave, "resposta": r.resposta} for r in RespostaAutomatica.query.filter_by(empresa_id=current_user.empresa_id).all()])

@bp.route('/api/regras/<int:id>', methods=['DELETE'])
@login_required
def apagar_regra(id):
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    regra = RespostaAutomatica.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if regra:
        db.session.delete(regra)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Não encontrada"}), 404

@bp.route('/configuracoes/bot', methods=['GET', 'POST'])
@login_required
def configuracoes_bot():
    if not current_user.is_admin: return 'Acesso negado.', 403
    empresa = db.session.get(Empresa, current_user.empresa_id)
    etapas = Etapa.query.filter_by(empresa_id=empresa.id).order_by(Etapa.id).all()
    erro, form_values = None, {}

    if request.method == 'POST':
        mensagem_saudacao = (request.form.get('mensagem_saudacao') or '').strip()
        prompt_personalidade = (request.form.get('prompt_personalidade') or '').strip() or 'Responda de forma curta, direta e amigável.'
        tom_resposta = (request.form.get('tom_resposta') or '').strip()
        mensagem_transbordo = (request.form.get('mensagem_transbordo') or '').strip()
        usar_menu = request.form.get('usar_menu_inicial') == 'on'
        etapas_menu, ids_exibidos = {}, set(request.form.getlist('etapa_exibir_no_menu'))

        if not mensagem_saudacao or len(mensagem_saudacao) > 2000: erro = 'Saudação inválida.'
        elif len(prompt_personalidade) > 10000: erro = 'Orientações grandes demais.'
        elif tom_resposta not in {'Profissional', 'Descontraído', 'Empático'}: erro = 'Escolha um tom.'
        elif not mensagem_transbordo or len(mensagem_transbordo) > 500: erro = 'Mensagem de transbordo inválida.'
        else:
            numeros_usados = set()
            for etapa in etapas:
                if str(etapa.id) not in ids_exibidos:
                    etapas_menu[etapa.id] = None
                    continue
                if len(numeros_usados) >= 9: erro = 'Máximo 9 etapas.'; break
                try: numero_int = int((request.form.get(f'etapa_numero_{etapa.id}') or '').strip())
                except: erro = 'Etapa precisa de número.'; break
                if numero_int < 1 or numero_int > 9 or numero_int in numeros_usados: erro = 'Números inválidos ou repetidos.'; break
                numeros_usados.add(numero_int)
                etapas_menu[etapa.id] = numero_int
            if not erro and usar_menu and not numeros_usados: erro = 'Selecione ao menos uma etapa para o menu.'

        form_values = {'mensagem_saudacao': mensagem_saudacao, 'prompt_personalidade': prompt_personalidade, 'tom_resposta': tom_resposta, 'mensagem_transbordo': mensagem_transbordo, 'etapas_menu': etapas_menu}
        if erro: return render_template('configuracoes.html', empresa=empresa, etapas=etapas, erro=erro, form_ativar=usar_menu, form_values=form_values), 400

        empresa.usar_menu_inicial, empresa.mensagem_saudacao, empresa.prompt_personalidade, empresa.tom_resposta, empresa.mensagem_transbordo = usar_menu, mensagem_saudacao, prompt_personalidade, tom_resposta, mensagem_transbordo
        for etapa in etapas:
            etapa.exibir_no_menu = etapas_menu.get(etapa.id) is not None
            etapa.numero_menu = etapas_menu.get(etapa.id)
        try: db.session.commit()
        except:
            db.session.rollback()
            return render_template('configuracoes.html', empresa=empresa, etapas=etapas, erro='Erro ao salvar.', form_ativar=usar_menu, form_values=form_values), 409
        return redirect(url_for('main.configuracoes_bot', salvo=1))
    return render_template('configuracoes.html', empresa=empresa, etapas=etapas, salvo=request.args.get('salvo') == '1')

@bp.route('/api/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
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
            if user_id and not User.query.filter_by(id=user_id, empresa_id=current_user.empresa_id).first(): return jsonify({"erro": "Inválido"}), 404
            negocio.user_id = user_id
        else: negocio.user_id = current_user.id
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Não encontrado"}), 404

@bp.route('/api/etapas', methods=['GET', 'POST'])
@login_required
def gerir_etapas():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
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
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    etapa = Etapa.query.filter_by(id=id, empresa_id=current_user.empresa_id).first()
    if not etapa: return jsonify({"erro": "Não encontrada"}), 404
    if request.method == 'PUT':
        etapa.nome = request.json.get('nome', etapa.nome)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    if len(etapa.negocios) > 0: return jsonify({"erro": "Possui negócios."}), 400
    db.session.delete(etapa)
    db.session.commit()
    return jsonify({"status": "sucesso"})

@bp.route('/')
@login_required
def index():
    return render_template('index.html', etapas=Etapa.query.filter_by(empresa_id=current_user.empresa_id).all(), users=User.query.filter_by(empresa_id=current_user.empresa_id).all() if current_user.is_admin else [])

@bp.route('/api/leads', methods=['POST'])
@login_required
def criar_lead():
    dados = request.get_json(silent=True) or {}
    nome, telefone, titulo = (dados.get('nome') or '').strip(), re.sub(r'\D', '', str(dados.get('telefone') or '')), (dados.get('titulo') or '').strip()
    if not nome or len(nome) > 100 or not telefone or len(telefone) > 20: return jsonify({"erro": "Dados inválidos."}), 400
    try: valor = float(dados.get('valor') or 0)
    except: return jsonify({"erro": "Valor inválido."}), 400
    etapa_id = dados.get('etapa_id')
    etapa = Etapa.query.filter_by(id=int(etapa_id) if etapa_id else None, empresa_id=current_user.empresa_id).first() if etapa_id else Etapa.query.filter_by(empresa_id=current_user.empresa_id).order_by(Etapa.id).first()
    if not etapa: return jsonify({"erro": "Etapa não encontrada."}), 400

    pessoa = Pessoa.query.filter_by(empresa_id=current_user.empresa_id, telefone=telefone).first()
    if not pessoa:
        pessoa = Pessoa(empresa_id=current_user.empresa_id, nome=nome, telefone=telefone, status_atendimento='menu' if current_user.empresa.usar_menu_inicial else 'ia')
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
    if not pessoa: return jsonify({"erro": "Não encontrada"}), 404
    for msg in Mensagem.query.filter_by(pessoa_id=pessoa_id, empresa_id=current_user.empresa_id, tipo='inbound', lida=False).all(): msg.lida = True
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
    if not pessoa: return jsonify({"erro": "Não encontrada."}), 404
    mensagens = Mensagem.query.filter(Mensagem.empresa_id == current_user.empresa_id, Mensagem.pessoa_id == pessoa.id, Mensagem.id > ultimo_id_mensagem).order_by(Mensagem.id).limit(100).all()
    for m in mensagens:
        if m.tipo == 'inbound': m.lida = True
    db.session.commit()
    return jsonify([_formatar_mensagem_chat(m) for m in mensagens])

@bp.route('/api/etiquetas', methods=['GET', 'POST'])
@login_required
def gerir_etiquetas():
    if request.method == 'GET': return jsonify([{"id": e.id, "nome": e.nome} for e in Etiqueta.query.filter_by(empresa_id=current_user.empresa_id).order_by(Etiqueta.nome).all()])
    nome = (request.get_json(silent=True) or {}).get('nome', '').strip()
    if not nome or len(nome) > 40: return jsonify({"erro": "Inválida"}), 400
    if Etiqueta.query.filter(Etiqueta.empresa_id == current_user.empresa_id, db.func.lower(Etiqueta.nome) == nome.lower()).first(): return jsonify({"erro": "Já existe."}), 409
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
        if acao == 'adicionar' and etiqueta not in pessoa.etiquetas: pessoa.etiquetas.append(etiqueta)
        elif acao == 'remover' and etiqueta in pessoa.etiquetas: pessoa.etiquetas.remove(etiqueta)
        db.session.commit()
        return jsonify({"etiquetas": [{"id": i.id, "nome": i.nome} for i in pessoa.etiquetas]})
    return jsonify({"erro": "Não encontrado"}), 404

@bp.route('/api/conversas/<int:pessoa_id>/assistente-ia', methods=['POST'])
@login_required
def atualizar_assistente_conversa(pessoa_id):
    pessoa = Pessoa.query.filter_by(id=pessoa_id, empresa_id=current_user.empresa_id).first()
    if pessoa:
        ativa = request.get_json(silent=True).get('ativa')
        pessoa.ia_ativa, pessoa.status_atendimento = ativa, 'ia' if ativa else 'humano'
        db.session.commit()
        return jsonify({"status": "sucesso", "ia_ativa": pessoa.ia_ativa})
    return jsonify({"erro": "Não encontrado"}), 404

@bp.route('/api/send_message', methods=['POST'])
@login_required
def send_message():
    dados = request.json
    pessoa = Pessoa.query.filter_by(id=dados.get('pessoa_id'), empresa_id=current_user.empresa_id).first()
    if not pessoa: return jsonify({"erro": "Não encontrado"}), 404
    texto, media_b64 = dados.get('texto', ''), dados.get('media')
    api_url, api_key, instance_name = current_app.config['EVOLUTION_API_URL'], current_app.config['EVOLUTION_API_KEY'], current_user.empresa.instancia_whatsapp
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
    try: processar_mensagem_whatsapp.delay(request.get_json(silent=True))
    except: return jsonify({"status": "queue_unavailable"}), 503
    return jsonify({"status": "queued"}), 200

@bp.route('/api/check_updates')
@login_required
def check_updates():
    msg = Mensagem.query.filter_by(empresa_id=current_user.empresa_id).order_by(Mensagem.id.desc()).first()
    if msg:
        txt = msg.mensagem
        try: txt = f"📎 [Ficheiro] {json.loads(txt).get('caption', '')}"
        except: pass
        return jsonify({"ultima_mensagem_id": msg.id, "remetente": msg.pessoa.nome, "texto": txt, "tipo": msg.tipo})
    return jsonify({"ultima_mensagem_id": 0})

@bp.route('/api/get_qr')
@login_required
def get_qr():
    try:
        res = requests.get(f"{current_app.config['EVOLUTION_API_URL']}/instance/connect/{current_user.empresa.instancia_whatsapp}", headers={"apikey": current_app.config['EVOLUTION_API_KEY']}).json()
        if res.get('instance', {}).get('state') == 'open': return jsonify({"status": "connected"}), 200
        b64 = res.get('base64') or (res.get('qrcode', {}).get('base64') if isinstance(res.get('qrcode'), dict) else None)
        return jsonify({"status": "qr", "qr_base64": b64} if b64 else {"status": "pending", "detalhe": "A aguardar QR Code"}), 200
    except Exception as e: return jsonify({"status": "error", "erro": str(e)}), 200

@bp.route('/api/disconnect', methods=['POST'])
@login_required
def disconnect_whatsapp():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado."}), 403
    requests.delete(f"{current_app.config['EVOLUTION_API_URL']}/instance/logout/{current_user.empresa.instancia_whatsapp}", headers=evolution_headers())
    return jsonify({"status": "desconectado"})

@bp.route('/api/metrics')
@login_required
def metrics():
    q = Negocio.query.filter_by(empresa_id=current_user.empresa_id)
    if not current_user.is_admin: q = q.filter_by(user_id=current_user.id)
    total, funil, fechamentos, tempos = q.count(), [], 0, []
    for e in Etapa.query.filter_by(empresa_id=current_user.empresa_id).all():
        qtd = q.filter_by(etapa_id=e.id).count()
        if 'fechamento' in e.nome.lower() or 'ganho' in e.nome.lower(): fechamentos += qtd
        funil.append({"nome": e.nome, "quantidade": qtd, "porcentagem": (qtd/total*100) if total>0 else 0})
    for p in Pessoa.query.filter_by(empresa_id=current_user.empresa_id).all():
        hin = None
        for m in Mensagem.query.filter_by(empresa_id=current_user.empresa_id, pessoa_id=p.id).order_by(Mensagem.data_envio).all():
            if not m.data_envio: continue
            if m.tipo == 'inbound' and not hin: hin = m.data_envio
            elif m.tipo == 'outbound' and hin: tempos.append((m.data_envio - hin).total_seconds()/60.0); hin = None
    return jsonify({"total_negocios": total, "taxa_conversao": round((fechamentos/total*100) if total>0 else 0, 1), "funil": funil, "sla_minutos": round(sum(tempos)/len(tempos) if tempos else 0, 1)})
