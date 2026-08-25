/* ============================================================
   Engineering Quiz — Multi-subject real-time multiplayer quiz
   Extended host flow: subject → saved test → lobby/game.
   ============================================================ */

const SHAPES = ['◆', '●', '▲', '■'];
const COLORS = ['color-0', 'color-1', 'color-2', 'color-3'];
const TIME_PER_Q = 30;

const WS_PROTOCOL = location.protocol === 'https:' ? 'wss:' : 'ws:';
const API_BASE = location.origin;

// Student identity is issued and signed by the server. The browser only stores
// the token; it cannot choose its own id, so it cannot claim someone else's
// session or score.
const VISITOR_TOKEN_KEY = 'quiz_visitor_token';
let visitorToken = '';

function readStoredVisitorToken() {
  try {
    return localStorage.getItem(VISITOR_TOKEN_KEY) || '';
  } catch (e) {
    return '';
  }
}

async function ensureVisitorToken({ attempts = 3 } = {}) {
  if (visitorToken) return visitorToken;
  const stored = readStoredVisitorToken();
  // Without a token the server hands out a fresh anonymous id on every
  // connection, so a student's identity churns and a reconnect looks like a
  // stranger arriving. Worth retrying: the free tier can be slow to wake.
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const resp = await fetch(`${API_BASE}/api/visitor-token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ token: stored })
      });
      if (resp.ok) {
        const data = await resp.json();
        visitorToken = data.token || '';
        try { localStorage.setItem(VISITOR_TOKEN_KEY, visitorToken); } catch (e) {}
        return visitorToken;
      }
    } catch (e) {
      console.warn(`Could not obtain a visitor token (attempt ${attempt + 1})`, e);
    }
    if (attempt < attempts - 1) {
      await new Promise((resolve) => setTimeout(resolve, 400 * (attempt + 1)));
    }
  }
  // Keep whatever we had rather than nothing: a stored token still identifies
  // this student even if refreshing it failed.
  visitorToken = stored;
  return visitorToken;
}

function buildWsUrl() {
  const suffix = visitorToken ? `?vt=${encodeURIComponent(visitorToken)}` : '';
  return `${WS_PROTOCOL}//${location.host}/ws${suffix}`;
}

function normalizeSubjectCode(value) {
  return (value || '').trim().toUpperCase();
}

function isValidSubjectCode(code) {
  return /^[A-Z0-9]{3,10}$/.test(code);
}

function isValidSubjectName(name) {
  const cleaned = (name || '').trim();
  return cleaned.length >= 2 && cleaned.length <= 60;
}

const $ = (sel) => document.querySelector(sel);
let ws = null;
let wsAllowReconnect = true;
let wsOnOpen = null;
let wsReconnectTimer = null;
let wsPingTimer = null;
let isHost = false;
let myPlayerId = null;
let myPlayerName = '';
let myStudentNumber = '';
let timerInterval = null;
let hostTimerInterval = null;
let timeLeft = TIME_PER_Q;
let wakeLock = null;
let selectedSubject = null;
let selectedTest = null;
let sessionName = '';
let sessionToken = '';
let hostGameCode = '';
let hostGameCodeCountdownInterval = null;
let storageInfo = null;
let lecturerSession = null;
let hostSubjectCode = null;
let editorMode = 'create';
let editingTestId = null;
let draftDirty = false;
let draftSaveTimer = null;
let currentDraftLoaded = null;
let originalEditingTest = null;
let authUiBound = false;
let editorInputBound = false;
let hostCorrectAnswer = -1;
let hostCurrentOptions = [];
let hostCurrentQuestion = '';
// The time limit the server set for the question currently on screen. Used for
// both the countdown value and the progress-bar percentage — computing the
// percentage against the TIME_PER_Q constant instead made a 90 second question
// render a bar 300% wide once limits became configurable.
let questionTimeLimit = TIME_PER_Q;
let hostQuestionTimeLimit = TIME_PER_Q;
let hostTimeLeft = TIME_PER_Q;
let playerAnswered = false;
let playerNeedsGameCode = false;
let statsAutoDownloaded = false;

const SUBJECT_COLORS = {
  MEC105B: { bg: 'var(--accent-blue)', icon: '⚙️' },
  '1EM105B': { bg: 'var(--accent-purple)', icon: '🔧' },
  DYN317B: { bg: 'var(--accent-orange)', icon: '🚀' }
};
const DEFAULT_SUBJECT_COLOR = { bg: 'var(--accent-green)', icon: '📚' };
const BUILTIN_SUBJECT_CODES = new Set(Object.keys(SUBJECT_COLORS));

function showScreen(id) {
  const leavingEditor = id !== 'screen-host-create-test'
    && document.querySelector('#screen-host-create-test.active');
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
  const el = $(`#${id}`);
  if (el) el.classList.add('active');
  // Countdown intervals belong to the screen that started them; a screen change
  // mid-countdown used to leave them running forever.
  clearScreenIntervals();
  // A queued autosave must never fire after the lecturer has navigated away and
  // re-save a stale form over newer state.
  if (leavingEditor && typeof flushPendingDraftSave === 'function') flushPendingDraftSave();
}

// Intervals owned by whichever screen is showing, cleared on every change.
let readyCountdownInterval = null;
let revealCountdownInterval = null;

function clearScreenIntervals() {
  if (readyCountdownInterval) {
    clearInterval(readyCountdownInterval);
    readyCountdownInterval = null;
  }
  if (revealCountdownInterval) {
    clearInterval(revealCountdownInterval);
    revealCountdownInterval = null;
  }
}

function connectWS(onOpen) {
  if (onOpen) wsOnOpen = onOpen;
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
  wsAllowReconnect = true;

  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    if (ws.readyState === WebSocket.OPEN && wsOnOpen) wsOnOpen();
    return;
  }

  ws = new WebSocket(buildWsUrl());
  ws.onopen = () => {
    startWsPing();
    if (wsOnOpen) wsOnOpen();
  };
  ws.onmessage = (evt) => {
    // One malformed frame used to throw out of the handler and kill it for the
    // rest of the session.
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch (err) {
      console.error('Ignoring malformed message from server:', err);
      return;
    }
    try {
      handleMessage(msg);
    } catch (err) {
      console.error('Error handling message', msg && msg.type, err);
    }
  };
  ws.onclose = () => {
    ws = null;
    stopWsPing();
    if (wsAllowReconnect) {
      wsReconnectTimer = setTimeout(() => connectWS(), 3000);
    }
  };
  ws.onerror = () => {};
}

function startWsPing() {
  stopWsPing();
  wsPingTimer = setInterval(() => {
    send({ action: 'ping' });
  }, 25000);
}

function stopWsPing() {
  if (wsPingTimer) {
    clearInterval(wsPingTimer);
    wsPingTimer = null;
  }
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}

function closeWS({ reconnect = false } = {}) {
  wsAllowReconnect = reconnect;
  if (wsReconnectTimer) {
    clearTimeout(wsReconnectTimer);
    wsReconnectTimer = null;
  }
  if (ws) {
    try {
      ws.onclose = null;
      ws.close();
    } catch (e) {}
    ws = null;
  }
  stopWsPing();
  wsOnOpen = null;
}

function showPlayerJoinError(message, { expired = false } = {}) {
  const text = message || 'Could not join this session.';
  const joinBtn = $('#btn-join');
  if (joinBtn) {
    joinBtn.disabled = false;
    joinBtn.textContent = 'Join Game';
  }
  const errEl = $('#name-error');
  if (errEl) {
    errEl.textContent = text;
    errEl.hidden = expired;
  }
  if (expired) {
    sessionToken = '';
    closeWS({ reconnect: false });
    showScreen('screen-token-expired');
    const expiredMsg = $('#token-expired-msg');
    if (expiredMsg) expiredMsg.textContent = text;
    return;
  }
  const codeScreen = $('#screen-game-code');
  if (codeScreen && codeScreen.classList.contains('active')) {
    showGameCodeError(text);
    return;
  }
  showScreen('screen-join');
}

function showGameCodeError(message) {
  const text = message || 'Incorrect code.';
  const errEl = $('#game-code-error');
  if (errEl) {
    errEl.textContent = text;
    errEl.hidden = false;
  }
  const btn = $('#btn-submit-game-code');
  if (btn) {
    btn.disabled = false;
    btn.textContent = myPlayerId ? 'Continue' : 'Join Game';
  }
}

function getGameCodeSubmitLabel() {
  return myPlayerId ? 'Continue' : 'Join Game';
}

function ensureGameCodeScreen() {
  const codeScreen = $('#screen-game-code');
  if (!codeScreen || !codeScreen.classList.contains('active')) {
    showGameCodeScreen();
  }
}

function clearHostGameCodeCountdown() {
  if (hostGameCodeCountdownInterval) {
    clearInterval(hostGameCodeCountdownInterval);
    hostGameCodeCountdownInterval = null;
  }
}

function setHostGameCode(code = '') {
  hostGameCode = code || '';
  document.querySelectorAll('.host-game-code-persistent-value').forEach((el) => {
    el.textContent = hostGameCode;
  });
  document.querySelectorAll('.host-game-code-bar').forEach((bar) => {
    bar.hidden = !hostGameCode;
  });
}

function handleMessage(msg) {
  if (msg.type === 'pong') return;
  if (msg.type === 'auth_required') {
    lecturerSession = null;
    updateHostAccountBar();
    showHostAuthScreen('login', msg.message || 'Please sign in as a lecturer.');
    return;
  }
  if (msg.type === 'error') {
    if (isHost) {
      showInlineStatus('#host-library-status', msg.message, true);
      showInlineStatus('#host-create-status', msg.message, true);
    } else {
      const message = msg.message || 'Could not join this session.';
      const expired = /current qr code|session link|session has already started/i.test(message);
      showPlayerJoinError(message, { expired });
    }
    console.error('Server error:', msg.message);
    return;
  }
  if (isHost) {
    handleHostMessage(msg);
  } else {
    handlePlayerMessage(msg);
  }
}

function showInlineStatus(selector, text, isError = false) {
  const el = selector.startsWith('#') || selector.startsWith('.') || selector.startsWith('[')
    ? $(selector)
    : document.getElementById(selector);
  if (!el) return;
  el.textContent = text || '';
  el.hidden = !text;
  el.classList.toggle('error-text', !!isError);
  el.classList.toggle('success-text', !isError && !!text);
}

async function parseApiResponse(resp) {
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item.msg || JSON.stringify(item)).join(' ')
      : (data.detail || data.error || 'Request failed');
    const err = new Error(detail);
    err.status = resp.status;
    err.payload = data;
    throw err;
  }
  return data;
}

async function apiGet(path) {
  const resp = await fetch(`${API_BASE}${path}`, { credentials: 'same-origin' });
  return await parseApiResponse(resp);
}

async function apiPost(path, payload = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(payload)
  });
  return await parseApiResponse(resp);
}

async function apiPut(path, payload = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'same-origin',
    body: JSON.stringify(payload)
  });
  return await parseApiResponse(resp);
}

async function apiDelete(path) {
  const resp = await fetch(`${API_BASE}${path}`, { method: 'DELETE', credentials: 'same-origin' });
  return await parseApiResponse(resp);
}

function getSubjectMetaText(sub) {
  if (typeof sub.testCount === 'number') {
    return sub.testCount === 1 ? '1 saved test' : `${sub.testCount} saved tests`;
  }
  return sub.questionCount > 0 ? `${sub.questionCount} questions` : 'No questions yet';
}

function createSubjectCardButton(sub, onSelect) {
  const colors = SUBJECT_COLORS[sub.code] || DEFAULT_SUBJECT_COLOR;
  const card = document.createElement('button');
  card.type = 'button';
  card.className = 'subject-card';
  card.style.setProperty('--card-accent', colors.bg);
  card.innerHTML = `
    <span class="subject-icon">${colors.icon}</span>
    <div class="subject-info">
      <span class="subject-name">${escapeHtml(sub.name)}</span>
      <span class="subject-code-label">${escapeHtml(sub.code)}</span>
      <span class="subject-q-count">${escapeHtml(getSubjectMetaText(sub))}</span>
    </div>
    <span class="subject-arrow">&rarr;</span>
  `;
  card.addEventListener('click', () => onSelect(sub));
  return card;
}

function renderSubjectCards(containerId, subjects, onSelect) {
  const container = $(`#${containerId}`);
  container.innerHTML = '';
  subjects.forEach((sub) => {
    container.appendChild(createSubjectCardButton(sub, onSelect));
  });
}

function renderHostSubjectCards(subjects, onSelect) {
  const container = $('#host-subject-list');
  if (!container) return;
  container.innerHTML = '';
  showInlineStatus('#host-subject-status', '', false);
  subjects.forEach((sub) => {
    container.appendChild(createSubjectCardButton(sub, onSelect));
  });
}

async function loadSubjects() {
  try {
    return await apiGet('/api/subjects');
  } catch (e) {
    console.error(e);
    return [
      { code: 'MEC105B', name: 'Mechanics', questionCount: 15, testCount: 1 },
      { code: '1EM105B', name: 'Mechanics', questionCount: 0, testCount: 0 },
      { code: 'DYN317B', name: 'Dynamics', questionCount: 0, testCount: 0 }
    ];
  }
}

async function loadTests(subjectCode) {
  return await apiGet(`/api/tests/${encodeURIComponent(subjectCode)}`);
}

async function loadStorageStatus() {
  try {
    storageInfo = await apiGet('/api/storage-status');
  } catch (e) {
    storageInfo = { mode: 'unknown', supabaseConfigured: false, note: 'Could not load storage status.' };
  }
  return storageInfo;
}

function getPrefilledSubjectFromURL(subjects) {
  const params = new URLSearchParams(location.search);
  const code = params.get('subject');
  if (!code) return null;
  return subjects.find((sub) => sub.code === code) || null;
}

function formatActiveTestLabel(subject, activeTest) {
  if (!subject) return '';
  if (!activeTest || !activeTest.title) return `${subject.name} (${subject.code})`;
  const chapter = activeTest.chapter ? ` — ${activeTest.chapter}` : '';
  return `${subject.name} (${subject.code}) • ${activeTest.title}${chapter}`;
}

// ════════════════════════════════════════════════════════════
// PLAYER
// ════════════════════════════════════════════════════════════

