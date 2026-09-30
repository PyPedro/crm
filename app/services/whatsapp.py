import requests
from flask import current_app

def enviar_mensagem_whatsapp(numero, texto, instance_name):
    # Pega as credenciais automaticamente da configuração do Flask (que lê do .env)
    api_url = current_app.config['EVOLUTION_API_URL']
    api_key = current_app.config['EVOLUTION_API_KEY']
    url = f"{api_url}/message/sendText/{instance_name}"
    headers = {
        "apikey": api_key,
        "Content-Type": "application/json"
    }
    payload = {
        "number": str(numero),
        "text": texto
    }
    
    try:
        resposta = requests.post(url, json=payload, headers=headers)
        resposta.raise_for_status() 
        return resposta.json()
    except requests.exceptions.RequestException as e:
        print(f"Erro na comunicação com a Evolution API: {e}")
        return None