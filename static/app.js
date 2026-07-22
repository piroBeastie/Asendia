// Asendia client — duplex voice loop with Gemini Live (via the backend).
//   mic → AudioWorklet (16 kHz PCM) → WS → Gemini
//   Gemini (24 kHz PCM) → WS → gapless Web Audio playback
//   barge-in: on {interrupted} we stop all queued Alex audio instantly.

// ── Elements ──
const startScreen  = document.getElementById('start-screen');
const callScreen   = document.getElementById('call-screen');
const reportScreen = document.getElementById('report-screen');
const startBtn     = document.getElementById('start-btn');
const voiceLine    = document.getElementById('voice-line');
const voicePath    = document.getElementById('voice-path');
const micLine      = document.getElementById('mic-line');
const micPath      = document.getElementById('mic-path');
const statusEl     = document.getElementById('status');
const statusText   = document.getElementById('status-text');
const turnBadge    = document.getElementById('turn-badge');
const micBtn       = document.getElementById('mic-btn');
const micHint      = document.getElementById('mic-hint');
const endBtn       = document.getElementById('end-btn');

// Waveform geometry (matches the SVG viewBoxes in index.html)
const VOICE_W = 340, VOICE_H = 72, VOICE_MID = VOICE_H / 2;
const MIC_W = 92, MIC_H = 22, MIC_MID = MIC_H / 2;
const WAVE_POINTS = 48;
const OUTPUT_RATE = 24000; // Gemini audio out

// ── State ──
let ws = null;
let audioCtx = null;
let micStream = null;
let workletNode = null;
let alexGain = null, alexAnalyser = null, micAnalyser = null;
let alexBuf = null, micBuf = null;

let nextStartTime = 0;              // gapless playback cursor
const scheduled = new Set();        // live AudioBufferSourceNodes (for barge-in flush)

let muted = false;
let uiState = 'connecting';         // connecting | live | scoring | done
let liveSub = '';                   // speaking | listening | thinking | idle
let lastUserActive = 0;

let vizRAF = null;

// ── Noise gate ──
// Only stream mic audio while YOU are actually speaking, so background noise or
// other people's voices never reach the model and derail Alex. Tune SPEAK_ON up
// if background still gets through, or down if soft speech gets cut.
const SPEAK_ON = 0.10;      // mic level (0..1) that opens the gate — you're talking
const SPEAK_OFF = 0.06;     // level below which we start closing (hysteresis)
const GATE_HOLD_MS = 500;   // keep sending this long after you go quiet (word gaps)
const PREROLL = 3;          // ~0.2s of audio kept so your first word isn't clipped
let gateOpen = false;       // also drives the mic waveform (moves only when you speak)
let lastLoud = 0;
const prerollBuf = [];

// ── Startup ──
startBtn.addEventListener('click', startInterview);
micBtn.addEventListener('click', toggleMute);
endBtn.addEventListener('click', endInterview);

async function startInterview() {
  startBtn.disabled = true;
  startBtn.textContent = 'Joining…';
  try {
    await initAudio();               // mic permission + graph
  } catch (e) {
    console.error('Mic/audio init failed:', e);
    startBtn.disabled = false;
    startBtn.textContent = 'Join Interview';
    alert('Microphone access is required. Please allow it and try again.');
    return;
  }

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = handleMessage;
  ws.onclose = () => { if (uiState !== 'done' && uiState !== 'scoring') setCallState('thinking', 'Disconnected'); };
  ws.onerror = () => setCallState('thinking', 'Connection error — refresh');

  startScreen.style.display = 'none';
  callScreen.style.display = 'flex';
  uiState = 'connecting';
  setCallState('thinking', 'Connecting…');
  startVizLoop();
}

// ── Audio graph ──
async function initAudio() {
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (audioCtx.state === 'suspended') await audioCtx.resume();
  await audioCtx.audioWorklet.addModule('/static/pcm-worklet.js');

  // Alex playback chain: sources → gain → analyser → speakers
  alexGain = audioCtx.createGain();
  alexAnalyser = audioCtx.createAnalyser();
  alexAnalyser.fftSize = 512;
  alexAnalyser.smoothingTimeConstant = 0.55;
  alexGain.connect(alexAnalyser);
  alexAnalyser.connect(audioCtx.destination);
  alexBuf = new Uint8Array(alexAnalyser.fftSize);

  // Mic: echo cancellation stops Alex's speaker output from re-triggering barge-in;
  // noise suppression trims ambient hiss; auto-gain is OFF so it doesn't amplify
  // background up to speech level (which would defeat the noise gate).
  micStream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: false },
  });
  const micSource = audioCtx.createMediaStreamSource(micStream);

  micAnalyser = audioCtx.createAnalyser();
  micAnalyser.fftSize = 512;
  micAnalyser.smoothingTimeConstant = 0.6;
  micSource.connect(micAnalyser);
  micBuf = new Uint8Array(micAnalyser.fftSize);

  workletNode = new AudioWorkletNode(audioCtx, 'pcm-worklet');
  micSource.connect(workletNode);
  const sink = audioCtx.createGain();       // keep the worklet in the render graph
  sink.gain.value = 0;
  workletNode.connect(sink);
  sink.connect(audioCtx.destination);

  // Gated send: keep a short rolling pre-roll; only flush + stream while the gate
  // is open (you're speaking). Silence and background stay below the gate and are
  // never sent, so Gemini's VAD can't mistake them for the candidate.
  workletNode.port.onmessage = (e) => {
    if (muted) return;
    const level = micLevel();
    const now = performance.now();
    if (level > SPEAK_ON) { gateOpen = true; lastLoud = now; }
    else if (gateOpen && level > SPEAK_OFF) { lastLoud = now; }        // mid-word dip
    else if (gateOpen && now - lastLoud > GATE_HOLD_MS) { gateOpen = false; }

    prerollBuf.push(e.data);
    if (prerollBuf.length > PREROLL) prerollBuf.shift();

    if (!gateOpen || !ws || ws.readyState !== WebSocket.OPEN) return;
    while (prerollBuf.length) ws.send(prerollBuf.shift());
  };
}