async function initPlayer() {
  isHost = false;
  sessionToken = '';
  const params = new URLSearchParams(location.search);
  const token = (params.get('token') || '').trim().toUpperCase();

  if (token) {
    showScreen('screen-token-loading');
    try {
      const result = await apiGet(`/api/session-token/${encodeURIComponent(token)}/validate`);
      selectedSubject = { code: result.subject_code, name: result.subject_name };
      sessionToken = token;
      showPlayerJoinScreen();
    } catch (e) {
      // Token invalid or expired — try to recover using the last known subject
      selectedSubject = null;
      sessionToken = '';
      let recovered = false;
      try {
        const cachedCode = localStorage.getItem('quiz_last_subject_code');
        if (cachedCode) {
          const subjects = await loadSubjects();
          const cached = subjects.find((s) => s.code === cachedCode);
          if (cached) {
            selectedSubject = cached;
            recovered = true;
            showPlayerJoinScreen();
            const errEl = $('#name-error');
            if (errEl) {
              errEl.textContent = 'Your session link has changed — please enter your details to rejoin.';
              errEl.hidden = false;
            }
          }
        }
      } catch (loadErr) {}
      if (!recovered) {
        // Fall back to subject selection instead of a dead-end expired screen
        showScreen('screen-subject');
        try {
          const subjects = await loadSubjects();
          renderSubjectCards('subject-list', subjects, (sub) => {
            selectedSubject = sub;
            sessionToken = '';
            showPlayerJoinScreen();
          });
        } catch (loadErr) {
          showScreen('screen-token-expired');
          $('#token-expired-msg').textContent = e.message || 'This session link has expired. Ask your lecturer for the current QR code.';
        }
      }
    }
    return;
  }

  showScreen('screen-subject');
  const subjects = await loadSubjects();
  renderSubjectCards('subject-list', subjects, (sub) => {
    selectedSubject = sub;
    sessionToken = '';
    showPlayerJoinScreen();
  });

  const preselected = getPrefilledSubjectFromURL(subjects);
  if (preselected) {
    selectedSubject = preselected;
    sessionToken = '';
    showPlayerJoinScreen();
  }
}

function showPlayerJoinScreen() {
  playerNeedsGameCode = false;
  showScreen('screen-join');
  $('#join-subject-title').textContent = selectedSubject.name;
  $('#join-subject-code').textContent = selectedSubject.code;
  const hint = $('#join-test-hint');
  if (hint) {
    hint.textContent = 'Your lecturer will choose the active test for this subject.';
  }

  const nameInput = $('#nickname-input');
  const numInput = $('#student-number-input');
  const btn = $('#btn-join');
  const errEl = $('#name-error');

  nameInput.value = '';
  numInput.value = '';
  btn.disabled = true;
  btn.textContent = 'Join Game';
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = '';
  }

  function checkReady() {
    const ready = !!(nameInput.value.trim() && numInput.value.trim());
    btn.disabled = !ready;
    btn.setAttribute('aria-disabled', String(!ready));
  }

  const bindCheck = (el) => {
    ['input', 'change', 'keyup', 'blur'].forEach((evt) => {
      el.addEventListener(evt, checkReady);
    });
  };

  bindCheck(nameInput);
  bindCheck(numInput);

  nameInput.onkeydown = (e) => {
    if (e.key === 'Enter') numInput.focus();
  };
  numInput.onkeydown = (e) => {
    if (e.key === 'Enter' && nameInput.value.trim() && numInput.value.trim()) {
      joinAsPlayer();
    }
  };
  btn.onclick = joinAsPlayer;

  // A student who picked the wrong subject used to be stuck here. Offer the way
  // back whenever they arrived via subject selection rather than a QR token —
  // with a token the subject is fixed by the lecturer's link.
  const backBtn = $('#btn-back-subject');
  if (backBtn) {
    const arrivedBySubjectChoice = !sessionToken;
    backBtn.hidden = !arrivedBySubjectChoice;
    backBtn.onclick = () => {
      closeWS({ reconnect: false });
      myPlayerId = null;
      selectedSubject = null;
      initPlayer();
    };
  }

  checkReady();
  setTimeout(checkReady, 0);
  setTimeout(checkReady, 150);
  setTimeout(checkReady, 600);
  nameInput.focus();
}

function showKickedScreen(message) {
  showScreen('screen-kicked');
  const msgEl = $('#kicked-msg');
  if (msgEl) msgEl.textContent = message;
  const nameEl = $('#kicked-player-name');
  if (nameEl) nameEl.textContent = myPlayerName ? `Playing as: ${myPlayerName}` : '';

  const rejoinBtn = $('#btn-rejoin-game');
  if (rejoinBtn) {
    const canRejoin = !!(myPlayerName && myStudentNumber && selectedSubject);
    rejoinBtn.hidden = !canRejoin;
    rejoinBtn.disabled = false;
    rejoinBtn.textContent = 'Rejoin Game';
    rejoinBtn.onclick = () => {
      rejoinBtn.disabled = true;
      rejoinBtn.textContent = 'Rejoining…';
      connectWS(() => {
        send({
          action: 'player_join',
          name: myPlayerName,
          studentNumber: myStudentNumber,
          subject: selectedSubject.code,
          token: sessionToken || '',
          gameCode: ''
        });
      });
    };
  }

  const startOverBtn = $('#btn-start-over');
  if (startOverBtn) {
    startOverBtn.onclick = () => {
      myPlayerName = '';
      myStudentNumber = '';
      showPlayerJoinScreen();
    };
  }
}

function showGameCodeScreen() {
  playerNeedsGameCode = true;
  clearTimer();
  showScreen('screen-game-code');
  $('#code-subject-title').textContent = selectedSubject ? selectedSubject.name : '';
  const input = $('#game-code-input');
  const btn = $('#btn-submit-game-code');
  const errEl = $('#game-code-error');
  input.value = '';
  btn.disabled = true;
  btn.textContent = getGameCodeSubmitLabel();
  if (errEl) {
    errEl.hidden = true;
    errEl.textContent = '';
  }

  input.oninput = () => {
    input.value = input.value.replace(/\D+/g, '').slice(0, 4);
    btn.disabled = input.value.trim().length !== 4;
  };
  input.onkeydown = (e) => {
    if (e.key === 'Enter' && input.value.trim().length === 4) {
      submitGameCode();
    }
  };
  btn.onclick = submitGameCode;
  setTimeout(() => input.focus(), 100);
}

function submitGameCode() {
  const input = $('#game-code-input');
  input.value = input.value.replace(/\D+/g, '').slice(0, 4);
  const code = input.value.trim();
  if (code.length !== 4) return;
  const errEl = $('#game-code-error');
  if (errEl) errEl.hidden = true;
  const btn = $('#btn-submit-game-code');
  if (btn) {
    btn.disabled = true;
    btn.textContent = myPlayerId ? 'Continuing...' : 'Joining...';
  }
  const joinPayload = {
    action: 'player_join',
    name: myPlayerName,
    studentNumber: myStudentNumber,
    subject: selectedSubject.code,
    token: sessionToken || '',
    gameCode: code
  };
  if (myPlayerId && ws && ws.readyState === WebSocket.OPEN) {
    send({
      action: 'verify_game_code',
      gameCode: code
    });
  } else if (ws && ws.readyState === WebSocket.OPEN) {
    send(joinPayload);
  } else {
    connectWS(() => send(joinPayload));
  }
}

async function joinAsPlayer() {
  const name = $('#nickname-input').value.trim();
  const studentNum = $('#student-number-input').value.trim();
  if (!name || !studentNum) return;
  myPlayerName = name;
  myStudentNumber = studentNum;
  const errEl = $('#name-error');
  if (errEl) errEl.hidden = true;
  $('#btn-join').disabled = true;
  $('#btn-join').textContent = 'Joining...';

  // Make sure this browser has a signed identity before it joins, so a later
  // reconnect is recognised as the same student.
  await ensureVisitorToken();

  connectWS(() => {
    send({
      action: 'player_join',
      name: myPlayerName,
      studentNumber: myStudentNumber,
      subject: selectedSubject.code,
      token: sessionToken || '',
      gameCode: ''
    });
  });
}

function leaveLobby() {
  try {
    send({ action: 'player_leave' });
  } catch (e) {}

  setTimeout(() => {
    closeWS({ reconnect: false });
    myPlayerId = null;
    myPlayerName = '';
    myStudentNumber = '';
    showPlayerJoinScreen();
  }, 150);
}

function handlePlayerMessage(msg) {
  switch (msg.type) {
    case 'name_taken': {
      showScreen('screen-join');
      const errEl = $('#name-error');
      if (errEl) {
        errEl.textContent = `"${msg.name}" is already taken. Please use a different name.`;
        errEl.hidden = false;
      }
      $('#btn-join').disabled = false;
      $('#btn-join').textContent = 'Join Game';
      $('#nickname-input').focus();
      $('#nickname-input').select();
      break;
    }
    case 'error_game_code': {
      ensureGameCodeScreen();
      showGameCodeError(msg.message || 'Incorrect code.');
      $('#btn-join').disabled = false;
      $('#btn-join').textContent = 'Join Game';
      break;
    }
    case 'game_code_required':
      ensureGameCodeScreen();
      break;
    case 'joined': {
      playerNeedsGameCode = false;
      myPlayerId = msg.playerId;
      history.pushState({ quizActive: true }, '', location.href);
      if (selectedSubject && selectedSubject.code) {
        try { localStorage.setItem('quiz_last_subject_code', selectedSubject.code); } catch (e) {}
      }
      $('#lobby-player-name').textContent = myPlayerName;
      $('#lobby-p-count').textContent = msg.playerCount;
      $('#lobby-subject-badge').textContent = formatActiveTestLabel(selectedSubject, msg.activeTest);
      const leaveBtn = $('#btn-leave-lobby');
      if (leaveBtn) {
        leaveBtn.hidden = !(!msg.phase || msg.phase === 'lobby');
        leaveBtn.onclick = leaveLobby;
      }
      $('#btn-join').disabled = false;
      $('#btn-join').textContent = 'Join Game';

      if (!msg.phase || msg.phase === 'lobby') {
        showScreen('screen-lobby-player');
      } else if (msg.phase === 'question' && msg.currentQuestion) {
        if (msg.alreadyAnswered) {
          // Reconnected mid-question having already answered. Dumping the
          // student on the lobby screen ("waiting for the lecturer to start")
          // reads as though their answer was lost.
          showAnswerSubmittedScreen(msg.currentQuestion);
        } else {
          const q = msg.currentQuestion;
          playerShowQuestion(q);
        }
      } else if (msg.phase === 'reveal' || msg.phase === 'get_ready') {
        showAnswerSubmittedScreen(msg.currentQuestion, { waitingForNext: true });
      }
      break;
    }
    case 'player_update':
      if (playerNeedsGameCode) {
        ensureGameCodeScreen();
      }
      $('#lobby-p-count').textContent = msg.playerCount ?? (msg.players ? msg.players.length : 0);
      if (selectedSubject) {
        $('#lobby-subject-badge').textContent = formatActiveTestLabel(selectedSubject, msg.activeTest);
      }
      break;
    case 'get_ready':
      if (playerNeedsGameCode) {
        ensureGameCodeScreen();
        break;
      }
      playerGetReady(msg.qNum, msg.totalQ);
      break;
    case 'question':
      if (playerNeedsGameCode) {
        ensureGameCodeScreen();
        break;
      }
      playerShowQuestion(msg);
      break;
    case 'answer_result':
      if (playerNeedsGameCode) {
        ensureGameCodeScreen();
        break;
      }
      playerShowResult(msg);
      break;
    case 'time_extended':
      // The lecturer gave the room longer on this question.
      questionTimeLimit = Number(msg.timeLimit) || questionTimeLimit;
      if (!playerAnswered) {
        timeLeft = Math.max(timeLeft, Number(msg.remaining) || timeLeft);
        if (!timerInterval && timeLeft > 0) startPlayerTimerInterval();
        updatePlayerTimerDisplay();
      }
      break;
    case 'pause_state':
      if (playerNeedsGameCode) {
        ensureGameCodeScreen();
        break;
      }
      if (msg.paused) {
        clearTimer();
        const pauseMsg = $('#player-pause-msg');
        if (pauseMsg) {
          pauseMsg.textContent = '⏸ Paused by lecturer…';
          pauseMsg.hidden = false;
        }
      } else {
        const pauseMsg = $('#player-pause-msg');
        if (pauseMsg) pauseMsg.hidden = true;
        // Resume countdown from current timeLeft without resetting playerAnswered
        if (!playerAnswered && timeLeft > 0) {
          timerInterval = setInterval(() => {
            timeLeft -= 0.1;
            if (timeLeft <= 0) {
              timeLeft = 0;
              clearTimer();
            }
            updatePlayerTimerDisplay();
          }, 100);
        }
      }
      break;
    case 'leaderboard':
      if (playerNeedsGameCode) {
        ensureGameCodeScreen();
        break;
      }
      playerShowLeaderboard(msg.leaderboard);
      break;
    case 'final':
      if (playerNeedsGameCode) {
        ensureGameCodeScreen();
        break;
      }
      playerShowFinal(msg.leaderboard);
      break;
    case 'review': {
      // Arrives just after 'final'. Students can look back at what they got
      // wrong, with the correct answer and the explanation.
      lastReview = Array.isArray(msg.questions) ? msg.questions : [];
      const reviewBtn = $('#btn-review-answers');
      if (reviewBtn) {
        reviewBtn.hidden = lastReview.length === 0;
        reviewBtn.onclick = playerShowReview;
      }
      break;
    }
    case 'reset':
      playerNeedsGameCode = false;
      if (typeof msg.playerCount === 'number') $('#lobby-p-count').textContent = msg.playerCount;
      if (selectedSubject) $('#lobby-subject-badge').textContent = formatActiveTestLabel(selectedSubject, msg.activeTest);
      showScreen('screen-lobby-player');
      break;
    case 'left':
      closeWS({ reconnect: false });
      showPlayerJoinScreen();
      break;
    case 'kicked': {
      closeWS({ reconnect: false });
      myPlayerId = null;
      // Keep myPlayerName and myStudentNumber so the student can rejoin
      showKickedScreen(msg.message || 'You were disconnected from the game.');
      break;
    }
    case 'game_ended': {
      closeWS({ reconnect: false });
      myPlayerId = null;
      showPlayerJoinScreen();
      break;
    }
  }
}

/**
 * Shown when a student reconnects mid-question having already answered, or
 * during the reveal/get-ready gap. Makes it clear their answer counted.
 */
