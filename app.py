"""Interface web (Flask) para migrar pastas do Google Drive para o Azure Blob Storage.

O app usa uma Service Account: ela só enxerga as pastas que a conta dona do Drive
compartilhou com o e-mail dela. O usuário navega por essas pastas e escolhe qual migrar.
"""
import os
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import migrador

load_dotenv()
app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # limite de upload: 100 MB por arquivo
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)


@app.template_filter("tamanho")
def tamanho(valor):
    """Formata bytes em B/KB/MB/GB."""
    if valor in (None, ""):
        return "—"
    n = float(valor)
    for unidade in ("B", "KB", "MB", "GB"):
        if n < 1024 or unidade == "GB":
            return f"{n:.0f} {unidade}" if unidade == "B" else f"{n:.1f} {unidade}"
        n /= 1024


@app.template_filter("tipo")
def tipo(arquivo):
    """Rótulo curto do tipo (PDF, DOCX, IMG...) para o ícone colorido."""
    nome, mime = arquivo.get("name", ""), arquivo.get("mimeType", "")
    google = {"document": "DOC", "spreadsheet": "XLS", "presentation": "PPT", "drawing": "IMG"}
    if mime.startswith("application/vnd.google-apps."):
        return google.get(mime.rsplit(".", 1)[-1], "GGL")
    if mime.startswith("image/"):
        return "IMG"
    if mime.startswith("video/"):
        return "VID"
    ext = nome.rsplit(".", 1)[-1].upper() if "." in nome else "ARQ"
    return ext[:4]


def _voltar(drive, pasta):
    """Destino do botão Voltar: a pasta-pai se a Service Account tiver acesso, senão a raiz."""
    if pasta["id"] == "shared" or not pasta.get("parents"):
        return None if pasta["id"] == "shared" else "shared"
    try:
        migrador.info_pasta(drive, pasta["parents"][0])
        return pasta["parents"][0]
    except Exception:  # noqa: BLE001 - pai não compartilhado com a Service Account
        return "shared"


def _carregar_pasta(pasta_id):
    """Dados da pasta + destino do botão Voltar (o Voltar depende da pasta, então vão juntos)."""
    drive = migrador.drive_service()
    pasta = migrador.info_pasta(drive, pasta_id)
    return pasta, _voltar(drive, pasta)


@app.route("/")
def index():
    pasta_id = request.args.get("pasta", "shared")
    ctx = {"container": os.getenv("AZURE_CONTAINER", ""), "conta": None, "pasta": None,
           "voltar": None, "subpastas": [], "origem": [], "destino": [], "erro": None,
           "resultados": session.pop("resultados", None)}

    # As consultas ao Google e ao Azure são independentes: rodam ao mesmo tempo.
    with ThreadPoolExecutor(max_workers=4) as pool:
        f_pasta = pool.submit(_carregar_pasta, pasta_id)
        f_sub = pool.submit(lambda: migrador.listar_pastas(migrador.drive_service(), pasta_id))
        f_origem = pool.submit(lambda: migrador.listar_origem(pasta_id, migrador.drive_service())
                               if pasta_id != "shared" else [])
        f_destino = pool.submit(migrador.listar_destino)

    try:
        ctx["conta"] = migrador.credenciais_google().service_account_email
        ctx["pasta"], ctx["voltar"] = f_pasta.result()
        ctx["subpastas"] = f_sub.result()
        ctx["origem"] = f_origem.result()
    except Exception as e:  # noqa: BLE001
        ctx["erro"] = f"Erro ao acessar o Google Drive: {e}"
    try:
        ctx["destino"] = f_destino.result()
    except Exception as e:  # noqa: BLE001
        ctx["erro"] = ctx["erro"] or f"Erro ao listar Blob Storage: {e}"
    ctx["no_destino"] = {b["name"] for b in ctx["destino"]}
    for a in ctx["origem"]:
        a["tipo"] = tipo(a)
    ctx["upload_feature"] = migrador.upload_habilitado()
    ctx["upload_ok"] = migrador.upload_disponivel()
    return render_template("index.html", **ctx)


@app.route("/migrar", methods=["POST"])
def migrar():
    pasta_id = request.form["pasta"]
    ids = request.form.getlist("arquivos")
    print(f"[MIGRAR]  pasta {pasta_id}: {len(ids)} arquivo(s) selecionado(s)", flush=True)
    if not ids:
        session["resultados"] = [{"arquivo": "-", "status": "erro", "detalhe": "Nenhum arquivo selecionado."}]
        return redirect(url_for("index", pasta=pasta_id))
    try:
        resultados = migrador.migrar(pasta_id, ids=ids)
    except Exception as e:  # noqa: BLE001
        resultados = [{"arquivo": "-", "status": "erro", "detalhe": str(e)}]
    session["resultados"] = resultados
    return redirect(url_for("index", pasta=pasta_id))


# ---------------------------------------------------------------- API usada pela interface
@app.post("/api/transferir")
def api_transferir():
    """Transfere 1 arquivo. A interface chama em paralelo e mostra o status de cada um na fila."""
    file_id = (request.get_json(silent=True) or {}).get("id")
    if not file_id:
        return jsonify({"status": "erro", "detalhe": "ID do arquivo não informado."}), 400
    return jsonify(migrador.migrar_arquivo(file_id))


@app.get("/api/origem")
def api_origem():
    """Arquivos da pasta do Drive (o painel do Drive é recarregado após um upload)."""
    pasta_id = request.args.get("pasta", "")
    if not pasta_id or pasta_id == "shared":
        return jsonify([])
    try:
        arquivos = migrador.listar_origem(pasta_id, migrador.drive_service())
        for a in arquivos:
            a["tipo"] = tipo(a)
        return jsonify(arquivos)
    except Exception as e:  # noqa: BLE001
        return jsonify({"erro": str(e)}), 500


@app.post("/api/upload-drive")
def api_upload_drive():
    """Recebe um arquivo do computador do usuário e o envia para a pasta atual do Drive."""
    if not migrador.upload_habilitado():
        return jsonify({"status": "erro", "detalhe": "Upload para o Drive desabilitado."}), 404
    pasta_id = request.form.get("pasta", "")
    arquivo = request.files.get("arquivo")
    if not arquivo or not pasta_id or pasta_id == "shared":
        return jsonify({"status": "erro", "detalhe": "Abra uma pasta do Drive antes de enviar."}), 400
    return jsonify(migrador.enviar_para_drive(pasta_id, arquivo.filename, arquivo.stream, arquivo.mimetype))


@app.errorhandler(413)
def muito_grande(_):
    return jsonify({"status": "erro", "detalhe": "Arquivo maior que 100 MB."}), 413


@app.get("/api/destino")
def api_destino():
    """Lista atualizada do contêiner (o painel do Azure é recarregado após as transferências)."""
    try:
        return jsonify(migrador.listar_destino())
    except Exception as e:  # noqa: BLE001
        return jsonify({"erro": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=True)
