import requests

url = "http://localhost:8080/webhook/set/crm_vendas"
headers = {"apikey": "sua_chave_secreta_aqui"}
payload = {
    "webhook": {
        "enabled": True,
        "url": "https://5a54-2804-2c98-ca-7400-a8c9-95d0-c0b9-c197.ngrok-free.app/webhook/whatsapp",
        "byEvents": False,
        "base64": False,
        "events": ["MESSAGES_UPSERT"]
    }
}

resposta = requests.post(url, json=payload, headers=headers)
print("Configuração atualizada:", resposta.json())