function showAnswerSubmittedScreen(currentQuestion, { waitingForNext = false } = {}) {
  clearTimer();
  showScreen('screen-answer-submitted');
  const title = $('#answer-submitted-title');
  const detail = $('#answer-submitted-detail');
  if (title) {
    title.textContent = waitingForNext ? 'Next question coming up' : 'Answer submitted';
  }
  if (detail) {
    detail.textContent = waitingForNext
      ? 'Hold tight — the lecturer is about to move on.'
      : 'Your answer was received. Waiting for the other students…';
  }
  const progress = $('#answer-submitted-progress');
  if (progress) {
    if (currentQuestion && currentQuestion.qNum && currentQuestion.totalQ) {
      progress.textContent = `Question ${currentQuestion.qNum} of ${currentQuestion.totalQ}`;
      progress.hidden = false;
    } else {
      progress.hidden = true;
    }
  }
  const nameEl = $('#answer-submitted-name');
  if (nameEl) nameEl.textContent = myPlayerName ? `Playing as ${myPlayerName}` : '';
}

function playerGetReady(qNum, totalQ) {
  showScreen('screen-ready');
  $('#ready-q-num').textContent = `Question ${qNum} of ${totalQ}`;
  let count = 3;
  $('#ready-count').textContent = count;
  readyCountdownInterval = setInterval(() => {
    count -= 1;
    if (count <= 0) {
      clearInterval(readyCountdownInterval);
      readyCountdownInterval = null;
    } else {
      $('#ready-count').textContent = count;
    }
  }, 1000);
}

function playerShowQuestion(msg) {
  clearTimer();
  showScreen('screen-question');
  $('#q-number').textContent = msg.qNum;
  $('#q-total').textContent = msg.totalQ;
  $('#question-text').textContent = msg.question;

  const grid = $('#answers-grid');
  grid.innerHTML = '';
  msg.options.forEach((opt, i) => {
    const btn = document.createElement('button');
    btn.className = `answer-btn ${COLORS[i]}`;
    btn.innerHTML = `<span class="shape">${SHAPES[i]}</span><span>${escapeHtml(opt)}</span>`;
    btn.addEventListener('click', () => playerAnswer(i, btn));
    grid.appendChild(btn);
  });

  questionTimeLimit = Number(msg.timeLimit) > 0 ? Number(msg.timeLimit) : TIME_PER_Q;
  const elapsed = msg.serverTimestamp ? (Date.now() / 1000 - msg.serverTimestamp) : 0;
  timeLeft = Math.max(0, questionTimeLimit - elapsed);
  if (typeof msg.remaining === 'number') {
    timeLeft = Math.max(0, msg.remaining);
  }
  startPlayerTimer();
  requestWakeLock();
}

function playerAnswer(choice, btnEl) {
  if (playerAnswered) return;
  playerAnswered = true;
  clearTimer();
  send({ action: 'answer', choice });
  document.querySelectorAll('.answer-btn').forEach((b) => b.classList.add('disabled'));
  btnEl.classList.add('selected');
}

function startPlayerTimer() {
  playerAnswered = false;
  updatePlayerTimerDisplay();
  startPlayerTimerInterval();
}

function startPlayerTimerInterval() {
  clearTimer();
  timerInterval = setInterval(() => {
    timeLeft -= 0.1;
    if (timeLeft <= 0) {
      timeLeft = 0;
      clearTimer();
    }
    updatePlayerTimerDisplay();
  }, 100);
}

function updatePlayerTimerDisplay() {
  const limit = questionTimeLimit > 0 ? questionTimeLimit : TIME_PER_Q;
  const pct = Math.max(0, Math.min(100, (timeLeft / limit) * 100));
  const bar = $('#timer-bar');
  const text = $('#timer-text');
  bar.style.width = `${pct}%`;
  text.textContent = Math.ceil(timeLeft);
  // "Urgent" at the last third for short questions, last 10 s for long ones.
  if (timeLeft <= Math.min(10, limit / 3)) {
    bar.classList.add('urgent');
    text.classList.add('urgent');
  } else {
    bar.classList.remove('urgent');
    text.classList.remove('urgent');
  }
}

function clearTimer() {
  if (timerInterval) {
    clearInterval(timerInterval);
    timerInterval = null;
  }
}

async function requestWakeLock() {
  try {
    if ('wakeLock' in navigator) {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => { wakeLock = null; });
    }
  } catch (e) {}
}

async function releaseWakeLock() {
  try {
    if (wakeLock) {
      await wakeLock.release();
      wakeLock = null;
    }
  } catch (e) {}
}

function playerShowResult(msg) {
  showScreen('screen-result');
  const icon = $('#result-icon');
  const text = $('#result-text');
  const detail = $('#result-detail');
  const scoreVal = $('#result-score-value');

  if (msg.timedOut) {
    icon.textContent = '⏰';
    text.textContent = "Time's Up!";
    text.style.color = 'var(--accent-orange)';
  } else if (msg.correct) {
    icon.textContent = '🎉';
    text.textContent = `Correct! +${msg.points}`;
    text.style.color = 'var(--accent-green)';
  } else {
    icon.textContent = '❌';
    text.textContent = 'Incorrect';
    text.style.color = 'var(--accent-red)';
  }

  detail.textContent = msg.explanation;
  scoreVal.textContent = msg.totalScore.toLocaleString();

  const streakEl = $('#streak-bonus-msg');
  if (streakEl) {
    if (msg.streak >= 3) {
      streakEl.textContent = `🔥 ${msg.streak} streak! +20% bonus`;
      streakEl.hidden = false;
    } else {
      streakEl.hidden = true;
    }
  }
}

function playerShowLeaderboard(lb) {
  showScreen('screen-leaderboard');
  renderLeaderboardList($('#leaderboard-list'), lb, myPlayerId);
}

function playerShowFinal(lb) {
  showScreen('screen-final');
  const reviewBtn = $('#btn-review-answers');
  if (reviewBtn) {
    reviewBtn.hidden = !(Array.isArray(lastReview) && lastReview.length);
    reviewBtn.onclick = playerShowReview;
  }
  const myRank = lb.findIndex((p) => p.id === myPlayerId) + 1;
  $('#final-title').textContent = myRank === 1 ? 'You Win! 🏆' : `Game Over — You placed #${myRank}`;
  renderPodium($('#final-podium'), lb);
  renderFullList($('#final-full-list'), lb.slice(3), myPlayerId, 4);
  releaseWakeLock();
}

// ════════════════════════════════════════════════════════════
// HOST
// ════════════════════════════════════════════════════════════

async function fetchLecturerSession() {
  try {
    const data = await apiGet('/api/lecturer/session');
    lecturerSession = data.authenticated ? data.lecturer : null;
  } catch (e) {
    lecturerSession = null;
  }
  updateHostAccountBar();
  return lecturerSession;
}

function updateHostAccountBar() {
  const bar = $('#host-account-bar');
  if (!bar) return;
  const visible = !!(isHost && lecturerSession);
  bar.hidden = !visible;
  if (visible) {
    $('#host-account-name').textContent = lecturerSession.name || lecturerSession.email || 'Lecturer';
  }
}

function showHostAuthScreen(mode = 'login', statusMessage = '', isError = false) {
  isHost = true;
  if (ws) closeWS({ reconnect: false });
  updateHostAccountBar();
  showScreen('screen-host-auth');
  showInlineStatus('#host-auth-status', statusMessage, isError);
  const focusSelector = '#login-email-input';
  setTimeout(() => {
    const target = $(focusSelector);
    if (target) target.focus();
  }, 0);
}

function showHostSignupScreen(statusMessage = '', isError = false) {
  isHost = true;
  if (ws) closeWS({ reconnect: false });
  updateHostAccountBar();
  showScreen('screen-host-signup');
  showInlineStatus('#host-signup-status', statusMessage, isError);
  const focusSelector = '#signup-name-input';
  setTimeout(() => {
    const target = $(focusSelector);
    if (target) target.focus();
  }, 0);
}

async function enterHostArea() {
  isHost = true;
  location.hash = '#host';
  const session = await fetchLecturerSession();
  if (session) {
    await initHost();
  } else {
    showHostAuthScreen('login');
  }
}

function resetToStudentView() {
  if (ws) closeWS({ reconnect: false });
  clearHostGameCodeCountdown();
  setHostGameCode('');
  isHost = false;
  selectedTest = null;
  sessionName = '';
  sessionToken = '';
  selectedSubject = null;
  hostSubjectCode = null;
  editingTestId = null;
  originalEditingTest = null;
  currentDraftLoaded = null;
  hideSessionNameModal();
  location.hash = '';
  updateHostAccountBar();
  initPlayer();
}

function bindHostAuthUI() {
  if (authUiBound) return;
  authUiBound = true;

  $('#btn-host-link').addEventListener('click', () => {
    enterHostArea();
  });

  const openSignupBtn = $('#btn-open-signup');
  if (openSignupBtn) {
    openSignupBtn.addEventListener('click', () => {
      showInlineStatus('#host-auth-status', '', false);
      showHostSignupScreen();
    });
  }

  $('#btn-auth-back').addEventListener('click', () => {
    resetToStudentView();
  });

  const signupBackBtn = $('#btn-signup-back');
  if (signupBackBtn) {
    signupBackBtn.addEventListener('click', () => {
      showInlineStatus('#host-signup-status', '', false);
      showHostAuthScreen('login');
    });
  }

  $('#host-login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    showInlineStatus('#host-auth-status', '', false);
    const btn = $('#btn-login-submit');
    btn.disabled = true;
    btn.textContent = 'Signing In...';
    try {
      await apiPost('/api/lecturer/login', {
        email: $('#login-email-input').value.trim(),
        password: $('#login-password-input').value
      });
      await fetchLecturerSession();
      $('#login-password-input').value = '';
      await initHost();
    } catch (e) {
      showInlineStatus('#host-auth-status', e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Sign In';
    }
  });

  $('#host-signup-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    showInlineStatus('#host-signup-status', '', false);
    const password = $('#signup-password-input').value;
    const confirm = $('#signup-password-confirm-input').value;
    if (password !== confirm) {
      showInlineStatus('#host-signup-status', 'Passwords do not match.', true);
      return;
    }
    const btn = $('#btn-signup-submit');
    btn.disabled = true;
    btn.textContent = 'Creating Account...';
    try {
      const inviteInput = $('#signup-invite-input');
      await apiPost('/api/lecturer/signup', {
        name: $('#signup-name-input').value.trim(),
        email: $('#signup-email-input').value.trim(),
        password,
        inviteCode: inviteInput ? inviteInput.value.trim() : ''
      });
      await fetchLecturerSession();
      $('#signup-password-input').value = '';
      $('#signup-password-confirm-input').value = '';
      await initHost();
    } catch (e) {
      showInlineStatus('#host-signup-status', e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Create Account';
    }
  });

  $('#btn-host-logout').addEventListener('click', async () => {
    try {
      await apiPost('/api/lecturer/logout', {});
    } catch (e) {
      console.error(e);
    }
    lecturerSession = null;
    updateHostAccountBar();
    showHostAuthScreen('login', 'Signed out.');
  });
}

function bindAddSubjectControls(refreshSubjects) {
  const addBtn = $('#btn-open-add-subject');
  const form = $('#host-add-subject-form');
  if (!addBtn || !form) return;

  const addClone = addBtn.cloneNode(true);
  addBtn.replaceWith(addClone);

  const formClone = form.cloneNode(true);
  form.replaceWith(formClone);

  const codeInput = formClone.querySelector('#new-subject-code');
  const nameInput = formClone.querySelector('#new-subject-name');
  const cancelBtn = formClone.querySelector('#btn-cancel-add-subject');
  const submitBtn = formClone.querySelector('#btn-submit-add-subject');

  const resetForm = () => {
    if (codeInput) codeInput.value = '';
    if (nameInput) nameInput.value = '';
    showInlineStatus('#host-add-subject-status', '', false);
    formClone.hidden = true;
    addClone.hidden = false;
  };

  if (codeInput) {
    codeInput.addEventListener('input', () => {
      codeInput.value = normalizeSubjectCode(codeInput.value);
    });
  }

  addClone.addEventListener('click', () => {
    formClone.hidden = false;
    addClone.hidden = true;
    showInlineStatus('#host-add-subject-status', '', false);
    if (codeInput) codeInput.focus();
  });

  if (cancelBtn) {
    cancelBtn.addEventListener('click', () => resetForm());
  }

  formClone.addEventListener('submit', async (e) => {
    e.preventDefault();
    const code = normalizeSubjectCode(codeInput ? codeInput.value : '');
    const name = (nameInput ? nameInput.value : '').trim();
    if (!isValidSubjectCode(code)) {
      showInlineStatus('#host-add-subject-status', 'Subject code must be 3-10 letters or numbers with no spaces.', true);
      return;
    }
    if (!isValidSubjectName(name)) {
      showInlineStatus('#host-add-subject-status', 'Subject name must be 2-60 characters.', true);
      return;
    }
    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.textContent = 'Adding...';
    }
    try {
      await apiPost('/api/subjects', { code, name });
      await refreshSubjects();
      resetForm();
    } catch (err) {
      showInlineStatus('#host-add-subject-status', err.message || 'Could not add subject.', true);
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        submitBtn.textContent = 'Add Subject';
      }
    }
  });

  resetForm();
}

function bindDeleteSubjectControls(subjects, refreshSubjects) {
  const deleteBtn = $('#btn-delete-subject');
  const select = $('#delete-subject-select');
  if (!deleteBtn || !select) return;

  const deleteClone = deleteBtn.cloneNode(true);
  deleteBtn.replaceWith(deleteClone);

  const subjectList = Array.isArray(subjects) ? subjects : [];
  const previousValue = select.value;
  select.innerHTML = '';
  subjectList.forEach((subject) => {
    const opt = document.createElement('option');
    opt.value = subject.code;
    opt.textContent = BUILTIN_SUBJECT_CODES.has(subject.code)
      ? `${subject.name} (${subject.code}) - built-in`
      : `${subject.name} (${subject.code})`;
    select.appendChild(opt);
  });

  const preferredSubject = subjectList.find((subject) => !BUILTIN_SUBJECT_CODES.has(subject.code));
  if (previousValue && subjectList.some((subject) => subject.code === previousValue)) {
    select.value = previousValue;
  } else if (preferredSubject) {
    select.value = preferredSubject.code;
  }

  select.disabled = subjectList.length === 0;
  deleteClone.disabled = subjectList.length === 0;
  deleteClone.title = subjectList.length === 0 ? 'No subjects available' : '';

  select.onchange = () => {
    showInlineStatus('#host-subject-status', '', false);
  };

  deleteClone.addEventListener('click', async () => {
    const code = select.value;
    const subject = subjectList.find((item) => item.code === code);
    if (!subject) return;
    if (BUILTIN_SUBJECT_CODES.has(code)) {
      showInlineStatus('#host-subject-status', `Built-in subject '${subject.code}' cannot be deleted.`, true);
      return;
    }
    if (!confirm(`Delete subject '${subject.name}'? This will fail if the subject has saved tests.`)) {
      return;
    }

    deleteClone.disabled = true;
    deleteClone.textContent = 'Deleting...';
    showInlineStatus('#host-subject-status', 'Deleting subject...', false);
    try {
      await apiDelete(`/api/subjects/${encodeURIComponent(code)}`);
      showInlineStatus('#host-subject-status', '', false);
      await refreshSubjects();
    } catch (err) {
      if (err.status === 401) {
        lecturerSession = null;
        updateHostAccountBar();
        showHostAuthScreen('login', 'Your lecturer session expired. Please sign in again.', true);
        return;
      }
      showInlineStatus('#host-subject-status', err.message || 'Could not delete subject.', true);
    } finally {
      deleteClone.disabled = subjectList.length === 0;
      deleteClone.textContent = 'Delete Subject';
    }
  });
}

