"""Lógica de migração: Google Drive (origem) -> Azure Blob Storage (destino)."""
import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobServiceClient, ContentSettings
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as CredenciaisUsuario
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Arquivos nativos do Google (Docs, Sheets, Slides) não têm binário: precisam ser exportados.
EXPORTACOES = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", ".docx"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    "application/vnd.google-apps.drawing": ("application/pdf", ".pdf"),
}


# ---------------------------------------------------------------- Autenticação
# Conexões reaproveitadas entre requisições (evita refazer login a cada clique):
_creds = None                 # credencial Google: o token vale ~1h e é renovado sozinho
_local = threading.local()    # um cliente do Drive por thread (o httplib2 não é thread-safe)
_container = None             # cliente do Azure (thread-safe), criado uma única vez
_lock = threading.Lock()


def credenciais_google():
    """Carrega a Service Account uma vez só.

    Aceita o JSON inteiro em GOOGLE_SERVICE_ACCOUNT_JSON (deploy)
    ou o caminho do arquivo em GOOGLE_APPLICATION_CREDENTIALS (local).
    """
    global _creds
    with _lock:
        if _creds is None:
            info = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
            if info:
                _creds = service_account.Credentials.from_service_account_info(json.loads(info), scopes=SCOPES)
            else:
                caminho = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "credentials.json")
                _creds = service_account.Credentials.from_service_account_file(caminho, scopes=SCOPES)
        return _creds


def drive_service():
    """Cliente da API do Drive autenticado com a Service Account (um por thread)."""
    if not hasattr(_local, "drive"):
        _local.drive = build("drive", "v3", credentials=credenciais_google(), cache_discovery=False)
    return _local.drive


_creds_upload = None


def credenciais_upload():
    """Token OAuth da conta dona do Drive (gerado com gerar_token.py), usado só para upload.

    Lido de GOOGLE_OAUTH_TOKEN_JSON (deploy) ou de token.json (local). O token de acesso
    é renovado automaticamente com o refresh_token.
    """
    global _creds_upload
    with _lock:
        if _creds_upload is None:
            info = os.getenv("GOOGLE_OAUTH_TOKEN_JSON")
            if info:
                dados = json.loads(info)
            else:
                caminho = os.getenv("GOOGLE_OAUTH_TOKEN_FILE", "token.json")
                if not os.path.exists(caminho):
                    raise RuntimeError("Upload para o Drive não configurado: gere o token.json com gerar_token.py.")
                with open(caminho) as f:
                    dados = json.load(f)
            _creds_upload = CredenciaisUsuario.from_authorized_user_info(dados)
        return _creds_upload


def upload_habilitado():
    """Feature de upload para o Drive (desligada por padrão; ligar com DRIVE_UPLOAD=1)."""
    return os.getenv("DRIVE_UPLOAD", "0") == "1"


def upload_disponivel():
    if not upload_habilitado():
        return False
    try:
        credenciais_upload()
        return True
    except Exception:  # noqa: BLE001
        return False


def drive_upload_service():
    """Cliente do Drive em nome da conta dona (um por thread)."""
    if not hasattr(_local, "drive_upload"):
        _local.drive_upload = build("drive", "v3", credentials=credenciais_upload(), cache_discovery=False)
    return _local.drive_upload


def container_client():
    """Autentica no Azure com a Connection String e garante (uma vez) que o contêiner existe."""
    global _container
    with _lock:
        if _container is None:
            servico = BlobServiceClient.from_connection_string(os.environ["AZURE_STORAGE_CONNECTION_STRING"])
            container = servico.get_container_client(os.environ["AZURE_CONTAINER"])
            try:
                container.create_container()
            except ResourceExistsError:
                pass
            _container = container
        return _container


# ---------------------------------------------------------------- Listagens
PASTA = "application/vnd.google-apps.folder"