// ── WebSocket messages ──
function handleMessage(event) {
  if (event.data instanceof ArrayBuffer) {
    if (uiState === 'connecting') uiState = 'live';
    playPcm(event.data);
    return;
  }
  let msg;
  try { msg = JSON.parse(event.data); } catch (_) { return; }

  switch (msg.type) {
    case 'turn':
      turnBadge.textContent = `Turn ${msg.n}`;
      if (uiState === 'connecting') uiState = 'live';
      break;
    case 'interrupted':
      flushAlex();                 // candidate barged in — cut Alex off now
      break;
    case 'status':
      if (msg.state === 'scoring') {
        uiState = 'scoring';
        setCallState('thinking', 'Scoring your interview…');
      }
      break;
    case 'report':
      showReport(msg.data);
      break;
    case 'error':
      console.warn('Server:', msg.message);
      setCallState('thinking', msg.message || 'Something went wrong');
      break;
  }
}

// ── Playback (gapless) + barge-in ──
function playPcm(arrayBuffer) {
  const int16 = new Int16Array(arrayBuffer);
  const f32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) f32[i] = int16[i] / 32768;

  const buf = audioCtx.createBuffer(1, f32.length, OUTPUT_RATE);
  buf.copyToChannel(f32, 0);
  const src = audioCtx.createBufferSource();
  src.buffer = buf;
  src.connect(alexGain);

  const now = audioCtx.currentTime;
  if (nextStartTime < now) nextStartTime = now + 0.03;
  src.start(nextStartTime);
  nextStartTime += buf.duration;

  scheduled.add(src);
  src.onended = () => scheduled.delete(src);
}

function flushAlex() {
  for (const s of scheduled) { try { s.stop(); } catch (_) {} }
  scheduled.clear();
  nextStartTime = 0;
}

function alexPlaying() {
  return audioCtx && nextStartTime > audioCtx.currentTime + 0.02;
}

// ── Controls ──
function toggleMute() {
  muted = !muted;
  if (muted) { gateOpen = false; prerollBuf.length = 0; }  // close the gate immediately
  micBtn.classList.toggle('muted', muted);
  micBtn.classList.toggle('live', !muted);
  micLine.classList.toggle('active', !muted);
  micBtn.setAttribute('aria-label', muted ? 'Unmute microphone' : 'Mute microphone');
}

function endInterview() {
  if (uiState === 'scoring' || uiState === 'done') return;
  endBtn.disabled = true;
  uiState = 'scoring';
  setCallState('thinking', 'Wrapping up…');
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'end' }));
}

// ── UI state ──
function setCallState(sub, text) {
  liveSub = sub;
  const lineState = sub === 'speaking' ? 'speaking' : sub === 'thinking' ? 'thinking' : 'idle';
  voiceLine.className = 'voice-line ' + lineState;
  statusEl.className = 'status ' + sub;
  statusText.textContent = text;

  if (muted) { micHint.textContent = 'Muted — tap the mic to rejoin'; return; }
  if (sub === 'speaking')       micHint.textContent = 'Alex is speaking — jump in any time';
  else if (sub === 'listening') micHint.textContent = 'Listening…';
  else                          micHint.textContent = 'Mic on — Alex is listening';
}

// Derive Listening / Thinking / Speaking from live audio each frame.
function updateLiveState() {
  if (uiState !== 'live') return;
  const now = performance.now();
  if (gateOpen) lastUserActive = now;

  let sub, text;
  if (alexPlaying())            { sub = 'speaking';  text = 'Speaking'; }
  else if (muted)               { sub = 'idle';      text = 'Muted'; }
  else if (gateOpen)            { sub = 'listening'; text = 'Listening'; }
  else if (now - lastUserActive < 1400) { sub = 'thinking'; text = 'Thinking'; }
  else                          { sub = 'listening'; text = 'Your turn'; }

  if (sub !== liveSub) setCallState(sub, text);
}

