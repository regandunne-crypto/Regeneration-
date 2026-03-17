/* ============================================================
   Engineering Quiz — Multi-subject real-time multiplayer quiz
   Extended host flow: subject → saved test → lobby/game.
   ============================================================ */

const SHAPES = ['◆', '●', '▲', '■'];
const COLORS = ['color-0', 'color-1', 'color-2', 'color-3'];
const TIME_PER_Q = 30;

function getOrCreateVisitorId() {
  try {
    const key = 'quiz_visitor_id';
    const existing = localStorage.getItem(key);
    if (existing) return existing;
    const created = (crypto && crypto.randomUUID) ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`;
    localStorage.setItem(key, created);
    return created;
  } catch (e) {
    return `${Date.now()}-${Math.random()}`;
  }
}

const WS_PROTOCOL = location.protocol === 'https:' ? 'wss:' : 'ws:';
const VISITOR_ID = getOrCreateVisitorId();
const WS_URL = `${WS_PROTOCOL}//${location.host}/ws?visitorId=${encodeURIComponent(VISITOR_ID)}`;
const API_BASE = location.origin;

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
let selectedSubject = null;
let selectedTest = null;
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
let playerAnswered = false;
let statsAutoDownloaded = false;

const SUBJECT_COLORS = {
  MEC105B: { bg: 'var(--accent-blue)', icon: '⚙️' },
  '1EM105B': { bg: 'var(--accent-purple)', icon: '🔧' },
  DYN317B: { bg: 'var(--accent-orange)', icon: '🚀' }
};
const DEFAULT_SUBJECT_COLOR = { bg: 'var(--accent-green)', icon: '📚' };

function showScreen(id) {
  document.querySelectorAll('.screen').forEach((s) => s.classList.remove('active'));
  const el = $(`#${id}`);
  if (el) el.classList.add('active');
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

  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    startWsPing();
    if (wsOnOpen) wsOnOpen();
  };
  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    handleMessage(msg);
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

function handleMessage(msg) {
  if (msg.type === 'pong') return;
  if (msg.type === 'auth_required') {
    lecturerSession = null;
    updateHostAccountBar();
    showHostAuthScreen('login', msg.message || 'Please sign in as a lecturer.');
    return;
  }
  if (msg.type === 'error') {
    showInlineStatus('#host-library-status', msg.message, true);
    showInlineStatus('#host-create-status', msg.message, true);
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
  const el = $(selector);
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
  const resp = await fetch(`${API_BASE}${path}`);
  return await parseApiResponse(resp);
}

async function apiPost(path, payload = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  return await parseApiResponse(resp);
}

async function apiPut(path, payload = {}) {
  const resp = await fetch(`${API_BASE}${path}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  return await parseApiResponse(resp);
}

async function apiDelete(path) {
  const resp = await fetch(`${API_BASE}${path}`, { method: 'DELETE' });
  return await parseApiResponse(resp);
}

function renderSubjectCards(containerId, subjects, onSelect) {
  const container = $(`#${containerId}`);
  container.innerHTML = '';
  subjects.forEach((sub) => {
    const colors = SUBJECT_COLORS[sub.code] || DEFAULT_SUBJECT_COLOR;
    const card = document.createElement('button');
    card.className = 'subject-card';
    card.style.setProperty('--card-accent', colors.bg);

    let metaText = '';
    if (typeof sub.testCount === 'number') {
      metaText = sub.testCount === 1 ? '1 saved test' : `${sub.testCount} saved tests`;
    } else {
      metaText = sub.questionCount > 0 ? `${sub.questionCount} questions` : 'No questions yet';
    }

    card.innerHTML = `
      <span class="subject-icon">${colors.icon}</span>
      <div class="subject-info">
        <span class="subject-name">${escapeHtml(sub.name)}</span>
        <span class="subject-code-label">${escapeHtml(sub.code)}</span>
        <span class="subject-q-count">${escapeHtml(metaText)}</span>
      </div>
      <span class="subject-arrow">→</span>
    `;
    card.addEventListener('click', () => onSelect(sub));
    container.appendChild(card);
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
  showScreen('screen-subject');
  const subjects = await loadSubjects();
  renderSubjectCards('subject-list', subjects, (sub) => {
    selectedSubject = sub;
    showPlayerJoinScreen();
  });

  const preselected = getPrefilledSubjectFromURL(subjects);
  if (preselected) {
    selectedSubject = preselected;
    showPlayerJoinScreen();
  }
}

function showPlayerJoinScreen() {
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

  $('#btn-back-subject').onclick = () => {
    selectedSubject = null;
    showScreen('screen-subject');
  };

  checkReady();
  setTimeout(checkReady, 0);
  setTimeout(checkReady, 150);
  setTimeout(checkReady, 600);
  nameInput.focus();
}

function joinAsPlayer() {
  const name = $('#nickname-input').value.trim();
  const studentNum = $('#student-number-input').value.trim();
  if (!name || !studentNum) return;
  myPlayerName = name;
  myStudentNumber = studentNum;
  const errEl = $('#name-error');
  if (errEl) errEl.hidden = true;
  $('#btn-join').disabled = true;
  $('#btn-join').textContent = 'Joining...';

  connectWS(() => {
    send({
      action: 'player_join',
      name: myPlayerName,
      studentNumber: myStudentNumber,
      subject: selectedSubject.code
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
    case 'joined': {
      myPlayerId = msg.playerId;
      $('#lobby-player-name').textContent = myPlayerName;
      $('#lobby-p-count').textContent = msg.playerCount;
      $('#lobby-subject-badge').textContent = formatActiveTestLabel(selectedSubject, msg.activeTest);
      const leaveBtn = $('#btn-leave-lobby');
      if (leaveBtn) leaveBtn.onclick = leaveLobby;
      if (msg.phase === 'lobby') showScreen('screen-lobby-player');
      $('#btn-join').disabled = false;
      $('#btn-join').textContent = 'Join Game';
      break;
    }
    case 'player_update':
      if (msg.players) $('#lobby-p-count').textContent = msg.players.length;
      if (selectedSubject) {
        $('#lobby-subject-badge').textContent = formatActiveTestLabel(selectedSubject, msg.activeTest);
      }
      break;
    case 'get_ready':
      playerGetReady(msg.qNum, msg.totalQ);
      break;
    case 'question':
      playerShowQuestion(msg);
      break;
    case 'answer_result':
      playerShowResult(msg);
      break;
    case 'leaderboard':
      playerShowLeaderboard(msg.leaderboard);
      break;
    case 'final':
      playerShowFinal(msg.leaderboard);
      break;
    case 'reset':
      if (typeof msg.playerCount === 'number') $('#lobby-p-count').textContent = msg.playerCount;
      if (selectedSubject) $('#lobby-subject-badge').textContent = formatActiveTestLabel(selectedSubject, msg.activeTest);
      showScreen('screen-lobby-player');
      break;
    case 'left':
      closeWS({ reconnect: false });
      showPlayerJoinScreen();
      break;
  }
}

function playerGetReady(qNum, totalQ) {
  showScreen('screen-ready');
  $('#ready-q-num').textContent = `Question ${qNum} of ${totalQ}`;
  let count = 3;
  $('#ready-count').textContent = count;
  const iv = setInterval(() => {
    count -= 1;
    if (count <= 0) {
      clearInterval(iv);
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

  timeLeft = msg.timeLimit || TIME_PER_Q;
  startPlayerTimer();
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
  const pct = (timeLeft / TIME_PER_Q) * 100;
  const bar = $('#timer-bar');
  const text = $('#timer-text');
  bar.style.width = `${pct}%`;
  text.textContent = Math.ceil(timeLeft);
  if (timeLeft <= 10) {
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
}

function playerShowLeaderboard(lb) {
  showScreen('screen-leaderboard');
  renderLeaderboardList($('#leaderboard-list'), lb, myPlayerId);
}

function playerShowFinal(lb) {
  showScreen('screen-final');
  const myRank = lb.findIndex((p) => p.id === myPlayerId) + 1;
  $('#final-title').textContent = myRank === 1 ? 'You Win! 🏆' : `Game Over — You placed #${myRank}`;
  renderPodium($('#final-podium'), lb);
  renderFullList($('#final-full-list'), lb.slice(3), myPlayerId, 4);
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
  isHost = false;
  selectedTest = null;
  selectedSubject = null;
  hostSubjectCode = null;
  editingTestId = null;
  originalEditingTest = null;
  currentDraftLoaded = null;
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
      await apiPost('/api/lecturer/signup', {
        name: $('#signup-name-input').value.trim(),
        email: $('#signup-email-input').value.trim(),
        password
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

async function initHost() {
  const session = lecturerSession || await fetchLecturerSession();
  if (!session) {
    showHostAuthScreen('login');
    return;
  }
  isHost = true;
  selectedTest = null;
  editingTestId = null;
  currentDraftLoaded = null;
  originalEditingTest = null;
  if (ws) closeWS({ reconnect: false });
  updateHostAccountBar();
  showScreen('screen-host-subject');

  const refreshSubjects = async () => {
    const subjects = await loadSubjects();
    renderSubjectCards('host-subject-list', subjects, (sub) => {
      selectedSubject = sub;
      hostSubjectCode = sub.code;
      showHostTestLibrary();
    });
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
    const secondaryLabel = test.canEdit
      ? 'Edit Test'
      : (test.source === 'built-in' ? 'Edit Test' : 'Duplicate Test');
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
      startHostForTest(test);
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
    await showCreateTestScreen({ mode: 'create' });
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

function getDraftEditingId(draft) {
  return draft?.editing_test_id || draft?.editingTestId || null;
}

function applyEditorData(data = {}) {
  $('#test-title-input').value = data.title || '';
  $('#test-chapter-input').value = data.chapter || '';
  $('#test-description-input').value = data.description || '';
  const questions = Array.isArray(data.questions) && data.questions.length
    ? data.questions
    : [{ q: '', options: ['', '', '', ''], correct: 0, explanation: '' }];
  renderQuestionEditors(questions);
}

function resetDraftStatus(text = 'No draft changes yet.', isError = false) {
  const el = $('#draft-status');
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('error-text', !!isError);
  el.classList.toggle('success-text', !isError && !!text && !text.toLowerCase().includes('unsaved'));
  el.classList.toggle('muted-text', !isError && (!text || text.toLowerCase().includes('unsaved') || text.toLowerCase().includes('no draft')));
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
    return { q, options, correct, explanation };
  });
  return {
    title,
    chapter,
    description,
    questions,
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
  try {
    const resp = await apiPost(`/api/drafts/${encodeURIComponent(selectedSubject.code)}`, payload);
    draftDirty = false;
    currentDraftLoaded = resp.draft || payload;
    const updatedAt = resp.draft?.updated_at || new Date().toISOString();
    resetDraftStatus(`Draft saved ${formatDateTime(updatedAt)}.`, false);
    return resp.draft;
  } catch (e) {
    if (!silent) {
      resetDraftStatus(e.message || 'Draft save failed.', true);
    } else {
      resetDraftStatus('Autosave failed. Use Save Draft before leaving.', true);
    }
    return null;
  }
}

async function discardDraft() {
  if (!selectedSubject) return;
  if (!confirm('Discard the saved draft for this subject?')) return;
  try {
    await apiDelete(`/api/drafts/${encodeURIComponent(selectedSubject.code)}`);
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

  let draft = null;
  try {
    const draftResp = await apiGet(`/api/drafts/${encodeURIComponent(selectedSubject.code)}`);
    draft = draftResp.draft;
  } catch (e) {
    draft = null;
  }

  try {
    if (editorMode === 'edit' && editingTestId) {
      originalEditingTest = await apiGet(`/api/tests/${encodeURIComponent(selectedSubject.code)}/${encodeURIComponent(editingTestId)}`);
      if (draft && getDraftEditingId(draft) === editingTestId) {
        currentDraftLoaded = draft;
        applyEditorData({
          title: draft.title || '',
          chapter: draft.chapter || '',
          description: draft.description || '',
          questions: draft.questions || []
        });
        resetDraftStatus(`Recovered your draft from ${formatDateTime(draft.updated_at)}.`, false);
      } else {
        applyEditorData(originalEditingTest);
        resetDraftStatus('Editing the saved test.', false);
      }
    } else {
      editingTestId = null;
      originalEditingTest = null;
      if (draft && !getDraftEditingId(draft)) {
        currentDraftLoaded = draft;
        applyEditorData({
          title: draft.title || '',
          chapter: draft.chapter || '',
          description: draft.description || '',
          questions: draft.questions || []
        });
        resetDraftStatus(`Recovered your draft from ${formatDateTime(draft.updated_at)}.`, false);
      } else {
        applyEditorData({ title: '', chapter: '', description: '', questions: [] });
        resetDraftStatus('Start building your test. Drafts save automatically.', false);
      }
    }
  } catch (e) {
    showInlineStatus('#host-create-status', e.message, true);
    applyEditorData({ title: '', chapter: '', description: '', questions: [] });
    resetDraftStatus('Could not load the editor data.', true);
  }

  bindEditorInputAutosave();

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

function renderQuestionEditors(questions) {
  const container = $('#question-editor-list');
  container.innerHTML = '';
  const normalized = Array.isArray(questions) && questions.length
    ? questions
    : [{ q: '', options: ['', '', '', ''], correct: 0, explanation: '' }];
  normalized.forEach((q) => addQuestionEditor(q));
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
      <div class="editor-grow">
        <label class="input-label">Explanation</label>
        <textarea class="editor-textarea editor-explanation" rows="2" placeholder="Short explanation shown after answering..."></textarea>
      </div>
    </div>
  `;
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
      showInlineStatus('#host-create-status', 'A test needs at least one question.', true);
      return;
    }
    if (!confirm('Are you sure you want to delete this question?')) return;
    card.remove();
    refreshQuestionEditorLabels();
    markDraftDirty();
  });
  container.appendChild(card);
  refreshQuestionEditorLabels();
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
    return { q, options, correct, explanation };
  });
  return { title, chapter, description, questions };
}

function updateHostLobbyHeading() {
  $('#host-lobby-title').textContent = `${selectedSubject.name} (${selectedSubject.code})`;
  const chapter = selectedTest && selectedTest.chapter ? ` — ${selectedTest.chapter}` : '';
  $('#host-lobby-subtitle').textContent = selectedTest ? `${selectedTest.title}${chapter}` : 'Lecturer Control Panel';
  $('#host-active-test-pill').textContent = selectedTest
    ? `${selectedTest.title}${chapter} • ${selectedTest.questionCount || 0} questions`
    : 'No test selected';
}

function startHostForTest(testSummary) {
  selectedTest = testSummary;
  statsAutoDownloaded = false;
  showScreen('screen-host-lobby');
  updateHostLobbyHeading();
  updateHostAccountBar();

  connectWS(() => {
    send({ action: 'host_join', subject: hostSubjectCode, testId: selectedTest.id });
  });

  const playerURL = new URL(location.origin + location.pathname);
  playerURL.searchParams.set('subject', hostSubjectCode);
  const qrContainer = $('#qr-code');
  qrContainer.innerHTML = '';
  try {
    new QRCode(qrContainer, {
      text: playerURL.toString(),
      width: 160,
      height: 160,
      colorDark: '#1a1027',
      colorLight: '#ffffff',
      correctLevel: QRCode.CorrectLevel.M
    });
  } catch (e) {}
  $('#qr-url-text').textContent = playerURL.toString();

  const startBtn = $('#btn-start-game');
  const newStart = startBtn.cloneNode(true);
  startBtn.replaceWith(newStart);
  newStart.addEventListener('click', () => {
    send({ action: 'start_game' });
    newStart.disabled = true;
    newStart.textContent = 'Starting...';
  });

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
  fetch(url)
    .then(async (resp) => {
      if (!resp.ok) {
        const detail = await resp.json().catch(() => ({}));
        throw new Error(detail.detail || detail.error || 'Download failed');
      }
      return resp.blob();
    })
    .then((blob) => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `Stats_${subjectCode}.xlsx`;
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
  fetch(url)
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
      if (msg.selectedTest) {
        selectedTest = { ...selectedTest, ...msg.selectedTest };
        updateHostLobbyHeading();
      }
      updateHostLobby(msg.players, msg.selectedTest || selectedTest);
      if (msg.phase === 'lobby') showScreen('screen-host-lobby');
      $('#btn-download-stats').hidden = !msg.hasStats;
      if (!msg.hasQuestions) {
        const startBtn = $('#btn-start-game');
        startBtn.disabled = true;
        startBtn.textContent = 'This test has no questions';
      }
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
    el.innerHTML = `
      <span class="host-player-dot ${p.connected ? 'connected' : 'disconnected'}"></span>
      <span class="host-player-name-text">${escapeHtml(p.name)}</span>
    `;
    list.appendChild(el);
  });
}

function hostGetReady(qNum, totalQ) {
  showScreen('screen-host-question');
  $('#host-q-num').textContent = `Q${qNum} / ${totalQ}`;
  $('#host-timer').textContent = '...';
  $('#host-q-text').textContent = 'Get Ready...';
  $('#host-answers').innerHTML = '';
  $('#host-answered-count').textContent = '0';
  $('#host-total-players').textContent = '0';
  $('#host-timer-bar').style.width = '100%';
}

function hostShowQuestion(msg) {
  showScreen('screen-host-question');
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

  let hostTimeLeft = msg.timeLimit || TIME_PER_Q;
  if (hostTimerInterval) clearInterval(hostTimerInterval);
  hostTimerInterval = setInterval(() => {
    hostTimeLeft -= 0.1;
    if (hostTimeLeft <= 0) {
      hostTimeLeft = 0;
      clearInterval(hostTimerInterval);
    }
    const pct = (hostTimeLeft / TIME_PER_Q) * 100;
    $('#host-timer-bar').style.width = `${pct}%`;
    $('#host-timer').textContent = Math.ceil(hostTimeLeft);
  }, 100);
}

function hostShowReveal(msg) {
  if (hostTimerInterval) {
    clearInterval(hostTimerInterval);
    hostTimerInterval = null;
  }
  showScreen('screen-host-reveal');
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
  renderLeaderboardList($('#host-reveal-leaderboard'), msg.leaderboard, null);

  let autoCountdown = 5;
  const countdownEl = $('#host-auto-countdown');
  countdownEl.textContent = `Next question in ${autoCountdown}s...`;
  countdownEl.style.display = 'block';
  const iv = setInterval(() => {
    autoCountdown -= 1;
    if (autoCountdown <= 0) {
      clearInterval(iv);
      countdownEl.textContent = 'Loading next question...';
    } else {
      countdownEl.textContent = `Next question in ${autoCountdown}s...`;
    }
  }, 1000);
}

function hostShowFinal(lb) {
  if (hostTimerInterval) {
    clearInterval(hostTimerInterval);
    hostTimerInterval = null;
  }
  showScreen('screen-host-final');
  renderPodium($('#host-final-podium'), lb);
  renderFullList($('#host-final-list'), lb.slice(3), null, 4);
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

window.addEventListener('DOMContentLoaded', async () => {
  bindHostAuthUI();
  if (location.hash === '#host') {
    await enterHostArea();
  } else {
    initPlayer();
  }
});
