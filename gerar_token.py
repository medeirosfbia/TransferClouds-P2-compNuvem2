"""Gera (uma única vez) o token OAuth da conta dona do Drive, usado para ENVIAR arquivos ao Drive.

Por que isso existe: Service Accounts não têm cota de armazenamento, então o Google recusa
uploads feitos por elas no Drive de uma conta Gmail. O upload é feito em nome da conta dona,
com este token; a leitura e a migração continuam usando a Service Account.

Uso:
  1. Baixe o cliente OAuth (tipo "App para computador") como oauth_client.json nesta pasta.
  2. python gerar_token.py   -> abre o navegador; entre com a conta dona do Drive.
  3. Será criado o token.json (no deploy, cole o conteúdo em GOOGLE_OAUTH_TOKEN_JSON).
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]

flow = InstalledAppFlow.from_client_secrets_file("oauth_client.json", SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
with open("token.json", "w") as f:
    f.write(creds.to_json())
print("token.json criado com sucesso.")
