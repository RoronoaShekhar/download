from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
from Crypto.Cipher import AES
import concurrent.futures
import urllib.parse
import os
import re
import time
import threading
import random

app = Flask(__name__)
CORS(app)

# ==========================================
# GLOBAL SETTINGS & STATE
# ==========================================
MAX_WORKERS = 16
DOWNLOAD_BASE_DIR = "pw_Downloads"
API_ENDPOINT = "https://download.pwthor.live/api/metadata"
SEGMENT_TIMEOUT = 20
SEGMENT_RETRIES = 3
META_RETRIES = 3

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")

state_lock = threading.Lock()
log_lock = threading.Lock()
proxy_lock = threading.Lock()

server_state = {
    "status": "idle",
    "total_videos": 0,
    "completed_videos": 0,
    "failed_videos": 0,
    "current_file": "",
    "progress": "0%",
    "error_msg": "",
    "queue": [],
    "log": []
}

SESSION_COOKIES = {}

# ==========================================
# PROXY MANAGEMENT
# ==========================================
PROXY_LIST = []
PROXY_SOURCES = [
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt",
]

def fetch_proxy_list():
    global PROXY_LIST
    print("[*] Fetching fresh proxy list...")
    proxies = set()
    for source in PROXY_SOURCES:
        try:
            r = requests.get(source, timeout=10)
            if r.status_code == 200:
                for line in r.text.strip().splitlines():
                    line = line.strip()
                    if re.match(r'^\d+\.\d+\.\d+\.\d+:\d+$', line):
                        proxies.add(line)
        except Exception:
            pass
    with proxy_lock:
        PROXY_LIST = list(proxies)
    print(f"[*] Loaded {len(PROXY_LIST)} proxies successfully.")

def test_proxy(proxy_str):
    proxies = {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"}
    try:
        r = requests.get("https://download.pwthor.live", proxies=proxies, timeout=8)
        return r.status_code < 500
    except Exception:
        return False

def get_working_proxy():
    with proxy_lock:
        candidates = list(PROXY_LIST)
    random.shuffle(candidates)
    test_candidates = candidates[:50]
    
    if not test_candidates:
        return None
        
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        future_to_proxy = {executor.submit(test_proxy, p): p for p in test_candidates}
        for future in concurrent.futures.as_completed(future_to_proxy):
            proxy_str = future_to_proxy[future]
            try:
                if future.result():
                    return {"http": f"http://{proxy_str}", "https": f"http://{proxy_str}"}
            except Exception:
                pass
    return None

threading.Thread(target=fetch_proxy_list, daemon=True).start()

ACTIVE_PROXY = None
PROXY_REFRESH_TIME = 0

def get_proxy():
    global ACTIVE_PROXY, PROXY_REFRESH_TIME
    now = time.time()
    if ACTIVE_PROXY is None or (now - PROXY_REFRESH_TIME) > 600:
        ACTIVE_PROXY = get_working_proxy()
        PROXY_REFRESH_TIME = now
    return ACTIVE_PROXY

def update_state(**kwargs):
    with state_lock:
        server_state.update(kwargs)

def add_log(msg):
    with log_lock:
        server_state["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(server_state["log"]) > 100:
            server_state["log"] = server_state["log"][-100:]
    print(msg)

# ==========================================
# COOKIE EXTRACTION & HEADERS
# ==========================================
def parse_curl_cookies(curl_cmd):
    normalised = re.sub(r'\^[ \t]*\r?\n', ' ', curl_cmd).replace('\\', '').replace('^', '')
    normalised = re.sub(r'\s+', ' ', normalised)
    cookie_string = None
    
    match = re.search(r'-H\s+"cookie:\s*([^"]+)"', normalised, re.IGNORECASE) or \
            re.search(r"-H\s+'cookie:\s*([^']+)'", normalised, re.IGNORECASE) or \
            re.search(r'(?:--cookie|-b)\s+["\']([^"\']+)["\']', normalised, re.IGNORECASE)
            
    if match:
        cookie_string = match.group(1).strip()
    if not cookie_string:
        return {}

    cookies = {}
    for part in cookie_string.split(';'):
        part = part.strip()
        if '=' in part:
            name, _, value = part.partition('=')
            cookies[name.strip()] = value.strip()
    return cookies

BASE_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
    "content-type": "application/json",
    "origin": "https://download.pwthor.live",
    "sec-ch-ua": '"Not A(Brand";v="8", "Chromium";v="132", "Google Chrome";v="132"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
    "user-agent": "Mozilla/5.0 (Windows NT 6.1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36"
}

def sanitize_filename(name):
    if not name:
        name = "Unknown"
    cleaned = "".join(c for c in str(name) if c.isalnum() or c in " ._-()[]").strip()
    return cleaned[:120] if cleaned else "Unknown"

def get_dynamic_headers(m3u8_url):
    headers = dict(BASE_HEADERS)
    encoded_url = urllib.parse.quote(m3u8_url, safe='')
    headers["referer"] = f"https://download.pwthor.live/?url={encoded_url}"
    return headers

def make_session(use_proxy=True):
    session = requests.Session()
    session.cookies.update(SESSION_COOKIES)
    if use_proxy:
        proxy = get_proxy()
        if proxy:
            session.proxies.update(proxy)
    return session

# ==========================================
# TELEGRAM UPLOAD
# ==========================================
def upload_to_telegram(file_path, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        add_log("[!] Telegram not configured — skipping upload.")
        return False
    try:
        add_log(f"[TG] Launching file upload to Telegram: {os.path.basename(file_path)}")
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
        with open(file_path, 'rb') as f:
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "caption": caption,
                "supports_streaming": True
            }, files={"video": f}, timeout=450)
        if resp.status_code == 200:
            add_log(f"[TG] Upload success: {os.path.basename(file_path)}")
            return True
        else:
            add_log(f"[TG] Upload failed Status ({resp.status_code}): {resp.text[:300]}")
            return False
    except Exception as e:
        add_log(f"[TG] Critical uploading exception occurred: {e}")
        return False

