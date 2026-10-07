import json
import logging
from datetime import datetime

import requests
import pytz
from flask import current_app
from celery.signals import worker_process_init
from google.genai import types

from app import celery, db
from app.models import (
    FUSO_HORARIO_BR,
    Configuracao,
    Empresa,
    Etapa,
    Mensagem,
    Negocio,
    Pessoa,
    RespostaAutomatica,
    hora_atual_br,
)
from app.services.ia_service import gerar_resposta_ia
from app.services.whatsapp import enviar_mensagem_whatsapp


@worker_process_init.connect
def init_celery_db_connection(**kwargs):
    flask_app = celery.flask_app
    with flask_app.app_context():
        db.session.remove()
        db.engine.dispose(close=False)


def _personalizar_mensagem(texto, empresa, pessoa):
    return (texto or '').replace(
        '{nome_cliente}', pessoa.nome or 'Cliente'
    ).replace('{nome_empresa}', empresa.nome or '')


def _montar_texto_menu(empresa, pessoa):
    etapas = Etapa.query.filter_by(
        empresa_id=empresa.id, exibir_no_menu=True
    ).order_by(Etapa.numero_menu, Etapa.id).all()
    linhas = [f'{etapa.numero_menu} - {etapa.nome}' for etapa in etapas]
    saudacao = _personalizar_mensagem(
        empresa.mensagem_saudacao or 'Olá! Como podemos ajudar hoje?',
        empresa,
        pessoa,
    )
    return f'{saudacao}\n\n' + '\n'.join(linhas) if linhas else saudacao


def _enviar_resposta(empresa_id, pessoa_id, telefone, instancia, texto):
    if not enviar_mensagem_whatsapp(telefone, texto, instancia):
        raise RuntimeError('Não foi possível enviar a resposta pela Evolution API.')
    db.session.add(Mensagem(
        empresa_id=empresa_id,
        pessoa_id=pessoa_id,
        mensagem=texto,
        tipo='outbound',
        data_envio=hora_atual_br(),
    ))
    db.session.commit()


def _extrair_data_mensagem(data_payload, msg_data):
    timestamp = msg_data.get('messageTimestamp')
    if timestamp is None:
        timestamp = data_payload.get('messageTimestamp')
    if timestamp is None:
        return hora_atual_br()

    try:
        timestamp = float(timestamp)
        if abs(timestamp) >= 1_000_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(
            timestamp, tz=FUSO_HORARIO_BR
        ).replace(tzinfo=None)
    except (TypeError, ValueError, OverflowError, OSError):
        current_app.logger.warning('Timestamp inválido recebido da Evolution API: %r', timestamp)
        return hora_atual_br()


def _preparar_contexto_ia(empresa, pessoa):
    config = Configuracao.query.filter_by(empresa_id=empresa.id).first()
    ultimas = Mensagem.query.filter_by(
        empresa_id=empresa.id, pessoa_id=pessoa.id
    ).order_by(Mensagem.id.desc()).limit(6).all()
    ultimas.reverse()
    historico = []
    for mensagem in ultimas[:-1]:
        texto_historico = mensagem.mensagem
        try:
            texto_historico = json.loads(mensagem.mensagem).get('caption', '[Arquivo]')
        except (TypeError, ValueError):
            pass
        historico.append(types.Content(
            role='user' if mensagem.tipo == 'inbound' else 'model',
            parts=[types.Part.from_text(text=texto_historico)],
        ))
    return historico, config.prompt_ia if config else None


def _transferir_para_humano(empresa, pessoa, telefone, instancia, erro_ia=False):
    pessoa.status_atendimento = 'humano'
    pessoa.ia_ativa = False
    db.session.commit()
    
    # Se for um erro da IA ou pedido de transferência, usa a mensagem de transbordo natural
    texto_transbordo = empresa.mensagem_transbordo or "Vou passar aqui para um dos nossos consultores dar continuidade ao seu atendimento, um momento!"
    texto_personalizado = _personalizar_mensagem(texto_transbordo, empresa, pessoa)
    
    _enviar_resposta(
        empresa.id, pessoa.id, telefone, instancia, texto_personalizado
    )


