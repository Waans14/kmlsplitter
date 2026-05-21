import os
import io
import re
import copy
import uuid
import logging
import threading
import zipfile
import xml.etree.ElementTree as ET
from flask import Flask, request, render_template_string, send_file, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import redis
from dotenv import load_dotenv

load_dotenv()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── App Config ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["SECRET_KEY"]          = os.getenv("SECRET_KEY", "dev-secret-change-me")
app.config["MAX_CONTENT_LENGTH"]  = int(os.getenv("MAX_CONTENT_LENGTH", 52_428_800))  # 50 MB

# ── Redis ─────────────────────────────────────────────────────────────────────
REDIS_URL   = os.getenv("REDIS_URL", "redis://localhost:6379/0")
JOB_TTL     = 300          # 5 minutes
rdb         = redis.from_url(REDIS_URL, decode_responses=False)

# ── Rate Limiter ──────────────────────────────────────────────────────────────
UPLOAD_RATE_LIMIT = os.getenv("UPLOAD_RATE_LIMIT", "20 per minute")
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri=REDIS_URL,
    default_limits=[],
    swallow_errors=True,   # Jangan crash jika Redis tidak tersedia
)

# ── KML Processing ────────────────────────────────────────────────────────────
KML_NS = "http://www.opengis.net/kml/2.2"
ET.register_namespace("", KML_NS)