async function initHost() {
  const session = lecturerSession || await fetchLecturerSession();
  if (!session) {
    showHostAuthScreen('login');
    return;
  }
  isHost = true;
  selectedSubject = null;
  selectedTest = null;
  sessionName = '';
  hostSubjectCode = null;
  editingTestId = null;
  currentDraftLoaded = null;
  originalEditingTest = null;
  if (ws) closeWS({ reconnect: false });
  hideSessionNameModal();
  updateHostAccountBar();
  showScreen('screen-host-subject');

  const refreshSubjects = async () => {
    const subjects = await loadSubjects();
    renderHostSubjectCards(subjects, (sub) => {
      selectedSubject = sub;
      hostSubjectCode = sub.code;
      showHostTestLibrary();
    });
    bindDeleteSubjectControls(subjects, refreshSubjects);
  };
  await refreshSubjects();
  bindAddSubjectControls(refreshSubjects);

  const backBtn = $('#btn-back-player');
  const newBack = backBtn.cloneNode(true);
  backBtn.replaceWith(newBack);
  newBack.addEventListener('click', () => {
    resetToStudentView();
  });
}

async function showHostTestLibrary() {
  if (!lecturerSession) {
    showHostAuthScreen('login', 'Please sign in as a lecturer.');
    return;
  }
  sessionName = '';
  hideSessionNameModal();
  showInlineStatus('#host-subject-status', '', false);
  if (ws) closeWS({ reconnect: false });
  showScreen('screen-host-tests');
  $('#host-tests-title').textContent = `${selectedSubject.name} (${selectedSubject.code})`;
  $('#host-tests-subtitle').textContent = 'Choose a saved test, edit one you own, or create a new one.';
  showInlineStatus('#host-library-status', 'Loading tests...', false);

  const storage = await loadStorageStatus();
  const badge = $('#host-storage-badge');
  if (storage.mode === 'supabase') {
    badge.textContent = 'Supabase storage active';
    badge.className = 'storage-badge storage-badge-live';
  } else if (storage.mode === 'local-file') {
    badge.textContent = 'Local file storage active';
    badge.className = 'storage-badge storage-badge-live';
  } else {
    badge.textContent = 'Temporary in-memory storage';
    badge.className = 'storage-badge storage-badge-warning';
  }
  $('#host-storage-note').textContent = storage.note || '';

  // A paused Supabase free project returns connection failures with nothing
  // explanatory. Say what it actually is.
  const sleepEl = $('#host-storage-asleep');
  if (sleepEl) {
    sleepEl.hidden = !storage.asleep;
    sleepEl.textContent = storage.asleep
      ? 'The question database appears to be asleep. Supabase pauses free projects after 7 days of inactivity — open your Supabase dashboard and press Resume, then reload this page.'
      : '';
  }

  renderResultsStorageLine();

  renderDraftResumeCard();   // fire and forget; renders when the draft arrives

  try {
    const tests = await loadTests(selectedSubject.code);
    renderHostTestCards(tests);
    showInlineStatus('#host-library-status', tests.length ? '' : 'No tests saved yet. Create your first one below.', false);
  } catch (e) {
    if (e.status === 401) {
      lecturerSession = null;
      updateHostAccountBar();
      showHostAuthScreen('login', 'Your lecturer session expired. Please sign in again.', true);
      return;
    }
    renderHostTestCards([]);
    showInlineStatus('#host-library-status', e.message, true);
  }

  const createBtn = $('#btn-create-test');
  const createClone = createBtn.cloneNode(true);
  createBtn.replaceWith(createClone);
  createClone.addEventListener('click', () => showCreateTestScreen({ mode: 'create' }));

  const backBtn = $('#btn-back-host-subjects');
  const backClone = backBtn.cloneNode(true);
  backBtn.replaceWith(backClone);
  backClone.addEventListener('click', () => initHost());

  const backupBtn = $('#btn-backup-tests');
  if (backupBtn) {
    const backupClone = backupBtn.cloneNode(true);
    backupBtn.replaceWith(backupClone);
    backupClone.addEventListener('click', () => downloadTestsBackup(backupClone));
  }
}

/**
 * Show how many past sessions are stored and roughly what they cost, so the
 * storage bill is visible rather than guessed at.
 */
async function renderResultsStorageLine() {
  const el = $('#host-results-storage');
  if (!el || !selectedSubject) return;
  el.hidden = true;
  try {
    const data = await apiGet(`/api/results/${encodeURIComponent(selectedSubject.code)}`);
    const storage = data.storage || {};
    const count = (data.results || []).length;
    if (!storage.enabled) {
      el.textContent = 'Storing past sessions is switched off (PERSIST_RESULTS=false). Your end-of-game downloads are the only record.';
      el.hidden = false;
      return;
    }
    if (!count) {
      el.textContent = `No past sessions stored yet for this subject. The most recent ${storage.retention} will be kept.`;
      el.hidden = false;
      return;
    }
    const size = storage.approxKb >= 1024
      ? `${(storage.approxKb / 1024).toFixed(1)} MB`
      : `${storage.approxKb} KB`;
    el.textContent = `${count} past session${count === 1 ? '' : 's'} stored for this subject (about ${size}). The most recent ${storage.retention} are kept; older ones are deleted automatically.`;
    el.hidden = false;
  } catch (e) {
    // Storage reporting is informational only — never block the library on it.
  }
}

/**
 * Show any draft in progress at the top of the test library. Without this a
 * correctly stored draft is unreachable — nothing in the UI says it exists.
 */
async function renderDraftResumeCard() {
  const container = $('#host-draft-card');
  if (!container || !selectedSubject) return;
  container.hidden = true;
  container.innerHTML = '';

  const { draft, source } = await loadBestDraft(selectedSubject.code);
  if (!draftHasContent(draft)) return;
  // The library may have moved on while we were fetching.
  if (!selectedSubject || !$('#screen-host-tests.active')) return;

  const count = draftQuestions(draft).length;
  const title = (draft.title || '').trim() || 'Untitled test';
  const editingId = getDraftEditingId(draft);
  const kind = editingId ? 'Unsaved changes to a saved test' : 'Draft in progress';
  const where = source === 'browser' ? ' • recovered from this browser' : '';

  container.innerHTML = `
    <div class="draft-card-main">
      <span class="draft-card-badge">${escapeHtml(kind)}</span>
      <h3 class="draft-card-title">${escapeHtml(title)}</h3>
      <p class="draft-card-meta">${count} question${count === 1 ? '' : 's'} • last saved ${escapeHtml(formatDateTime(draft.updated_at))}${escapeHtml(where)}</p>
    </div>
    <div class="draft-card-actions">
      <button class="btn btn-primary draft-resume-btn" type="button">Resume</button>
      <button class="btn btn-secondary draft-discard-btn" type="button">Discard</button>
    </div>
  `;
  container.hidden = false;

  container.querySelector('.draft-resume-btn').addEventListener('click', () => {
    showCreateTestScreen({
      mode: editingId ? 'edit' : 'create',
      testId: editingId || null,
      resumeDraft: true
    });
  });
  container.querySelector('.draft-discard-btn').addEventListener('click', async () => {
    if (!confirm(`Discard the draft “${title}”? This cannot be undone.`)) return;
    await destroyDraftEverywhere(selectedSubject.code);
    container.hidden = true;
    container.innerHTML = '';
  });
}

function formatDateTime(value) {
  if (!value) return 'Just now';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return 'Just now';
  return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function renderHostTestCards(tests) {
  const container = $('#host-test-list');
  container.innerHTML = '';
  if (!tests || tests.length === 0) {
    container.innerHTML = '<p class="empty-msg">No saved tests for this subject yet.</p>';
    return;
  }

  tests.forEach((test) => {
    const card = document.createElement('div');
    card.className = 'test-card';
    const sourceText = test.source === 'supabase'
      ? 'Stored in Supabase'
      : (test.source === 'built-in'
        ? 'Built-in starter quiz'
        : (test.source === 'local-file' ? 'Stored locally on server' : 'Temporary local test'));
    const chapter = test.chapter ? `<p class="test-card-chapter">${escapeHtml(test.chapter)}</p>` : '';
    const desc = test.description ? `<p class="test-card-desc">${escapeHtml(test.description)}</p>` : '';
    const owner = test.ownerName ? `<p class="test-card-owner">Owner: ${escapeHtml(test.ownerName)}</p>` : '';
    const updated = `<p class="test-card-updated">Updated ${escapeHtml(formatDateTime(test.updated_at || test.created_at))}</p>`;
    // The label must match what the button actually does. A built-in test used
    // to read "Edit Test" while carrying the duplicate class, so it silently
    // made a copy instead.
    const secondaryLabel = test.canEdit ? 'Edit Test' : 'Copy & Edit';
    const secondaryClass = test.canEdit ? 'test-edit-btn' : 'test-duplicate-btn';
    const deleteButton = test.canEdit
      ? '<button class="btn btn-danger test-delete-btn">Delete</button>'
      : '';
    card.innerHTML = `
      <div class="test-card-main">
        <div>
          <h3 class="test-card-title">${escapeHtml(test.title)}</h3>
          ${chapter}
          ${desc}
          ${owner}
          ${updated}
        </div>
        <div class="test-card-meta">
          <span class="test-pill">${test.questionCount} question${test.questionCount === 1 ? '' : 's'}</span>
          <span class="test-card-source">${escapeHtml(sourceText)}</span>
        </div>
      </div>
      <div class="test-card-actions">
        <button class="btn btn-primary test-use-btn">Use This Test</button>
        <button class="btn btn-secondary ${secondaryClass}">${secondaryLabel}</button>
        ${deleteButton}
      </div>
    `;
    card.querySelector('.test-use-btn').addEventListener('click', () => {
      selectedTest = test;
      promptSessionName(test);
    });
    const editBtn = card.querySelector('.test-edit-btn');
    if (editBtn) {
      editBtn.addEventListener('click', () => showCreateTestScreen({ mode: 'edit', testId: test.id }));
    }
    const duplicateBtn = card.querySelector('.test-duplicate-btn');
    if (duplicateBtn) {
      duplicateBtn.addEventListener('click', () => duplicateTestFrom(test));
    }
    const deleteBtn = card.querySelector('.test-delete-btn');
    if (deleteBtn) {
      deleteBtn.addEventListener('click', () => deleteTestFromLibrary(test));
    }
    container.appendChild(card);
  });
}

async function duplicateTestFrom(testSummary) {
  if (!selectedSubject) return;
  const copyingBuiltIn = testSummary && testSummary.source === 'built-in';
  showInlineStatus(
    '#host-library-status',
    copyingBuiltIn ? 'Preparing an editable copy of the built-in quiz...' : 'Preparing a copy...',
    false
  );
  try {
    const detail = await apiGet(`/api/tests/${encodeURIComponent(selectedSubject.code)}/${encodeURIComponent(testSummary.id)}`);
    await showCreateTestScreen({ mode: 'create', intent: 'duplicate' });
    const copyTitle = detail.title ? `${detail.title} (Copy)` : 'Untitled Test (Copy)';
    applyEditorData({
      title: copyTitle,
      chapter: detail.chapter || '',
      description: detail.description || '',
      questions: detail.questions || []
    });
    showInlineStatus('#host-create-status', 'Copied test. Update it, then save.', false);
    resetDraftStatus('Copied from a saved test. Drafts will auto-save.', false);
    markDraftDirty();
  } catch (e) {
    showInlineStatus('#host-library-status', e.message || 'Could not duplicate the test.', true);
  }
}

async function deleteTestFromLibrary(testSummary) {
  if (!selectedSubject || !testSummary) return;
  const title = testSummary.title || 'Untitled Test';
  if (!confirm(`Delete '${title}'? This cannot be undone.`)) return;
  showInlineStatus('#host-library-status', 'Deleting test...', false);
  try {
    await apiDelete(`/api/tests/${encodeURIComponent(selectedSubject.code)}/${encodeURIComponent(testSummary.id)}`);
    await showHostTestLibrary();
  } catch (e) {
    showInlineStatus('#host-library-status', e.message || 'Could not delete the test.', true);
  }
}

// ── Drafts ───────────────────────────────────────────────────────────────────
// Drafts are keyed (lecturer, subject) — one per subject. Three things can hold
// a copy: Supabase, the server's local file, and this browser's localStorage.
// The browser copy is the one that survives a Render redeploy, so it is always
// written first and always consulted on the way back in.

const DRAFT_LS_PREFIX = 'quiz_draft_';

function draftStorageKey(subjectCode) {
  return `${DRAFT_LS_PREFIX}${subjectCode}`;
}

function readLocalDraft(subjectCode) {
  try {
    const raw = localStorage.getItem(draftStorageKey(subjectCode));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    // Never hand one lecturer another lecturer's work on a shared machine.
    if (lecturerSession && parsed.lecturer_id && parsed.lecturer_id !== lecturerSession.id) return null;
    return parsed;
  } catch (e) {
    return null;
  }
}

function writeLocalDraft(subjectCode, payload) {
  if (!subjectCode) return;
  try {
    localStorage.setItem(draftStorageKey(subjectCode), JSON.stringify({
      ...payload,
      lecturer_id: lecturerSession ? lecturerSession.id : null,
      updated_at: new Date().toISOString()
    }));
  } catch (e) {
    // Quota or private browsing — the server copy is still the primary store.
  }
}

function clearLocalDraft(subjectCode) {
  try {
    localStorage.removeItem(draftStorageKey(subjectCode));
  } catch (e) {}
}

// getDraftEditingId, draftTimestamp, draftQuestions, draftHasContent,
// chooseNewerDraft and decideDraftAction come from draft_utils.js, which
// index.html loads first. They are pure and unit tested in
// tests/test_draft_logic.js.

function describeDraft(draft) {
  const count = draftQuestions(draft).length;
  const title = (draft?.title || '').trim() || 'Untitled test';
  const questions = `${count} question${count === 1 ? '' : 's'}`;
  return `“${title}” — ${questions}, last saved ${formatDateTime(draft?.updated_at)}`;
}

/**
 * Fetch the server draft and compare it with the browser copy, returning
 * whichever is newer. The browser copy wins after a server restart wiped the
 * ephemeral store, which is exactly the case this is here to survive.
 */
async function loadBestDraft(subjectCode) {
  let serverDraft = null;
  let storedIn = null;
  let error = null;
  try {
    const resp = await apiGet(`/api/drafts/${encodeURIComponent(subjectCode)}`);
    serverDraft = resp.draft || null;
    storedIn = resp.storedIn || null;
    error = resp.error || null;
  } catch (e) {
    error = e.message;
  }
  const localDraft = readLocalDraft(subjectCode);
  const chosen = chooseNewerDraft(serverDraft, localDraft);
  return {
    draft: chosen.draft,
    source: chosen.source,
    storedIn: chosen.source === 'browser' ? 'browser' : storedIn,
    error
  };
}

/**
 * A small promise-based chooser. Never destroys anything by itself — the caller
 * acts on the returned key.
 */
function askDraftChoice({ title, message, detail, options }) {
  const modal = $('#draft-choice-modal');
  const actions = $('#draft-choice-actions');
  if (!modal || !actions) return Promise.resolve(options[0]?.key);

  $('#draft-choice-title').textContent = title;
  $('#draft-choice-message').textContent = message;
  const detailEl = $('#draft-choice-detail');
  detailEl.textContent = detail || '';
  detailEl.hidden = !detail;

  actions.innerHTML = '';
  modal.hidden = false;

  return new Promise((resolve) => {
    const finish = (key) => {
      modal.hidden = true;
      modal.onclick = null;
      document.removeEventListener('keydown', onKey);
      resolve(key);
    };
    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        finish(options[options.length - 1].key);
      }
    };
    options.forEach((option, index) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = `btn ${option.className || (index === 0 ? 'btn-primary' : 'btn-secondary')}`;
      btn.textContent = option.label;
      btn.addEventListener('click', () => finish(option.key));
      actions.appendChild(btn);
    });
    modal.onclick = (event) => {
      if (event.target === modal) finish(options[options.length - 1].key);
    };
    document.addEventListener('keydown', onKey);
    requestAnimationFrame(() => {
      const first = actions.querySelector('button');
      if (first) first.focus();
    });
  });
}

