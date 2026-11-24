# Migrador Google Drive → Azure Blob Storage

Aplicação Flask em que o usuário navega pelas pastas do Google Drive liberadas para o app, escolhe uma, copia os arquivos dela para um contêiner do Azure Blob Storage e vê o status de cada transferência (sucesso/erro) na interface web e no console.

- `migrador.py`: autenticação, `listar_origem()` (Drive), `listar_destino()` (Blob) e `migrar()`
- `app.py` + `templates/`: navegador de pastas e interface gráfica

## 1. Configurar o Google Drive (Service Account)

O app usa uma conta Google dedicada. Uma **Service Account** só enxerga as pastas que essa conta compartilhou com ela, então o restante do Drive fica inacessível. Na tela, o usuário navega pelas pastas liberadas e escolhe qual migrar.

Faça tudo logado na conta Google dona dos arquivos:

1. Em <https://console.cloud.google.com>, crie um projeto (ex.: `p2-nuvem`).
2. **APIs e serviços → Biblioteca** → **Google Drive API** → **Ativar**.
3. **IAM e administrador → Contas de serviço → Criar conta de serviço** (nome: `migrador`). Pule os papéis e clique em Concluir.
4. Abra a conta → aba **Chaves** → **Adicionar chave → Criar nova chave → JSON**. Salve como `credentials.json` nesta pasta. **Nunca faça commit desse arquivo.**
5. Copie o e-mail da Service Account (`migrador@p2-nuvem.iam.gserviceaccount.com`).
6. No Google Drive, clique com o botão direito em cada pasta que o app pode acessar → **Compartilhar** → cole o e-mail como **Leitor** e desmarque "Notificar".

> Se a tela mostrar "Nenhuma pasta foi compartilhada com a Service Account", refaça o passo 6. O compartilhamento pode levar alguns segundos para aparecer.

## 1.1 (Opcional, desligado) Upload para o Drive pela interface (token OAuth)

Recurso pronto, mas desativado por padrão. Para ligar, siga os passos abaixo e defina `DRIVE_UPLOAD=1`.

**Service Accounts não têm cota de armazenamento**, então o Google recusa uploads feitos por elas no Drive de uma conta Gmail. Por isso o upload é feito **em nome da conta dona do Drive**, com um token gerado uma única vez. A leitura e a migração continuam usando a Service Account.

No mesmo projeto do Google Cloud, logado na conta dona do Drive:

1. **Google Auth Platform → Branding**: preencha o nome do app e o e-mail de suporte.
2. **Público-alvo (Audience)**: tipo **Externo**. Clique em **Publicar app** para colocá-lo **Em produção**. Em modo "Teste", o token expira em 7 dias.
3. **Clientes → Criar cliente → App para computador** e baixe o JSON como `oauth_client.json` nesta pasta.
4. Rode `python gerar_token.py`. O navegador abre: entre com a conta dona do Drive. No aviso "O Google não verificou este app", clique em **Avançado → Acessar** e autorize.
5. O arquivo `token.json` será criado. Ele não vai para o Git, porque está no `.gitignore`.
6. No deploy, crie a variável `GOOGLE_OAUTH_TOKEN_JSON` com o **conteúdo** do `token.json`.

O indicador **● Upload** na barra de ferramentas fica verde quando o token está configurado.

## 2. Configurar o Azure

Use a Connection String da prova. O nome do contêiner **só pode ter letras minúsculas, números e hífen** (3–63 caracteres). Então `Aluno_xxx` vira algo como `aluno-seunome`. A aplicação cria o contêiner se ele não existir.

## 3. Rodar localmente

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # e preencha os valores
python app.py             # http://localhost:5000
# ou só no console:
python migrador.py        # modo console: migra a pasta de DRIVE_FOLDER_ID
```

## 4. Publicar

### Opção A: Render (mais rápido)
1. Suba o código para o GitHub (`credentials.json` e `.env` ficam fora por causa do `.gitignore`).
2. Em <https://render.com> crie um **New → Web Service**, conecte o repositório e configure:
   - Build: `pip install -r requirements.txt`
   - Start: `gunicorn --threads 8 --timeout 600 app:app`
3. Em **Environment**, adicione:
   - `GOOGLE_SERVICE_ACCOUNT_JSON` = conteúdo inteiro do `credentials.json`
   - `GOOGLE_OAUTH_TOKEN_JSON` = conteúdo inteiro do `token.json`
   - `FLASK_SECRET_KEY` (string aleatória), `AZURE_STORAGE_CONNECTION_STRING`, `AZURE_CONTAINER`

### Opção B: Azure App Service
```bash
az login
az webapp up --runtime PYTHON:3.12 --sku F1 --name migrador-seunome
az webapp config appsettings set --name migrador-seunome --resource-group <rg exibido acima> --settings \
  FLASK_SECRET_KEY=... GOOGLE_SERVICE_ACCOUNT_JSON="$(cat credentials.json)" GOOGLE_OAUTH_TOKEN_JSON="$(cat token.json)" \
  AZURE_CONTAINER=aluno-seunome AZURE_STORAGE_CONNECTION_STRING="..."
az webapp config set --name migrador-seunome --resource-group <rg> --startup-file "gunicorn --bind=0.0.0.0 --threads 8 --timeout 600 app:app"
```
