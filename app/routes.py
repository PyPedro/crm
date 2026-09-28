import os
import requests
import json
from dotenv import load_dotenv, find_dotenv
from flask import Blueprint, request, jsonify, render_template, current_app, redirect, url_for, render_template_string
from datetime import datetime
from flask_login import login_user, logout_user, login_required, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from itsdangerous import URLSafeTimedSerializer, SignatureExpired, BadTimeSignature

from app.models import db, Etapa, Negocio, Pessoa, Mensagem, User, Configuracao
from app.services.whatsapp import enviar_mensagem_whatsapp
from google import genai
from google.genai import types

load_dotenv(find_dotenv(), override=True)
bp = Blueprint('main', __name__)

# --- FUNÇÃO PARA GERAR TOKENS SEGUROS ---
def get_serializer():
    return URLSafeTimedSerializer(current_app.config.get('SECRET_KEY', 'optmiza-secure-key-2026'))

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

# --- ROTAS DE AUTENTICAÇÃO PADRÃO ---
@bp.route('/login', methods=['GET', 'POST'])
def login():
    if User.query.count() == 0:
        admin = User(username='admin', password_hash=generate_password_hash('admin123'), is_admin=True)
        db.session.add(admin)
        if Etapa.query.count() == 0:
            db.session.add_all([Etapa(nome='Qualificação'), Etapa(nome='Contato Feito'), Etapa(nome='Proposta Enviada'), Etapa(nome='Fechamento')])
        if Configuracao.query.count() == 0:
            db.session.add(Configuracao())
        db.session.commit()

    msg_sucesso = request.args.get('msg')
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password_hash, password):
            login_user(user)
            return redirect(url_for('main.index'))
        return render_template('login.html', erro="Credenciais inválidas")
        
    return render_template('login.html', msg=msg_sucesso)

@bp.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('main.login'))

# --- ROTAS DE SEGURANÇA: CONVITES E RESET DE SENHA ---
@bp.route('/api/invite', methods=['POST'])
@login_required
def gerar_link_convite():
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
        
    is_admin = request.json.get('is_admin', False)
    s = get_serializer()
    # Cria um token válido por 24 horas
    token = s.dumps({"is_admin": is_admin, "action": "invite"})
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
            
        novo_user = User(username=username, password_hash=generate_password_hash(password), is_admin=data['is_admin'])
        db.session.add(novo_user)
        db.session.commit()
        return redirect(url_for('main.login', msg="Conta criada! Já pode fazer login."))
        
    return render_template_string(AUTH_HTML, title="Criar Conta", subtitle="Foi convidado para a equipa Optmiza", action="invite", btn_text="Concluir Registo")

@bp.route('/api/usuarios/<int:id>/reset_link', methods=['POST'])
@login_required
def gerar_link_reset(id):
    if not current_user.is_admin:
        return jsonify({"erro": "Acesso negado"}), 403
    user = db.session.get(User, id)
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

        novo_user = User(username=username, password_hash=generate_password_hash(password), is_admin=is_admin)
        db.session.add(novo_user)
        db.session.commit()
        return jsonify({"status": "sucesso"})

    users = User.query.all()
    return jsonify([{"id": u.id, "username": u.username, "is_admin": u.is_admin} for u in users])

@bp.route('/api/usuarios/<int:id>', methods=['DELETE'])
@login_required
def apagar_usuario(id):
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    if id == current_user.id: return jsonify({"erro": "Não pode apagar a sua própria conta"}), 400
    user = db.session.get(User, id)
    if user:
        Negocio.query.filter_by(user_id=id).update({'user_id': None})
        db.session.delete(user)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Utilizador não encontrado"}), 404

# --- CONFIGURAÇÕES E KANBAN ---
@bp.route('/api/configuracoes', methods=['GET', 'POST'])
@login_required
def configuracoes():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    config = Configuracao.query.first()
    if request.method == 'POST':
        config.prompt_ia = request.json.get('prompt_ia', config.prompt_ia)
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"prompt_ia": config.prompt_ia})