function applyEditorData(data = {}) {
  $('#test-title-input').value = data.title || '';
  $('#test-chapter-input').value = data.chapter || '';
  $('#test-description-input').value = data.description || '';
  // Set the test default before rendering the questions, so each per-question
  // "Use test default (Ns)" label is right from the start.
  setTestDefaultTimeLimit(data.defaultTimeLimit || data.default_time_limit || TIME_PER_Q);
  const questions = Array.isArray(data.questions) && data.questions.length
    ? data.questions
    : [{ q: '', options: ['', '', '', ''], correct: 0, explanation: '' }];
  renderQuestionEditors(questions);
  refreshTimingLabels();
}

function resetDraftStatus(text = 'No draft changes yet.', isError = false, isWarning = false) {
  const el = $('#draft-status');
  if (!el) return;
  el.textContent = text;
  const neutral = !text || text.toLowerCase().includes('unsaved') || text.toLowerCase().includes('no draft');
  el.classList.toggle('error-text', !!isError);
  el.classList.toggle('warning-text', !isError && !!isWarning);
  el.classList.toggle('success-text', !isError && !isWarning && !!text && !neutral);
  el.classList.toggle('muted-text', !isError && !isWarning && neutral);
}

function markDraftDirty() {
  draftDirty = true;
  resetDraftStatus('Unsaved changes…', false);
  if (draftSaveTimer) clearTimeout(draftSaveTimer);
  draftSaveTimer = setTimeout(() => {
    saveDraft({ silent: true });
  }, 1500);
}

function bindEditorInputAutosave() {
  if (editorInputBound) return;
  editorInputBound = true;
  const screen = $('#screen-host-create-test');
  ['input', 'change'].forEach((evt) => {
    screen.addEventListener(evt, (e) => {
      if (!(e.target instanceof HTMLElement)) return;
      if (!e.target.closest('.create-test-form')) return;
      markDraftDirty();
    });
  });
}

/** The per-question override, or null to inherit the test default. */
function readQuestionTimeLimit(card) {
  const select = card.querySelector('.editor-time-limit');
  if (!select || !select.value) return null;
  return clampTimeLimit(select.value);
}

function collectDraftFormPayload() {
  const title = $('#test-title-input').value.trim();
  const chapter = $('#test-chapter-input').value.trim();
  const description = $('#test-description-input').value.trim();
  const questionCards = Array.from(document.querySelectorAll('.question-editor-card'));
  const questions = questionCards.map((card) => {
    const q = card.querySelector('.editor-question').value.trim();
    const options = Array.from(card.querySelectorAll('.editor-option')).map((input) => input.value.trim());
    const correct = Number(card.querySelector('.editor-correct').value || 0);
    const explanation = card.querySelector('.editor-explanation').value.trim();
    // Timing must survive a draft too — leaving it out here is an easy bug and
    // there is a test for it.
    return { q, options, correct, explanation, time_limit: readQuestionTimeLimit(card) };
  });
  return {
    title,
    chapter,
    description,
    questions,
    default_time_limit: getTestDefaultTimeLimit(),
    editingTestId: editingTestId || null
  };
}

async function saveDraft({ silent = false } = {}) {
  if (!selectedSubject || !lecturerSession) return null;
  if (draftSaveTimer) {
    clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
  }
  const payload = collectDraftFormPayload();

  // Write the browser copy first, before any network call. This is the copy
  // that survives a server restart, a Supabase outage or a Render redeploy,
  // and it costs nothing.
  writeLocalDraft(selectedSubject.code, payload);

  try {
    const resp = await apiPost(`/api/drafts/${encodeURIComponent(selectedSubject.code)}`, payload);
    // The old server returned HTTP 200 with {"ok": false} when the write had
    // failed, and this function only ever looked at the HTTP status — so a
    // failed save was reported to the lecturer as a success.
    if (resp && resp.ok === false) {
      throw new Error(resp.error || 'The server could not store this draft.');
    }
    draftDirty = false;
    currentDraftLoaded = resp.draft || payload;
    const when = formatDateTime(resp.draft?.updated_at || new Date().toISOString());
    if (resp.storedIn === 'supabase') {
      resetDraftStatus(`Draft saved to Supabase ${when}.`, false);
    } else if (resp.storedIn === 'local-file') {
      resetDraftStatus(
        `Draft saved locally ${when} — it will be lost if the server redeploys. A copy is also kept in this browser.`,
        false,
        true
      );
    } else {
      resetDraftStatus(
        `Draft saved to server memory only ${when} — it will be lost when the server restarts. A copy is also kept in this browser.`,
        false,
        true
      );
    }
    return resp.draft;
  } catch (e) {
    const reason = e.message || 'the server did not respond';
    resetDraftStatus(
      `Draft NOT saved — ${reason}. A copy is kept in this browser and will be offered when you come back.`,
      true
    );
    if (!silent) console.error('Draft save failed:', e);
    return null;
  }
}

/** Cancel a queued autosave, flushing it first if there are pending changes. */
function flushPendingDraftSave() {
  if (draftSaveTimer) {
    clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
  }
  if (!draftDirty || !selectedSubject || !lecturerSession) return;
  // Always capture the browser copy synchronously; the network save is
  // best-effort and must not block navigation.
  writeLocalDraft(selectedSubject.code, collectDraftFormPayload());
  saveDraft({ silent: true });
}

async function discardDraft() {
  if (!selectedSubject) return;
  if (!confirm('Discard the saved draft for this subject? This cannot be undone.')) return;
  if (draftSaveTimer) {
    clearTimeout(draftSaveTimer);
    draftSaveTimer = null;
  }
  try {
    await apiDelete(`/api/drafts/${encodeURIComponent(selectedSubject.code)}`);
    clearLocalDraft(selectedSubject.code);
    currentDraftLoaded = null;
    draftDirty = false;
    if (editorMode === 'edit' && originalEditingTest) {
      applyEditorData(originalEditingTest);
      resetDraftStatus('Draft discarded. Restored the saved test.', false);
    } else {
      applyEditorData({ title: '', chapter: '', description: '', questions: [] });
      resetDraftStatus('Draft discarded.', false);
    }
  } catch (e) {
    resetDraftStatus(e.message || 'Could not discard the draft.', true);
  }
}

/** Destroy every copy of the draft. Only ever called on an explicit choice. */
async function destroyDraftEverywhere(subjectCode) {
  clearLocalDraft(subjectCode);
  try {
    await apiDelete(`/api/drafts/${encodeURIComponent(subjectCode)}`);
  } catch (e) {
    // Non-fatal: the next autosave overwrites whatever is left behind.
  }
}

async function showCreateTestScreen(options = {}) {
  if (!lecturerSession) {
    showHostAuthScreen('login', 'Please sign in as a lecturer.');
    return;
  }
  editorMode = options.mode || 'create';
  editingTestId = options.testId || null;
  draftDirty = false;
  currentDraftLoaded = null;
  originalEditingTest = null;

  showScreen('screen-host-create-test');
  $('#create-test-title').textContent = editorMode === 'edit' ? 'Edit Test' : 'Create Test';
  $('#create-test-subject').textContent = `${selectedSubject.name} (${selectedSubject.code})`;
  $('#btn-save-test').textContent = 'Save';
  showInlineStatus('#host-create-status', '', false);
  resetDraftStatus('Loading editor...', false);

  // Always read the draft on the way in — including in create mode. The old
  // code fired an unconditional DELETE here, which destroyed the draft the
  // lecturer had saved in a previous session before it was ever read. That,
  // plus only ever restoring drafts whose editing_test_id matched the test
  // being edited (never true for a new-test draft), is why "Save Draft" looked
  // like it did nothing.
  const { draft, source: draftSource } = await loadBestDraft(selectedSubject.code);
  const decision = decideDraftAction({
    mode: editorMode,
    editingTestId,
    draft,
    resumeDraft: !!options.resumeDraft
  });
  const draftEditingId = decision.draftEditingId;

  const loadSavedTest = async (testId) => {
    try {
      return await apiGet(`/api/tests/${encodeURIComponent(selectedSubject.code)}/${encodeURIComponent(testId)}`);
    } catch (e) {
      return null;   // the underlying test may since have been deleted
    }
  };

  const applyDraft = () => {
    currentDraftLoaded = draft;
    editingTestId = draftEditingId || null;
    editorMode = draftEditingId ? 'edit' : 'create';
    $('#create-test-title').textContent = editorMode === 'edit' ? 'Edit Test' : 'Create Test';
    applyEditorData({
      title: draft.title || '',
      chapter: draft.chapter || '',
      description: draft.description || '',
      questions: draft.questions || []
    });
    const where = draftSource === 'browser' ? ' (recovered from this browser)' : '';
    resetDraftStatus(`Recovered your draft from ${formatDateTime(draft.updated_at)}${where}.`, false);
  };

  const startBlank = async () => {
    // The only path that destroys a draft, and only on an explicit choice.
    await destroyDraftEverywhere(selectedSubject.code);
    editingTestId = null;
    editorMode = 'create';
    originalEditingTest = null;
    applyEditorData({ title: '', chapter: '', description: '', questions: [] });
    resetDraftStatus('Previous draft discarded. Drafts save automatically.', false);
  };

  try {
    if (decision.action === 'saved-test') {
      originalEditingTest = await apiGet(`/api/tests/${encodeURIComponent(selectedSubject.code)}/${encodeURIComponent(editingTestId)}`);
      applyEditorData(originalEditingTest);
      resetDraftStatus('Editing the saved test.', false);

    } else if (decision.action === 'blank') {
      editingTestId = null;
      originalEditingTest = null;
      applyEditorData({ title: '', chapter: '', description: '', questions: [] });
      resetDraftStatus('Start building your test. Drafts save automatically.', false);

    } else if (decision.action === 'resume') {
      if (draftEditingId) originalEditingTest = await loadSavedTest(draftEditingId);
      applyDraft();

    } else if (decision.kind === 'edit-vs-draft') {
      // A draft exists but belongs elsewhere. Warn before the first autosave
      // silently replaces it.
      originalEditingTest = await apiGet(`/api/tests/${encodeURIComponent(selectedSubject.code)}/${encodeURIComponent(editingTestId)}`);
      const choice = await askDraftChoice({
        title: 'You have an unsaved draft',
        message: 'This draft is not part of the test you just opened. If you carry on editing this test, your changes will replace the draft the next time they save.',
        detail: describeDraft(draft),
        options: [
          { key: 'draft', label: 'Open my draft instead' },
          { key: 'test', label: 'Continue editing this test' }
        ]
      });
      if (choice === 'draft') {
        applyDraft();
      } else {
        applyEditorData(originalEditingTest);
        resetDraftStatus('Editing the saved test. Your previous draft will be replaced when this autosaves.', false, true);
      }

    } else {
      // create-vs-draft
      const belongsElsewhere = !!decision.belongsElsewhere;
      const choice = await askDraftChoice({
        title: 'Resume your unsaved draft?',
        message: belongsElsewhere
          ? 'You have an unsaved draft of a saved test in this subject.'
          : 'You have an unsaved draft for this subject.',
        detail: describeDraft(draft),
        options: [
          { key: 'draft', label: belongsElsewhere ? 'Open that draft' : 'Resume draft' },
          {
            key: 'blank',
            label: options.intent === 'duplicate'
              ? 'Continue with the copy (discards draft)'
              : 'Start blank (discards draft)',
            className: 'btn-secondary'
          }
        ]
      });
      if (choice === 'draft') {
        if (draftEditingId) originalEditingTest = await loadSavedTest(draftEditingId);
        applyDraft();
      } else {
        await startBlank();
      }
    }
  } catch (e) {
    showInlineStatus('#host-create-status', e.message, true);
    applyEditorData({ title: '', chapter: '', description: '', questions: [] });
    resetDraftStatus('Could not load the editor data.', true);
  }

  bindEditorInputAutosave();
  bindTimingControls();
  bindImportUI();
  showInlineStatus('#import-status', '', false);

  const addBtn = $('#btn-add-question');
  const addClone = addBtn.cloneNode(true);
  addBtn.replaceWith(addClone);
  addClone.addEventListener('click', () => {
    addQuestionEditor();
    markDraftDirty();
  });

  const cancelBtn = $('#btn-cancel-create-test');
  const cancelClone = cancelBtn.cloneNode(true);
  cancelBtn.replaceWith(cancelClone);
  cancelClone.addEventListener('click', async () => {
    if (draftDirty) await saveDraft({ silent: false });
    showHostTestLibrary();
  });

  const draftBtn = $('#btn-save-draft');
  const draftClone = draftBtn.cloneNode(true);
  draftBtn.replaceWith(draftClone);
  draftClone.addEventListener('click', async () => {
    draftClone.disabled = true;
    draftClone.textContent = 'Saving Draft...';
    await saveDraft({ silent: false });
    draftClone.disabled = false;
    draftClone.textContent = 'Save Draft';
  });

  const discardBtn = $('#btn-discard-draft');
  const discardClone = discardBtn.cloneNode(true);
  discardBtn.replaceWith(discardClone);
  discardClone.addEventListener('click', discardDraft);

  const saveBtn = $('#btn-save-test');
  const saveClone = saveBtn.cloneNode(true);
  saveBtn.replaceWith(saveClone);
  saveClone.addEventListener('click', async () => {
    showInlineStatus('#host-create-status', '', false);
    saveClone.disabled = true;
    saveClone.textContent = 'Saving...';
    try {
      const payload = collectTestFormPayload();
      const resp = editingTestId
        ? await apiPut(`/api/tests/${encodeURIComponent(selectedSubject.code)}/${encodeURIComponent(editingTestId)}`, payload)
        : await apiPost(`/api/tests/${encodeURIComponent(selectedSubject.code)}`, payload);
      selectedTest = resp.test;
      editingTestId = resp.test.id;
      editorMode = 'edit';
      originalEditingTest = resp.test;
      currentDraftLoaded = null;
      draftDirty = false;
      // The server clears its own draft on a successful save; clear the browser
      // mirror too, or it would be offered back as "unsaved work" next time.
      if (draftSaveTimer) {
        clearTimeout(draftSaveTimer);
        draftSaveTimer = null;
      }
      clearLocalDraft(selectedSubject.code);
      resetDraftStatus('Saved. No unsaved draft.', false);
      showInlineStatus('#host-create-status', 'Test saved.', false);
      saveClone.disabled = false;
      saveClone.textContent = 'Save';
    } catch (e) {
      showInlineStatus('#host-create-status', e.message, true);
      saveClone.disabled = false;
      saveClone.textContent = 'Save';
    }
  });
}

