// ==UserScript==
// @name         PW Master Harvester (Strict Verification Edition)
// @namespace    http://tampermonkey.net/
// @version      26.0
// @description  Zero-click MPD catching. Sends to Railway server instead of localhost.
// @match        *://*.pwthor.live/*
// @match        *://*.pw.live/*
// @match        *://rarestudy.in/*
// @match        *://*.rarestudy.in/*
// @grant        GM_setValue
// @grant        GM_getValue
// @grant        GM_setClipboard
// @grant        GM_registerMenuCommand
// @run-at       document-start
// ==/UserScript==

(function () {
    'use strict';

    const CFG = {
        BASE_URL: 'https://pwthor.live',
        // ⚠️ REPLACE THIS with your Railway URL after deploying:
        PYTHON_URL: 'RAILWAY_URL_PLACEHOLDER',
        ADVANCE_DELAY_MS: 1500,
    };

    const STATE_KEY = 'pw_state';
    const QUEUE_KEY = 'pw_watch_queue';
    const HARVEST_KEY = 'pw_harvested_mpds';

    const fetchConfig = { method: 'GET', headers: { 'accept': '*/*' }, credentials: 'include' };

    GM_registerMenuCommand('🚨 Force Reset Script Memory', () => {
        forceReset();
        alert('Memory wiped. Script reset to IDLE.');
    });

    function $(id) { return document.getElementById(id); }
    function setState(s) { GM_setValue(STATE_KEY, s); }
    function getState() { return GM_getValue(STATE_KEY, 'IDLE'); }

    // =========================================================================
    // 1. STRICT MPD HARVESTER (RUNS ON rarestudy.in)
    // =========================================================================
    if (window.location.href.includes('rarestudy.in')) {
        const state = getState();
        if (state !== 'HARVESTING') return;

        let queue = GM_getValue(QUEUE_KEY, []);
        if (queue.length === 0) {
            setState('HANDOFF');
            window.top.location.href = CFG.BASE_URL + '/';
            return;
        }

        const expectedJob = queue[0];
        const currentUrl = window.location.href;

        const getParam = (urlStr, param) => {
            try { return new URL(urlStr).searchParams.get(param); } catch(e) { return null; }
        };

        const currentId = getParam(currentUrl, 'scheduleId') || getParam(currentUrl, 'ChildId');
        const expectedId = getParam(expectedJob.watch_url, 'scheduleId') || getParam(expectedJob.watch_url, 'ChildId');

        if (currentId && expectedId && currentId !== expectedId) {
            console.warn(`[SYNC ERROR] Expected video ${expectedId} but page loaded ${currentId}. Forcing redirect...`);
            window.top.location.href = expectedJob.watch_url;
            return;
        }

        let watchdogTimer = null;
        let manifestFound = false;

        document.addEventListener('PW_SKIP_VIDEO', () => {
            if (manifestFound) return;
            manifestFound = true;
            clearTimeout(watchdogTimer);
            advanceHarvester('MANUALLY_SKIPPED');
        });

        document.addEventListener("DOMContentLoaded", () => {
            if (window !== window.top) return;

            const ui = document.createElement('div');
            ui.style.cssText = `position: fixed; top: 10px; left: 50%; transform: translateX(-50%); background: #eab308; color: black; padding: 6px 12px; border-radius: 6px; z-index: 9999999; font-weight: bold; font-family: monospace; font-size: 12px; box-shadow: 0 4px 10px rgba(0,0,0,0.5); border: 2px solid #ca8a04; display: flex; gap: 10px; align-items: center;`;
            ui.innerHTML = `
                <span id="pw-harvest-status">⚡ Target: ${expectedJob.lecture.substring(0,25)}... (${queue.length - 1} left)</span>
                <button id="pw-skip-btn" style="background:#ef4444; color:white; border:none; border-radius:4px; padding:4px 8px; cursor:pointer; font-weight:bold;">Skip</button>
            `;
            document.body.appendChild(ui);

            document.getElementById('pw-skip-btn').addEventListener('click', () => {
                document.dispatchEvent(new CustomEvent('PW_SKIP_VIDEO'));
            });

            watchdogTimer = setTimeout(() => {
                if (!manifestFound) {
                    advanceHarvester("FAILED_TIMEOUT");
                }
            }, 15000);
        });

        const observer = new PerformanceObserver((list) => {
            list.getEntries().forEach(entry => {
                const url = entry.name;
                if (url && (url.includes('.mpd') || url.includes('.m3u8')) && !manifestFound) {
                    manifestFound = true;
                    clearTimeout(watchdogTimer);
                    const m3u8Url = url.replace('.mpd', '.m3u8');

                    const statusEl = document.getElementById('pw-harvest-status');
                    if (statusEl) statusEl.innerText = "✅ Link Caught! Moving to next...";

                    advanceHarvester(m3u8Url);
                }
            });
        });
        observer.observe({ entryTypes: ['resource'] });

        function advanceHarvester(finalUrl) {
            if (getState() !== 'HARVESTING') return;
            setState('NAVIGATING');

            const finishedJob = queue.shift();
            const collected = GM_getValue(HARVEST_KEY, []);

            if (finalUrl !== "FAILED_TIMEOUT" && finalUrl !== "MANUALLY_SKIPPED") {
                const isDuplicate = collected.some(item => item.mpd_url === finalUrl);
                if (isDuplicate) {
                    finishedJob.mpd_url = "DUPLICATE_ERROR";
                } else {
                    finishedJob.mpd_url = finalUrl;
                }
                collected.push(finishedJob);
            } else {
                finishedJob.mpd_url = finalUrl;
                collected.push(finishedJob);
            }

            GM_setValue(QUEUE_KEY, queue);
            GM_setValue(HARVEST_KEY, collected);

            setTimeout(() => {
                if (queue.length > 0) {
                    setState('HARVESTING');
                    window.top.location.href = queue[0].watch_url;
                } else {
                    setState('HANDOFF');
                    window.top.location.href = CFG.BASE_URL + '/';
                }
            }, CFG.ADVANCE_DELAY_MS);
        }
        return;
    }

    // =========================================================================
    // 2. DASHBOARD INJECTOR (Runs on pwthor.live)
    // =========================================================================
    if (window.location.href.includes('pwthor.live') && !window.location.href.includes('/watch')) {

        setInterval(() => {
            if (document.body && !$('pw-permanent-dashboard')) {
                injectDashboard();
            } else if ($('pw-permanent-dashboard')) {
                updateLiveCounters();
            }
        }, 500);

        function injectDashboard() {
            const dash = document.createElement('div');
            dash.id = 'pw-permanent-dashboard';
            dash.style.cssText = `
                position: fixed; top: 20px; right: 20px; width: 380px; max-height: 85vh; overflow-y: auto;
                background: #0f1115; border: 2px solid #4f46e5; z-index: 2147483647;
                padding: 18px; font-family: monospace; color: #fff;
                box-shadow: 0px 10px 50px rgba(0,0,0,0.9); border-radius: 12px;
            `;
            document.body.appendChild(dash);
            renderUIState();
        }

        function updateLiveCounters() {
            const state = getState();
            if (state === 'HARVESTING' || state === 'NAVIGATING') {
                const qCount = GM_getValue(QUEUE_KEY, []).length;
                const hCount = GM_getValue(HARVEST_KEY, []).length;
                const qEl = $('pw-live-queue');
                const hEl = $('pw-live-harvested');
                if (qEl) qEl.innerText = qCount;
                if (hEl) hEl.innerText = hCount;
            }
        }

        function renderUIState() {
            const dash = $('pw-permanent-dashboard');
            if (!dash) return;
            const state = getState();

            if (state === 'IDLE') {
                dash.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 10px;">
                        <h3 style="margin:0; color:#818cf8; font-size:16px;">PW Harvester → Railway</h3>
                        <button id="pw-hard-reset" style="background:#ef4444; color:#fff; border:none; border-radius:4px; padding:4px 8px; font-weight:bold; cursor:pointer; font-size:10px;">RESET</button>
                    </div>
                    <input type="text" id="tm-batch-id" placeholder="Enter Batch ID..." style="width: 100%; padding: 10px; margin-bottom: 10px; background: #1e293b; border: 1px solid #475569; color: #fff; border-radius: 6px; box-sizing: border-box; outline: none;">
                    <button id="tm-sync-btn" style="width: 100%; padding: 12px; background: #4f46e5; color: #fff; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; margin-bottom: 10px;">1. Sync Entire Batch</button>
                    <div id="tm-status" style="font-size: 12px; color: #94a3b8; text-align: center; margin-bottom: 10px; font-weight: bold;">Waiting for input...</div>
                    <div id="tm-tree-container" style="background: #1e293b; padding: 10px; border-radius: 6px; display: none; max-height: 300px; overflow-y: auto; margin-bottom: 10px; border: 1px solid #334155;"></div>
                    <button id="tm-execute-btn" style="width: 100%; padding: 12px; background: #10b981; color: #111827; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; display: none;">2. Start Unlimited Harvest</button>
                `;
                $('pw-hard-reset').addEventListener('click', forceReset);
                $('tm-sync-btn').addEventListener('click', startSync);
                $('tm-execute-btn').addEventListener('click', startHarvesting);
            }
            else if (state === 'HARVESTING' || state === 'NAVIGATING') {
                const queue = GM_getValue(QUEUE_KEY, []);
                const harvested = GM_getValue(HARVEST_KEY, []);
                dash.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 10px;">
                        <h3 style="margin:0; color:#eab308; font-size:16px;">⚡ Harvesting Links...</h3>
                    </div>
                    <div style="font-size: 12px; color: #94a3b8; margin-bottom: 15px;">Strict URL matching active. Do not close tab.</div>
                    <div style="background: #1e293b; padding: 10px; border-radius: 6px; border: 1px solid #333; margin-bottom: 15px; font-size: 14px;">
                        <div style="margin-bottom: 6px;"><b>Remaining in Queue:</b> <span id="pw-live-queue" style="color: #3b82f6; font-weight: bold;">${queue.length}</span></div>
                        <div><b>Links Caught:</b> <span id="pw-live-harvested" style="color: #10b981; font-weight: bold;">${harvested.length}</span></div>
                    </div>
                    <button id="tm-stop-send-btn" style="width: 100%; padding: 14px; background: #ef4444; color: #fff; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 14px;">⏹️ STOP & SEND PAYLOAD</button>
                `;
                $('tm-stop-send-btn').addEventListener('click', () => {
                    GM_setValue(QUEUE_KEY, []);
                    setState('HANDOFF');
                    renderUIState();
                });
            }
            else if (state === 'HANDOFF') {
                const harvested = GM_getValue(HARVEST_KEY, []);
                dash.innerHTML = `
                    <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #333; padding-bottom: 10px; margin-bottom: 10px;">
                        <h3 style="margin:0; color:#10b981; font-size:16px;">Harvest Complete!</h3>
                        <button id="pw-hard-reset" style="background:#ef4444; color:#fff; border:none; border-radius:4px; padding:4px 8px; font-weight:bold; cursor:pointer; font-size:10px;">RESET</button>
                    </div>
                    <div style="font-size: 12px; color: #94a3b8; margin-bottom: 15px;">Collected ${harvested.length} links.</div>
                    <div id="tm-python-status" style="margin-top: 15px; font-size: 13px; color: #fbbf24; text-align: center; font-weight: bold;">Auto-sending to Railway server...</div>
                    <button id="tm-retry-btn" style="display: none; width: 100%; padding: 10px; background: #3b82f6; color: #fff; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; margin-top: 15px;">Retry Connection</button>
                `;
                $('pw-hard-reset').addEventListener('click', forceReset);
                $('tm-retry-btn').addEventListener('click', sendBulkToPython);

                if (!window.autoHandoffTriggered) {
                    window.autoHandoffTriggered = true;
                    setTimeout(sendBulkToPython, 1000);
                }
            }
        }

        // ==========================================
        // 3. TREE SYNC LOGIC
        // ==========================================
        let batchDataTree = {};
        let batchNameGlobal = "Unknown Batch";

        async function startSync() {
            const batchId = $('tm-batch-id').value.trim();
            if (!batchId) return;

            $('tm-sync-btn').disabled = true;
            const statusEl = $('tm-status');
            batchDataTree = {};

            try {
                statusEl.innerText = "Fetching Metadata...";
                const res = await fetch(`https://pwthor.live/api/BatchInfo?BatchId=${batchId}&Type=details`, fetchConfig);
                const json = await res.json();
                if (!json.data || !json.data.subjects) throw new Error("No subjects found.");

                batchNameGlobal = json.data.name || "Unknown Batch";

                for (const sub of json.data.subjects) {
                    statusEl.innerText = `Syncing Subject: ${sub.subject}...`;
                    batchDataTree[sub.subject] = {};
                    let page = 1, hasMore = true;

                    while (hasMore) {
                        const tRes = await fetch(`https://pwthor.live/api/TopicInfo?BatchId=${batchId}&SubjectId=${sub.slug}&TopicId=all&ContentType=videos&page=${page}`, fetchConfig);
                        const tJson = await tRes.json();

                        if (tJson.data && tJson.data.length > 0) {
                            tJson.data.forEach(lec => {
                                let chapter = "Uncategorized";
                                if (lec.tags && lec.tags.length > 0) chapter = lec.tags[0].name;
                                if (!batchDataTree[sub.subject][chapter]) batchDataTree[sub.subject][chapter] = [];

                                const subjectId = sub._id || sub.slug;
                                const scheduleId = lec._id;
                                const locahostUrl = `https://rarestudy.in/schedule-details?batchId=${batchId}&subjectId=${subjectId}&scheduleId=${scheduleId}&tap=video`;

                                batchDataTree[sub.subject][chapter].push({
                                    batch: batchNameGlobal,
                                    subject: sub.subject,
                                    chapter: chapter,
                                    lecture: lec.topic,
                                    watch_url: locahostUrl
                                });
                            });
                            page++;
                        } else { hasMore = false; }
                    }
                }
                statusEl.innerText = "✅ Sync Complete! Select lectures below.";
                renderTree();
            } catch (err) {
                statusEl.innerText = "Error: " + err.message;
            } finally {
                $('tm-sync-btn').disabled = false;
            }
        }

        function renderTree() {
            const container = $('tm-tree-container');
            container.innerHTML = "";
            container.style.display = "block";
            $('tm-execute-btn').style.display = "block";

            for (const [subjName, chapters] of Object.entries(batchDataTree)) {
                const subDetails = document.createElement('details');
                subDetails.innerHTML = `<summary style="cursor: pointer; font-weight: bold; color: #818cf8; padding: 4px; background: #000; border-radius: 4px;"><input type="checkbox" class="subj-cb" data-subj="${subjName}"> 📁 ${subjName}</summary>`;
                const chapContainer = document.createElement('div');
                chapContainer.style.paddingLeft = "20px";

                for (const [chapName, lectures] of Object.entries(chapters)) {
                    const chapDetails = document.createElement('details');
                    chapDetails.innerHTML = `<summary style="cursor: pointer; color: #eab308; padding: 2px;"><input type="checkbox" class="chap-cb" data-subj="${subjName}" data-chap="${chapName}"> 📄 ${chapName}</summary>`;
                    const lecContainer = document.createElement('div');
                    lecContainer.style.paddingLeft = "24px";

                    lectures.forEach((lec, idx) => {
                        const lecDiv = document.createElement('div');
                        lecDiv.innerHTML = `<label style="cursor: pointer; color: #cbd5e1;"><input type="checkbox" class="lec-cb" data-subj="${subjName}" data-chap="${chapName}" data-idx="${idx}"> 🎬 ${lec.lecture}</label>`;
                        lecContainer.appendChild(lecDiv);
                    });
                    chapDetails.appendChild(lecContainer);
                    chapContainer.appendChild(chapDetails);
                }
                subDetails.appendChild(chapContainer);
                container.appendChild(subDetails);
            }

            document.querySelectorAll('.subj-cb').forEach(cb => cb.addEventListener('change', (e) => {
                const subj = e.target.getAttribute('data-subj');
                document.querySelectorAll(`.chap-cb[data-subj="${subj}"], .lec-cb[data-subj="${subj}"]`).forEach(c => c.checked = e.target.checked);
            }));
            document.querySelectorAll('.chap-cb').forEach(cb => cb.addEventListener('change', (e) => {
                const subj = e.target.getAttribute('data-subj');
                const chap = e.target.getAttribute('data-chap');
                document.querySelectorAll(`.lec-cb[data-subj="${subj}"][data-chap="${chap}"]`).forEach(c => c.checked = e.target.checked);
            }));
        }

        async function startHarvesting() {
            let executionQueue = [];
            document.querySelectorAll('.lec-cb:checked').forEach(cb => {
                const subj = cb.getAttribute('data-subj');
                const chap = cb.getAttribute('data-chap');
                const idx = cb.getAttribute('data-idx');
                executionQueue.push(batchDataTree[subj][chap][idx]);
            });

            if (executionQueue.length === 0) return alert("Select at least one lecture.");

            try {
                const pingRes = await fetch(`${CFG.PYTHON_URL}/ping`);
                if (!pingRes.ok) throw new Error();
            } catch (e) {
                return alert("Connection Failed! Is the Railway server running?");
            }

            GM_setValue(QUEUE_KEY, executionQueue);
            GM_setValue(HARVEST_KEY, []);
            setState('HARVESTING');

            window.top.location.href = executionQueue[0].watch_url;
        }

        // ==========================================
        // 4. RAILWAY SERVER HANDOFF
        // ==========================================
        async function sendBulkToPython() {
            const statusEl = $('tm-python-status');
            const retryBtn = $('tm-retry-btn');
            const harvested = GM_getValue(HARVEST_KEY, []);

            if (harvested.length === 0) {
                statusEl.innerText = "No links were caught! Aborting.";
                setTimeout(forceReset, 3000);
                return;
            }

            if (retryBtn) retryBtn.style.display = "none";
            statusEl.innerText = "Sending Payload to Railway server...";

            try {
                const res = await fetch(`${CFG.PYTHON_URL}/download_batch`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ videos: harvested })
                });

                if (res.ok) {
                    pollPythonProgress();
                } else { throw new Error("Server rejected payload."); }

            } catch (e) {
                statusEl.innerText = "❌ Connection Failed! Check Railway server is running.";
                if (retryBtn) retryBtn.style.display = "block";
            }
        }

        function pollPythonProgress() {
            const statusEl = $('tm-python-status');
            const pollInterval = setInterval(async () => {
                try {
                    const res = await fetch(`${CFG.PYTHON_URL}/status`);
                    const st = await res.json();

                    if (st.status === 'completed') {
                        clearInterval(pollInterval);
                        statusEl.innerText = "🎉 ALL DONE! Check your Telegram channel.";
                        statusEl.style.color = "#10b981";
                        setTimeout(forceReset, 4000);
                    } else {
                        statusEl.innerText = `Downloading ${st.current_file}\n${st.progress} (${st.completed_videos}/${st.total_videos} done)`;
                    }
                } catch (e) {
                    statusEl.innerText = "Server busy... waiting to reconnect.";
                }
            }, 3000);
        }

        function forceReset() {
            setState('IDLE');
            GM_setValue(QUEUE_KEY, []);
            GM_setValue(HARVEST_KEY, []);
            window.location.reload();
        }
    }
})();
