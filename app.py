"""
Baixador de Vídeos - app único (frontend + backend)
====================================================

Um único arquivo Python que:
  1. Serve a página web (HTML/CSS/JS embutidos, sem arquivos externos).
  2. Expõe uma API que usa o yt-dlp para extrair informações e baixar
     o vídeo de verdade do YouTube, TikTok, Instagram ou Kwai.
  3. Faz o proxy da miniatura (evita bloqueio de CORS/hotlink no navegador).

COMO RODAR (na sua máquina, não funciona dentro deste sandbox):
  1. Instale o Python 3.10+ e o ffmpeg (necessário para juntar áudio/vídeo
     e converter para MP3):
       - Windows: baixe o ffmpeg em https://ffmpeg.org/download.html e
         adicione a pasta "bin" na variável de ambiente PATH.
       - Ou, se tiver o Chocolatey: choco install ffmpeg
  2. Instale as dependências:
       pip install -r requirements.txt
  3. Rode o servidor:
       python app.py
  4. Abra no navegador:
       http://localhost:5000

AVISO IMPORTANTE:
  Baixar vídeos de plataformas de terceiros pode violar os Termos de
  Serviço delas e direitos autorais de quem publicou o conteúdo. Use
  apenas para vídeos que você tem direito de baixar (próprios ou com
  autorização) ou para uso pessoal. Você é responsável pelo uso desta
  ferramenta.
"""

import os
import re
import shutil
import tempfile
import unicodedata
from urllib.parse import urlparse, quote

from flask import Flask, request, jsonify, send_file, Response, abort
from flask_cors import CORS

import requests
import yt_dlp

app = Flask(__name__)

# Libera chamadas vindas do front-end estático hospedado em baixarvideos.site
# (necessário porque o front (Hostinger) e o back (Render/Railway/etc.) ficam
# em domínios diferentes).
CORS(app, resources={r"/api/*": {
    "origins": [
        "https://baixarvideos.site",
        "https://www.baixarvideos.site",
        "http://localhost:5000",
    ],
    # Sem isso, o navegador esconde o header Content-Disposition do
    # JavaScript em respostas cross-origin (front em baixarvideos.site,
    # back em onrender.com são origens diferentes) — o front acaba sem
    # saber o nome real do arquivo e usa um nome genérico ("video.mp4").
    "expose_headers": ["Content-Disposition"],
}})

# ----------------------------------------------------------------------
# Configuração de plataformas suportadas
# ----------------------------------------------------------------------

ALLOWED_DOMAINS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "m.youtube.com": "youtube",
    "tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "instagram.com": "instagram",
    "kwai.com": "kwai",
    "kwai.app": "kwai",
    "m.kwai.com": "kwai",
}


