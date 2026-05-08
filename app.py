import os
import random
import string
import requests
from datetime import datetime, timezone, timedelta
from flask import Flask, render_template, request, jsonify, session
from dotenv import load_dotenv
from supabase import create_client, Client

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-secret-key")

TABLE_VOTOS   = "marcos_filhopastor_votos"
TABLE_CODIGOS = "marcos_filhopastor_codigos"

# ─── Supabase ────────────────────────────────────────────────

def get_supabase() -> Client:
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL e SUPABASE_SERVICE_KEY precisam estar no .env")
    return create_client(url, key)

# ─── WhatsApp ────────────────────────────────────────────────

def send_whatsapp_code(phone_number: str, code: str):
    token         = os.getenv("WHATSAPP_TOKEN")
    phone_id      = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
    template_name = os.getenv("WHATSAPP_TEMPLATE_NAME", "votacao")
    template_lang = os.getenv("WHATSAPP_TEMPLATE_LANGUAGE", "pt_BR")

    url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": phone_number,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": template_lang},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                {"type": "button", "sub_type": "url", "index": "0",
                 "parameters": [{"type": "text", "text": code}]},
            ],
        },
    }

    print(f"[WhatsApp] Enviando para: {phone_number} | Template: {template_name}/{template_lang}")
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        print(f"[WhatsApp] Status: {resp.status_code} | {resp.text}")
        if resp.status_code == 200:
            return True, None
        err_msg = resp.json().get("error", {}).get("message", resp.text)
        return False, err_msg
    except Exception as e:
        print(f"[WhatsApp] Exceção: {e}")
        return False, str(e)


def generate_code(length=6):
    return "".join(random.choices(string.digits, k=length))

# ─── Rotas ───────────────────────────────────────────────────

@app.route("/")
def index():
    aberta = os.getenv("VOTACAO_ABERTA", "true").lower() == "true"
    titulo = os.getenv("VOTACAO_TITULO", "Votação do Culto")
    opcoes = [o.strip() for o in os.getenv("VOTACAO_OPCOES", "Opção A,Opção B").split(",") if o.strip()]
    return render_template("index.html", titulo=titulo, opcoes=opcoes, votacao_aberta=aberta)


@app.route("/api/enviar-codigo", methods=["POST"])
def enviar_codigo():
    if os.getenv("VOTACAO_ABERTA", "true").lower() != "true":
        return jsonify({"ok": False, "erro": "A votação está encerrada."}), 403

    data     = request.get_json()
    nome     = (data.get("nome") or "").strip()
    ddi      = (data.get("ddi") or "").strip()
    telefone = (data.get("telefone") or "").strip().replace(" ", "").replace("-", "")

    if not nome or not ddi or not telefone:
        return jsonify({"ok": False, "erro": "Preencha todos os campos."}), 400

    numero = ddi.lstrip("+") + telefone.lstrip("0")
    sb = get_supabase()

    # Verifica voto duplicado
    existe = sb.table(TABLE_VOTOS).select("id").eq("numero", numero).execute()
    if existe.data:
        return jsonify({"ok": False, "erro": "Este número já participou da votação."}), 409

    # Remove código anterior (se houver) e insere novo
    code = generate_code()
    sb.table(TABLE_CODIGOS).delete().eq("numero", numero).execute()
    sb.table(TABLE_CODIGOS).insert({
        "numero":     numero,
        "nome":       nome,
        "codigo":     code,
        "gerado_em":  datetime.now(timezone.utc).isoformat(),
        "tentativas": 0,
    }).execute()

    # Envia via WhatsApp
    enviado, erro_api = send_whatsapp_code("+" + numero, code)
    if not enviado:
        detalhe = f" Detalhe: {erro_api}" if erro_api else ""
        return jsonify({"ok": False, "erro": f"Não foi possível enviar o código.{detalhe}"}), 500

    session["numero"] = numero
    session["nome"]   = nome
    return jsonify({"ok": True})


@app.route("/api/verificar-codigo", methods=["POST"])
def verificar_codigo():
    if os.getenv("VOTACAO_ABERTA", "true").lower() != "true":
        return jsonify({"ok": False, "erro": "A votação está encerrada."}), 403

    data             = request.get_json()
    codigo_informado = (data.get("codigo") or "").strip()
    opcao_escolhida  = (data.get("opcao") or "").strip()
    numero           = session.get("numero")

    if not numero:
        return jsonify({"ok": False, "erro": "Sessão expirada. Recarregue a página."}), 401
    if not opcao_escolhida:
        return jsonify({"ok": False, "erro": "Selecione uma opção antes de confirmar."}), 400

    opcoes_validas = [o.strip() for o in os.getenv("VOTACAO_OPCOES", "").split(",") if o.strip()]
    if opcao_escolhida not in opcoes_validas:
        return jsonify({"ok": False, "erro": "Opção inválida."}), 400

    sb = get_supabase()

    res = sb.table(TABLE_CODIGOS).select("*").eq("numero", numero).execute()
    if not res.data:
        return jsonify({"ok": False, "erro": "Código não encontrado. Solicite um novo código."}), 404

    entrada = res.data[0]

    # Expiração: 10 minutos
    gerado_em = datetime.fromisoformat(entrada["gerado_em"].replace("Z", "+00:00"))
    if gerado_em.tzinfo is None:
        gerado_em = gerado_em.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - gerado_em > timedelta(minutes=10):
        return jsonify({"ok": False, "erro": "Código expirado. Solicite um novo código."}), 410

    if entrada["tentativas"] >= 5:
        return jsonify({"ok": False, "erro": "Muitas tentativas. Solicite um novo código."}), 429

    if entrada["codigo"] != codigo_informado:
        sb.table(TABLE_CODIGOS).update({"tentativas": entrada["tentativas"] + 1}).eq("numero", numero).execute()
        restantes = 4 - entrada["tentativas"]
        return jsonify({"ok": False, "erro": f"Código incorreto. {restantes} tentativa(s) restante(s)."}), 400

    # Dupla checagem de voto
    existe = sb.table(TABLE_VOTOS).select("id").eq("numero", numero).execute()
    if existe.data:
        return jsonify({"ok": False, "erro": "Este número já participou da votação."}), 409

    # Regista voto e apaga código usado
    sb.table(TABLE_VOTOS).insert({
        "numero": numero,
        "nome":   entrada["nome"],
        "opcao":  opcao_escolhida,
    }).execute()
    sb.table(TABLE_CODIGOS).delete().eq("numero", numero).execute()

    session.clear()
    return jsonify({"ok": True, "opcao": opcao_escolhida})


@app.route("/admin/resultados")
def resultados():
    opcoes = [o.strip() for o in os.getenv("VOTACAO_OPCOES", "").split(",") if o.strip()]
    titulo = os.getenv("VOTACAO_TITULO", "Votação do Culto")

    sb = get_supabase()
    res = sb.table(TABLE_VOTOS).select("nome, opcao, votado_em").order("votado_em", desc=True).execute()
    votos = res.data or []

    contagem = {op: 0 for op in opcoes}
    for v in votos:
        if v["opcao"] in contagem:
            contagem[v["opcao"]] += 1

    total = sum(contagem.values())
    return render_template("resultados.html", titulo=titulo, contagem=contagem, total=total, votos=votos)


if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
