import requests

url = "http://localhost:5000/webhook/whatsapp"
payload = {
    "event": "messages.upsert",
    "data": {
        "key": {
            "remoteJid": "5581999999999@s.whatsapp.net", 
            "fromMe": False
        },
        "message": {
            "conversation": "Olá, gostaria de saber mais sobre a plataforma corporativa!"
        }
    }
}

resposta = requests.post(url, json=payload)
print(resposta.status_code, resposta.json())