# DEPLOYMENT:
# 1. Push server.py, requirements.txt to a GitHub repo
# 2. Go to railway.app, sign in with GitHub, create new project from repo
# 3. Set environment variables in Railway dashboard:
#    TELEGRAM_BOT_TOKEN = your bot token from @BotFather
#    TELEGRAM_CHANNEL_ID = your channel ID (e.g. -1001234567890)
# 4. Railway auto-detects requirements.txt and installs deps
# 5. Copy your Railway public URL, replace RAILWAY_URL_PLACEHOLDER in Tampermonkey

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
import json

app = Flask(__name__)
CORS(app)

# ==========================================
# GLOBAL SETTINGS
# ==========================================
MAX_WORKERS = 16
DOWNLOAD_BASE_DIR = "pw_Downloads"
API_ENDPOINT = "https://download.pwthor.live/api/metadata"
SEGMENT_TIMEOUT = 20
SEGMENT_RETRIES = 3
META_RETRIES = 3

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.environ.get("TELEGRAM_CHANNEL_ID", "")
# ==========================================

state_lock = threading.Lock()
log_lock = threading.Lock()

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

def update_state(**kwargs):
    with state_lock:
        server_state.update(kwargs)

def add_log(msg):
    with log_lock:
        server_state["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        if len(server_state["log"]) > 50:
            server_state["log"] = server_state["log"][-50:]
    print(msg)

# ==========================================
# CURL COOKIE EXTRACTION
# ==========================================
def parse_curl_cookies(curl_cmd):
    normalised = re.sub(r'\^[ \t]*\r?\n', ' ', curl_cmd)
    normalised = re.sub(r'\\[ \t]*\r?\n', ' ', normalised)
    normalised = normalised.replace('^', '')
    normalised = re.sub(r'\s+', ' ', normalised)

    cookie_string = None
    match = re.search(r'-H\s+"cookie:\s*([^"]+)"', normalised, re.IGNORECASE)
    if match:
        cookie_string = match.group(1).strip()

    if not cookie_string:
        match = re.search(r"-H\s+'cookie:\s*([^']+)'", normalised, re.IGNORECASE)
        if match:
            cookie_string = match.group(1).strip()

    if not cookie_string:
        match = re.search(r'(?:--cookie|-b)\s+["\']([^"\']+)["\']', normalised, re.IGNORECASE)
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

# ==========================================
# HEADERS / SESSION HELPERS
# ==========================================
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

def make_session():
    session = requests.Session()
    session.cookies.update(SESSION_COOKIES)
    return session

# ==========================================
# TELEGRAM UPLOAD
# ==========================================
def upload_to_telegram(file_path, caption=""):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHANNEL_ID:
        add_log("[!] Telegram not configured — skipping upload.")
        return False
    try:
        add_log(f"[TG] Uploading to Telegram: {os.path.basename(file_path)}")
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo"
        with open(file_path, 'rb') as f:
            resp = requests.post(url, data={
                "chat_id": TELEGRAM_CHANNEL_ID,
                "caption": caption,
                "supports_streaming": True
            }, files={"video": f}, timeout=300)
        if resp.status_code == 200:
            add_log(f"[TG] Upload success: {os.path.basename(file_path)}")
            return True
        else:
            add_log(f"[TG] Upload failed ({resp.status_code}): {resp.text[:200]}")
            return False
    except Exception as e:
        add_log(f"[TG] Upload error: {e}")
        return False

# ==========================================
# CORE DOWNLOADER
# ==========================================
def post_metadata(session, url):
    last_err = None
    for attempt in range(META_RETRIES):
        try:
            resp = session.post(API_ENDPOINT, json={"url": url}, headers=get_dynamic_headers(url), timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise Exception(f"Metadata fetch failed: {last_err}")

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
        except Exception:
            if attempt < SEGMENT_RETRIES - 1:
                time.sleep(2)
            else:
                return index, None
    return index, None

def custom_hls_downloader(m3u8_url, output_path):
    if not m3u8_url:
        raise Exception("No mpd_url provided.")
    session = make_session()
    data = post_metadata(session, m3u8_url)

    if data.get('type') == 'master':
        qualities = data.get('qualities', [])
        if not qualities:
            raise Exception("Master playlist has no qualities.")
        target_url = qualities[0]['url']
        for quality in qualities:
            if '480' in str(quality.get('label', '')):
                target_url = quality['url']
                break
        data = post_metadata(session, target_url)
        m3u8_url = target_url

    if data.get('type') != 'media':
        raise Exception(f"Failed to resolve media segments (type={data.get('type')}).")

    segments = data['data']['segments']
    key_info = data['data'].get('key')
    total_segments = len(segments)
    headers = get_dynamic_headers(m3u8_url)

    if total_segments == 0:
        raise Exception("No segments found.")

    add_log(f"[*] {total_segments} chunks to download.")
    downloaded_chunks = [None] * total_segments
    start_time = time.time()
    failed = 0

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
            if done % 5 == 0 or done == total_segments:
                pct = int((done / total_segments) * 100)
                update_state(progress=f"{pct}%")
                add_log(f"Progress: {pct}% ({done}/{total_segments} chunks, {failed} failed)")

    add_log("[*] Compiling video file...")
    written = 0
    with open(output_path, 'wb') as outfile:
        for chunk in downloaded_chunks:
            if chunk:
                outfile.write(chunk)
                written += 1

    if written == 0:
        raise Exception("All chunks failed to download.")
    add_log(f"[+] Done: {written}/{total_segments} chunks in {int(time.time() - start_time)}s")

# ==========================================
# BATCH ORCHESTRATOR
# ==========================================
def batch_orchestrator(video_list):
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

        add_log(f">> [{idx+1}/{len(video_list)}] {lec_name}")

        safe_batch = sanitize_filename(video.get('batch', 'Unknown'))
        safe_subj  = sanitize_filename(video.get('subject', 'Unknown'))
        safe_chap  = sanitize_filename(video.get('chapter', 'Unknown'))
        safe_lec   = sanitize_filename(lec_name)

        save_dir = os.path.join(DOWNLOAD_BASE_DIR, safe_batch, safe_subj, safe_chap)
        os.makedirs(save_dir, exist_ok=True)
        output_path = os.path.join(save_dir, f"{safe_lec}.ts")

        mpd_link = video.get('mpd_url', '')
        if mpd_link in ["FAILED_TIMEOUT", "MANUALLY_SKIPPED", "DUPLICATE_ERROR"] or not mpd_link:
            add_log(f"[!] SKIP: {mpd_link} — {safe_lec}")
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
                add_log(f"[*] Deleted local file: {safe_lec}.ts")

            with state_lock:
                server_state["completed_videos"] += 1
                if idx < len(server_state["queue"]):
                    server_state["queue"][idx]["status"] = "done"

        except Exception as e:
            add_log(f"[!] ERROR: {safe_lec} — {e}")
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
    add_log(f"[OK] BATCH DONE in {int(time.time() - batch_start)}s")

# ==========================================
# FLASK ROUTES
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
    add_log(f"[+] Cookies updated ({len(cookies)} cookies loaded)")
    return jsonify({"status": "ok", "count": len(cookies)})

@app.route('/download_batch', methods=['POST'])
def start_batch_download():
    with state_lock:
        if server_state["status"] == "downloading":
            return jsonify({"error": "Server is busy"}), 400
    data = request.get_json(silent=True) or {}
    video_list = data.get('videos', [])
    if not video_list:
        return jsonify({"error": "Empty video list"}), 400
    thread = threading.Thread(target=batch_orchestrator, args=(video_list,))
    thread.daemon = True
    thread.start()
    return jsonify({"status": "Batch Started", "count": len(video_list)})

# ==========================================
# WEB UI
# ==========================================
@app.route('/', methods=['GET'])
def index():
    html = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PW Downloader</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0d0d0d; color: #e0e0e0; font-family: 'Courier New', monospace; font-size: 13px; padding: 20px; }
  h2 { color: #818cf8; font-size: 15px; margin-bottom: 10px; }
  .card { background: #1a1a1a; border: 1px solid #2a2a2a; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  textarea { width: 100%; height: 100px; background: #111; border: 1px solid #333; color: #e0e0e0; border-radius: 6px; padding: 8px; font-family: monospace; font-size: 12px; resize: vertical; }
  button { background: #4f46e5; color: #fff; border: none; border-radius: 6px; padding: 8px 18px; cursor: pointer; font-size: 13px; }
  button:hover { background: #4338ca; }
  .status-bar { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 6px; }
  .stat { color: #94a3b8; }
  .stat span { color: #e0e0e0; font-weight: bold; }
  .progress-wrap { background: #111; border-radius: 4px; height: 10px; margin: 8px 0; overflow: hidden; }
  .progress-bar { height: 10px; background: #4f46e5; transition: width 0.3s; border-radius: 4px; }
  .queue-item { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid #222; }
  .queue-item:last-child { border-bottom: none; }
  .badge { font-size: 11px; padding: 2px 8px; border-radius: 4px; font-weight: bold; min-width: 74px; text-align: center; }
  .badge.waiting   { background: #1e293b; color: #64748b; }
  .badge.downloading { background: #1e3a5f; color: #60a5fa; }
  .badge.uploading { background: #1a2e1a; color: #4ade80; }
  .badge.done      { background: #14532d; color: #86efac; }
  .badge.failed    { background: #450a0a; color: #f87171; }
  .lec-name { flex: 1; color: #cbd5e1; }
  .lec-meta { color: #475569; font-size: 11px; }
  #log { background: #0a0a0a; border: 1px solid #222; border-radius: 6px; padding: 10px; height: 200px; overflow-y: auto; font-size: 11px; color: #6b7280; }
  #log p { margin: 1px 0; line-height: 1.5; }
  #log p.info  { color: #60a5fa; }
  #log p.ok    { color: #4ade80; }
  #log p.err   { color: #f87171; }
  #log p.tg    { color: #a78bfa; }
  #cookie-msg { font-size: 12px; margin-top: 6px; color: #4ade80; display: none; }
  .current-file { color: #fbbf24; font-size: 12px; margin-top: 4px; }
  #queue-container { max-height: 300px; overflow-y: auto; }
</style>
</head>
<body>

<div class="card">
  <h2>⚙️ Cookie Setup</h2>
  <textarea id="curl-input" placeholder="Paste your cURL command here..."></textarea>
  <button onclick="saveCookies()" style="margin-top:8px;">Save Cookies</button>
  <div id="cookie-msg">✅ Cookies saved!</div>
</div>

<div class="card">
  <h2>📊 Status</h2>
  <div class="status-bar">
    <div class="stat">State: <span id="s-status">idle</span></div>
    <div class="stat">Total: <span id="s-total">0</span></div>
    <div class="stat">Done: <span id="s-done">0</span></div>
    <div class="stat">Failed: <span id="s-failed">0</span></div>
  </div>
  <div class="current-file" id="s-current"></div>
  <div class="progress-wrap"><div class="progress-bar" id="s-progress" style="width:0%"></div></div>
  <div style="font-size:11px; color:#475569;" id="s-pct">0%</div>
</div>

<div class="card">
  <h2>📋 Queue</h2>
  <div id="queue-container"><div style="color:#475569;">Waiting for Tampermonkey payload...</div></div>
</div>

<div class="card">
  <h2>📜 Log</h2>
  <div id="log"></div>
</div>

<script>
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
        msg.textContent = '✅ ' + d.count + ' cookies saved!';
        msg.style.color = '#4ade80';
      } else {
        msg.textContent = '❌ ' + (d.error || 'Failed');
        msg.style.color = '#f87171';
      }
      msg.style.display = 'block';
      setTimeout(() => msg.style.display = 'none', 3000);
    });
  }

  let lastLogCount = 0;

  function colorLog(line) {
    if (line.includes('[TG]')) return 'tg';
    if (line.includes('[+]') || line.includes('Done') || line.includes('success')) return 'ok';
    if (line.includes('[!]') || line.includes('ERROR') || line.includes('failed')) return 'err';
    if (line.includes('Progress') || line.includes('>>')) return 'info';
    return '';
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
      document.getElementById('s-current').textContent = d.current_file ? '▶ ' + d.current_file : '';

      if (d.queue && d.queue.length > 0) {
        const qc = document.getElementById('queue-container');
        qc.innerHTML = d.queue.map(item => `
          <div class="queue-item">
            <span class="badge ${item.status}">${item.status}</span>
            <div>
              <div class="lec-name">${item.lecture}</div>
              <div class="lec-meta">${item.subject} › ${item.chapter}</div>
            </div>
          </div>
        `).join('');
      }

      if (d.log && d.log.length > lastLogCount) {
        const logEl = document.getElementById('log');
        const newLines = d.log.slice(lastLogCount);
        newLines.forEach(line => {
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

  setInterval(poll, 2000);
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
    print(f"PW Server starting on port {port}")
    app.run(host='0.0.0.0', port=port, threaded=True)
