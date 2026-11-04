import json
import logging
import os
import time
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
    ).order_by(Mensagem.id.desc()).limit(10).all()
    ultimas.reverse()

    historico_genai = []
    historico_dict = []

    # Exclui a última mensagem pois ela já é a `texto_recebido` atual
    for mensagem in ultimas[:-1]:
        texto_historico = mensagem.mensagem
        try:
            obj = json.loads(mensagem.mensagem)
            if isinstance(obj, dict) and 'type' in obj:
                texto_historico = obj.get('caption') or f"[Cliente enviou um arquivo de {obj.get('type')}]"
        except (TypeError, ValueError):
            pass

        texto_historico = (texto_historico or '').strip()
        if not texto_historico:
            continue

        papel_genai = 'user' if mensagem.tipo == 'inbound' else 'model'
        papel_dict = 'user' if mensagem.tipo == 'inbound' else 'assistant'

        historico_genai.append(types.Content(
            role=papel_genai,
            parts=[types.Part.from_text(text=texto_historico)],
        ))
        historico_dict.append({
            'role': papel_dict,
            'content': texto_historico
        })

    return historico_genai, historico_dict, (config.prompt_ia if config else None)


def _chamar_ia_com_fallback(empresa, pessoa, texto_recebido, historico_genai, historico_dict, prompt_adicional):
    """
    Tenta gerar uma resposta contextualizada e natural:
    1. Tenta provedores ultra-estáveis (Groq / OpenAI) se configurados no ambiente.
    2. Tenta a API REST direta do Gemini com BLOCK_NONE e Retries.
    3. Usa o gerar_resposta_ia original como último recurso.
    """
    instrucoes_empresa = f"{empresa.prompt_personalidade or ''}\n\n{prompt_adicional or ''}".strip()

    system_prompt = f"""Você é a assistente virtual de atendimento da empresa {empresa.nome}.
Seu tom de conversa deve ser: {empresa.tom_resposta or 'Profissional e acolhedor'}.

INFORMAÇÕES E ROTEIRO DA EMPRESA:
{instrucoes_empresa}

REGRAS OBRIGATÓRIAS DE INTERAÇÃO:
1. Você está conversando pelo WhatsApp. Seja natural, humana, simpática e direta.
2. Responda exatamente ao que a pessoa perguntou com base nas informações da empresa acima. NUNCA diga que "desconhece a frase" e NUNCA dê respostas vagas ou robóticas.
3. Se o cliente mandar apenas uma saudação ("Oi", "Bom dia", "Tudo bem?"), cumprimente de volta com simpatia, apresente-se brevemente e pergunte como pode ajudar.
4. Faça apenas UMA pergunta por vez para não confundir o cliente.
5. SÓ escreva a tag [TRANSFERIR] se o cliente pedir explicitamente para falar com um atendente/secretária humana, ou quando você já tiver concluído toda a triagem/coleta de dados pedida no roteiro."""

    mensagens_api = [{'role': 'system', 'content': system_prompt}] + historico_dict + [
        {'role': 'user', 'content': texto_recebido}
    ]

    # 1. TENTATIVA VIA GROQ (Llama 3.3 70B - Gratuito, rápido e não cai)
    groq_key = os.environ.get('GROQ_API_KEY')
    if groq_key:
        try:
            res = requests.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization': f'Bearer {groq_key}', 'Content-Type': 'application/json'},
                json={
                    'model': 'llama-3.3-70b-versatile',
                    'messages': mensagens_api,
                    'temperature': 0.3,
                    'max_tokens': 500,
                },
                timeout=12,
            )
            if res.status_code == 200:
                texto = res.json()['choices'][0]['message']['content'].strip()
                if texto:
                    return True, texto
        except Exception as e:
            current_app.logger.warning('Falha no provedor Groq: %s', e)

    # 2. TENTATIVA VIA OPENAI (gpt-4o-mini)
    openai_key = os.environ.get('OPENAI_API_KEY')
    if openai_key:
        try:
            res = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={'Authorization': f'Bearer {openai_key}', 'Content-Type': 'application/json'},
                json={
                    'model': 'gpt-4o-mini',
                    'messages': mensagens_api,
                    'temperature': 0.3,
                    'max_tokens': 500,
                },
                timeout=12,
            )
            if res.status_code == 200:
                texto = res.json()['choices'][0]['message']['content'].strip()
                if texto:
                    return True, texto
        except Exception as e:
            current_app.logger.warning('Falha no provedor OpenAI: %s', e)

    # 3. TENTATIVA VIA GEMINI DIRETO COM RETRY E SEM FILTROS DE BLOQUEIO FALSO-POSITIVO
    gemini_key = (
        os.environ.get('GEMINI_API_KEY')
        or os.environ.get('GOOGLE_API_KEY')
        or current_app.config.get('GEMINI_API_KEY')
    )
    if gemini_key:
        conteudos_gemini = []
        for msg in historico_dict:
            papel = 'user' if msg['role'] == 'user' else 'model'
            conteudos_gemini.append({'role': papel, 'parts': [{'text': msg['content']}]})
        conteudos_gemini.append({'role': 'user', 'parts': [{'text': texto_recebido}]})

        payload_gemini = {
            'systemInstruction': {'parts': [{'text': system_prompt}]},
            'contents': conteudos_gemini,
            'generationConfig': {
                'temperature': 0.35,
                'maxOutputTokens': 600,
            },
            'safetySettings': [
                {'category': 'HARM_CATEGORY_HARASSMENT', 'threshold': 'BLOCK_NONE'},
                {'category': 'HARM_CATEGORY_HATE_SPEECH', 'threshold': 'BLOCK_NONE'},
                {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
                {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_NONE'},
            ],
        }

        # Tenta gemini-2.5-flash e, se oscilar (erro 503/429), tenta gemini-2.0-flash
        for modelo in ('gemini-2.5-flash', 'gemini-2.0-flash'):
            for tentativa in range(2):
                try:
                    url = f'https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={gemini_key}'
                    res = requests.post(url, json=payload_gemini, timeout=15)
                    if res.status_code == 200:
                        candidatos = res.json().get('candidates') or []
                        if candidatos:
                            partes = candidatos[0].get('content', {}).get('parts') or []
                            if partes and partes[0].get('text'):
                                return True, partes[0]['text'].strip()
                    elif res.status_code in (429, 500, 503):
                        time.sleep(1.5)
                except Exception as e:
                    current_app.logger.warning('Tentativa Gemini (%s) falhou: %s', modelo, e)
                    time.sleep(1)

    # 4. FALLBACK PARA O SERVIÇO INTERNO ORIGINAL (ia_service.py)
    try:
        sucesso_ia, resposta_bot = gerar_resposta_ia(
            empresa.tom_resposta,
            empresa.prompt_personalidade,
            texto_recebido,
            historico_genai,
            prompt_adicional,
        )
        if sucesso_ia and resposta_bot:
            return True, resposta_bot
    except Exception as e:
        current_app.logger.error('Erro no gerar_resposta_ia legado: %s', e)

    return False, None


def _transferir_para_humano(empresa, pessoa, telefone, instancia, texto):
    pessoa.status_atendimento = 'humano'
    pessoa.ia_ativa = False
    db.session.commit()
    texto_personalizado = _personalizar_mensagem(texto, empresa, pessoa)
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
        if media_type == 'audio':
            texto_recebido = '[O cliente enviou um áudio]'
        elif media_type == 'image':
            texto_recebido = '[O cliente enviou uma imagem]'
        else:
            texto_recebido = '[O cliente enviou um documento]'
    if not texto_recebido and not media_type:
        return 'ignored'

    pessoa = Pessoa.query.filter_by(
        empresa_id=empresa.id, telefone=telefone
    ).first()
    atendimento_reaberto = False
    eh_novo_contato = False

    if not pessoa:
        eh_novo_contato = True
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
            titulo=f'Op. - {pessoa.nome}',
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

    # 1. Se a empresa usa Menu Numérico e o atendimento acabou de ser reaberto ou criado
    if (atendimento_reaberto or eh_novo_contato) and empresa.usar_menu_inicial:
        _enviar_resposta(
            empresa.id, pessoa.id, telefone, instancia, _montar_texto_menu(empresa, pessoa)
        )
        return 'menu_sent'

    if pessoa.status_atendimento == 'humano':
        return 'human'

    # 2. Processamento do Menu Numérico (se ativo)
    if pessoa.status_atendimento == 'menu':
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
                _personalizar_mensagem(empresa.mensagem_transbordo, empresa, pessoa),
            )
            return 'transferred'
        _enviar_resposta(
            empresa.id, pessoa.id, telefone, instancia,
            _montar_texto_menu(empresa, pessoa),
        )
        return 'menu'

    if pessoa.status_atendimento != 'ia' or not pessoa.ia_ativa:
        return 'ignored'

    # 3. Verifica Gatilhos de Respostas Automáticas (exatas ou contidas) antes da IA
    texto_limpo = (texto_recebido or '').strip().lower()
    if texto_limpo:
        regras = RespostaAutomatica.query.filter_by(empresa_id=empresa.id).all()
        for regra in regras:
            gatilho = (regra.palavra_chave or '').strip().lower()
            if gatilho and (texto_limpo == gatilho or gatilho in texto_limpo):
                resposta_regra = _personalizar_mensagem(regra.resposta, empresa, pessoa)
                _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, resposta_regra)
                return 'auto_rule_replied'

    # 4. Simula digitação humana (2 segundos) para evitar bloqueios no WhatsApp
    try:
        requests.post(
            f"{current_app.config['EVOLUTION_API_URL'].rstrip('/')}/chat/sendPresence/{instancia}",
            headers={'apikey': current_app.config['EVOLUTION_API_KEY']},
            json={'number': telefone, 'presence': 'composing', 'delay': 2000},
            timeout=10,
        ).raise_for_status()
        time.sleep(1.5)
    except requests.RequestException as erro:
        current_app.logger.warning(
            'Não foi possível enviar presença de digitação: %s', erro
        )

    # 5. Prepara contexto (últimas 10 mensagens) e chama a IA Blindada
    historico_genai, historico_dict, prompt_adicional = _preparar_contexto_ia(empresa, pessoa)
    db.session.commit()

    sucesso_ia, resposta_bot = _chamar_ia_com_fallback(
        empresa,
        pessoa,
        texto_recebido,
        historico_genai,
        historico_dict,
        prompt_adicional,
    )

    # IMPORTANTE: Se todas as IAs falharem por queda de internet/servidor,
    # NÃO desativa a IA da conversa! Apenas pede para o cliente repetir em instantes.
    if not sucesso_ia or not resposta_bot:
        current_app.logger.error(
            'Todas as tentativas de IA falharam para a empresa %s (Pessoa %s). Mantendo IA ativa.',
            empresa.id, pessoa.id
        )
        return 'ai_temporary_failure'

    # 6. Se a IA decidiu que é hora de transferir para a secretária/consultor
    if '[TRANSFERIR]' in resposta_bot:
        texto_limpo_ia = resposta_bot.replace('[TRANSFERIR]', '').strip()
        msg_final = texto_limpo_ia if texto_limpo_ia else empresa.mensagem_transbordo
        _transferir_para_humano(
            empresa, pessoa, telefone, instancia, msg_final
        )
        return 'transferred'

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