def sanitize(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    return cleaned if cleaned else "Tanpa_Nama"


def build_kml_bytes(pm) -> bytes:
    root = ET.Element(f"{{{KML_NS}}}kml")
    doc  = ET.SubElement(root, f"{{{KML_NS}}}Document")
    doc.append(copy.deepcopy(pm))
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def process_kml(job_id: str, kml_bytes: bytes):
    """Run in background thread; store result or error in Redis."""
    try:
        rdb.hset(f"job:{job_id}", mapping={"status": "processing"})
        root     = ET.fromstring(kml_bytes)
        buf      = io.BytesIO()
        counter  = {"n": 0}
        names    = {}

        def unique_path(folder, base):
            key   = f"{folder}/{base}".strip("/").lower()
            names[key] = names.get(key, 0) + 1
            suffix = f"_{names[key]}" if names[key] > 1 else ""
            return f"{folder}/{base}{suffix}.kml".strip("/")

        def walk(el, zf, path):
            for child in el:
                tag = child.tag.replace(f"{{{KML_NS}}}", "")
                if tag in ("Document", "Folder"):
                    ne = child.find(f"{{{KML_NS}}}name")
                    fn = sanitize(ne.text) if (ne is not None and ne.text) else f"Folder_{counter['n']}"
                    walk(child, zf, f"{path}/{fn}".strip("/"))
                elif tag == "Placemark":
                    counter["n"] += 1
                    ne = child.find(f"{{{KML_NS}}}name")
                    pm = sanitize(ne.text) if (ne is not None and ne.text) else f"Polygon_{counter['n']}"
                    zf.writestr(unique_path(path, pm), build_kml_bytes(child))

        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            walk(root, zf, "")

        if counter["n"] == 0:
            rdb.hset(f"job:{job_id}", mapping={"status": "error", "msg": "Tidak ditemukan Polygon/Placemark."})
        else:
            rdb.hset(f"job:{job_id}", mapping={"status": "done", "count": counter["n"]})
            rdb.set(f"zip:{job_id}", buf.getvalue(), ex=JOB_TTL)
            log.info("job=%s polygons=%d", job_id, counter["n"])

    except ET.ParseError as e:
        rdb.hset(f"job:{job_id}", mapping={"status": "error", "msg": f"KML tidak valid: {e}"})
    except Exception as e:
        log.exception("job=%s failed", job_id)
        rdb.hset(f"job:{job_id}", mapping={"status": "error", "msg": f"Kesalahan server: {e}"})
    finally:
        rdb.expire(f"job:{job_id}", JOB_TTL)


# ── HTML Template ─────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KML Splitter Pro — Ekstrak Polygon KML</title>
<meta name="description" content="Pecah file KML menjadi polygon individual dalam format ZIP.">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%234f46e5' stroke-width='2' stroke-linecap='round' stroke-linejoin='round'><polygon points='12 2 2 7 12 12 22 7 12 2'/><polyline points='2 17 12 22 22 17'/><polyline points='2 12 12 17 22 12'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{--p:#4f46e5;--pd:#4338ca;--pl:#eef2ff;--ok:#10b981;--err:#ef4444;
      --tx:#1e293b;--mu:#64748b;--bd:#e2e8f0;--bg:#f8fafc}
body{font-family:'Inter',sans-serif;background:var(--bg);color:var(--tx);min-height:100vh;display:flex;flex-direction:column}
/* header */
header{background:#fff;border-bottom:1px solid var(--bd);padding:16px 24px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:10;box-shadow:0 1px 4px rgba(0,0,0,.07)}
.logo{display:flex;align-items:center;gap:10px;font-weight:700;font-size:1.05rem;color:var(--p)}
.badge{font-size:11px;background:var(--pl);color:var(--p);padding:3px 10px;border-radius:99px;font-weight:600}
/* main */
main{flex:1;display:flex;align-items:center;justify-content:center;padding:48px 16px}
.card{background:#fff;border:1px solid var(--bd);border-radius:20px;padding:40px;width:100%;max-width:520px;box-shadow:0 4px 24px rgba(0,0,0,.07)}
.card-title{font-size:1.65rem;font-weight:800;text-align:center;margin-bottom:8px}
.card-desc{font-size:.875rem;color:var(--mu);text-align:center;line-height:1.7;margin-bottom:32px}
/* drop zone */
.dz{border:2px dashed var(--bd);border-radius:14px;padding:36px 24px;text-align:center;cursor:pointer;transition:border-color .2s,background .2s,transform .15s;position:relative}
.dz:hover,.dz.over{border-color:var(--p);background:var(--pl);transform:scale(1.01)}
.dz input{position:absolute;inset:0;opacity:0;width:100%;height:100%;cursor:pointer}
.dz-icon{margin-bottom:10px;display:flex;justify-content:center;color:var(--p);transition:transform .2s}
.dz:hover .dz-icon,.dz.over .dz-icon{transform:translateY(-5px)}
.dz-main{font-weight:600;font-size:.95rem}
.dz-sub{font-size:.8rem;color:var(--mu);margin-top:5px}
/* file info */
#fi{display:none;align-items:center;gap:12px;background:var(--pl);border:1px solid #c7d2fe;border-radius:12px;padding:12px 16px;margin-top:16px;animation:slideDown .25s ease}
#fi.show{display:flex}
.fi-icon{flex-shrink:0;color:var(--p);display:flex;align-items:center}
.fi-body{flex:1;min-width:0}
.fi-name{font-weight:600;font-size:.875rem;color:var(--p);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fi-size{font-size:.75rem;color:var(--mu)}
.fi-clr{background:none;border:none;cursor:pointer;font-size:1.1rem;color:var(--mu);padding:4px 6px;border-radius:6px;transition:background .15s,color .15s;line-height:1}
.fi-clr:hover{background:#fee2e2;color:var(--err)}
/* button */
.btn{width:100%;background:var(--p);color:#fff;border:none;border-radius:12px;padding:14px 20px;font-size:.95rem;font-weight:600;font-family:'Inter',sans-serif;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;margin-top:24px;transition:background .2s,transform .15s,box-shadow .2s;box-shadow:0 4px 16px rgba(79,70,229,.3)}
.btn:hover{background:var(--pd);transform:translateY(-1px);box-shadow:0 6px 22px rgba(79,70,229,.4)}
.btn:active{transform:translateY(0)}
.btn:disabled{background:#a5b4fc;cursor:not-allowed;transform:none;box-shadow:none}
/* overlay */
#ov{display:none;position:fixed;inset:0;background:rgba(15,23,42,.65);backdrop-filter:blur(6px);z-index:9999;align-items:center;justify-content:center}
#ov.show{display:flex;animation:fadeIn .3s ease}
#ov.out{animation:fadeOut .4s ease forwards}
.lbox{background:#fff;border-radius:20px;padding:40px 48px;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.2);min-width:300px;animation:popIn .3s cubic-bezier(.34,1.56,.64,1)}
.spin{width:54px;height:54px;border:5px solid #e0e7ff;border-top-color:var(--p);border-radius:50%;margin:0 auto 20px;animation:spin .8s linear infinite}
.lt{font-weight:700;font-size:1.1rem;margin-bottom:6px}
.ls{font-size:.85rem;color:var(--mu);min-height:1.2em}
.pw{background:#e0e7ff;border-radius:99px;height:6px;margin-top:20px;overflow:hidden}
.pb{height:100%;background:linear-gradient(90deg,var(--p),#818cf8);border-radius:99px;animation:prog 2.2s ease-in-out infinite}
/* toasts */
#toasts{position:fixed;bottom:24px;right:24px;z-index:10000;display:flex;flex-direction:column;gap:10px}
.toast{display:flex;align-items:center;gap:10px;padding:13px 18px;border-radius:12px;font-size:.875rem;font-weight:500;box-shadow:0 8px 24px rgba(0,0,0,.12);animation:slideRight .3s cubic-bezier(.34,1.56,.64,1);min-width:250px;max-width:360px}
.t-ok{background:#ecfdf5;border:1px solid #6ee7b7;color:#065f46}
.t-err{background:#fef2f2;border:1px solid #fca5a5;color:#7f1d1d}
/* footer */
footer{text-align:center;padding:20px;font-size:.8rem;color:var(--mu);border-top:1px solid var(--bd);background:#fff}
/* keyframes */
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes fadeOut{from{opacity:1}to{opacity:0}}
@keyframes popIn{from{opacity:0;transform:scale(.85)}to{opacity:1;transform:scale(1)}}
@keyframes slideDown{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideRight{from{opacity:0;transform:translateX(40px)}to{opacity:1;transform:translateX(0)}}
@keyframes prog{0%{width:0%;margin-left:0}50%{width:65%;margin-left:10%}100%{width:5%;margin-left:95%}}
</style>
</head>
<body>
<div id="ov" role="status" aria-live="polite">
  <div class="lbox">
    <div class="spin"></div>
    <div class="lt" id="lt">Memproses File KML…</div>
    <div class="ls" id="ls">Sedang memisahkan setiap polygon</div>
    <div class="pw"><div class="pb"></div></div>
  </div>
</div>
<div id="toasts"></div>
<header>
  <div class="logo">
    <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>
      <polyline points="2 17 12 22 22 17"></polyline>
      <polyline points="2 12 12 17 22 12"></polyline>
    </svg>
    KML Splitter Pro
  </div>
  <span class="badge">v2.0</span>
</header>
<main>
  <div class="card">
    <h1 class="card-title">Ekstrak Polygon KML</h1>
    <p class="card-desc">Unggah file <strong>.kml</strong> untuk memecah setiap bidang/polygon menjadi file terpisah dalam satu arsip <strong>ZIP</strong>.</p>
    <div class="dz" id="dz">
      <input type="file" id="kml_file" accept=".kml">
      <span class="dz-icon">
        <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
          <polyline points="17 8 12 3 7 8"></polyline>
          <line x1="12" y1="3" x2="12" y2="15"></line>
        </svg>
      </span>
      <div class="dz-main">Klik atau seret file KML ke sini</div>
      <div class="dz-sub">Hanya mendukung format <strong>.kml</strong></div>
    </div>
    <div id="fi">
      <span class="fi-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
          <polyline points="14 2 14 8 20 8"></polyline>
        </svg>
      </span>
      <div class="fi-body">
        <div class="fi-name" id="fi-name">—</div>
        <div class="fi-size" id="fi-size">—</div>
      </div>
      <button type="button" class="fi-clr" id="fi-clr" title="Hapus">✕</button>
    </div>
    <button class="btn" id="btn-sub">
      <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/></svg>
      Proses &amp; Unduh ZIP
    </button>
  </div>
</main>
<footer>&copy; 2026 KML Splitter Utility &mdash; @afwansu_</footer>
<script>
const dz=document.getElementById('dz'),inp=document.getElementById('kml_file'),
      fi=document.getElementById('fi'),fiN=document.getElementById('fi-name'),
      fiS=document.getElementById('fi-size'),fiClr=document.getElementById('fi-clr'),
      btn=document.getElementById('btn-sub'),ov=document.getElementById('ov'),
      lt=document.getElementById('lt'),ls=document.getElementById('ls');

function fmt(b){if(b<1024)return b+' B';if(b<1048576)return(b/1024).toFixed(1)+' KB';return(b/1048576).toFixed(2)+' MB';}
function showFile(f){fiN.textContent=f.name;fiS.textContent=fmt(f.size);fi.classList.add('show');}
function clearFile(){inp.value='';fi.classList.remove('show');}

inp.addEventListener('change',()=>{if(inp.files.length)showFile(inp.files[0]);});
fiClr.addEventListener('click',clearFile);
dz.addEventListener('dragover',e=>{e.preventDefault();dz.classList.add('over');});
dz.addEventListener('dragleave',()=>dz.classList.remove('over'));
dz.addEventListener('drop',e=>{
  e.preventDefault();dz.classList.remove('over');
  const f=e.dataTransfer.files;
  if(f.length&&f[0].name.endsWith('.kml')){
    const dt=new DataTransfer();dt.items.add(f[0]);inp.files=dt.files;showFile(f[0]);
  } else toast('err','❌ Hanya file .kml yang didukung');
});

btn.addEventListener('click',async()=>{
  if(!inp.files.length){toast('err','⚠️ Pilih file KML terlebih dahulu');return;}
  showOv();
  const fd=new FormData();fd.append('kml_file',inp.files[0]);
  try{
    const r=await fetch('/upload',{method:'POST',body:fd});
    const data=await r.json();
    if(!r.ok){hideOv();toast('err','❌ '+(data.error||'Gagal mengunggah'));return;}
    pollStatus(data.job_id);
  }catch(e){hideOv();toast('err','❌ Koneksi gagal. Coba lagi.');}
});

function pollStatus(jobId){
  const steps=[
    [1200,null,'Menganalisis struktur folder & Placemark…'],
    [2600,'Membangun Arsip ZIP…','Menyusun file KML individual…'],
    [4200,'Hampir Selesai…','Mempersiapkan unduhan untuk Anda…']
  ];
  steps.forEach(([ms,t,s])=>setTimeout(()=>{if(t)lt.textContent=t;ls.textContent=s;},ms));

  const iv=setInterval(async()=>{
    try{
      const r=await fetch('/status/'+jobId);
      const d=await r.json();
      if(d.status==='done'){
        clearInterval(iv);
        window.location.href='/download/'+jobId;
        setTimeout(()=>{hideOv();toast('ok','✅ File ZIP berhasil diunduh! ('+d.count+' polygon)');},1200);
      } else if(d.status==='error'){
        clearInterval(iv);hideOv();toast('err','❌ '+(d.msg||'Terjadi kesalahan'));
      }
    }catch{clearInterval(iv);hideOv();toast('err','❌ Koneksi terputus');}
  },1000);
  setTimeout(()=>{clearInterval(iv);hideOv();toast('err','⏱ Timeout. Coba lagi.');},60000);
}

function showOv(){ov.classList.remove('out');ov.classList.add('show');btn.disabled=true;lt.textContent='Memproses File KML…';ls.textContent='Sedang memisahkan setiap polygon';}
function hideOv(){ov.classList.add('out');setTimeout(()=>{ov.classList.remove('show','out');},400);btn.disabled=false;}

function toast(type,msg){
  const el=document.createElement('div');
  el.className='toast t-'+type;el.textContent=msg;
  document.getElementById('toasts').appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transition='opacity .4s';setTimeout(()=>el.remove(),400);},5000);
}

window.addEventListener('pageshow',()=>{ov.classList.remove('show','out');btn.disabled=false;});
</script>
</body>
</html>"""


# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return render_template_string(HTML)


@app.get("/health")
def health():
    try:
        rdb.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    status = "ok" if redis_ok else "degraded"
    code   = 200 if redis_ok else 503
    return jsonify({"status": status, "redis": "ok" if redis_ok else "error"}), code


@app.post("/upload")
@limiter.limit(UPLOAD_RATE_LIMIT)
def upload():
    if "kml_file" not in request.files:
        return jsonify({"error": "Tidak ada file yang diunggah"}), 400
    f = request.files["kml_file"]
    if not f.filename or not f.filename.endswith(".kml"):
        return jsonify({"error": "Format file harus .kml"}), 400

    kml_bytes = f.read()
    job_id    = uuid.uuid4().hex
    rdb.hset(f"job:{job_id}", mapping={"status": "queued"})
    rdb.expire(f"job:{job_id}", JOB_TTL)

    threading.Thread(target=process_kml, args=(job_id, kml_bytes), daemon=True).start()
    log.info("job=%s queued file=%s size=%d", job_id, f.filename, len(kml_bytes))
    return jsonify({"job_id": job_id}), 202


@app.get("/status/<job_id>")
def status(job_id: str):
    data = rdb.hgetall(f"job:{job_id}")
    if not data:
        return jsonify({"status": "not_found"}), 404
    decoded = {k.decode(): v.decode() for k, v in data.items()}
    return jsonify(decoded)


@app.get("/download/<job_id>")
def download(job_id: str):
    data = rdb.hgetall(f"job:{job_id}")
    if not data:
        return jsonify({"error": "Job tidak ditemukan atau sudah kedaluwarsa"}), 404
    decoded = {k.decode(): v.decode() for k, v in data.items()}
    if decoded.get("status") != "done":
        return jsonify({"error": "File belum siap"}), 404

    zip_bytes = rdb.get(f"zip:{job_id}")
    if not zip_bytes:
        return jsonify({"error": "File sudah kedaluwarsa. Silakan upload ulang."}), 410

    return send_file(
        io.BytesIO(zip_bytes),
        mimetype="application/zip",
        as_attachment=True,
        download_name="Hasil_Pecahan_KML.zip",
    )


@app.errorhandler(413)
def too_large(_):
    return jsonify({"error": "File terlalu besar. Maksimum 50 MB."}), 413


@app.errorhandler(429)
def rate_limited(_):
    return jsonify({"error": "Terlalu banyak permintaan. Coba lagi dalam 1 menit."}), 429


if __name__ == "__main__":
    app.run(debug=True, port=5000)