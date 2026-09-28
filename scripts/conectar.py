import os
import requests
import qrcode
from dotenv import load_dotenv

# Tenta carregar o .env da raiz do projeto
basedir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
load_dotenv(os.path.join(basedir, '.env'))

# Carrega as configurações (AGORA COM A REDE DE SEGURANÇA NA API KEY)
api_url = os.environ.get('EVOLUTION_API_URL', 'http://localhost:8080')
api_key = os.environ.get('EVOLUTION_API_KEY', 'sua_chave_secreta_aqui')
instance_name = os.environ.get('INSTANCE_NAME', 'crm_vendas')

def gerar_qrcode_terminal():
    print(f"🔄 A verificar estado da instância '{instance_name}'...")
    
    url = f"{api_url}/instance/connect/{instance_name}"
    headers = {"apikey": api_key}

    try:
        response = requests.get(url, headers=headers)
        
        # Se der erro 401, avisa logo o motivo
        if response.status_code == 401:
            print(f"\n❌ Erro 401: A API rejeitou a senha. A chave usada foi: '{api_key}'")
            return

        dados = response.json()

        # Verifica se já está conectado
        if isinstance(dados, dict):
            estado = dados.get('instance', {}).get('state') or dados.get('state')
            if estado == 'open':
                print("\n✅ O seu WhatsApp já está conectado e pronto a processar vendas!")
                return

        # Tenta extrair a string de texto puro do QR Code da API
        codigo_qr_cru = None
        if 'code' in dados and not dados['code'].startswith('data:image'):
            codigo_qr_cru = dados['code']
        elif 'qrcode' in dados:
            if isinstance(dados['qrcode'], dict) and 'code' in dados['qrcode']:
                codigo_qr_cru = dados['qrcode']['code']
            elif isinstance(dados['qrcode'], str) and not dados['qrcode'].startswith('data:image'):
                codigo_qr_cru = dados['qrcode']

        if codigo_qr_cru:
            print("\n📱 Aponte a câmara do seu telemóvel para o QR Code abaixo:\n")
            
            # Gera e imprime o QR code no terminal
            qr = qrcode.QRCode()
            qr.add_data(codigo_qr_cru)
            qr.make(fit=True)
            
            qr.print_ascii(invert=True)
            
            print("\nA aguardar leitura...")
        else:
            print("\n❌ Não foi possível obter o código. O sistema devolveu:")
            print(dados)

    except Exception as e:
        print(f"\n❌ Erro ao tentar conectar à Evolution API: {e}")

if __name__ == '__main__':
    gerar_qrcode_terminal()