@bp.route('/api/assign_user', methods=['POST'])
@login_required
def assign_user():
    dados = request.json
    negocio = db.session.get(Negocio, dados.get('negocio_id'))
    if negocio:
        if current_user.is_admin:
            negocio.user_id = dados.get('user_id') or None
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
            db.session.add(Etapa(nome=nome))
            db.session.commit()
            return jsonify({"status": "sucesso"})
        return jsonify({"erro": "Nome inválido"}), 400
    return jsonify([{"id": e.id, "nome": e.nome, "qtd_negocios": len(e.negocios)} for e in Etapa.query.all()])

@bp.route('/api/etapas/<int:id>', methods=['PUT', 'DELETE'])
@login_required
def editar_etapa(id):
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado"}), 403
    etapa = db.session.get(Etapa, id)
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
    return render_template('index.html', etapas=Etapa.query.all(), users=User.query.all() if current_user.is_admin else [])

@bp.route('/api/get_chat/<int:pessoa_id>')
@login_required
def get_chat(pessoa_id):
    pessoa = db.session.get(Pessoa, pessoa_id)
    mensagens_nao_lidas = Mensagem.query.filter_by(pessoa_id=pessoa_id, tipo='inbound', lida=False).all()
    for msg in mensagens_nao_lidas: msg.lida = True
    db.session.commit()
    formatadas = [{"direcao": m.tipo, "conteudo": m.mensagem, "hora": m.data_envio.strftime("%H:%M") if m.data_envio else ""} for m in pessoa.mensagens]
    return jsonify({"nome": pessoa.nome, "telefone": pessoa.telefone, "mensagens": formatadas})

@bp.route('/api/send_message', methods=['POST'])
@login_required
def send_message():
    dados = request.json
    pessoa_id = dados.get('pessoa_id') 
    numero = dados.get('numero')
    texto = dados.get('texto', '')
    media_b64 = dados.get('media')
    api_url = current_app.config['EVOLUTION_API_URL']
    api_key = current_app.config['EVOLUTION_API_KEY']
    instance_name = current_app.config['INSTANCE_NAME']

    if media_b64:
        header, encoded = media_b64.split(",", 1)
        mimetype = header.split(":")[1].split(";")[0]
        mtype = 'image' if 'image' in mimetype else 'audio' if 'audio' in mimetype else 'video' if 'video' in mimetype else 'document'
        res = requests.post(f"{api_url}/message/sendMedia/{instance_name}", headers={"apikey": api_key}, json={"number": numero, "mediatype": mtype, "mimetype": mimetype, "caption": texto, "media": encoded, "fileName": dados.get('fileName', 'arquivo')})
        if res.status_code in [200, 201]:
            db.session.add(Mensagem(pessoa_id=pessoa_id, mensagem=json.dumps({"type": mtype, "content": media_b64, "caption": texto}), tipo='outbound', data_envio=datetime.now()))
            db.session.commit()
            return jsonify({"status": "sucesso"})
    else:
        if enviar_mensagem_whatsapp(numero, texto):
            db.session.add(Mensagem(pessoa_id=pessoa_id, mensagem=texto, tipo='outbound', data_envio=datetime.now()))
            db.session.commit()
            return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Falha no envio"}), 500

@bp.route('/api/update_deal_stage', methods=['POST'])
@login_required
def update_deal_stage():
    negocio = db.session.get(Negocio, request.json.get('negocio_id'))
    if negocio:
        negocio.etapa_id = request.json.get('nova_etapa_id')
        db.session.commit()
        return jsonify({"status": "sucesso"})
    return jsonify({"erro": "Não encontrado"}), 404

