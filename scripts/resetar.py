import requests
import base64
import time

headers = {"apikey": "sua_chave_secreta_aqui"}
nome_instancia = "crm_vendas"

# 1. Força a eliminação da instância bloqueada
print("A limpar a instância corrompida...")
requests.delete(f"http://localhost:8080/instance/delete/{nome_instancia}", headers=headers)
time.sleep(2) # Pausa rápida para o Docker processar a eliminação

# 2. Cria uma nova instância fresca
print("A gerar uma nova sessão limpa...")
payload = {
    "instanceName": nome_instancia,
    "qrcode": True,
    "integration": "WHATSAPP-BAILEYS"
}
resposta = requests.post("http://localhost:8080/instance/create", json=payload, headers=headers)
dados = resposta.json()

# 3. Guarda o novo QR Code
if "qrcode" in dados and "base64" in dados["qrcode"]:
    imagem_b64 = dados["qrcode"]["base64"].split(",")[1]
    with open("qrcode.png", "wb") as f:
        f.write(base64.b64decode(imagem_b64))
    print("Sucesso! O ficheiro 'qrcode.png' foi substituído. Leia-o de imediato.")
else:
    print("Falha ao gerar o código:", dados)