# ==========================================
# CORE DOWNLOADER
# ==========================================
def post_metadata(url):
    """Directly connect to API without proxies to avoid timeout walls."""
    last_err = None
    add_log(f"[*] Posting API payload metadata for url extraction (Direct Connection)...")
    
    direct_session = requests.Session()
    direct_session.cookies.update(SESSION_COOKIES)
    
    for attempt in range(META_RETRIES):
        try:
            resp = direct_session.post(API_ENDPOINT, json={"url": url}, headers=get_dynamic_headers(url), timeout=20)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            add_log(f"[!] Metadata direct attempt {attempt+1}/{META_RETRIES} failed: {e}. Retrying...")
            time.sleep(2)
            
    raise Exception(f"Metadata extraction totally failed after {META_RETRIES} loops: {last_err}")

def download_and_decrypt(index, segment_url, key_info, headers, session):
    for attempt in range(SEGMENT_RETRIES):
        try:
            res = session.get(segment_url, headers=headers, timeout=SEGMENT_TIMEOUT)
            res.raise_for_status()
            encrypted_data = res.content
            if not key_info:
                return index, encrypted_data
            key = bytes.fromhex(key_info['keyHex'])
            iv = bytes.fromhex(key_info['ivHex']) if key_info.get('ivHex') else (index + 1).to_bytes(16, byteorder='big')
            cipher = AES.new(key, AES.MODE_CBC, iv)
            return index, cipher.decrypt(encrypted_data)
        except Exception as e:
            if attempt < SEGMENT_RETRIES - 1:
                time.sleep(1.5)
            else:
                add_log(f"[!] Chunk #{index} failed completely after max retries. Error: {e}")
                return index, None
    return index, None