// ── Word import ──────────────────────────────────────────────────────────────

let importUiBound = false;

function bindImportUI() {
  if (importUiBound) return;
  importUiBound = true;

  const dropzone = $('#import-dropzone');
  const input = $('#import-file-input');
  if (!dropzone || !input) return;

  const choose = () => input.click();
  dropzone.addEventListener('click', choose);
  dropzone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      choose();
    }
  });

  ['dragenter', 'dragover'].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.add('is-dragging');
    });
  });
  ['dragleave', 'drop'].forEach((evt) => {
    dropzone.addEventListener(evt, (e) => {
      e.preventDefault();
      dropzone.classList.remove('is-dragging');
    });
  });
  dropzone.addEventListener('drop', (e) => {
    const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (file) uploadImportFile(file);
  });

  input.addEventListener('change', () => {
    const file = input.files && input.files[0];
    if (file) uploadImportFile(file);
    input.value = '';       // allow re-selecting the same file
  });
}

async function uploadImportFile(file) {
  if (!file) return;
  if (!/\.docx$/i.test(file.name)) {
    showInlineStatus('#import-status', 'Please choose a .docx file. Word’s older .doc format and PDFs cannot be read.', true);
    return;
  }
  showInlineStatus('#import-status', `Reading ${file.name}…`, false);
  const form = new FormData();
  form.append('file', file, file.name);
  try {
    const resp = await fetch(`${API_BASE}/api/import/questions`, {
      method: 'POST',
      credentials: 'same-origin',
      body: form
    });
    const data = await parseApiResponse(resp);
    showInlineStatus('#import-status', '', false);
    showImportReview(data, file.name);
  } catch (e) {
    showInlineStatus('#import-status', e.message || 'Could not read that document.', true);
  }
}

function showImportReview(data, filename) {
  const modal = $('#import-review-modal');
  const list = $('#import-review-list');
  const summaryEl = $('#import-review-summary');
  const warningsEl = $('#import-review-warnings');
  if (!modal || !list) return;

  const questions = Array.isArray(data.questions) ? data.questions : [];
  const warnings = Array.isArray(data.warnings) ? data.warnings : [];
  const meta = data.meta || {};

  const bits = [`${questions.length} question${questions.length === 1 ? '' : 's'} found in ${filename}`];
  if (meta.skipped) bits.push(`${meta.skipped} skipped`);
  if (meta.layout) bits.push(`${meta.layout} layout`);
  summaryEl.textContent = bits.join(' • ');

  if (warnings.length) {
    warningsEl.hidden = false;
    warningsEl.innerHTML = `
      <p class="import-warnings-title">${warnings.length} thing${warnings.length === 1 ? '' : 's'} to check</p>
      <ul>${warnings.map((w) => `<li>${w.index ? `<strong>Q${w.index}:</strong> ` : ''}${escapeHtml(w.message)}</li>`).join('')}</ul>
    `;
  } else {
    warningsEl.hidden = true;
    warningsEl.innerHTML = '';
  }

  list.innerHTML = questions.length
    ? questions.map((q, i) => {
      const options = (q.options || []).map((opt, oi) => `
        <li class="${oi === q.correct ? 'is-correct' : ''}">
          <span class="import-opt-letter">${'ABCD'[oi] || ''}</span>
          <span>${escapeHtml(opt)}</span>
          ${oi === q.correct ? '<span class="import-opt-tick">✓ correct</span>' : ''}
        </li>`).join('');
      const time = q.time_limit ? `<span class="import-q-time">${q.time_limit}s</span>` : '';
      const explanation = q.explanation
        ? `<p class="import-q-explanation">${escapeHtml(q.explanation)}</p>`
        : '';
      return `
        <div class="import-review-item">
          <p class="import-q-text"><strong>${i + 1}.</strong> ${escapeHtml(q.q)} ${time}</p>
          <ul class="import-q-options">${options}</ul>
          ${explanation}
        </div>`;
    }).join('')
    : '<p class="empty-msg">No questions could be read from that document. Check the format against the template.</p>';

  const appendBtn = $('#btn-import-append');
  const replaceBtn = $('#btn-import-replace');
  const cancelBtn = $('#btn-import-cancel');
  appendBtn.disabled = questions.length === 0;
  replaceBtn.disabled = questions.length === 0;

  const close = () => {
    modal.hidden = true;
    modal.onclick = null;
    document.removeEventListener('keydown', onKey);
  };
  const onKey = (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      close();
    }
  };

  const apply = (mode) => {
    const existing = mode === 'replace' ? [] : collectDraftFormPayload().questions.filter(hasQuestionContent);
    renderQuestionEditors(existing.concat(questions));
    close();
    showInlineStatus(
      '#host-create-status',
      `${questions.length} question${questions.length === 1 ? '' : 's'} imported. Review them, then press Save.`,
      false
    );
    // Mark dirty so the import is autosaved as a draft straight away.
    markDraftDirty();
  };

  appendBtn.onclick = () => apply('append');
  replaceBtn.onclick = () => apply('replace');
  cancelBtn.onclick = close;
  modal.onclick = (e) => {
    if (e.target === modal) close();
  };
  document.addEventListener('keydown', onKey);
  modal.hidden = false;
}

function hasQuestionContent(q) {
  if (!q) return false;
  return !!((q.q || '').trim()
    || (q.explanation || '').trim()
    || (q.options || []).some((opt) => (opt || '').trim()));
}

function renderQuestionEditors(questions) {
  const container = $('#question-editor-list');
  container.innerHTML = '';
  const normalized = Array.isArray(questions) && questions.length
    ? questions
    : [{ q: '', options: ['', '', '', ''], correct: 0, explanation: '' }];
  normalized.forEach((q) => addQuestionEditor(q));
}

// ── Question timing ──────────────────────────────────────────────────────────

const TIME_PRESETS = [
  { value: 10, label: '10s — quick recall' },
  { value: 30, label: '30s — standard' },
  { value: 60, label: '60s' },
  { value: 90, label: '90s' },
  { value: 120, label: '120s — calculation' }
];

/** The test-level default currently shown in the editor. */
function getTestDefaultTimeLimit() {
  const select = $('#test-default-time');
  if (!select) return TIME_PER_Q;
  if (select.value === 'custom') {
    const custom = Number($('#test-default-time-custom').value);
    return clampTimeLimit(custom);
  }
  return clampTimeLimit(Number(select.value));
}

function clampTimeLimit(value) {
  const seconds = Number(value);
  if (!Number.isFinite(seconds)) return TIME_PER_Q;
  return Math.max(5, Math.min(300, Math.round(seconds)));
}

function setTestDefaultTimeLimit(seconds) {
  const select = $('#test-default-time');
  const customWrap = $('#test-default-time-custom-wrap');
  const custom = $('#test-default-time-custom');
  if (!select) return;
  const value = clampTimeLimit(seconds || TIME_PER_Q);
  const preset = TIME_PRESETS.find((p) => p.value === value);
  if (preset) {
    select.value = String(preset.value);
    if (customWrap) customWrap.hidden = true;
  } else {
    select.value = 'custom';
    if (customWrap) customWrap.hidden = false;
    if (custom) custom.value = String(value);
  }
}

function buildQuestionTimeOptions(selected) {
  const testDefault = getTestDefaultTimeLimit();
  const options = [`<option value="">Use test default (${testDefault}s)</option>`];
  TIME_PRESETS.forEach((preset) => {
    options.push(`<option value="${preset.value}">${escapeHtml(preset.label)}</option>`);
  });
  const value = selected ? clampTimeLimit(selected) : '';
  if (value && !TIME_PRESETS.some((p) => p.value === value)) {
    options.push(`<option value="${value}">${value}s</option>`);
  }
  return options.join('');
}

/**
 * Keep every per-question "Use test default (Ns)" label truthful when the
 * test-level default changes, and refresh the estimate.
 */
function refreshTimingLabels() {
  const testDefault = getTestDefaultTimeLimit();
  document.querySelectorAll('.editor-time-limit').forEach((select) => {
    const inherit = select.querySelector('option[value=""]');
    if (inherit) inherit.textContent = `Use test default (${testDefault}s)`;
  });
  updateEstimatedLength();
}

function updateEstimatedLength() {
  const el = $('#estimated-length');
  if (!el) return;
  const testDefault = getTestDefaultTimeLimit();
  const cards = Array.from(document.querySelectorAll('.question-editor-card'));
  if (!cards.length) {
    el.textContent = 'Estimated quiz length: —';
    return;
  }
  // Each question costs its own limit plus the 3 s ready countdown and the
  // 5 s reveal pause.
  const totalSeconds = cards.reduce((sum, card) => {
    const select = card.querySelector('.editor-time-limit');
    const own = select && select.value ? clampTimeLimit(select.value) : testDefault;
    return sum + own + 3 + 5;
  }, 0);
  const minutes = Math.round(totalSeconds / 60);
  el.textContent = minutes < 1
    ? `Estimated quiz length: under a minute (${cards.length} question${cards.length === 1 ? '' : 's'})`
    : `Estimated quiz length: ${minutes} min (${cards.length} question${cards.length === 1 ? '' : 's'})`;
}

function bindTimingControls() {
  const select = $('#test-default-time');
  const custom = $('#test-default-time-custom');
  const customWrap = $('#test-default-time-custom-wrap');
  if (!select || select.dataset.bound === '1') return;
  select.dataset.bound = '1';
  select.addEventListener('change', () => {
    if (customWrap) customWrap.hidden = select.value !== 'custom';
    refreshTimingLabels();
  });
  if (custom) custom.addEventListener('input', refreshTimingLabels);
}

function addQuestionEditor(data = { q: '', options: ['', '', '', ''], correct: 0, explanation: '' }) {
  const container = $('#question-editor-list');
  const card = document.createElement('div');
  card.className = 'question-editor-card';
  card.innerHTML = `
    <div class="question-editor-header">
      <h3 class="question-editor-title">Question</h3>
      <button type="button" class="question-remove-btn">Remove</button>
    </div>
    <label class="input-label">Question text</label>
    <textarea class="editor-textarea editor-question" rows="3" placeholder="Type the question here..."></textarea>
    <div class="editor-options-grid">
      <div>
        <label class="input-label">Option A</label>
        <input class="editor-input editor-option" data-opt="0" type="text" placeholder="Option A">
      </div>
      <div>
        <label class="input-label">Option B</label>
        <input class="editor-input editor-option" data-opt="1" type="text" placeholder="Option B">
      </div>
      <div>
        <label class="input-label">Option C</label>
        <input class="editor-input editor-option" data-opt="2" type="text" placeholder="Option C">
      </div>
      <div>
        <label class="input-label">Option D</label>
        <input class="editor-input editor-option" data-opt="3" type="text" placeholder="Option D">
      </div>
    </div>
    <div class="editor-row-two">
      <div>
        <label class="input-label">Correct answer</label>
        <select class="editor-select editor-correct">
          <option value="0">Option A</option>
          <option value="1">Option B</option>
          <option value="2">Option C</option>
          <option value="3">Option D</option>
        </select>
      </div>
      <div>
        <label class="input-label">Time for this question</label>
        <select class="editor-select editor-time-limit">${buildQuestionTimeOptions(data.time_limit)}</select>
      </div>
      <div class="editor-grow">
        <label class="input-label">Explanation</label>
        <textarea class="editor-textarea editor-explanation" rows="2" placeholder="Short explanation shown after answering..."></textarea>
      </div>
    </div>
  `;
  const timeSelect = card.querySelector('.editor-time-limit');
  timeSelect.value = data.time_limit ? String(clampTimeLimit(data.time_limit)) : '';
  timeSelect.addEventListener('change', updateEstimatedLength);
  card.querySelector('.editor-question').value = data.q || '';
  card.querySelector('.editor-correct').value = String(data.correct || 0);
  const optionInputs = card.querySelectorAll('.editor-option');
  optionInputs.forEach((input, idx) => {
    input.value = (data.options && data.options[idx]) || '';
  });
  card.querySelector('.editor-explanation').value = data.explanation || '';
  card.querySelector('.question-remove-btn').addEventListener('click', () => {
    const total = container.querySelectorAll('.question-editor-card').length;
    if (total <= 1) {
      showInlineStatus('host-create-status', 'A test needs at least one question.', true);
      return;
    }
    if (!confirm('Are you sure you want to remove this question?')) return;
    card.remove();
    refreshQuestionEditorLabels();
    markDraftDirty();
  });
  container.appendChild(card);
  refreshQuestionEditorLabels();
  updateEstimatedLength();
}