function micLevel() {
  if (!micAnalyser || muted) return 0;
  micAnalyser.getByteTimeDomainData(micBuf);
  let peak = 0;
  for (let i = 0; i < micBuf.length; i++) {
    const v = Math.abs((micBuf[i] - 128) / 128);
    if (v > peak) peak = v;
  }
  return peak;
}

// ── Waveform viz ──
// A smooth traveling wave whose height follows the speaker's loudness. The
// amplitude is EASED (not raw samples), so it swells and settles gently instead
// of jittering — flat when silent, flowing when speaking.
let alexAmp = 0, micAmp = 0;
const AMP_EASE = 0.16;   // lower = smoother/slower, higher = snappier

function rms(analyser, buf) {
  analyser.getByteTimeDomainData(buf);
  let s = 0;
  for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; s += v * v; }
  return Math.sqrt(s / buf.length);   // 0..~1 loudness
}

function smoothWave(pathEl, ampPx, w, mid, t) {
  const n = WAVE_POINTS, step = w / (n - 1);
  let pts = '';
  for (let i = 0; i < n; i++) {
    const x = i / (n - 1);
    const env = Math.sin(x * Math.PI);                                  // ends anchored to baseline
    const wave = Math.sin(x * Math.PI * 4 - t * 0.005)
               + 0.35 * Math.sin(x * Math.PI * 7 - t * 0.008);          // gentle, organic
    pts += (i * step).toFixed(1) + ',' + (mid + wave * ampPx * env).toFixed(1) + ' ';
  }
  pathEl.setAttribute('points', pts.trim());
}

function flatLine(pathEl, w, mid) {
  pathEl.setAttribute('points', `0,${mid} ${w},${mid}`);
}

function vizFrame(t) {
  // Alex's line: height follows his voice, eased. Flat unless he's speaking.
  const alexTarget = (alexPlaying() && alexAnalyser) ? Math.min(rms(alexAnalyser, alexBuf) * 4.5, 1) : 0;
  alexAmp += (alexTarget - alexAmp) * AMP_EASE;
  if (alexAmp > 0.01) smoothWave(voicePath, alexAmp * 20, VOICE_W, VOICE_MID, t);
  else flatLine(voicePath, VOICE_W, VOICE_MID);

  // Mic line: height follows YOUR voice, eased. Flat unless you're speaking.
  const micTarget = (!muted && gateOpen && micAnalyser) ? Math.min(rms(micAnalyser, micBuf) * 4.5, 1) : 0;
  micAmp += (micTarget - micAmp) * AMP_EASE;
  if (micAmp > 0.01) smoothWave(micPath, micAmp * 6.5, MIC_W, MIC_MID, t);
  else flatLine(micPath, MIC_W, MIC_MID);

  updateLiveState();
  vizRAF = requestAnimationFrame(vizFrame);
}

function startVizLoop() { if (!vizRAF) vizRAF = requestAnimationFrame(vizFrame); }
function stopVizLoop() { if (vizRAF) cancelAnimationFrame(vizRAF); vizRAF = null; }

// ── Report ──
function escHtml(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML;
}

function showReport(data) {
  uiState = 'done';
  stopVizLoop();
  try { micStream && micStream.getTracks().forEach((tr) => tr.stop()); } catch (_) {}
  try { audioCtx && audioCtx.close(); } catch (_) {}

  callScreen.style.display = 'none';
  reportScreen.style.display = 'block';

  document.getElementById('report-meta').textContent =
    `${data.role || 'Interview'} · ${data.duration_minutes ?? 0} min · ${data.language_detected || 'en'}`;

  const dims = data.dimensions || {};
  const dimHtml = Object.entries(dims).map(([k, v]) =>
    `<div class="dim"><span class="label">${k.replace(/_/g, ' ')}</span><span class="val">${v}/10</span></div>`
  ).join('');

  const section = (title, arr, cls) => {
    const items = (arr || []).map((a) => `<div class="insight ${cls}">${escHtml(a)}</div>`).join('');
    return items ? `<div class="section-label">${title}</div><div class="insight-list">${items}</div>` : '';
  };

  const action = (data.recommended_action || 'hold').toLowerCase();
  const vc = action === 'advance' ? 'advance' : action === 'reject' ? 'reject' : 'hold';

  document.getElementById('report-card').innerHTML = `
    <div class="score-big"><span class="num">${data.overall_score ?? 0}</span><span class="denom">/10</span></div>
    <div class="dims">${dimHtml}</div>
    ${section('Strengths', data.strong_areas, 'good')}
    ${section('Weak Areas', data.weak_areas, 'weak')}
    ${section('Red Flags', data.red_flags, 'flag')}
    ${section('How to Improve', data.improvement_tips, 'tip')}
    <p class="summary-text">${escHtml(data.summary || '')}</p>
    <div class="verdict ${vc}">${action.toUpperCase()}</div>
  `;
}