def custom_hls_downloader(m3u8_url, output_path):
    if not m3u8_url:
        raise Exception("No m3u8 stream URL provided.")
    
    data = post_metadata(m3u8_url)

    if data.get('type') == 'master':
        qualities = data.get('qualities', [])
        if not qualities:
            raise Exception("Master playlist detected but contains no available video streams.")
        
        target_url = qualities[0]['url']
        chosen_label = qualities[0].get('label', 'Default')
        for quality in qualities:
            if '480' in str(quality.get('label', '')):
                target_url = quality['url']
                chosen_label = quality['label']
                break
        add_log(f"[*] HLS Master stream resolved. Selecting stream profile: {chosen_label}")
        data = post_metadata(target_url)
        m3u8_url = target_url

    if data.get('type') != 'media':
        raise Exception(f"Failed to resolve media tracks payload (type received={data.get('type')}).")

    segments = data['data']['segments']
    key_info = data['data'].get('key')
    total_segments = len(segments)
    headers = get_dynamic_headers(m3u8_url)

    if total_segments == 0:
        raise Exception("Zero stream components found inside the payload configuration.")

    add_log(f"[*] Extracting video pipeline: {total_segments} HLS chunks found.")
    downloaded_chunks = [None] * total_segments
    start_time = time.time()
    failed = 0

    session = make_session(use_proxy=True) # Proxies used here to fetch actual heavy video segments
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(download_and_decrypt, i, url, key_info, headers, session): i for i, url in enumerate(segments)}
        done = 0
        for future in concurrent.futures.as_completed(futures):
            index, chunk_data = future.result()
            if chunk_data is not None:
                downloaded_chunks[index] = chunk_data
            else:
                failed += 1
            done += 1
            
            pct = int((done / total_segments) * 100)
            update_state(progress=f"{pct}%")
            
            if done % 10 == 0 or done == total_segments:
                add_log(f"Progress Status: {pct}% ({done}/{total_segments} files completed, {failed} broken updates)")

    add_log("[*] Finalizing download — merging fragmented transport streams...")
    written = 0
    with open(output_path, 'wb') as outfile:
        for chunk in downloaded_chunks:
            if chunk:
                outfile.write(chunk)
                written += 1

    if written == 0:
        raise Exception("Download aborted: 100% of pipeline data streams dropped.")
    add_log(f"[+] Download phase successful: {written}/{total_segments} chunks packed in {int(time.time() - start_time)}s")

# ==========================================
# BATCH ORCHESTRATOR
# ==========================================
def batch_orchestrator(video_list):
    add_log(f"[*] Processing incoming payload structure containing {len(video_list)} entries...")
    update_state(
        status="downloading",
        total_videos=len(video_list),
        completed_videos=0,
        failed_videos=0,
        error_msg="",
        progress="0%"
    )

    with state_lock:
        server_state["queue"] = [
            {
                "lecture": v.get("lecture", "Unknown"),
                "subject": v.get("subject", ""),
                "chapter": v.get("chapter", ""),
                "status": "waiting"
            }
            for v in video_list
        ]

    batch_start = time.time()

    for idx, video in enumerate(video_list):
        lec_name = video.get('lecture', f'Lecture_{idx}')
        update_state(current_file=lec_name, progress="0%")

        with state_lock:
            if idx < len(server_state["queue"]):
                server_state["queue"][idx]["status"] = "downloading"

        add_log(f"\n==================================================")
        add_log(f">> PIPELINE DEPLOYMENT [{idx+1}/{len(video_list)}]: {lec_name}")
        add_log(f"==================================================")

        safe_batch = sanitize_filename(video.get('batch', 'Unknown'))
        safe_subj  = sanitize_filename(video.get('subject', 'Unknown'))
        safe_chap  = sanitize_filename(video.get('chapter', 'Unknown'))
        safe_lec   = sanitize_filename(lec_name)

        save_dir = os.path.join(DOWNLOAD_BASE_DIR, safe_batch, safe_subj, safe_chap)
        os.makedirs(save_dir, exist_ok=True)
        output_path = os.path.join(save_dir, f"{safe_lec}.ts")

        mpd_link = video.get('mpd_url', '')
        if mpd_link in ["FAILED_TIMEOUT", "MANUALLY_SKIPPED", "DUPLICATE_ERROR"] or not mpd_link:
            add_log(f"[!] Target URL blacklisted or empty ({mpd_link}) — skipping item.")
            with state_lock:
                server_state["failed_videos"] += 1
                if idx < len(server_state["queue"]):
                    server_state["queue"][idx]["status"] = "failed"
            continue

        try:
            custom_hls_downloader(mpd_link, output_path)

            with state_lock:
                if idx < len(server_state["queue"]):
                    server_state["queue"][idx]["status"] = "uploading"

            caption = f"📚 {safe_batch}\n📖 {safe_subj}\n📂 {safe_chap}\n🎬 {safe_lec}"
            upload_to_telegram(output_path, caption=caption)

            if os.path.exists(output_path):
                os.remove(output_path)
                add_log(f"[*] Flushed localized storage artifacts for: {safe_lec}.ts")

            with state_lock:
                server_state["completed_videos"] += 1
                if idx < len(server_state["queue"]):
                    server_state["queue"][idx]["status"] = "done"

        except Exception as e:
            add_log(f"[!] CRITICAL SYSTEM FAULT processing {safe_lec}: {e}")
            with state_lock:
                server_state["failed_videos"] += 1
                if idx < len(server_state["queue"]):
                    server_state["queue"][idx]["status"] = "failed"
            if os.path.exists(output_path):
                try:
                    os.remove(output_path)
                except:
                    pass

    update_state(status="completed")
    add_log(f"\n[OK] ALL BATCH JOBS COMPLETED in {int(time.time() - batch_start)}s")