def _processar_payload(payload):
    if not isinstance(payload, dict):
        current_app.logger.warning('Payload inválido recebido na tarefa do WhatsApp.')
        return 'ignored'

    data_payload = payload.get('data') or {}
    if not isinstance(data_payload, dict):
        return 'ignored'
    chave = data_payload.get('key') or {}
    if not isinstance(chave, dict):
        return 'ignored'
    remote_jid = chave.get('remoteJid') or ''
    if '@g.us' in remote_jid or chave.get('fromMe', False):
        return 'ignored'
    if payload.get('event') not in {'MESSAGES_UPSERT', 'messages.upsert'}:
        return 'ignored'

    instancia = payload.get('instance')
    empresa = Empresa.query.filter_by(instancia_whatsapp=instancia).first()
    if not empresa or not empresa.is_ativa:
        current_app.logger.warning(
            'Instância WhatsApp sem empresa ativa: %s', instancia
        )
        return 'ignored'

    telefone = remote_jid.split('@', 1)[0]
    if not telefone:
        return 'ignored'
    msg_data = data_payload.get('message') or {}
    if not isinstance(msg_data, dict):
        return 'ignored'
    data_mensagem = _extrair_data_mensagem(data_payload, msg_data)

    media_type = next((
        tipo for campo, tipo in (
            ('imageMessage', 'image'),
            ('audioMessage', 'audio'),
            ('documentMessage', 'document'),
        ) if campo in msg_data
    ), None)
    texto_recebido = (
        msg_data.get('conversation')
        or (msg_data.get('extendedTextMessage') or {}).get('text', '')
    )
    caption = (
        (msg_data.get('imageMessage') or {}).get('caption', '')
        or (msg_data.get('documentMessage') or {}).get('caption', '')
    )
    if caption:
        texto_recebido = caption
    if media_type and not texto_recebido:
        texto_recebido = '[Ficheiro Multimédia]'
    if not texto_recebido and not media_type:
        return 'ignored'

    pessoa = Pessoa.query.filter_by(
        empresa_id=empresa.id, telefone=telefone
    ).first()
    atendimento_reaberto = False
    
    if not pessoa:
        primeira_etapa = Etapa.query.filter_by(
            empresa_id=empresa.id
        ).order_by(Etapa.id).first()
        if not primeira_etapa:
            current_app.logger.warning(
                'Empresa %s sem etapas configuradas.', empresa.id
            )
            return 'no_stage'
        pessoa = Pessoa(
            empresa_id=empresa.id,
            nome=data_payload.get('pushName', 'Novo Contato'),
            telefone=telefone,
            status_atendimento='menu' if empresa.usar_menu_inicial else 'ia',
            ia_ativa=True,
        )
        db.session.add(pessoa)
        db.session.flush()
        db.session.add(Negocio(
            empresa_id=empresa.id,
            titulo=f'Op. - {telefone}',
            pessoa_id=pessoa.id,
            etapa_id=primeira_etapa.id,
            user_id=None,
        ))
        db.session.commit()
    elif pessoa.status_atendimento == 'fechado':
        pessoa.status_atendimento = 'menu' if empresa.usar_menu_inicial else 'ia'
        pessoa.ia_ativa = True
        atendimento_reaberto = True
        db.session.commit()

    mensagem_db = texto_recebido
    if media_type:
        try:
            resposta_media = requests.post(
                f"{current_app.config['EVOLUTION_API_URL'].rstrip('/')}/chat/getBase64FromMediaMessage/{instancia}",
                headers={'apikey': current_app.config['EVOLUTION_API_KEY']},
                json={'message': data_payload},
                timeout=20,
            )
            resposta_media.raise_for_status()
            base64_media = resposta_media.json().get('base64')
            if base64_media and not base64_media.startswith('data:'):
                mime = {
                    'image': 'image/jpeg',
                    'audio': 'audio/ogg',
                    'document': 'application/pdf',
                }[media_type]
                base64_media = f'data:{mime};base64,{base64_media}'
            if base64_media:
                mensagem_db = json.dumps({
                    'type': media_type,
                    'content': base64_media,
                    'caption': texto_recebido,
                })
        except (requests.RequestException, ValueError) as erro:
            current_app.logger.warning('Falha ao obter mídia do WhatsApp: %s', erro)

    db.session.add(Mensagem(
        empresa_id=empresa.id,
        pessoa_id=pessoa.id,
        mensagem=mensagem_db,
        tipo='inbound',
        data_envio=data_mensagem,
    ))
    db.session.commit()

    if atendimento_reaberto:
        if empresa.usar_menu_inicial:
            mensagem_reinicio = _montar_texto_menu(empresa, pessoa)
            _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, mensagem_reinicio)
            pessoa.status_atendimento = 'aguardando_menu'
            db.session.commit()
        else:
            mensagem_reinicio = _personalizar_mensagem(
                empresa.mensagem_saudacao or 'Olá! Como podemos ajudar hoje?',
                empresa,
                pessoa,
            )
            _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, mensagem_reinicio)
        return 'reopened'

    if pessoa.status_atendimento == 'humano':
        return 'human'

    # ESTADO 1: Acabou de entrar, enviamos o menu
    if pessoa.status_atendimento == 'menu':
        _enviar_resposta(
            empresa.id, pessoa.id, telefone, instancia,
            _montar_texto_menu(empresa, pessoa),
        )
        pessoa.status_atendimento = 'aguardando_menu'
        db.session.commit()
        return 'menu_sent'

    # ESTADO 2: Já recebeu o menu, está a responder
    if pessoa.status_atendimento == 'aguardando_menu':
        etapa = None
        texto_opcao = (texto_recebido or '').strip()
        if texto_opcao.isdecimal():
            etapa = Etapa.query.filter_by(
                empresa_id=empresa.id,
                exibir_no_menu=True,
                numero_menu=int(texto_opcao),
            ).first()
            
        if etapa:
            negocio = Negocio.query.filter_by(
                empresa_id=empresa.id, pessoa_id=pessoa.id
            ).order_by(Negocio.id).first()
            if negocio:
                negocio.etapa_id = etapa.id
            pessoa.status_atendimento = 'humano'
            pessoa.ia_ativa = False
            db.session.commit()
            
            _enviar_resposta(
                empresa.id, pessoa.id, telefone, instancia,
                empresa.mensagem_transbordo or "Entendido! Já vou te direcionar para a pessoa certa.",
            )
            return 'transferred_via_menu'
        else:
            # Se não digitou um número válido do menu, passa para humano silenciosamente
            pessoa.status_atendimento = 'humano'
            pessoa.ia_ativa = False
            db.session.commit()
            return 'human_fallback_menu'

    if pessoa.status_atendimento != 'ia' or not pessoa.ia_ativa:
        return 'ignored'

    # --- 1. NOVA CAMADA DE GATILHOS ESTÁTICOS ---
    texto_limpo = (texto_recebido or '').lower()
    regras_automaticas = RespostaAutomatica.query.filter_by(empresa_id=empresa.id).all()
    
    for regra in regras_automaticas:
        if regra.palavra_chave.lower() in texto_limpo:
            _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, regra.resposta)
            return 'replied_by_rule'

    # --- 2. FLUXO DE INTELIGÊNCIA ARTIFICIAL ---
    try:
        requests.post(
            f"{current_app.config['EVOLUTION_API_URL'].rstrip('/')}/chat/sendPresence/{instancia}",
            headers={'apikey': current_app.config['EVOLUTION_API_KEY']},
            json={'number': telefone, 'presence': 'composing', 'delay': 2000},
            timeout=10,
        ).raise_for_status()
    except requests.RequestException as erro:
        current_app.logger.warning(
            'Não foi possível enviar presença de digitação: %s', erro
        )

    tom_resposta = empresa.tom_resposta
    prompt_personalidade = empresa.prompt_personalidade
    historico, prompt_adicional = _preparar_contexto_ia(empresa, pessoa)
    db.session.commit()
    
    # Gerar resposta com a IA
    sucesso_ia, resposta_bot = gerar_resposta_ia(
        tom_resposta,
        prompt_personalidade,
        texto_recebido,
        historico,
        prompt_adicional,
    )
    
    # Fallback caso a API da IA falhe (erro de limite de tokens, timeout, etc)
    if not sucesso_ia:
        _transferir_para_humano(empresa, pessoa, telefone, instancia, erro_ia=True)
        return 'transferred_ia_error'

    # Fallback manual pedido pela própria IA no prompt
    if '[TRANSFERIR]' in resposta_bot:
        _transferir_para_humano(empresa, pessoa, telefone, instancia)
        return 'transferred_ia_request'

    _enviar_resposta(
        empresa.id, pessoa.id, telefone, instancia, resposta_bot
    )
    return 'replied'


@celery.task(name='app.tasks.processar_mensagem_whatsapp')
def processar_mensagem_whatsapp(payload):
    try:
        return _processar_payload(payload)
    except Exception:
        db.session.rollback()
        instancia = payload.get('instance') if isinstance(payload, dict) else None
        logging.error(
            'Falha ao processar mensagem WhatsApp da instância %s.',
            instancia,
            exc_info=True,
        )
        raise
    finally:
        db.session.remove()