function refreshQuestionEditorLabels() {
  document.querySelectorAll('.question-editor-card').forEach((card, index) => {
    const title = card.querySelector('.question-editor-title');
    if (title) title.textContent = `Question ${index + 1}`;
  });
}

function collectTestFormPayload() {
  const title = $('#test-title-input').value.trim();
  const chapter = $('#test-chapter-input').value.trim();
  const description = $('#test-description-input').value.trim();
  const questionCards = Array.from(document.querySelectorAll('.question-editor-card'));
  const questions = questionCards.map((card) => {
    const q = card.querySelector('.editor-question').value.trim();
    const options = Array.from(card.querySelectorAll('.editor-option')).map((input) => input.value.trim());
    const correct = Number(card.querySelector('.editor-correct').value);
    const explanation = card.querySelector('.editor-explanation').value.trim();
    return { q, options, correct, explanation, time_limit: readQuestionTimeLimit(card) };
  });
  return { title, chapter, description, questions, default_time_limit: getTestDefaultTimeLimit() };
}

function updateHostLobbyHeading() {
  $('#host-lobby-title').textContent = `${selectedSubject.name} (${selectedSubject.code})`;
  const chapter = selectedTest && selectedTest.chapter ? ` — ${selectedTest.chapter}` : '';
  $('#host-lobby-subtitle').textContent = selectedTest ? `${selectedTest.title}${chapter}` : 'Lecturer Control Panel';
  $('#host-active-test-pill').textContent = selectedTest
    ? `${selectedTest.title}${chapter} • ${selectedTest.questionCount || 0} questions`
    : 'No test selected';
}

function getDefaultSessionName(testSummary) {
  const title = testSummary && testSummary.title ? testSummary.title : 'Quiz Session';
  return `${title} - ${new Date().toLocaleDateString()}`;
}

function hideSessionNameModal() {
  const modal = $('#session-name-modal');
  if (!modal) return;
  modal.hidden = true;
}

function promptSessionName(testSummary) {
  selectedTest = testSummary;
  const modal = $('#session-name-modal');
  const titleEl = $('#modal-test-label');
  const input = $('#session-name-input');
  const confirmBtn = $('#btn-session-confirm');
  const cancelBtn = $('#btn-session-cancel');
  const defaultValue = getDefaultSessionName(testSummary).slice(0, 80);

  if (!modal || !titleEl || !input || !confirmBtn || !cancelBtn) {
    sessionName = defaultValue;
    startHostForTest(testSummary);
    return;
  }

  sessionName = '';
  titleEl.textContent = testSummary && testSummary.title ? testSummary.title : 'Selected test';
  input.value = defaultValue;
  modal.hidden = false;

  const closeModal = () => {
    sessionName = '';
    hideSessionNameModal();
  };

  const startSession = () => {
    sessionName = (input.value.trim() || defaultValue).slice(0, 80);
    hideSessionNameModal();
    startHostForTest(testSummary);
  };

  const confirmClone = confirmBtn.cloneNode(true);
  confirmBtn.replaceWith(confirmClone);
  const cancelClone = cancelBtn.cloneNode(true);
  cancelBtn.replaceWith(cancelClone);

  confirmClone.addEventListener('click', startSession);
  cancelClone.addEventListener('click', closeModal);
  modal.onclick = (event) => {
    if (event.target === modal) closeModal();
  };
  input.onkeydown = (event) => {
    if (event.key === 'Enter') {
      event.preventDefault();
      startSession();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      closeModal();
    }
  };

  requestAnimationFrame(() => {
    input.focus();
    input.select();
  });
}

function buildPlayerJoinURL(subjectCode, token = '') {
  const playerURL = new URL(location.origin + location.pathname);
  if (token) {
    playerURL.searchParams.set('token', token);
  } else if (subjectCode) {
    playerURL.searchParams.set('subject', subjectCode);
  }
  return playerURL;
}

function renderHostJoinQRCode(playerURL) {
  const qrContainer = $('#qr-code');
  if (!qrContainer) return;
  qrContainer.innerHTML = '';
  const urlText = $('#qr-url-text');
  if (urlText) urlText.textContent = playerURL.toString();
  try {
    if (typeof QRCode === 'undefined') {
      throw new Error('QRCode library not loaded');
    }
    new QRCode(qrContainer, {
      text: playerURL.toString(),
      width: 480,
      height: 480,
      colorDark: '#1a1027',
      colorLight: '#ffffff',
      correctLevel: QRCode.CorrectLevel.M
    });
  } catch (e) {
    qrContainer.textContent = 'QR code unavailable — students can use the link below.';
    console.error('QR code generation failed:', e);
  }
}

async function requestHostSessionToken(subjectCode) {
  const tokenResp = await apiPost(`/api/session-token/${encodeURIComponent(subjectCode)}`, {});
  const token = (tokenResp.token || '').trim().toUpperCase();
  if (!token) {
    throw new Error('Token generation returned an empty token.');
  }
  return token;
}

async function startHostForTest(testSummary) {
  selectedTest = testSummary;
  sessionName = (sessionName || getDefaultSessionName(testSummary)).trim().slice(0, 80);
  sessionToken = '';
  clearHostGameCodeCountdown();
  setHostGameCode('');
  statsAutoDownloaded = false;
  hideSessionNameModal();
  showScreen('screen-host-lobby');
  updateHostLobbyHeading();
  updateHostAccountBar();

  try {
    sessionToken = await requestHostSessionToken(hostSubjectCode);
  } catch (e) {
    sessionToken = '';
    console.warn('Could not generate session token, falling back to subject URL', e);
  }

  // Tell the server this is a deliberate new session, not a dropped socket
  // reconnecting. A reconnect must never reset a live game, but a new session
  // must clear a room still sitting in the previous game's finished state.
  let firstHostJoin = true;
  connectWS(() => {
    send({
      action: 'host_join',
      subject: hostSubjectCode,
      testId: selectedTest.id,
      sessionName: sessionName || '',
      token: sessionToken || '',
      newSession: firstHostJoin
    });
    firstHostJoin = false;
  });

  renderHostJoinQRCode(buildPlayerJoinURL(hostSubjectCode, sessionToken));

  const regenBtn = $('#btn-regenerate-qr');
  if (regenBtn) {
    const newRegen = regenBtn.cloneNode(true);
    regenBtn.replaceWith(newRegen);
    newRegen.addEventListener('click', async () => {
      newRegen.disabled = true;
      newRegen.textContent = 'Regenerating...';
      try {
        sessionToken = await requestHostSessionToken(hostSubjectCode);
        renderHostJoinQRCode(buildPlayerJoinURL(hostSubjectCode, sessionToken));
      } catch (e) {
        console.error('Could not regenerate QR code', e);
      } finally {
        newRegen.disabled = false;
        newRegen.textContent = 'Regenerate QR Code';
      }
    });
  }

  const startBtn = $('#btn-start-game');
  const newStart = startBtn.cloneNode(true);
  startBtn.replaceWith(newStart);
  newStart.addEventListener('click', () => {
    send({
      action: 'start_game',
      shuffle: $('#chk-shuffle-questions') ? $('#chk-shuffle-questions').checked : false,
      useCode: $('#chk-use-game-code') ? $('#chk-use-game-code').checked : false
    });
    newStart.disabled = true;
    newStart.textContent = 'Starting...';
  });

  const extendBtn = $('#btn-extend-time');
  if (extendBtn) {
    const newExtend = extendBtn.cloneNode(true);
    extendBtn.replaceWith(newExtend);
    newExtend.addEventListener('click', () => {
      send({ action: 'extend_time', seconds: 15 });
      newExtend.disabled = true;
      newExtend.textContent = '+15s added';
      setTimeout(() => {
        newExtend.disabled = false;
        newExtend.textContent = '+15 seconds';
      }, 1200);
    });
  }

  const nextBtn = $('#btn-next-question');
  const newNext = nextBtn.cloneNode(true);
  nextBtn.replaceWith(newNext);
  newNext.addEventListener('click', () => send({ action: 'next_question' }));

  bindConfirmAction('#btn-end-game', 'End the game now and show the final leaderboard?', 'end_game');
  bindConfirmAction('#btn-end-game-reveal', 'End the game now and show the final leaderboard?', 'end_game');
  bindConfirmAction('#btn-cancel-game', 'Cancel this game and return everyone to the lobby?', 'cancel_game');
  bindConfirmAction('#btn-cancel-game-reveal', 'Cancel this game and return everyone to the lobby?', 'cancel_game');

  const playAgainBtn = $('#btn-play-again');
  const newPlayAgain = playAgainBtn.cloneNode(true);
  playAgainBtn.replaceWith(newPlayAgain);
  newPlayAgain.addEventListener('click', () => send({ action: 'reset_game' }));

  setupStatsDownload('#btn-download-stats');
  setupStatsDownload('#btn-download-stats-final');

  const changeBtn = $('#btn-change-test');
  const newChange = changeBtn.cloneNode(true);
  changeBtn.replaceWith(newChange);
  newChange.addEventListener('click', () => showHostTestLibrary());

  const changeBtnFinal = $('#btn-change-test-final');
  const newChangeFinal = changeBtnFinal.cloneNode(true);
  changeBtnFinal.replaceWith(newChangeFinal);
  newChangeFinal.addEventListener('click', () => showHostTestLibrary());
}

function bindConfirmAction(selector, prompt, action) {
  const btn = $(selector);
  if (!btn) return;
  const clone = btn.cloneNode(true);
  btn.replaceWith(clone);
  clone.addEventListener('click', () => {
    if (confirm(prompt)) send({ action });
  });
}

function setupStatsDownload(selector) {
  const btn = $(selector);
  if (!btn) return;
  const clone = btn.cloneNode(true);
  btn.replaceWith(clone);
  clone.addEventListener('click', () => {
    if (!hostSubjectCode) return;
    downloadStatsNow(hostSubjectCode, clone);
  });
}

function downloadStatsNow(subjectCode, buttonEl) {
  const url = `${API_BASE}/api/stats/${subjectCode}`;
  if (buttonEl) buttonEl.textContent = 'Downloading...';
  fetch(url, { credentials: 'same-origin' })
    .then(async (resp) => {
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || detail.error || 'Download failed');
      }
      const disposition = resp.headers.get('content-disposition') || '';
      const match = /filename="?([^"]+)"?/i.exec(disposition);
      const filename = match ? match[1] : `Stats_${subjectCode}.xlsx`;
      const blob = await resp.blob();
      return { blob, filename };
    })
    .then(({ blob, filename }) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
      if (buttonEl) buttonEl.textContent = 'Download Stats (Excel)';
    })
    .catch((err) => {
      if (buttonEl) {
        buttonEl.textContent = err.message || 'Download failed';
        setTimeout(() => {
          buttonEl.textContent = 'Download Stats (Excel)';
        }, 3000);
      }
    });
}

function downloadTestsBackup(buttonEl) {
  const url = `${API_BASE}/api/export/tests`;
  const originalLabel = buttonEl ? buttonEl.textContent : '';
  if (buttonEl) buttonEl.textContent = 'Preparing backup...';
  fetch(url, { credentials: 'same-origin' })
    .then(async (resp) => {
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || detail.error || 'Backup failed');
      }
      const disposition = resp.headers.get('content-disposition') || '';
      const match = /filename="?([^"]+)"?/i.exec(disposition);
      const filename = match ? match[1] : 'quiz_backup.json';
      const blob = await resp.blob();
      return { blob, filename };
    })
    .then(({ blob, filename }) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = filename;
      a.click();
      URL.revokeObjectURL(a.href);
      if (buttonEl) buttonEl.textContent = originalLabel || 'Backup My Tests';
    })
    .catch((err) => {
      showInlineStatus('#host-library-status', err.message || 'Backup failed.', true);
      if (buttonEl) {
        buttonEl.textContent = err.message || 'Backup failed';
        setTimeout(() => {
          buttonEl.textContent = originalLabel || 'Backup My Tests';
        }, 3000);
      }
    });
}