# ==========================================
# FLASK ROUTES & WEB UI
# ==========================================
@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({"status": "ok", "cookies_loaded": len(SESSION_COOKIES)})

@app.route('/status', methods=['GET'])
def get_status():
    with state_lock:
        return jsonify(dict(server_state))

@app.route('/set_cookies', methods=['POST'])
def set_cookies():
    global SESSION_COOKIES
    data = request.get_json(silent=True) or {}
    curl_cmd = data.get('curl', '')
    if not curl_cmd:
        return jsonify({"error": "No curl command provided"}), 400
    cookies = parse_curl_cookies(curl_cmd)
    if not cookies:
        return jsonify({"error": "Could not parse cookies from cURL command"}), 400
    SESSION_COOKIES = cookies
    add_log(f"[+] Cookies parsed successfully ({len(cookies)} header profiles mapped)")
    return jsonify({"status": "ok", "count": len(cookies)})

@app.route('/download_batch', methods=['POST'])
def start_batch_download():
    with state_lock:
        if server_state["status"] == "downloading":
            return jsonify({"error": "Server operation currently busy with active queues"}), 400
    data = request.get_json(silent=True) or {}
    video_list = data.get('videos', [])
    if not video_list:
        return jsonify({"error": "Received empty batch manifest list"}), 400
    thread = threading.Thread(target=batch_orchestrator, args=(video_list,))
    thread.daemon = True
    thread.start()
    return jsonify({"status": "Batch Started", "count": len(video_list)})

