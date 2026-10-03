import logging
import os
import unicodedata

import httpx
from google import genai
from google.genai import errors, types


_MENSAGENS_FALHA_IA = {
    'profissional': (
        'Ocorreu uma instabilidade momentânea em nosso sistema de atendimento '
        'automático. Estou transferindo você para um consultor humano.'
    ),
    'descontraido': (
        'Ops, meu cérebro virtual deu um branco aqui! 😅 Já estou chamando '
        'alguém da equipe para te ajudar!'
    ),
    'empatico': (
        'Entendo que você precisa de ajuda. Estou encaminhando sua conversa '
        'para uma pessoa da nossa equipe.'
    ),
    'direto': 'Sistema temporariamente indisponível. Transferindo para um atendente.',
}


def obter_mensagem_falha_ia(tom_resposta):
    tom_normalizado = unicodedata.normalize(
        'NFKD', (tom_resposta or 'Profissional').strip().casefold()
    ).encode('ascii', 'ignore').decode('ascii')
    return _MENSAGENS_FALHA_IA.get(
        tom_normalizado, _MENSAGENS_FALHA_IA['profissional']
    )


def gerar_resposta_ia(
    tom_resposta,
    prompt_personalidade,
    texto_recebido,
    historico,
    prompt_adicional=None,
):
    system_instruction = (
        f'Aja como assistente da empresa. Tom: {tom_resposta}. '
        f'Regras: {prompt_personalidade}. SEJA EXTREMAMENTE CONCISO. '
        'Não gaste tokens com respostas longas. Responda apenas com base no contexto.'
    )
    if prompt_adicional:
        system_instruction += f' Instruções adicionais: {prompt_adicional}'

    try:
        client = genai.Client(
            api_key=os.environ.get('GEMINI_API_KEY'),
            http_options=types.HttpOptions(
                timeout=5_000,
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        chat = client.chats.create(
            model='gemini-3.6-flash',
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                temperature=0.4,
            ),
            history=historico,
        )
        resposta = chat.send_message(texto_recebido).text
        if not resposta or not resposta.strip():
            raise RuntimeError('O Gemini retornou uma resposta vazia.')
        return True, resposta.strip()
    except (httpx.TimeoutException, httpx.NetworkError) as erro:
        logging.error('Timeout ou falha de conexão com o Gemini: %s', erro, exc_info=True)
    except errors.APIError as erro:
        logging.error('Erro da API Gemini: %s', erro, exc_info=True)
    except Exception as erro:
        logging.error('Falha inesperada na integração Gemini: %s', erro, exc_info=True)
    return False, obter_mensagem_falha_ia(tom_resposta)