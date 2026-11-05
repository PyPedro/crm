import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime

import requests
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


STOPWORDS_PT = {
    'a', 'o', 'as', 'os', 'um', 'uma', 'de', 'do', 'da', 'dos', 'das', 'em', 'no', 'na',
    'por', 'para', 'com', 'sem', 'que', 'qual', 'quais', 'como', 'onde', 'quando', 'quem',
    'eu', 'voce', 'voces', 'ele', 'ela', 'nos', 'me', 'te', 'se', 'meu', 'minha', 'seu',
    'sua', 'esse', 'essa', 'isso', 'este', 'esta', 'isto', 'ai', 'ali', 'la', 'ca', 'e',
    'ou', 'mas', 'se', 'ja', 'so', 'nao', 'sim', 'bom', 'boa', 'dia', 'tarde', 'noite',
    'oi', 'ola', 'opa', 'tudo', 'bem', 'gostaria', 'quero', 'queria', 'saber', 'sobre',
    'tem', 'ter', 'esta', 'estao', 'ser', 'foi', 'sao', 'era', 'pelo', 'pela', 'ao', 'aos'
}

SAUDACOES_COMUNS = {
    'oi', 'ola', 'opa', 'bom dia', 'boa tarde', 'boa noite', 'tudo bem',
    'tudo bom', 'como vai', 'ei', 'alo', 'menu', 'inicio', 'comecar'
}

INTENCOES_HUMANO_OU_AGENDAMENTO = {
    'agendar', 'marcar', 'consulta', 'horario', 'atendente', 'secretaria',
    'recepcao', 'humano', 'pessoa', 'falar com alguem', 'emergencia', 'urgente', 'dor'
}


@worker_process_init.connect
def init_celery_db_connection(**kwargs):
    flask_app = celery.flask_app
    with flask_app.app_context():
        db.session.remove()
        db.engine.dispose(close=False)


def _normalizar_texto(texto):
    if not texto:
        return ''
    texto = unicodedata.normalize('NFKD', texto.lower())
    texto = ''.join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r'[^\w\s]', ' ', texto)
    return re.sub(r'\s+', ' ', texto).strip()


def _extrair_palavras_relevantes(texto_norm):
    return {
        p for p in texto_norm.split()
        if len(p) > 2 and p not in STOPWORDS_PT
    }