@app.route('/', methods=['GET'])
def index():
    html = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>EduSphere | Personal Learning & Archive Suite</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #f8fafc; color: #1e293b; font-family: 'Inter', sans-serif; display: flex; min-height: 100vh; }
  .sidebar { width: 260px; background: #0f172a; color: #fff; padding: 24px; display: flex; flex-direction: column; gap: 8px; }
  .sidebar h1 { font-size: 20px; font-weight: 600; margin-bottom: 24px; color: #38bdf8; display: flex; align-items: center; gap: 8px; }
  .nav-item { padding: 12px 16px; border-radius: 8px; color: #94a3b8; text-decoration: none; font-weight: 500; display: flex; align-items: center; gap: 12px; cursor: pointer; transition: 0.2s; }
  .nav-item:hover, .nav-item.active { background: #1e293b; color: #fff; }
  .main-content { flex: 1; padding: 40px; max-width: 1200px; margin: 0 auto; width: 100%; display: none; }
  .main-content.active-panel { display: block; }
  .header-area { margin-bottom: 32px; }
  .header-area h2 { font-size: 24px; font-weight: 600; color: #0f172a; }
  .header-area p { color: #64748b; font-size: 14px; margin-top: 4px; }
  .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; margin-bottom: 24px; }
  .card { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.05); }
  .card h3 { font-size: 16px; font-weight: 600; margin-bottom: 16px; color: #334155; }
  textarea { width: 100%; height: 120px; background: #f1f5f9; border: 1px solid #cbd5e1; color: #334155; border-radius: 8px; padding: 12px; font-family: monospace; font-size: 13px; resize: none; margin-bottom: 12px; outline: none; }
  textarea:focus { border-color: #3b82f6; background: #fff; }
  button { background: #2563eb; color: #fff; border: none; border-radius: 8px; padding: 10px 20px; cursor: pointer; font-size: 14px; font-weight: 500; transition: 0.2s; }
  button:hover { background: #1d4ed8; }
  .status-metric { display: flex; justify-content: space-between; padding: 12px 0; border-bottom: 1px solid #f1f5f9; font-size: 14px; color: #475569; }
  .status-metric span { font-weight: 600; color: #0f172a; }
  .progress-wrap { background: #e2e8f0; border-radius: 6px; height: 8px; margin: 16px 0 8px; overflow: hidden; }
  .progress-bar { height: 100%; width: 0%; background: #3b82f6; transition: width 0.3s; }
  .queue-list { display: flex; flex-direction: column; gap: 8px; max-height: 250px; overflow-y: auto; }
  .queue-item { display: flex; justify-content: space-between; align-items: center; padding: 12px; background: #f8fafc; border-radius: 8px; border: 1px solid #e2e8f0; }
  .badge { font-size: 12px; padding: 4px 10px; border-radius: 6px; font-weight: 500; text-transform: capitalize; }
  .badge.waiting { background: #e2e8f0; color: #475569; }
  .badge.downloading { background: #dbeafe; color: #1e40af; }
  .badge.uploading { background: #fef9c3; color: #854d0e; }
  .badge.done { background: #dcfce7; color: #14532d; }
  .badge.failed { background: #fee2e2; color: #991b1b; }
  #log { background: #0f172a; color: #94a3b8; border-radius: 8px; padding: 16px; height: 200px; overflow-y: auto; font-family: monospace; font-size: 12px; }
  #log p { margin-bottom: 4px; line-height: 1.4; }
  #log p.ok { color: #4ade80; }
  #log p.err { color: #f87171; }
  #log p.info { color: #38bdf8; }
</style>
</head>
<body>

<div class="sidebar">
  <h1>🎓 EduSphere</h1>
  <div class="nav-item active" onclick="switchPanel('dashboard')">📊 Main Dashboard</div>
  <div class="nav-item" onclick="switchPanel('archiver')">⚙️ Archive Settings</div>
  <div class="nav-item" onclick="switchPanel('notes')">📝 Study Notes</div>
</div>

<div class="main-content active-panel" id="panel-dashboard">
  <div class="header-area">
    <h2>Academic Workspace Overview</h2>
    <p>Monitor your structured repository and content integration pipelines below.</p>
  </div>
  <div class="grid-2">
    <div class="card">
      <h3>Sync Infrastructure Status</h3>
      <div class="status-metric">System Engine State: <span id="s-status">Idle</span></div>
      <div class="status-metric">Total Tracked Resources: <span id="s-total">0</span></div>
      <div class="status-metric">Successfully Processed: <span id="s-done">0</span></div>
      <div class="status-metric">Failed Operations: <span id="s-failed">0</span></div>
      <div class="progress-wrap"><div class="progress-bar" id="s-progress"></div></div>
      <div style="display:flex; justify-content:space-between; font-size:12px; color:#64748b;">
        <span id="s-current" style="font-weight:500; color:#3b82f6;"></span>
        <span id="s-pct">0%</span>
      </div>
    </div>
    <div class="card">
      <h3>Active Pipeline Stream Queue</h3>
      <div class="queue-list" id="queue-container">
        <div style="color:#64748b; font-size: 13px;">No active stream batches are loaded. Waiting for deployment manifest...</div>
      </div>
    </div>
  </div>
  <div class="card">
    <h3>Developer Infrastructure Debug Logs</h3>
    <div id="log"></div>
  </div>
</div>

<div class="main-content" id="panel-archiver">
  <div class="header-area">
    <h2>Secure Session Integration</h2>
    <p>Configure internal platform request cookies to sync protected educational modules smoothly.</p>
  </div>
  <div class="card" style="max-width: 600px;">
    <h3>Authentication Header Payload</h3>
    <textarea id="curl-input" placeholder="Paste structural cURL configuration strings here..."></textarea>
    <button onclick="saveCookies()">Verify & Mount Session</button>
    <p id="cookie-msg" style="margin-top:12px; font-size:14px; font-weight:500; display:none;"></p>
  </div>
</div>

<div class="main-content" id="panel-notes">
  <div class="header-area">
    <h2>Study Notebook Workspace</h2>
    <p>Draft documentation and annotations corresponding to targeted academic structures.</p>
  </div>
  <div class="card">
    <h3>Quick Notes Scratchpad</h3>
    <textarea style="height: 250px;" placeholder="Type workspace annotations, commands, or reference URLs here..."></textarea>
    <button onclick="alert('Notes cached locally to system storage.')">Save Workspace Notebook</button>
  </div>
</div>

<script>
  function switchPanel(panelId) {
    document.querySelectorAll('.main-content').forEach(el => el.classList.remove('active-panel'));
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
    document.getElementById('panel-' + panelId).classList.add('active-panel');
    event.currentTarget.classList.add('active');
  }

  function saveCookies() {
    const curl = document.getElementById('curl-input').value.trim();
    if (!curl) return;
    fetch('/set_cookies', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({curl})
    }).then(r => r.json()).then(d => {
      const msg = document.getElementById('cookie-msg');
      if (d.status === 'ok') {
        msg.textContent = '✅ Secure authentication profile loaded (' + d.count + ' verification headers mounted).';
        msg.style.color = '#16a34a';
      } else {
        msg.textContent = '❌ Credentials verification mismatch: ' + (d.error || 'Syntax invalid');
        msg.style.color = '#dc2626';
      }
      msg.style.display = 'block';
    });
  }

  let lastLogCount = 0;
  function colorLog(line) {
    if (line.includes('[TG]')) return 'tg';
    if (line.includes('[+]') || line.includes('success') || line.includes('COMPLETED')) return 'ok';
    if (line.includes('[!]') || line.includes('FAULT') || line.includes('failed')) return 'err';
    return 'info';
  }

  function poll() {
    fetch('/status').then(r => r.json()).then(d => {
      document.getElementById('s-status').textContent = d.status;
      document.getElementById('s-total').textContent = d.total_videos;
      document.getElementById('s-done').textContent = d.completed_videos;
      document.getElementById('s-failed').textContent = d.failed_videos;
      const pct = parseInt(d.progress) || 0;
      document.getElementById('s-progress').style.width = pct + '%';
      document.getElementById('s-pct').textContent = d.progress;
      document.getElementById('s-current').textContent = d.current_file ? 'Processing: ' + d.current_file : '';

      if (d.queue && d.queue.length > 0) {
        document.getElementById('queue-container').innerHTML = d.queue.map(item => `
          <div class="queue-item">
            <div>
              <div style="font-weight:500; color:#1e293b; font-size:13px;">${item.lecture}</div>
              <div style="font-size:11px; color:#64748b; margin-top:2px;">${item.subject} &middot; ${item.chapter}</div>
            </div>
            <span class="badge ${item.status}">${item.status}</span>
          </div>
        `).join('');
      }

      if (d.log && d.log.length > lastLogCount) {
        const logEl = document.getElementById('log');
        d.log.slice(lastLogCount).forEach(line => {
          const p = document.createElement('p');
          p.className = colorLog(line);
          p.textContent = line;
          logEl.appendChild(p);
        });
        lastLogCount = d.log.length;
        logEl.scrollTop = logEl.scrollHeight;
      }
    }).catch(() => {});
  }

  setInterval(poll, 1500);
  poll();
</script>
</body>
</html>'''
    return Response(html, mimetype='text/html')

if __name__ == '__main__':
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, threaded=True)