# --- WEBHOOK WHATSAPP (SEM LOGIN, INTERAGE COM IA) ---
@bp.route('/webhook/whatsapp', methods=['POST'])
def webhook_whatsapp():
    dados = request.json
    if dados.get('event') not in ['MESSAGES_UPSERT', 'messages.upsert']: return jsonify({"status": "ignorado"}), 200
        
    try:
        data_payload = dados.get('data', {})
        msg_data = data_payload.get('message', {})
        if data_payload.get('key', {}).get('fromMe', False): return jsonify({"status": "ignorado"}), 200

        telefone_remetente = data_payload.get('key', {}).get('remoteJid', '').split('@')[0]
        nome_contato = data_payload.get('pushName', 'Novo Contato')
        
        is_media = False
        media_type = 'text'
        if 'imageMessage' in msg_data: is_media = True; media_type = 'image'
        elif 'audioMessage' in msg_data: is_media = True; media_type = 'audio'
        elif 'documentMessage' in msg_data: is_media = True; media_type = 'document'
        
        texto_recebido = msg_data.get('conversation') or msg_data.get('extendedTextMessage', {}).get('text', '')
        caption = msg_data.get('imageMessage', {}).get('caption', '') or msg_data.get('documentMessage', {}).get('caption', '')
        if caption: texto_recebido = caption
        if is_media and not texto_recebido: texto_recebido = "[Ficheiro Multimédia]"

        if telefone_remetente and (texto_recebido or is_media):
            pessoa = Pessoa.query.filter_by(telefone=telefone_remetente).first()
            if not pessoa:
                pessoa = Pessoa(nome=nome_contato, telefone=telefone_remetente)
                db.session.add(pessoa)
                db.session.commit()
                db.session.add(Negocio(titulo=f"Op. - {telefone_remetente}", pessoa_id=pessoa.id, etapa_id=Etapa.query.first().id, user_id=None))
                db.session.commit()

            msg_db = texto_recebido
            if is_media:
                try:
                    res_media = requests.post(f"{current_app.config['EVOLUTION_API_URL']}/chat/getBase64FromMediaMessage/{current_app.config['INSTANCE_NAME']}", headers={"apikey": current_app.config['EVOLUTION_API_KEY']}, json={"message": data_payload})
                    if res_media.status_code in [200, 201]:
                        b64 = res_media.json().get('base64')
                        if b64 and not b64.startswith('data:'):
                            mime = "image/jpeg" if media_type == 'image' else "audio/ogg" if media_type == 'audio' else "application/pdf"
                            b64 = f"data:{mime};base64,{b64}"
                        msg_db = json.dumps({"type": media_type, "content": b64, "caption": texto_recebido})
                except: pass

            db.session.add(Mensagem(pessoa_id=pessoa.id, mensagem=msg_db, tipo='inbound', data_envio=datetime.now()))
            db.session.commit()

            # IA Triage com NOVO google.genai e Fallback de Segurança
            try:
                chave_gemini = os.environ.get('GEMINI_API_KEY')
                client = genai.Client(api_key=chave_gemini)
                config = Configuracao.query.first()
                sys_inst = config.prompt_ia if config and config.prompt_ia else "Atuas como assistente virtual. Transfira para um humano se necessário usando [TRANSFERIR]."

                ultimas = Mensagem.query.filter_by(pessoa_id=pessoa.id).order_by(Mensagem.id.desc()).limit(6).all()
                ultimas.reverse()
                historico = []
                for m in ultimas[:-1]:
                    th = m.mensagem
                    try: th = json.loads(m.mensagem).get("caption", "[Arquivo]")
                    except: pass
                    historico.append(types.Content(role="user" if m.tipo == 'inbound' else "model", parts=[types.Part.from_text(text=th)]))
                
                try:
                    chat = client.chats.create(model="gemini-3.6-flash", config=types.GenerateContentConfig(system_instruction=sys_inst, temperature=0.8), history=historico)
                    resposta_bot = chat.send_message(texto_recebido).text
                except Exception:
                    chat_fallback = client.chats.create(model="gemini-1.5-flash", config=types.GenerateContentConfig(system_instruction=sys_inst, temperature=0.8), history=historico)
                    resposta_bot = chat_fallback.send_message(texto_recebido).text

                if "[TRANSFERIR]" in resposta_bot:
                    resposta_bot = resposta_bot.replace("[TRANSFERIR]", "").strip() or "Vou transferir a conversa para um consultor."
                    negocio = Negocio.query.filter_by(pessoa_id=pessoa.id).first()
                    if negocio and negocio.etapa_id == Etapa.query.first().id:
                        negocio.etapa_id = Etapa.query.filter_by(nome='Contato Feito').first().id
                        db.session.commit()
            except Exception as e:
                resposta_bot = "Olá! A nossa assistente virtual está temporariamente indisponível. Um consultor assumirá o atendimento."

            if resposta_bot:
                enviar_mensagem_whatsapp(telefone_remetente, resposta_bot)
                db.session.add(Mensagem(pessoa_id=pessoa.id, mensagem=resposta_bot, tipo='outbound', data_envio=datetime.now()))
                db.session.commit()

    except Exception as e: print(f"Erro Webhook: {e}")
    return jsonify({"status": "processado"}), 200