def listar_pastas(drive, pai="root"):
    """Lista as subpastas de uma pasta do Drive do usuário ('shared' = pastas compartilhadas com a Service Account)."""
    filtro = "sharedWithMe" if pai == "shared" else f"'{pai}' in parents"
    resp = drive.files().list(
        q=f"{filtro} and mimeType = '{PASTA}' and trashed = false",
        fields="files(id, name)", orderBy="name", pageSize=200,
        supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    return resp.get("files", [])


def info_pasta(drive, pasta_id):
    """Nome e pasta-pai (para o botão Voltar)."""
    if pasta_id == "shared":
        return {"id": "shared", "name": "Pastas disponíveis", "parents": []}
    return drive.files().get(fileId=pasta_id, fields="id, name, parents", supportsAllDrives=True).execute()


def listar_origem(pasta_id=None, drive=None):
    """Lista os arquivos da pasta do Google Drive (ignora subpastas)."""
    pasta_id = pasta_id or os.environ["DRIVE_FOLDER_ID"]
    drive = drive or drive_service()
    arquivos, token = [], None
    while True:
        resp = drive.files().list(
            q=f"'{pasta_id}' in parents and trashed = false "
              f"and mimeType != '{PASTA}'",
            fields="nextPageToken, files(id, name, mimeType, size, modifiedTime)",
            pageToken=token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        arquivos.extend(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            return arquivos


def listar_destino():
    """Lista os blobs do contêiner no Azure Blob Storage."""
    return [
        {"name": b.name, "size": b.size, "modified": b.last_modified.isoformat() if b.last_modified else ""}
        for b in container_client().list_blobs()
    ]


# ---------------------------------------------------------------- Migração
def _baixar(drive, arquivo):
    """Baixa um arquivo do Drive para memória. Retorna (bytes, nome_final, content_type)."""
    nome, mime = arquivo["name"], arquivo["mimeType"]
    if mime in EXPORTACOES:
        mime, ext = EXPORTACOES[mime]
        req = drive.files().export_media(fileId=arquivo["id"], mimeType=mime)
        if not nome.lower().endswith(ext):
            nome += ext
    elif mime.startswith("application/vnd.google-apps"):
        raise ValueError(f"tipo Google não suportado: {mime}")
    else:
        req = drive.files().get_media(fileId=arquivo["id"], supportsAllDrives=True)

    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, req)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()
    buffer.seek(0)
    return buffer, nome, mime


def enviar_para_drive(pasta_id, nome, stream, mime=None):
    """Envia um arquivo (vindo do navegador) para uma pasta do Google Drive."""
    try:
        media = MediaIoBaseUpload(stream, mimetype=mime or "application/octet-stream",
                                  resumable=True, chunksize=8 * 1024 * 1024)
        criado = drive_upload_service().files().create(
            body={"name": nome, "parents": [pasta_id]}, media_body=media,
            fields="id, name, size", supportsAllDrives=True,
        ).execute()
        print(f"[UPLOAD]  {nome} -> Drive ({criado['id']})", flush=True)
        return {"arquivo": criado["name"], "status": "sucesso", "detalhe": "Enviado para o Google Drive"}
    except Exception as e:  # noqa: BLE001
        print(f"[ERRO]    upload {nome}: {e}", flush=True)
        return {"arquivo": nome, "status": "erro", "detalhe": str(e)}


def _migrar_um(arquivo):
    """Baixa um arquivo do Drive e envia ao Blob. Roda em paralelo, então usa o cliente da própria thread."""
    try:
        dados, nome, mime = _baixar(drive_service(), arquivo)
        container_client().upload_blob(
            name=nome, data=dados, overwrite=True,
            content_settings=ContentSettings(content_type=mime),
        )
        print(f"[SUCESSO] {nome}", flush=True)
        return {"arquivo": nome, "status": "sucesso", "detalhe": "Enviado para o Blob Storage"}
    except Exception as e:  # noqa: BLE001 - queremos reportar qualquer falha por arquivo
        print(f"[ERRO]    {arquivo['name']}: {e}", flush=True)
        return {"arquivo": arquivo["name"], "status": "erro", "detalhe": str(e)}


def migrar_arquivo(file_id):
    """Transfere um único arquivo pelo ID (usado pela fila de transferência da interface)."""
    try:
        arquivo = drive_service().files().get(
            fileId=file_id, fields="id, name, mimeType, size", supportsAllDrives=True).execute()
    except Exception as e:  # noqa: BLE001
        print(f"[ERRO]    {file_id}: {e}", flush=True)
        return {"arquivo": file_id, "status": "erro", "detalhe": str(e)}
    return _migrar_um(arquivo)


def migrar(pasta_id=None, drive=None, ids=None, paralelo=4):
    """Copia os arquivos da origem para o destino e devolve o status de cada um.

    ids: IDs do Drive para enviar só os selecionados (None = todos da pasta).
    paralelo: quantos arquivos são transferidos ao mesmo tempo.
    """
    arquivos = listar_origem(pasta_id, drive)
    if ids is not None:
        selecionados = set(ids)
        arquivos = [a for a in arquivos if a["id"] in selecionados]
    if not arquivos:
        return []
    with ThreadPoolExecutor(max_workers=min(paralelo, len(arquivos))) as pool:
        return list(pool.map(_migrar_um, arquivos))  # mantém a ordem original


if __name__ == "__main__":
    # Modo console: python migrador.py
    from dotenv import load_dotenv

    load_dotenv()
    print("== Origem (Google Drive) ==")
    for a in listar_origem():
        print(f"  {a['name']}  ({a['mimeType']})")
    print("== Migrando ==")
    migrar()
    print("== Destino (Azure Blob) ==")
    for b in listar_destino():
        print(f"  {b['name']}  ({b['size']} bytes)")