def _personalizar_mensagem(texto, empresa, pessoa):
    nome_cli = pessoa.nome if (pessoa.nome and pessoa.nome != 'Novo Contato') else 'Cliente'
    return (texto or '').replace(
        '{nome_cliente}', nome_cli
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


def _simular_digitacao(instancia, telefone, segundos=1.5):
    try:
        requests.post(
            f"{current_app.config['EVOLUTION_API_URL'].rstrip('/')}/chat/sendPresence/{instancia}",
            headers={'apikey': current_app.config['EVOLUTION_API_KEY']},
            json={'number': telefone, 'presence': 'composing', 'delay': int(segundos * 1000)},
            timeout=8,
        )
        time.sleep(segundos)
    except requests.RequestException as erro:
        current_app.logger.warning('Não foi possível enviar presença de digitação: %s', erro)


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
        return hora_atual_br()


def _buscar_melhor_faq(regras, texto_recebido):
    """
    Procura nas Perguntas e Respostas cadastradas a melhor resposta para o cliente,
    mesmo que o cliente escreva com outras palavras ou sem acento.
    Permite cadastrar gatilhos separados por vírgula (ex: "valor, preço, quanto custa").
    """
    texto_norm = _normalizar_texto(texto_recebido)
    if not texto_norm:
        return None

    palavras_msg = _extrair_palavras_relevantes(texto_norm)
    melhor_regra = None
    maior_pontuacao = 0.0

    for regra in regras:
        gatilho_bruto = regra.palavra_chave or ''
        # Permite separar sinônimos por vírgula ou ponto-e-vírgula no cadastro
        sub_gatilhos = [g.strip() for g in re.split(r'[,;|/]', gatilho_bruto) if g.strip()]
        if not sub_gatilhos:
            sub_gatilhos = [gatilho_bruto]

        for sub in sub_gatilhos:
            sub_norm = _normalizar_texto(sub)
            if not sub_norm:
                continue

            # 1. Match exato ou frase contida diretamente
            if sub_norm == texto_norm or sub_norm in texto_norm:
                pontuacao = 100.0 + len(sub_norm)
                if pontuacao > maior_pontuacao:
                    maior_pontuacao = pontuacao
                    melhor_regra = regra
                continue

            # 2. Match por interseção de palavras relevantes
            palavras_gatilho = _extrair_palavras_relevantes(sub_norm)
            if not palavras_gatilho or not palavras_msg:
                continue

            em_comum = palavras_gatilho.intersection(palavras_msg)
            if em_comum:
                proporcao = len(em_comum) / len(palavras_gatilho)
                # Aceita se acertou pelo menos 60% das palavras-chave da pergunta ou >= 2 palavras fortes
                if proporcao >= 0.6 or len(em_comum) >= 2:
                    pontuacao = (proporcao * 50.0) + (len(em_comum) * 10.0)
                    if pontuacao > maior_pontuacao:
                        maior_pontuacao = pontuacao
                        melhor_regra = regra

    return melhor_regra


def _eh_apenas_saudacao(texto_recebido):
    texto_norm = _normalizar_texto(texto_recebido)
    if not texto_norm:
        return False
    if texto_norm in SAUDACOES_COMUNS:
        return True
    palavras = texto_norm.split()
    if len(palavras) <= 4 and any(s in texto_norm for s in SAUDACOES_COMUNS):
        palavras_uteis = _extrair_palavras_relevantes(texto_norm)
        return len(palavras_uteis) == 0
    return False


def _simular_assistente_local(empresa, pessoa, texto_recebido, regras, eh_primeira_interacao):
    """
    Motor Conversacional Local (Funciona 100% sem IA externa):
    Simula a assistente virtual usando as saudações, perguntas/respostas e transbordo da empresa.
    Retorna: (acao, texto_resposta) onde acao pode ser 'replied' ou 'transfer'.
    """
    saudacao_padrao = _personalizar_mensagem(
        empresa.mensagem_saudacao or f'Olá! Bem-vindo(a) à {empresa.nome}. Como posso ajudar você hoje?',
        empresa,
        pessoa,
    )

    # 1. Verifica se bate com alguma Pergunta/Resposta (FAQ) cadastrada
    regra_match = _buscar_melhor_faq(regras, texto_recebido)
    if regra_match:
        resposta_faq = _personalizar_mensagem(regra_match.resposta, empresa, pessoa)
        if eh_primeira_interacao:
            return 'replied', f'{saudacao_padrao}\n\n{resposta_faq}'
        return 'replied', resposta_faq

    # 2. Se for a primeira mensagem do cliente ou uma saudação ("Oi", "Bom dia")
    if eh_primeira_interacao or _eh_apenas_saudacao(texto_recebido):
        if regras:
            # Lista sutilmente até 4 tópicos que o sistema sabe responder
            topicos = []
            for r in regras[:4]:
                primeiro_termo = re.split(r'[,;|/]', r.palavra_chave or '')[0].strip()
                if primeiro_termo:
                    topicos.append(f'• {primeiro_termo}')
            if topicos:
                lista_topicos = '\n'.join(topicos)
                return 'replied', f'{saudacao_padrao}\n\nSe preferir, você pode perguntar sobre:\n{lista_topicos}'
        return 'replied', saudacao_padrao

    # 3. Se o cliente já respondeu algo específico (ex: disse o nome, sintoma, pedido de agendamento)
    texto_norm = _normalizar_texto(texto_recebido)
    pediu_humano = any(termo in texto_norm for termo in INTENCOES_HUMANO_OU_AGENDAMENTO)

    # Conta quantas respostas automáticas o bot já deu hoje para este contato
    respostas_bot = Mensagem.query.filter_by(
        empresa_id=empresa.id, pessoa_id=pessoa.id, tipo='outbound'
    ).count()

    # Se o cliente falou o nome/assunto na segunda mensagem e ainda não foi transferido,
    # faz uma pergunta de qualificação ou transfere suavemente para a recepção
    if not pediu_humano and respostas_bot <= 1 and len(texto_norm.split()) <= 4:
        # Provavelmente o cliente acabou de responder "Como você prefere ser chamado(a)?"
        nome_informado = texto_recebido.strip().title()
        if len(nome_informado) <= 30 and not any(c.isdigit() for c in nome_informado):
            pessoa.nome = nome_informado
            db.session.commit()
        return (
            'replied',
            f'Prazer, {pessoa.nome}! 😊 Me conta rapidinho: qual procedimento, produto ou horário você procura hoje?'
        )

    # 4. Caso já tenha coletado a necessidade do cliente ou não haja FAQ específica,
    # encaminha com empatia para a recepção/equipe humana assumir
    msg_transbordo = _personalizar_mensagem(
        empresa.mensagem_transbordo
        or 'Perfeito! Já anotei suas informações aqui e vou transferir você para nossa equipe finalizar seu atendimento. Um momento, por favor! ✨',
        empresa,
        pessoa,
    )
    return 'transfer', msg_transbordo


def _preparar_contexto_ia(empresa, pessoa):
    config = Configuracao.query.filter_by(empresa_id=empresa.id).first()
    ultimas = Mensagem.query.filter_by(
        empresa_id=empresa.id, pessoa_id=pessoa.id
    ).order_by(Mensagem.id.desc()).limit(10).all()
    ultimas.reverse()

    historico_genai = []
    historico_dict = []

    for mensagem in ultimas[:-1]:
        texto_historico = mensagem.mensagem
        try:
            obj = json.loads(mensagem.mensagem)
            if isinstance(obj, dict) and 'type' in obj:
                texto_historico = obj.get('caption') or f"[Arquivo de {obj.get('type')}]"
        except (TypeError, ValueError):
            pass

        texto_historico = (texto_historico or '').strip()
        if not texto_historico:
            continue

        historico_genai.append(types.Content(
            role='user' if mensagem.tipo == 'inbound' else 'model',
            parts=[types.Part.from_text(text=texto_historico)],
        ))
        historico_dict.append({
            'role': 'user' if mensagem.tipo == 'inbound' else 'assistant',
            'content': texto_historico
        })

    return historico_genai, historico_dict, (config.prompt_ia if config else None)


def _chamar_ia_com_fallback(empresa, pessoa, texto_recebido, historico_genai, historico_dict, prompt_adicional, regras):
    bloco_faq = ''
    if regras:
        linhas_faq = [
            f"- Pergunta/Assunto: {r.palavra_chave}\n  Resposta Oficial: {_personalizar_mensagem(r.resposta, empresa, pessoa)}"
            for r in regras
        ]
        bloco_faq = '\n\nTABELA DE PERGUNTAS E RESPOSTAS OFICIAIS DA EMPRESA:\n' + '\n'.join(linhas_faq)

    instrucoes_empresa = f"{empresa.prompt_personalidade or ''}\n\n{prompt_adicional or ''}{bloco_faq}".strip()

    system_prompt = f"""Você é a assistente virtual da empresa {empresa.nome}.
Tom de voz: {empresa.tom_resposta or 'Acolhedor e empático'}.
Saudação base da empresa: {_personalizar_mensagem(empresa.mensagem_saudacao, empresa, pessoa)}

DIRETRIZES E BASE DE CONHECIMENTO:
{instrucoes_empresa}

REGRAS OBRIGATÓRIAS:
1. Converse pelo WhatsApp de forma natural, acolhedora e objetiva.
2. Sempre que a dúvida do cliente estiver na TABELA DE PERGUNTAS E RESPOSTAS OFICIAIS acima, utilize a Resposta Oficial como base.
3. Faça apenas UMA pergunta por vez.
4. Quando concluir a coleta de dados para agendamento/atendimento ou se o cliente pedir para falar com humano/secretária, finalize sua frase e adicione a tag [TRANSFERIR]."""

    mensagens_api = [{'role': 'system', 'content': system_prompt}] + historico_dict + [
        {'role': 'user', 'content': texto_recebido}
    ]

    # 1. Tentativa via Groq
    groq_key = os.environ.get('GROQ_API_KEY')
    if groq_key:
        try:
            res = requests.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization': f'Bearer {groq_key}', 'Content-Type': 'application/json'},
                json={'model': 'llama-3.3-70b-versatile', 'messages': mensagens_api, 'temperature': 0.3, 'max_tokens': 450},
                timeout=10,
            )
            if res.status_code == 200:
                texto = res.json()['choices'][0]['message']['content'].strip()
                if texto:
                    return True, texto
        except Exception as e:
            current_app.logger.warning('Groq indisponível: %s', e)

    # 2. Tentativa via OpenAI
    openai_key = os.environ.get('OPENAI_API_KEY')
    if openai_key:
        try:
            res = requests.post(
                'https://api.openai.com/v1/chat/completions',
                headers={'Authorization': f'Bearer {openai_key}', 'Content-Type': 'application/json'},
                json={'model': 'gpt-4o-mini', 'messages': mensagens_api, 'temperature': 0.3, 'max_tokens': 450},
                timeout=10,
            )
            if res.status_code == 200:
                texto = res.json()['choices'][0]['message']['content'].strip()
                if texto:
                    return True, texto
        except Exception as e:
            current_app.logger.warning('OpenAI indisponível: %s', e)

    # 3. Tentativa via Gemini REST API
    gemini_key = (
        os.environ.get('GEMINI_API_KEY')
        or os.environ.get('GOOGLE_API_KEY')
        or current_app.config.get('GEMINI_API_KEY')
    )
    if gemini_key:
        conteudos_gemini = [
            {'role': 'user' if m['role'] == 'user' else 'model', 'parts': [{'text': m['content']}]}
            for m in historico_dict
        ]
        conteudos_gemini.append({'role': 'user', 'parts': [{'text': texto_recebido}]})

        payload_gemini = {
            'systemInstruction': {'parts': [{'text': system_prompt}]},
            'contents': conteudos_gemini,
            'generationConfig': {'temperature': 0.3, 'maxOutputTokens': 500},
            'safetySettings': [
                {'category': 'HARM_CATEGORY_HARASSMENT', 'threshold': 'BLOCK_NONE'},
                {'category': 'HARM_CATEGORY_HATE_SPEECH', 'threshold': 'BLOCK_NONE'},
                {'category': 'HARM_CATEGORY_SEXUALLY_EXPLICIT', 'threshold': 'BLOCK_NONE'},
                {'category': 'HARM_CATEGORY_DANGEROUS_CONTENT', 'threshold': 'BLOCK_NONE'},
            ],
        }
        for modelo in ('gemini-2.5-flash', 'gemini-2.0-flash'):
            try:
                url = f'https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent?key={gemini_key}'
                res = requests.post(url, json=payload_gemini, timeout=12)
                if res.status_code == 200:
                    candidatos = res.json().get('candidates') or []
                    if candidatos:
                        partes = candidatos[0].get('content', {}).get('parts') or []
                        if partes and partes[0].get('text'):
                            return True, partes[0]['text'].strip()
            except Exception as e:
                current_app.logger.warning('Gemini (%s) indisponível: %s', modelo, e)

    # 4. Tentativa via serviço legado
    try:
        sucesso_ia, resposta_bot = gerar_resposta_ia(
            empresa.tom_resposta,
            instrucoes_empresa,
            texto_recebido,
            historico_genai,
            prompt_adicional,
        )
        if sucesso_ia and resposta_bot:
            return True, resposta_bot
    except Exception:
        pass

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
        texto_recebido = '[Arquivo enviado pelo cliente]'
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
                mime = {'image': 'image/jpeg', 'audio': 'audio/ogg', 'document': 'application/pdf'}[media_type]
                base64_media = f'data:{mime};base64,{base64_media}'
            if base64_media:
                mensagem_db = json.dumps({'type': media_type, 'content': base64_media, 'caption': texto_recebido})
        except (requests.RequestException, ValueError):
            pass

    db.session.add(Mensagem(
        empresa_id=empresa.id,
        pessoa_id=pessoa.id,
        mensagem=mensagem_db,
        tipo='inbound',
        data_envio=data_mensagem,
    ))
    db.session.commit()

    if (atendimento_reaberto or eh_novo_contato) and empresa.usar_menu_inicial:
        _simular_digitacao(instancia, telefone, 1.2)
        _enviar_resposta(
            empresa.id, pessoa.id, telefone, instancia, _montar_texto_menu(empresa, pessoa)
        )
        return 'menu_sent'

    if pessoa.status_atendimento == 'humano':
        return 'human'

    if pessoa.status_atendimento == 'menu':
        etapa = None
        texto_opcao = (texto_recebido or '').strip()
        if texto_opcao.isdecimal():
            etapa = Etapa.query.filter_by(
                empresa_id=empresa.id, exibir_no_menu=True, numero_menu=int(texto_opcao)
            ).first()
        _simular_digitacao(instancia, telefone, 1.2)
        if etapa:
            negocio = Negocio.query.filter_by(
                empresa_id=empresa.id, pessoa_id=pessoa.id
            ).order_by(Negocio.id).first()
            if negocio:
                negocio.etapa_id = etapa.id
            _transferir_para_humano(
                empresa, pessoa, telefone, instancia, empresa.mensagem_transbordo
            )
            return 'transferred'
        _enviar_resposta(
            empresa.id, pessoa.id, telefone, instancia, _montar_texto_menu(empresa, pessoa)
        )
        return 'menu'

    if pessoa.status_atendimento != 'ia' or not pessoa.ia_ativa:
        return 'ignored'

    # Carrega todas as Perguntas e Respostas cadastradas para esta empresa
    regras = RespostaAutomatica.query.filter_by(empresa_id=empresa.id).order_by(RespostaAutomatica.id).all()
    eh_primeira_interacao = eh_novo_contato or atendimento_reaberto

    # Simula digitação humana no WhatsApp ("Digitando...")
    _simular_digitacao(instancia, telefone, 1.8)

    # PRIORIDADE 1: Se a mensagem do cliente bate com alguma Pergunta/Resposta cadastrada,
    # o próprio sistema já responde instantaneamente de forma humanizada (sem depender da IA!)
    regra_direta = _buscar_melhor_faq(regras, texto_recebido)
    if regra_direta:
        resposta_pronta = _personalizar_mensagem(regra_direta.resposta, empresa, pessoa)
        if eh_primeira_interacao and empresa.mensagem_saudacao:
            saudacao = _personalizar_mensagem(empresa.mensagem_saudacao, empresa, pessoa)
            resposta_pronta = f'{saudacao}\n\n{resposta_pronta}'
        _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, resposta_pronta)
        return 'faq_simulated_replied'

    # PRIORIDADE 2: Se for a primeira mensagem e for apenas uma saudação ("Oi", "Bom dia"),
    # envia a Primeira Mensagem (Saudação) configurada na empresa sem gastar API nem correr risco de erro
    if (eh_primeira_interacao or _eh_apenas_saudacao(texto_recebido)) and empresa.mensagem_saudacao:
        _, texto_saudacao = _simular_assistente_local(
            empresa, pessoa, texto_recebido, regras, eh_primeira_interacao=True
        )
        _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, texto_saudacao)
        return 'greeting_simulated_replied'

    # PRIORIDADE 3: Tenta gerar resposta contextual com a IA (alimentada também pelas FAQs)
    historico_genai, historico_dict, prompt_adicional = _preparar_contexto_ia(empresa, pessoa)
    db.session.commit()

    sucesso_ia, resposta_bot = _chamar_ia_com_fallback(
        empresa, pessoa, texto_recebido, historico_genai, historico_dict, prompt_adicional, regras
    )

    if sucesso_ia and resposta_bot:
        if '[TRANSFERIR]' in resposta_bot:
            texto_limpo_ia = resposta_bot.replace('[TRANSFERIR]', '').strip()
            msg_final = texto_limpo_ia if texto_limpo_ia else empresa.mensagem_transbordo
            _transferir_para_humano(empresa, pessoa, telefone, instancia, msg_final)
            return 'transferred'

        _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, resposta_bot)
        return 'replied'

    # PRIORIDADE 4 (FALLBACK TOTAL SEM IA):
    # Se não houver chave de IA ou se todas as IAs estiverem fora do ar, o próprio sistema simula o fluxo!
    acao_local, resposta_local = _simular_assistente_local(
        empresa, pessoa, texto_recebido, regras, eh_primeira_interacao
    )
    if acao_local == 'transfer':
        _transferir_para_humano(empresa, pessoa, telefone, instancia, resposta_local)
        return 'simulated_transferred'

    _enviar_resposta(empresa.id, pessoa.id, telefone, instancia, resposta_local)
    return 'simulated_replied'


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