function handleHostMessage(msg) {
  switch (msg.type) {
    case 'host_joined':
      clearHostGameCodeCountdown();
      if (msg.gameCodeEnabled && msg.gameCode) {
        setHostGameCode(msg.gameCode);
      } else {
        setHostGameCode('');
      }
      if (msg.selectedTest) {
        selectedTest = { ...selectedTest, ...msg.selectedTest };
        updateHostLobbyHeading();
      }
      updateHostLobby(msg.players, msg.selectedTest || selectedTest);
      if (msg.phase === 'lobby') {
        showScreen('screen-host-lobby');
        if (msg.sessionToken) {
          sessionToken = msg.sessionToken;
        }
        renderHostJoinQRCode(buildPlayerJoinURL(hostSubjectCode, sessionToken));
      }
      $('#btn-download-stats').hidden = !msg.hasStats;
      if (!msg.hasQuestions) {
        const startBtn = $('#btn-start-game');
        startBtn.disabled = true;
        startBtn.textContent = 'This test has no questions';
      }
      break;
    case 'game_code_display':
      hostShowGameCode(msg.code, msg.countdown);
      break;
    case 'player_update':
      if (msg.activeTest) {
        selectedTest = { ...selectedTest, ...msg.activeTest };
        updateHostLobbyHeading();
      }
      updateHostLobby(msg.players, msg.activeTest || selectedTest);
      break;
    case 'get_ready':
      hostGetReady(msg.qNum, msg.totalQ);
      break;
    case 'question':
      hostShowQuestion(msg);
      break;
    case 'answer_count':
      $('#host-answered-count').textContent = msg.answered;
      $('#host-total-players').textContent = msg.total;
      break;
    case 'pause_state': {
      document.querySelectorAll('.btn-pause-game').forEach((pauseBtn) => {
        pauseBtn.textContent = msg.paused ? '▶ Resume' : '⏸ Pause';
      });
      if (msg.paused) {
        // Freeze the host timer bar
        if (hostTimerInterval) {
          clearInterval(hostTimerInterval);
          hostTimerInterval = null;
        }
      } else {
        // Resume the host timer bar from wherever hostTimeLeft currently is
        if (!hostTimerInterval && hostTimeLeft > 0) {
          hostTimerInterval = setInterval(tickHostTimer, 100);
        }
      }
      break;
    }
    case 'time_extended': {
      hostQuestionTimeLimit = Number(msg.timeLimit) || hostQuestionTimeLimit;
      hostTimeLeft = Number(msg.remaining) || hostTimeLeft;
      const limitEl = $('#host-q-limit');
      if (limitEl) limitEl.textContent = `${Math.round(hostQuestionTimeLimit)}s for this question`;
      if (!hostTimerInterval && hostTimeLeft > 0) {
        hostTimerInterval = setInterval(tickHostTimer, 100);
      }
      break;
    }
    case 'reveal':
      hostShowReveal(msg);
      break;
    case 'final':
      hostShowFinal(msg.leaderboard);
      $('#btn-download-stats-final').hidden = !msg.hasStats;
      if (!statsAutoDownloaded && msg.hasStats && hostSubjectCode) {
        statsAutoDownloaded = true;
        const btn = $('#btn-download-stats-final');
        downloadStatsNow(hostSubjectCode, btn);
      }
      break;
  }
}

function updateHostLobby(players, activeTest = selectedTest) {
  const count = players ? players.length : 0;
  $('#host-player-count').textContent = count;
  const startBtn = $('#btn-start-game');
  const qCount = activeTest && activeTest.questionCount ? activeTest.questionCount : 0;
  if (qCount > 0) {
    startBtn.disabled = count === 0;
    startBtn.textContent = count === 0 ? 'Waiting for players…' : `Start Game (${count} players)`;
  } else {
    startBtn.disabled = true;
    startBtn.textContent = 'This test has no questions';
  }

  const list = $('#host-player-list');
  if (!players || players.length === 0) {
    list.innerHTML = '<p class="empty-msg">Waiting for students to join...</p>';
    return;
  }
  list.innerHTML = '';
  players.forEach((p) => {
    const el = document.createElement('div');
    el.className = 'host-player-item';
    const actionLabel = p.connected ? 'Kick' : 'Remove';
    el.innerHTML = `
      <span class="host-player-dot ${p.connected ? 'connected' : 'disconnected'}"></span>
      <span class="host-player-name-text">${escapeHtml(p.name)}</span>
      <button type="button" class="host-player-kick-btn">${actionLabel}</button>
    `;
    const kickBtn = el.querySelector('.host-player-kick-btn');
    if (kickBtn) {
      kickBtn.addEventListener('click', () => {
        const prompt = p.connected
          ? `Kick ${p.name} out of this session?`
          : `Remove ${p.name} from this session?`;
        if (!confirm(prompt)) return;
        send({ action: 'kick_player', playerId: p.id });
      });
    }
    list.appendChild(el);
  });
}

function hostGetReady(qNum, totalQ) {
  clearHostGameCodeCountdown();
  showScreen('screen-host-question');
  document.querySelectorAll('.host-game-code-bar').forEach((bar) => {
    bar.hidden = !hostGameCode;
  });
  $('#host-q-num').textContent = `Q${qNum} / ${totalQ}`;
  $('#host-timer').textContent = '...';
  $('#host-q-text').textContent = 'Get Ready...';
  $('#host-answers').innerHTML = '';
  $('#host-answered-count').textContent = '0';
  $('#host-total-players').textContent = '0';
  $('#host-timer-bar').style.width = '100%';
}

function hostShowQuestion(msg) {
  clearHostGameCodeCountdown();
  showScreen('screen-host-question');
  document.querySelectorAll('.host-game-code-bar').forEach((bar) => {
    bar.hidden = !hostGameCode;
  });
  $('#host-q-num').textContent = `Q${msg.qNum} / ${msg.totalQ}`;
  $('#host-q-text').textContent = msg.question;
  $('#host-answered-count').textContent = '0';
  hostCorrectAnswer = msg.correctAnswer;
  hostCurrentOptions = msg.options;
  hostCurrentQuestion = msg.question;

  const grid = $('#host-answers');
  grid.innerHTML = '';
  msg.options.forEach((opt, i) => {
    const div = document.createElement('div');
    div.className = `host-answer-option ${COLORS[i]}`;
    div.innerHTML = `<span class="shape">${SHAPES[i]}</span><span>${escapeHtml(opt)}</span>`;
    grid.appendChild(div);
  });

  hostQuestionTimeLimit = Number(msg.timeLimit) > 0 ? Number(msg.timeLimit) : TIME_PER_Q;
  hostTimeLeft = hostQuestionTimeLimit;
  const limitEl = $('#host-q-limit');
  if (limitEl) limitEl.textContent = `${Math.round(hostQuestionTimeLimit)}s for this question`;
  if (hostTimerInterval) clearInterval(hostTimerInterval);
  hostTimerInterval = setInterval(tickHostTimer, 100);
}

function tickHostTimer() {
  hostTimeLeft -= 0.1;
  if (hostTimeLeft <= 0) {
    hostTimeLeft = 0;
    clearInterval(hostTimerInterval);
    hostTimerInterval = null;
  }
  const limit = hostQuestionTimeLimit > 0 ? hostQuestionTimeLimit : TIME_PER_Q;
  const pct = Math.max(0, Math.min(100, (hostTimeLeft / limit) * 100));
  $('#host-timer-bar').style.width = `${pct}%`;
  $('#host-timer').textContent = Math.ceil(hostTimeLeft);
}

function hostShowReveal(msg) {
  clearHostGameCodeCountdown();
  if (hostTimerInterval) {
    clearInterval(hostTimerInterval);
    hostTimerInterval = null;
  }
  showScreen('screen-host-reveal');
  document.querySelectorAll('.host-game-code-bar').forEach((bar) => {
    bar.hidden = !hostGameCode;
  });
  const answerEl = $('#host-reveal-answer');
  const correctIdx = msg.correctAnswer !== undefined ? msg.correctAnswer : hostCorrectAnswer;
  const correctText = hostCurrentOptions[correctIdx] || '';
  const shape = SHAPES[correctIdx] || '';
  const color = COLORS[correctIdx] || '';
  answerEl.innerHTML = `
    <p class="reveal-question-text">${escapeHtml(hostCurrentQuestion)}</p>
    <div class="reveal-correct-answer ${color}">
      <span class="shape">${shape}</span>
      <span>${escapeHtml(correctText)} ✓</span>
    </div>
  `;
  $('#host-reveal-explanation').textContent = msg.explanation;
  renderAnswerDistribution(msg, correctIdx);
  renderLeaderboardList($('#host-reveal-leaderboard'), msg.leaderboard, null);

  let autoCountdown = Math.max(1, Math.round(msg.revealSeconds || 5));
  const countdownEl = $('#host-auto-countdown');
  countdownEl.textContent = `Next question in ${autoCountdown}s...`;
  countdownEl.style.display = 'block';
  revealCountdownInterval = setInterval(() => {
    autoCountdown -= 1;
    if (autoCountdown <= 0) {
      clearInterval(revealCountdownInterval);
      revealCountdownInterval = null;
      countdownEl.textContent = 'Loading next question...';
    } else {
      countdownEl.textContent = `Next question in ${autoCountdown}s...`;
    }
  }, 1000);
}

/**
 * How many students picked each option. The single most useful teaching signal
 * in a live quiz — it shows at a glance which distractor caught the class.
 */
function renderAnswerDistribution(msg, correctIdx) {
  const container = $('#host-answer-distribution');
  const title = $('#host-distribution-title');
  if (!container) return;
  const counts = Array.isArray(msg.distribution) ? msg.distribution : null;
  const options = Array.isArray(msg.options) && msg.options.length ? msg.options : hostCurrentOptions;
  if (!counts || !options || !options.length) {
    container.hidden = true;
    if (title) title.hidden = true;
    return;
  }
  const total = counts.reduce((sum, n) => sum + n, 0);
  container.innerHTML = options.map((opt, i) => {
    const count = counts[i] || 0;
    const pct = total ? Math.round((count / total) * 100) : 0;
    const isCorrect = i === correctIdx;
    return `
      <div class="dist-row${isCorrect ? ' is-correct' : ''}">
        <span class="dist-shape ${COLORS[i]}">${SHAPES[i] || ''}</span>
        <span class="dist-label">${escapeHtml(opt)}</span>
        <span class="dist-bar-wrap"><span class="dist-bar ${COLORS[i]}" style="width:${pct}%"></span></span>
        <span class="dist-count">${count}${total ? ` (${pct}%)` : ''}</span>
      </div>`;
  }).join('') + (total === 0 ? '<p class="empty-msg">Nobody answered this question.</p>' : '');
  container.hidden = false;
  if (title) title.hidden = false;
}

let lastReview = null;

function playerShowReview() {
  if (!Array.isArray(lastReview) || !lastReview.length) return;
  showScreen('screen-review');
  const wrong = lastReview.filter((entry) => !entry.wasCorrect).length;
  const summary = $('#review-summary');
  if (summary) {
    summary.textContent = wrong === 0
      ? `You answered all ${lastReview.length} questions correctly.`
      : `You got ${lastReview.length - wrong} of ${lastReview.length} right. Here is what to look at again.`;
  }
  const list = $('#review-list');
  list.innerHTML = lastReview.map((entry) => {
    const yours = entry.options[entry.yourChoice];
    const right = entry.options[entry.correct];
    const yourLine = entry.wasCorrect
      ? `<p class="review-your correct">Your answer: ${escapeHtml(yours || '—')} ✓</p>`
      : `<p class="review-your wrong">Your answer: ${escapeHtml(yours || 'no answer')} ✗</p>`;
    const rightLine = entry.wasCorrect
      ? ''
      : `<p class="review-correct">Correct answer: ${escapeHtml(right || '')}</p>`;
    const explanation = entry.explanation
      ? `<p class="review-explanation">${escapeHtml(entry.explanation)}</p>`
      : '';
    return `
      <div class="review-item${entry.wasCorrect ? ' is-correct' : ' is-wrong'}">
        <p class="review-question"><strong>Q${entry.qNum}.</strong> ${escapeHtml(entry.question)}</p>
        ${yourLine}
        ${rightLine}
        ${explanation}
      </div>`;
  }).join('');

  const back = $('#btn-review-back');
  if (back) back.onclick = () => showScreen('screen-final');
}

function hostShowFinal(lb) {
  clearHostGameCodeCountdown();
  setHostGameCode('');
  if (hostTimerInterval) {
    clearInterval(hostTimerInterval);
    hostTimerInterval = null;
  }
  showScreen('screen-host-final');
  renderPodium($('#host-final-podium'), lb);
  renderFullList($('#host-final-list'), lb.slice(3), null, 4);
}

function hostShowGameCode(code, countdown) {
  clearHostGameCodeCountdown();
  setHostGameCode(code || '');
  showScreen('screen-host-game-code');
  $('#host-game-code-display').textContent = code || '----';
  let remaining = countdown;
  $('#host-code-countdown').textContent = `Game starts in ${remaining}s`;
  hostGameCodeCountdownInterval = setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      clearHostGameCodeCountdown();
      $('#host-code-countdown').textContent = 'Game starting...';
    } else {
      $('#host-code-countdown').textContent = `Game starts in ${remaining}s`;
    }
  }, 1000);
}

// ════════════════════════════════════════════════════════════
// SHARED RENDERING
// ════════════════════════════════════════════════════════════

function renderLeaderboardList(container, lb, myId) {
  container.innerHTML = '';
  lb.forEach((p, i) => {
    const row = document.createElement('div');
    row.className = `lb-row${p.id === myId ? ' is-you' : ''}`;
    row.innerHTML = `
      <span class="lb-rank">${p.rank || i + 1}</span>
      <span class="lb-name">${escapeHtml(p.name)}${p.id === myId ? ' (You)' : ''}</span>
      <span class="lb-score">${p.score.toLocaleString()}</span>
    `;
    container.appendChild(row);
  });
}

function renderPodium(container, lb) {
  container.innerHTML = '';
  const medals = ['🥇', '🥈', '🥉'];
  const classes = ['gold', 'silver', 'bronze'];
  const order = [1, 0, 2];
  order.forEach((pos) => {
    if (lb[pos]) {
      const item = document.createElement('div');
      item.className = `podium-item ${classes[pos]}`;
      item.innerHTML = `
        <span class="podium-medal">${medals[pos]}</span>
        <span class="podium-name">${escapeHtml(lb[pos].name)}</span>
        <span class="podium-score">${lb[pos].score.toLocaleString()} pts</span>
      `;
      container.appendChild(item);
    }
  });
}

function renderFullList(container, items, myId, startRank) {
  container.innerHTML = '';
  items.forEach((p, i) => {
    const row = document.createElement('div');
    row.className = `lb-row${p.id === myId ? ' is-you' : ''}`;
    row.innerHTML = `
      <span class="lb-rank">${startRank + i}</span>
      <span class="lb-name">${escapeHtml(p.name)}${p.id === myId ? ' (You)' : ''}</span>
      <span class="lb-score">${p.score.toLocaleString()}</span>
    `;
    container.appendChild(row);
  });
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

window.addEventListener('beforeunload', (e) => {
  const inEditor = !!document.querySelector('#screen-host-create-test.active');
  if (!inEditor || !draftDirty) return;
  // Capture the browser copy on the way out — this is the last chance.
  if (selectedSubject) writeLocalDraft(selectedSubject.code, collectDraftFormPayload());
  e.preventDefault();
  e.returnValue = '';
  return '';
});

window.addEventListener('DOMContentLoaded', async () => {
  await ensureVisitorToken();
  bindHostAuthUI();
  window.addEventListener('popstate', (e) => {
    if (myPlayerId) {
      history.pushState({ quizActive: true }, '', location.href);
    }
  });
  if (location.hash === '#host') {
    await enterHostArea();
  } else {
    initPlayer();
  }
});