# --- ROTAS AUXILIARES E MÉTRICAS ---
@bp.route('/api/check_updates')
@login_required
def check_updates():
    ultima_msg = Mensagem.query.order_by(Mensagem.id.desc()).first()
    if ultima_msg:
        txt = ultima_msg.mensagem
        try: txt = f"📎 [Ficheiro] {json.loads(txt).get('caption', '')}"
        except: pass
        return jsonify({"ultima_mensagem_id": ultima_msg.id, "remetente": ultima_msg.pessoa.nome, "texto": txt, "tipo": ultima_msg.tipo})
    return jsonify({"ultima_mensagem_id": 0})

@bp.route('/api/get_qr')
@login_required
def get_qr():
    res = requests.get(f"{current_app.config['EVOLUTION_API_URL']}/instance/connect/{current_app.config['INSTANCE_NAME']}", headers={"apikey": current_app.config['EVOLUTION_API_KEY']}).json()
    if res.get('instance', {}).get('state') == 'open': 
        return jsonify({"status": "connected"})
        
    b64 = res.get('base64') or (res.get('qrcode', {}).get('base64') if isinstance(res.get('qrcode'), dict) else None)
    
    if b64:
        return jsonify({"status": "qr", "qr_base64": b64}), 200
    else:
        return jsonify({"erro": "A carregar..."}), 400
@bp.route('/api/disconnect', methods=['POST'])
@login_required
def disconnect_whatsapp():
    if not current_user.is_admin: return jsonify({"erro": "Acesso negado."}), 403
    requests.delete(f"{current_app.config['EVOLUTION_API_URL']}/instance/logout/{current_app.config['INSTANCE_NAME']}", headers={"apikey": current_app.config['EVOLUTION_API_KEY']})
    return jsonify({"status": "desconectado"})

@bp.route('/api/metrics')
@login_required
def metrics():
    q_negocios = Negocio.query if current_user.is_admin else Negocio.query.filter_by(user_id=current_user.id)
    total = q_negocios.count()
    funil, fechamentos = [], 0
    
    for e in Etapa.query.all():
        qtd = q_negocios.filter_by(etapa_id=e.id).count()
        if 'fechamento' in e.nome.lower() or 'ganho' in e.nome.lower(): fechamentos += qtd
        funil.append({"nome": e.nome, "quantidade": qtd, "porcentagem": (qtd/total*100) if total>0 else 0})

    tempos = []
    for pessoa in Pessoa.query.all():
        msgs = Mensagem.query.filter_by(pessoa_id=pessoa.id).order_by(Mensagem.data_envio).all()
        hin = None
        for m in msgs:
            if not m.data_envio: continue
            if m.tipo == 'inbound' and not hin: hin = m.data_envio
            elif m.tipo == 'outbound' and hin:
                tempos.append((m.data_envio - hin).total_seconds()/60.0)
                hin = None
    
    return jsonify({"total_negocios": total, "taxa_conversao": round((fechamentos/total*100) if total>0 else 0, 1), "funil": funil, "sla_minutos": round(sum(tempos)/len(tempos) if tempos else 0, 1)})