def detect_platform(url: str):
    """Identifica a plataforma a partir do domínio da URL."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return None
    host = host.split("@")[-1]  # remove eventual user:pass@
    for domain, name in ALLOWED_DOMAINS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def is_allowed(url: str) -> bool:
    return detect_platform(url) is not None


def format_selector(quality: str) -> str:
    """Monta a string de seleção de formato do yt-dlp de acordo com a
    qualidade escolhida no front-end.

    Histórico:
    - Versão original: terminava a cadeia de fallback em "/best" sem
      NENHUMA restrição de altura E com filtros extras de "ext=mp4"/
      "ext=m4a". Em plataformas onde os formatos não batem exatamente com
      esses ext (comum em TikTok/Instagram/Kwai), o yt-dlp caía nesse
      fallback cego e baixava a "melhor" qualidade disponível — ignorando
      silenciosamente a qualidade escolhida pelo usuário.
    - Correção de 2026-07-06 (parcial): removidos os filtros de "ext" e o
      fallback final sem teto de altura, para respeitar de fato a
      qualidade escolhida. Isso resolveu o problema acima, mas introduziu
      um bug novo e mais grave: quando o vídeo simplesmente NÃO tem nenhum
      formato disponível dentro do teto pedido (comum em duas situações
      reais, confirmadas em teste): (1) plataformas como o Kwai, que o
      yt-dlp extrai via extractor "generic" e não expõe metadado de altura
      nos formatos — QUALQUER filtro de altura falha, inclusive 1080p, que
      é a qualidade padrão selecionada na tela; (2) vídeos do TikTok/
      Instagram que só têm UMA renderização nativa (ex.: só 1080p) — pedir
      720p ou 480p falhava com "Requested format is not available" em vez
      de simplesmente entregar a única qualidade que existe.
    - Correção de hoje: mantém o teto de altura como preferência (então a
      qualidade escolhida continua sendo respeitada sempre que existir uma
      opção dentro do teto), mas adiciona "/best" como último recurso ao
      final da cadeia — só é usado quando NENHUMA alternativa anterior tem
      qualquer formato disponível, então não reintroduz o bug antigo (que
      era causado pelos filtros de ext, já removidos), apenas evita que o
      download falhe por completo quando o vídeo não tem opção dentro do
      teto pedido.
    """
    if quality == "mp3":
        return "bestaudio/best"
    if quality == "max":
        # Qualidade máxima: sem limite de altura, baixa a maior resolução
        # que o vídeo original tiver disponível (pode passar de 1080p em
        # uploads feitos em 4K/8K).
        return "bestvideo+bestaudio/best"
    height_map = {"1080p": 1080, "720p": 720, "480p": 480}
    h = height_map.get(quality, 1080)
    return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"


def short_error(exc: Exception, limit: int = 220) -> str:
    """Encurta a mensagem de exceção do yt-dlp antes de mandar para o
    front-end. Sem isso, alguns erros (principalmente do YouTube) vêm com
    parágrafos inteiros de texto (dicas, links, avisos), o que aparecia
    como uma parede de texto ilegível no alert() do navegador."""
    msg = str(exc).strip()
    msg = re.sub(r"\s+", " ", msg)
    return msg[:limit] + ("…" if len(msg) > limit else "")


def safe_filename(name: str, fallback: str = "video") -> str:
    name = name or fallback
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    name = re.sub(r"[^\w\s.-]", "", name).strip()
    name = re.sub(r"\s+", "_", name)
    return name[:80] or fallback


# Caminho de um arquivo de cookies (formato Netscape) exportado de uma conta
# real do YouTube logada. NUNCA fica no repositório (que é público) — é
# configurado como "Secret File" direto no painel do Render, fora do Git.
# Sem esse arquivo, o YouTube passa a exigir "confirme que você não é um
# robô" para a maioria dos vídeos comuns (não afeta vídeos virais/oficiais
# muito cacheados, nem TikTok/Instagram/Kwai).
_RAW_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE", "/etc/secrets/youtube_cookies.txt")
# Secret Files do Render são somente leitura, mas o yt-dlp tenta reescrever
# o arquivo de cookies depois de usá-lo (para salvar tokens renovados, tipo
# SIDCC). Por isso copiamos para um local gravável (/tmp) uma vez, quando o
# processo sobe, e usamos essa cópia no dia a dia.
_WRITABLE_COOKIES_FILE = "/tmp/youtube_cookies_writable.txt"


def _prepare_writable_cookies_file() -> None:
    try:
        if os.path.isfile(_RAW_COOKIES_FILE) and not os.path.isfile(_WRITABLE_COOKIES_FILE):
            shutil.copyfile(_RAW_COOKIES_FILE, _WRITABLE_COOKIES_FILE)
    except OSError:
        pass


_prepare_writable_cookies_file()


# O YouTube passou a exigir a resolução de um "desafio JavaScript" (n
# challenge, ligado a PO Token/SABR streaming) para liberar os formatos de
# vídeo de verdade — sem isso, o yt-dlp só consegue extrair imagens
# (storyboard/thumbnail), mesmo com cookies válidos. A própria equipe do
# yt-dlp recomenda instalar um runtime de JavaScript (Deno) ao lado dele.
# No Render, o Build Command instala o Deno dentro do próprio diretório do
# projeto (pasta ".deno", ver README/CLAUDE.md), então localizamos o
# binário de forma relativa a este arquivo — funciona tanto no Render
# quanto localmente (se o dev tiver instalado o Deno do mesmo jeito).
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
_DENO_PATH = os.path.join(_PROJECT_DIR, ".deno", "bin", "deno")
if not os.path.isfile(_DENO_PATH):
    # Fallback: Deno instalado no PATH padrão do sistema (ex.: ambiente
    # local do desenvolvedor, ou outra forma de instalação no host).
    _DENO_PATH = shutil.which("deno") or ""


def base_ydl_opts() -> dict:
    """Opções compartilhadas com o yt-dlp para driblar os bloqueios que o
    YouTube aplica a servidores/datacenter (comum em hosts como o Render):

    1) "Sign in to confirm you're not a bot" — resolvido usando cookies de
       uma conta real (se o arquivo existir). É a única forma hoje
       reconhecida pela própria equipe do yt-dlp de driblar esse bloqueio
       de forma consistente para vídeos comuns.
    2) "Requested format is not available" (mesmo com cookies) — causado
       pela falta de um runtime de JavaScript para resolver o desafio
       "n challenge" do YouTube. Resolvido apontando o yt-dlp para o
       binário do Deno (se instalado).

    Importante: quando há cookies, NÃO forçamos os clients 'android'/'ios'
    junto com 'web'. Testamos e essa combinação quebrava a seleção de
    formato ("Requested format is not available") — os clients android/ios
    não usam os cookies da mesma forma que o 'web' e misturar as listas de
    formato dos três causava conflito. Só forçamos android/ios como
    fallback SEM cookies (não afeta TikTok/Instagram/Kwai, que ignoram essa
    opção).
    """
    has_cookies = os.path.isfile(_WRITABLE_COOKIES_FILE)
    opts = {}
    if has_cookies:
        opts["cookiefile"] = _WRITABLE_COOKIES_FILE
    else:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "ios", "web"],
            }
        }
    if _DENO_PATH:
        opts["js_runtimes"] = {"deno": {"path": _DENO_PATH}}
    return opts


def build_download_title(info: dict, platform: str, url: str = "") -> str:
    """Monta o nome do arquivo que o usuário vai ver ao salvar.

    - YouTube: usa o título do vídeo (já é descritivo).
    - TikTok / Instagram / Kwai: o "título" que o yt-dlp extrai costuma ser
      vago, cortado ou até ausente. Para facilitar localizar o vídeo depois,
      usa "@usuario - legenda".
    """
    if platform == "youtube":
        return info.get("title") or "video"

    # O @usuario direto na URL é mais confiável do que os campos do yt-dlp:
    # em TikTok, por exemplo, "uploader_id" costuma vir como o ID numérico
    # interno da conta, não o @handle visível (que é o que o usuário quer).
    # TikTok e Kwai usam o formato .../@usuario/... na URL.
    handle_match = re.search(r"/@([^/?#]+)", url)
    uploader = handle_match.group(1) if handle_match else ""

    if not uploader:
        uploader = (
            info.get("uploader")
            or info.get("channel")
            or info.get("uploader_id")
            or info.get("channel_id")
            or ""
        )
    uploader = uploader.strip()
    if uploader and not uploader.startswith("@"):
        uploader = "@" + uploader.lstrip("@")

    caption = (info.get("title") or info.get("description") or "").strip()
    caption = caption.splitlines()[0] if caption else ""  # só a 1ª linha

    parts = [p for p in (uploader, caption) if p]
    return " - ".join(parts) if parts else (info.get("title") or "video")


# ----------------------------------------------------------------------
# Rotas da API
# ----------------------------------------------------------------------

@app.route("/api/analyze", methods=["POST"])
def analyze():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify(error="Cole um link antes de continuar."), 400
    if not is_allowed(url):
        return jsonify(
            error="Link não suportado. Use um link do YouTube, TikTok, Instagram ou Kwai."
        ), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "socket_timeout": 15,
        **base_ydl_opts(),
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        return jsonify(
            error="Não foi possível processar este link. Verifique se o vídeo é público. "
                  f"Detalhe técnico: {short_error(exc)}"
        ), 502

    return jsonify(
        title=info.get("title") or "Vídeo",
        thumbnail=info.get("thumbnail"),
        duration=info.get("duration"),
        platform=detect_platform(url),
    )


@app.route("/api/thumbnail")
def thumbnail_proxy():
    """Faz o proxy da miniatura para evitar bloqueio de CORS/hotlink
    no navegador e permitir o download direto da imagem."""
    img_url = request.args.get("url", "")
    if not img_url:
        abort(400)
    try:
        r = requests.get(img_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception:
        abort(502)

    content_type = r.headers.get("Content-Type", "image/jpeg")
    resp = Response(r.content, mimetype=content_type)
    if request.args.get("download"):
        resp.headers["Content-Disposition"] = 'attachment; filename="miniatura.jpg"'
    return resp


@app.route("/api/download")
def download():
    url = request.args.get("url", "").strip()
    quality = request.args.get("quality", "1080p")

    if not url or not is_allowed(url):
        return jsonify(error="Link inválido ou plataforma não suportada."), 400

    tmpdir = tempfile.mkdtemp(prefix="baixador_")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "format": format_selector(quality),
        "socket_timeout": 30,
        # Resiliência contra falhas de rede passageiras (comum em vídeos
        # maiores/"qualidade máxima", onde a conexão fica aberta por mais
        # tempo) — sem isso, um único soquete que falhar no meio do
        # download derruba o processo inteiro sem tentar de novo.
        "retries": 3,
        "fragment_retries": 3,
        **base_ydl_opts(),
    }

    if quality == "mp3":
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            if quality == "mp3":
                filename = os.path.splitext(filename)[0] + ".mp3"
    except Exception as exc:
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify(
            error=f"Falha ao baixar o vídeo. Detalhe técnico: {short_error(exc)}"
        ), 502

    if not os.path.exists(filename):
        shutil.rmtree(tmpdir, ignore_errors=True)
        return jsonify(error="O arquivo baixado não foi encontrado."), 500

    platform = detect_platform(url)
    download_name = safe_filename(build_download_title(info, platform, url))
    ext = os.path.splitext(filename)[1]

    response = send_file(
        filename,
        as_attachment=True,
        download_name=f"{download_name}{ext}",
    )

    @response.call_on_close
    def _cleanup():
        shutil.rmtree(tmpdir, ignore_errors=True)

    return response


# ----------------------------------------------------------------------
# Frontend (HTML + CSS + JS embutidos)
# ----------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Baixar Vídeos do YouTube, TikTok, Instagram e Kwai Grátis | BAIXAR VIDEOS</title>
<meta name="description" content="Baixe vídeos do YouTube, TikTok, Instagram e Kwai gratuitamente, sem cadastro e sem instalar programas. Rápido, simples e seguro.">
<meta name="keywords" content="baixar video, baixar video youtube, baixar video tiktok, baixar video instagram, baixar video kwai, download de video gratis">
<link rel="canonical" href="https://baixarvideos.site/">
<style>
  :root{
    --bg:#0f1220; --bg-soft:#161a2c; --card:#1c2136; --border:#2a2f47;
    --text:#eef0f7; --muted:#9aa1b8; --accent:#7c5cff; --accent2:#22d3aa;
    --danger:#ff5c7a; --radius:14px;
  }
  *{box-sizing:border-box;}
  body{margin:0;font-family:'Segoe UI',system-ui,-apple-system,Roboto,Helvetica,Arial,sans-serif;
    background:linear-gradient(180deg,var(--bg) 0%, #0b0d17 100%);color:var(--text);line-height:1.5;}
  a{color:inherit;}
  header{display:flex;align-items:center;justify-content:space-between;padding:20px 24px;max-width:1080px;margin:0 auto;}
  .logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:20px;}
  .logo .mark{width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,var(--accent),var(--accent2));
    display:flex;align-items:center;justify-content:center;font-size:18px;}
  .logo .placeholder-tag{font-size:11px;color:var(--muted);border:1px dashed var(--border);
    padding:2px 8px;border-radius:20px;margin-left:6px;font-weight:400;}
  nav a{color:var(--muted);text-decoration:none;margin-left:28px;font-size:14px;}
  nav a:hover{color:var(--text);}
  .hero{text-align:center;padding:48px 24px 16px;}
  .hero h1{font-size:38px;margin:0 0 12px;font-weight:800;}
  .hero h1 span{background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;background-clip:text;color:transparent;}
  .hero p{color:var(--muted);font-size:17px;max-width:560px;margin:0 auto 32px;}
  .platforms{display:flex;justify-content:center;gap:10px;flex-wrap:wrap;margin-bottom:20px;}
  .platform-btn{display:flex;align-items:center;gap:8px;background:var(--card);border:1px solid var(--border);
    color:var(--muted);padding:9px 16px;border-radius:30px;font-size:14px;cursor:pointer;transition:.15s;}
  .platform-btn:hover{color:var(--text);border-color:var(--accent);}
  .platform-btn.active{color:#fff;border-color:transparent;background:linear-gradient(135deg,var(--accent),#5b3df0);}
  .search-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
    padding:10px;max-width:680px;margin:0 auto;display:flex;gap:8px;flex-wrap:wrap;}
  .search-card input{flex:1;min-width:200px;background:transparent;border:none;outline:none;
    color:var(--text);font-size:15px;padding:12px 14px;}
  .search-card input::placeholder{color:var(--muted);}
  .btn{border:none;border-radius:10px;padding:12px 18px;font-size:14px;font-weight:600;cursor:pointer;transition:.15s;}
  .btn-ghost{background:var(--bg-soft);color:var(--text);border:1px solid var(--border);}
  .btn-ghost:hover{border-color:var(--accent);}
  .btn-primary{background:linear-gradient(135deg,var(--accent),#5b3df0);color:#fff;}
  .btn-primary:hover{filter:brightness(1.08);}
  .btn-primary:disabled{opacity:.6;cursor:not-allowed;}
  .disclaimer{text-align:center;color:var(--muted);font-size:12px;margin-top:14px;}
  .disclaimer a{color:var(--accent2);text-decoration:none;}
  .status{max-width:680px;margin:20px auto 0;text-align:center;color:var(--muted);font-size:14px;display:none;}
  .spinner{width:18px;height:18px;border:2px solid var(--border);border-top-color:var(--accent);
    border-radius:50%;display:inline-block;vertical-align:middle;margin-right:8px;animation:spin .8s linear infinite;}
  @keyframes spin{to{transform:rotate(360deg);}}
  .ad-slot{display:none;max-width:680px;margin:24px auto 0;border:2px dashed #ff9f43;border-radius:var(--radius);
    background:rgba(255,159,67,.08);padding:28px 16px;text-align:center;color:#ffb870;font-size:13px;letter-spacing:.3px;}
  .ad-slot strong{display:block;font-size:14px;color:#ffcf9c;margin-bottom:4px;}
  .ad-slot small{color:#c98a4f;}
  .result-card{display:none;max-width:680px;margin:24px auto 0;background:var(--card);
    border:1px solid var(--border);border-radius:var(--radius);padding:20px;}
  .result-top{display:flex;gap:16px;align-items:center;}
  .thumb{width:160px;height:96px;border-radius:8px;flex-shrink:0;background:linear-gradient(135deg,#2a2f47,#1c2136);
    display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:22px;overflow:hidden;position:relative;}
  .thumb img{width:100%;height:100%;object-fit:cover;display:block;}
  .thumb-note{font-size:11px;color:var(--muted);margin-top:8px;}
  .thumb-download-row{margin-top:14px;display:flex;align-items:center;gap:10px;}
  .btn-small{background:var(--bg-soft);border:1px solid var(--border);color:var(--text);
    padding:8px 14px;border-radius:8px;font-size:13px;cursor:pointer;}
  .btn-small:hover{border-color:var(--accent2);color:var(--accent2);}
  .btn-small:disabled{opacity:.5;cursor:not-allowed;}
  .result-info{flex:1;min-width:0;}
  .result-info .title{font-weight:600;font-size:15px;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
  .result-info .meta{color:var(--muted);font-size:13px;}
  .qualities{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px;}
  .quality-btn{background:var(--bg-soft);border:1px solid var(--border);color:var(--text);
    padding:8px 14px;border-radius:8px;font-size:13px;cursor:pointer;}
  .quality-btn.active{border-color:var(--accent2);color:var(--accent2);}
  .download-row{margin-top:18px;display:flex;gap:10px;}
  .download-row .btn-primary{flex:1;}
  .section{padding:56px 24px;}
  .section h2{font-size:26px;text-align:center;margin-bottom:36px;}
  .steps{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;max-width:960px;margin:0 auto;}
  .step{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:22px;}
  .step .num{width:30px;height:30px;border-radius:50%;background:rgba(124,92,255,.15);
    color:var(--accent);display:flex;align-items:center;justify-content:center;font-weight:700;margin-bottom:14px;font-size:14px;}
  .step h3{font-size:16px;margin:0 0 8px;}
  .step p{color:var(--muted);font-size:14px;margin:0;}
  .features{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;max-width:1000px;margin:0 auto;}
  .feature{text-align:center;padding:20px 10px;}
  .feature .icon{font-size:26px;margin-bottom:10px;}
  .feature h4{font-size:14px;margin:0 0 6px;}
  .feature p{font-size:13px;color:var(--muted);margin:0;}
  .faq{max-width:720px;margin:0 auto;}
  .faq-item{border-bottom:1px solid var(--border);padding:16px 0;cursor:pointer;}
  .faq-item .q{display:flex;justify-content:space-between;align-items:center;font-weight:600;font-size:15px;}
  .faq-item .a{color:var(--muted);font-size:14px;margin-top:10px;display:none;}
  .faq-item.open .a{display:block;}
  .faq-item .q .chevron{transition:.2s;color:var(--muted);}
  .faq-item.open .q .chevron{transform:rotate(180deg);}
  footer{border-top:1px solid var(--border);padding:28px 24px;text-align:center;color:var(--muted);font-size:13px;}
  footer .links{margin-bottom:10px;}
  footer .links a{margin:0 10px;text-decoration:none;color:var(--muted);}
  footer .links a:hover{color:var(--text);}
  @media (max-width:720px){.steps{grid-template-columns:1fr;}.features{grid-template-columns:1fr 1fr;}.hero h1{font-size:28px;}}
</style>
</head>
<body>

<header>
  <div class="logo">
    <div class="mark">&#9654;</div>
    BAIXAR VIDEOS
  </div>
  <nav>
    <a href="#como-funciona">Como funciona</a>
    <a href="#faq">FAQ</a>
  </nav>
</header>

<section class="hero">
  <h1>Baixe vídeos do <span>YouTube, TikTok, Instagram e Kwai</span></h1>
  <p>Cole o link do vídeo, escolha o formato e baixe gratuitamente. Sem instalar nada, sem cadastro.</p>

  <div class="platforms">
    <button class="platform-btn active" data-platform="youtube">&#9654; YouTube</button>
    <button class="platform-btn" data-platform="tiktok">&#127925; TikTok</button>
    <button class="platform-btn" data-platform="instagram">&#128247; Instagram</button>
    <button class="platform-btn" data-platform="kwai">&#9889; Kwai</button>
  </div>

  <div class="search-card">
    <input type="text" id="urlInput" placeholder="Cole aqui o link do vídeo...">
    <button class="btn btn-ghost" id="pasteBtn">Colar</button>
    <button class="btn btn-primary" id="downloadBtn">Baixar</button>
  </div>
  <p class="disclaimer">Ao utilizar este serviço você concorda com nossos <a href="#">Termos de Uso</a> e <a href="#">Política de Privacidade</a>.</p>

  <div class="status" id="statusBox"><span class="spinner"></span>Processando o link, aguarde...</div>

  <!-- ================= ESPAÇO RESERVADO PARA ANÚNCIO ================= -->
  <div class="ad-slot" id="adSlot">
    <strong>[ ESPAÇO RESERVADO PARA ANÚNCIO ]</strong>
    Bloco de anúncio exibido antes da liberação do download (ex: 300x250, 728x90 ou responsivo)
    <br><small>&lt;!-- inserir script/tag do anúncio aqui --&gt;</small>
  </div>
  <!-- ================================================================== -->

  <div class="result-card" id="resultCard">
    <div class="result-top">
      <div class="thumb" id="thumbBox">&#127916;</div>
      <div class="result-info">
        <div class="title" id="resultTitle">Pré-visualização do vídeo</div>
        <div class="meta" id="resultMeta">Plataforma detectada</div>
        <div class="thumb-download-row">
          <button class="btn-small" id="thumbDownloadBtn" disabled>Baixar miniatura (máx. qualidade)</button>
        </div>
        <div class="thumb-note" id="thumbNote"></div>
      </div>
    </div>
    <div class="qualities">
      <button class="quality-btn active" data-q="1080p">MP4 1080p</button>
      <button class="quality-btn" data-q="720p">MP4 720p</button>
      <button class="quality-btn" data-q="480p">MP4 480p</button>
      <button class="quality-btn" data-q="mp3">MP3 áudio</button>
    </div>
    <div class="download-row">
      <button class="btn btn-primary" id="finalDownloadBtn">Baixar arquivo</button>
    </div>
  </div>
</section>

<section class="section" id="como-funciona">
  <h2>Como funciona</h2>
  <div class="steps">
    <div class="step"><div class="num">1</div><h3>Copie o link</h3>
      <p>Abra o vídeo no YouTube, TikTok, Instagram ou Kwai e copie o link de compartilhamento.</p></div>
    <div class="step"><div class="num">2</div><h3>Cole no campo acima</h3>
      <p>Cole o link na caixa de busca e clique em "Baixar" para processar o vídeo.</p></div>
    <div class="step"><div class="num">3</div><h3>Escolha e baixe</h3>
      <p>Selecione o formato e a qualidade desejada e salve o vídeo no seu dispositivo.</p></div>
  </div>
</section>

<section class="section">
  <h2>Por que usar este site</h2>
  <div class="features">
    <div class="feature"><div class="icon">&#128181;</div><h4>100% Gratuito</h4><p>Sem mensalidade e sem cadastro.</p></div>
    <div class="feature"><div class="icon">&#9889;</div><h4>Rápido</h4><p>Processamento em poucos segundos.</p></div>
    <div class="feature"><div class="icon">&#128274;</div><h4>Sem login</h4><p>Não pedimos senha nem dados pessoais.</p></div>
    <div class="feature"><div class="icon">&#128241;</div><h4>Qualquer dispositivo</h4><p>Funciona em celular, tablet e computador.</p></div>
  </div>
</section>

<section class="section" id="faq">
  <h2>Perguntas frequentes</h2>
  <div class="faq">
    <div class="faq-item"><div class="q">É realmente gratuito? <span class="chevron">&#9662;</span></div>
      <div class="a">Sim, o download é 100% gratuito. Antes de iniciar o download, exibimos um anúncio para manter o serviço sem custos para você.</div></div>
    <div class="faq-item"><div class="q">Preciso instalar algum programa? <span class="chevron">&#9662;</span></div>
      <div class="a">Não. Todo o processo acontece direto no navegador, sem instalação de aplicativos ou extensões.</div></div>
    <div class="faq-item"><div class="q">Quais plataformas são suportadas? <span class="chevron">&#9662;</span></div>
      <div class="a">YouTube, TikTok, Instagram e Kwai, com suporte a vídeo (MP4) e áudio (MP3).</div></div>
    <div class="faq-item"><div class="q">Meus dados ficam salvos? <span class="chevron">&#9662;</span></div>
      <div class="a">Não armazenamos os links processados nem exigimos login ou informações pessoais.</div></div>
  </div>
</section>

<footer>
  <div class="links">
    <a href="#">Sobre</a><a href="#">Contato</a><a href="#">Termos de Uso</a><a href="#">Política de Privacidade</a>
  </div>
  &copy; 2026 BAIXAR VIDEOS — todos os direitos reservados.
</footer>

<script>
  const platformBtns = document.querySelectorAll('.platform-btn');
  platformBtns.forEach(btn=>{
    btn.addEventListener('click', ()=>{
      platformBtns.forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
    });
  });

  function detectPlatform(url){
    url = (url||'').toLowerCase();
    if(url.includes('youtube.com') || url.includes('youtu.be')) return 'youtube';
    if(url.includes('tiktok.com')) return 'tiktok';
    if(url.includes('instagram.com')) return 'instagram';
    if(url.includes('kwai.com') || url.includes('kwai.app')) return 'kwai';
    return null;
  }

  function formatDuration(sec){
    if(!sec && sec !== 0) return '';
    sec = Math.round(sec);
    const m = Math.floor(sec/60), s = sec%60;
    return m + ':' + String(s).padStart(2,'0');
  }

  const urlInput = document.getElementById('urlInput');
  const pasteBtn = document.getElementById('pasteBtn');
  const downloadBtn = document.getElementById('downloadBtn');
  const statusBox = document.getElementById('statusBox');
  const adSlot = document.getElementById('adSlot');
  const resultCard = document.getElementById('resultCard');
  const resultTitle = document.getElementById('resultTitle');
  const resultMeta = document.getElementById('resultMeta');
  const thumbBox = document.getElementById('thumbBox');
  const thumbDownloadBtn = document.getElementById('thumbDownloadBtn');
  const thumbNote = document.getElementById('thumbNote');
  const finalDownloadBtn = document.getElementById('finalDownloadBtn');

  let currentUrl = null;
  let currentThumbProxyUrl = null;
  let selectedQuality = '1080p';

  pasteBtn.addEventListener('click', async ()=>{
    try{
      const text = await navigator.clipboard.readText();
      urlInput.value = text;
      urlInput.focus();
    }catch(e){ urlInput.focus(); }
  });

  urlInput.addEventListener('input', ()=>{
    urlInput.style.borderColor = '';
    const p = detectPlatform(urlInput.value);
    if(p){
      platformBtns.forEach(b=>b.classList.toggle('active', b.dataset.platform === p));
    }
  });

  downloadBtn.addEventListener('click', async ()=>{
    const link = urlInput.value.trim();
    const platform = detectPlatform(link);
    if(!link || !platform){
      urlInput.style.borderColor = 'var(--danger)';
      urlInput.focus();
      return;
    }

    adSlot.style.display = 'none';
    resultCard.style.display = 'none';
    statusBox.style.display = 'block';
    downloadBtn.disabled = true;

    try{
      const resp = await fetch('/api/analyze', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url: link})
      });
      const data = await resp.json();
      if(!resp.ok) throw new Error(data.error || 'Não foi possível processar o link.');

      currentUrl = link;
      resultTitle.textContent = data.title || 'Vídeo pronto para download';
      let meta = 'Plataforma: ' + (data.platform || platform);
      if(data.duration) meta += ' • ' + formatDuration(data.duration);
      resultMeta.textContent = meta;

      if(data.thumbnail){
        const proxied = '/api/thumbnail?url=' + encodeURIComponent(data.thumbnail);
        thumbBox.innerHTML = '<img src="' + proxied + '" alt="Miniatura do vídeo">';
        currentThumbProxyUrl = proxied + '&download=1';
        thumbDownloadBtn.disabled = false;
        thumbNote.textContent = '';
      }else{
        thumbBox.innerHTML = '&#127916;';
        currentThumbProxyUrl = null;
        thumbDownloadBtn.disabled = true;
        thumbNote.textContent = 'Miniatura não disponível para este vídeo.';
      }

      statusBox.style.display = 'none';
      adSlot.style.display = 'block';
      resultCard.style.display = 'block';
      resultCard.scrollIntoView({behavior:'smooth', block:'center'});
    }catch(e){
      statusBox.style.display = 'none';
      alert(e.message);
    }finally{
      downloadBtn.disabled = false;
    }
  });

  thumbDownloadBtn.addEventListener('click', async ()=>{
    if(!currentThumbProxyUrl) return;
    try{
      const resp = await fetch(currentThumbProxyUrl);
      const blob = await resp.blob();
      const blobUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = blobUrl; a.download = 'miniatura.jpg';
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(blobUrl);
    }catch(e){
      window.open(currentThumbProxyUrl, '_blank');
    }
  });

  document.querySelectorAll('.quality-btn').forEach(btn=>{
    btn.addEventListener('click', ()=>{
      document.querySelectorAll('.quality-btn').forEach(b=>b.classList.remove('active'));
      btn.classList.add('active');
      selectedQuality = btn.dataset.q;
    });
  });

  finalDownloadBtn.addEventListener('click', ()=>{
    if(!currentUrl) return;
    // Navegação direta (sem fetch+blob): o navegador lida nativamente com
    // o header Content-Disposition (nome do arquivo correto) e o download
    // fica muito mais confiável em celular (iOS/Android), já que não
    // precisa carregar o vídeo inteiro na memória antes de salvar.
    const dlUrl = '/api/download?url=' + encodeURIComponent(currentUrl) + '&quality=' + encodeURIComponent(selectedQuality);
    const originalText = finalDownloadBtn.textContent;
    finalDownloadBtn.disabled = true;
    finalDownloadBtn.textContent = 'Preparando download...';
    const a = document.createElement('a');
    a.href = dlUrl;
    a.rel = 'noopener';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(()=>{
      finalDownloadBtn.disabled = false;
      finalDownloadBtn.textContent = originalText;
    }, 4000);
  });

  document.querySelectorAll('.faq-item').forEach(item=>{
    item.addEventListener('click', ()=> item.classList.toggle('open'));
  });
</script>

</body>
</html>
"""


@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
