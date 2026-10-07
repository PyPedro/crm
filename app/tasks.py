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
            empresa_id=
