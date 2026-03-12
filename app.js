/* ============================================================
   Engineering Quiz — Multi-subject real-time multiplayer quiz
   Host (lecturer): /#host → subject selection → lobby
   Player (student): / → subject selection → name+student# → lobby
   ============================================================ */

const SHAPES = ['◆', '●', '▲', '■'];
const COLORS = ['color-0', 'color-1', 'color-2', 'color-3'];
const TIME_PER_Q = 30;
const HOST_PASSCODE = 'Regan@1990';
const HOST_AUTH_KEY = 'engineering_quiz_host_auth';

// WebSocket/API URLs — same-origin for both local runs and Render deployments
const WS_PROTOCOL = location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_URL = `${WS_PROTOCOL}//${location.host}/ws`;
const API_BASE = location.origin;

const $ = (sel) => document.querySelector(sel);
let ws = null;
let isHost = false;
let myPlayerId = null;
let timerInterval = null;
let timeLeft = TIME_PER_Q;
let selectedSubject = null; // {code, name, questionCount}

// Subject colors for the cards
const SUBJECT_COLORS = {
  'MEC105B': { bg: 'var(--accent-blue)', icon: '⚙️' },
  '1EM105B': { bg: 'var(--accent-purple)', icon: '🔧' },
  'DYN317B': { bg: 'var(--accent-orange)', icon: '🚀' }
};

// Default fallback for unknown subjects
const DEFAULT_SUBJECT_COLOR = { bg: 'var(--accent-green)', icon: '📚' };


// ─── Screen management ───
function showScreen(id) {
  document.querySelectorAll('.screen').forEach(s => s.classList.remove('active'));
  const el = $(`#${id}`);
  if (el) el.classList.add('active');
}


// ─── WebSocket connection ───
let wsOnOpen = null;
let wsReconnectTimer = null;

function connectWS(onOpen) {
  if (onOpen) wsOnOpen = onOpen;
  if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }

  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    if (ws.readyState === WebSocket.OPEN && wsOnOpen) wsOnOpen();
    return;
  }

  ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    console.log('WS connected');
    if (wsOnOpen) wsOnOpen();
  };

  ws.onmessage = (evt) => {
    const msg = JSON.parse(evt.data);
    handleMessage(msg);
  };

  ws.onclose = () => {
    console.log('WS disconnected, reconnecting in 3s...');
    ws = null;
    wsReconnectTimer = setTimeout(() => connectWS(), 3000);
  };

  ws.onerror = () => {};
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(msg));
  }
}


// ─── Route messages ───
function handleMessage(msg) {
  if (msg.type === 'error') {
    console.error('Server error:', msg.message);
    return;
  }
  if (isHost) {
    handleHostMessage(msg);
  } else {
    handlePlayerMessage(msg);
  }
}


// ════════════════════════════════════════════════════════════
//  SUBJECT SELECTION (shared logic)
// ════════════════════════════════════════════════════════════

function renderSubjectCards(containerId, subjects, onSelect) {
  const container = $(`#${containerId}`);
  container.innerHTML = '';

  subjects.forEach(sub => {
    const colors = SUBJECT_COLORS[sub.code] || DEFAULT_SUBJECT_COLOR;
    const card = document.createElement('button');
    card.className = 'subject-card';
    card.style.setProperty('--card-accent', colors.bg);

    const qCountText = sub.questionCount > 0
      ? `${sub.questionCount} questions`
      : 'No questions yet';

    card.innerHTML = `
      <span class="subject-icon">${colors.icon}</span>
      <div class="subject-info">
        <span class="subject-name">${escapeHtml(sub.name)}</span>
        <span class="subject-code-label">${escapeHtml(sub.code)}</span>
        <span class="subject-q-count">${qCountText}</span>
      </div>
      <span class="subject-arrow">→</span>
    `;
    card.addEventListener('click', () => onSelect(sub));
    container.appendChild(card);
  });
}

async function loadSubjects() {
  try {
    const resp = await fetch(`${API_BASE}/api/subjects`);
    if (resp.ok) return await resp.json();
  } catch (e) {
    console.error('Failed to load subjects:', e);
  }
  // Fallback: hardcoded subjects (in case API isn't up yet)
  return [
    { code: 'MEC105B', name: 'Mechanics', questionCount: 15 },
    { code: '1EM105B', name: 'Mechanics', questionCount: 0 },
    { code: 'DYN317B', name: 'Dynamics', questionCount: 0 }
  ];
}


// ════════════════════════════════════════════════════════════
//  PLAYER LOGIC
// ════════════════════════════════════════════════════════════

async function initPlayer() {
  isHost = false;
  showScreen('screen-subject');

  const subjects = await loadSubjects();
  renderSubjectCards('subject-list', subjects, (sub) => {
    selectedSubject = sub;
    showPlayerJoinScreen();
  });

  // Lecturer link → passcode modal
}

function showPlayerJoinScreen() {
  showScreen('screen-join');
  $('#join-subject-title').textContent = selectedSubject.name;
  $('#join-subject-code').textContent = selectedSubject.code;

  const nameInput = $('#nickname-input');
  const numInput = $('#student-number-input');
  const btn = $('#btn-join');

  // Clear old state
  nameInput.value = '';
  numInput.value = '';
  btn.disabled = true;
  const errEl = $('#name-error');
  if (errEl) errEl.hidden = true;

  function checkReady() {
    btn.disabled = !(nameInput.value.trim() && numInput.value.trim());
  }

  // Remove old listeners by cloning
  const newName = nameInput.cloneNode(true);
  const newNum = numInput.cloneNode(true);
  const newBtn = btn.cloneNode(true);
  nameInput.replaceWith(newName);
  numInput.replaceWith(newNum);
  btn.replaceWith(newBtn);

  newName.addEventListener('input', checkReady);
  newNum.addEventListener('input', checkReady);
  newName.addEventListener('keydown', (e) => { if (e.key === 'Enter') newNum.focus(); });
  newNum.addEventListener('keydown', (e) => { if (e.key === 'Enter' && newName.value.trim() && newNum.value.trim()) joinAsPlayer(); });
  newBtn.addEventListener('click', joinAsPlayer);

  // Back button
  $('#btn-back-subject').onclick = () => {
    selectedSubject = null;
    showScreen('screen-subject');
  };

  newName.focus();
}

var myPlayerName = '';
var myStudentNumber = '';

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

function handlePlayerMessage(msg) {
  switch (msg.type) {
    case 'name_taken':
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

    case 'joined':
      myPlayerId = msg.playerId;
      $('#lobby-player-name').textContent = myPlayerName;
      $('#lobby-p-count').textContent = msg.playerCount;
      $('#lobby-subject-badge').textContent = `${selectedSubject.name} (${selectedSubject.code})`;
      if (msg.phase === 'lobby') {
        showScreen('screen-lobby-player');
      }
      $('#btn-join').disabled = false;
      $('#btn-join').textContent = 'Join Game';
      break;

    case 'player_update':
      if (msg.players) {
        $('#lobby-p-count').textContent = msg.players.length;
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
      showScreen('screen-lobby-player');
      break;
  }
}

function playerGetReady(qNum, totalQ) {
  showScreen('screen-ready');
  $('#ready-q-num').textContent = `Question ${qNum} of ${totalQ}`;
  let count = 3;
  $('#ready-count').textContent = count;
  const iv = setInterval(() => {
    count--;
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

let playerAnswered = false;

function playerAnswer(choice, btnEl) {
  if (playerAnswered) return;
  playerAnswered = true;
  clearTimer();
  send({ action: 'answer', choice });

  document.querySelectorAll('.answer-btn').forEach(b => b.classList.add('disabled'));
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
  bar.style.width = pct + '%';
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
  if (timerInterval) { clearInterval(timerInterval); timerInterval = null; }
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

  if (msg.correctAnswer !== undefined) {
    const btns = document.querySelectorAll('.answer-btn');
    btns.forEach((b, i) => {
      if (i === msg.correctAnswer) b.classList.add('correct-reveal');
    });
  }
}

function playerShowLeaderboard(lb) {
  showScreen('screen-leaderboard');
  renderLeaderboardList($('#leaderboard-list'), lb, myPlayerId);
}

function playerShowFinal(lb) {
  showScreen('screen-final');
  const myRank = lb.findIndex(p => p.id === myPlayerId) + 1;
  $('#final-title').textContent = myRank === 1 ? 'You Win! 🏆' : `Game Over — You placed #${myRank}`;
  renderPodium($('#final-podium'), lb);
  renderFullList($('#final-full-list'), lb.slice(3), myPlayerId, 4);
}


// ════════════════════════════════════════════════════════════
//  HOST (LECTURER) LOGIC
// ════════════════════════════════════════════════════════════

var hostSubjectCode = null;

async function initHost() {
  isHost = true;

  // Close any existing WS when re-entering host subject selection
  if (ws) {
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    const oldWs = ws;
    ws = null;
    oldWs.onclose = null;
    oldWs.close();
  }

  showScreen('screen-host-subject');

  const subjects = await loadSubjects();
  renderSubjectCards('host-subject-list', subjects, (sub) => {
    selectedSubject = sub;
    hostSubjectCode = sub.code;
    startHostForSubject();
  });

  // Back to player view (clone to avoid duplicate listeners)
  const backBtn = $('#btn-back-player');
  const newBack = backBtn.cloneNode(true);
  backBtn.replaceWith(newBack);
  newBack.addEventListener('click', () => {
    isHost = false;
    location.hash = '';
    location.reload();
  });
}

function startHostForSubject() {
  showScreen('screen-host-lobby');
  $('#host-lobby-title').textContent = `${selectedSubject.name} (${selectedSubject.code})`;
  $('#host-lobby-subtitle').textContent = 'Lecturer Control Panel';

  connectWS(() => {
    send({ action: 'host_join', subject: hostSubjectCode });
  });

  // Generate QR code pointing to the deployed player URL
  const playerURL = location.origin + location.pathname.replace(/\/#.*$/, '/').replace(/\/+$/, '/');
  const qrContainer = $('#qr-code');
  qrContainer.innerHTML = '';
  try {
    new QRCode(qrContainer, {
      text: playerURL,
      width: 160,
      height: 160,
      colorDark: '#1a1027',
      colorLight: '#ffffff',
      correctLevel: QRCode.CorrectLevel.M
    });
  } catch(e) {}
  $('#qr-url-text').textContent = playerURL;

  // Clone buttons to remove old listeners
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
  newNext.addEventListener('click', () => {
    send({ action: 'next_question' });
  });

  const playAgainBtn = $('#btn-play-again');
  const newPlayAgain = playAgainBtn.cloneNode(true);
  playAgainBtn.replaceWith(newPlayAgain);
  newPlayAgain.addEventListener('click', () => {
    send({ action: 'reset_game' });
  });

  // Stats download buttons
  setupStatsDownload('#btn-download-stats');
  setupStatsDownload('#btn-download-stats-final');

  // Change Subject buttons — return to subject picker without re-entering passcode
  const changeBtn = $('#btn-change-subject');
  const newChange = changeBtn.cloneNode(true);
  changeBtn.replaceWith(newChange);
  newChange.addEventListener('click', () => {
    initHost(); // re-shows subject picker, closes old WS
  });

  const changeBtnFinal = $('#btn-change-subject-final');
  const newChangeFinal = changeBtnFinal.cloneNode(true);
  changeBtnFinal.replaceWith(newChangeFinal);
  newChangeFinal.addEventListener('click', () => {
    initHost();
  });
}

function setupStatsDownload(selector) {
  const btn = $(selector);
  if (!btn) return;
  const newBtn = btn.cloneNode(true);
  btn.replaceWith(newBtn);
  newBtn.addEventListener('click', () => {
    if (hostSubjectCode) {
      const url = `${API_BASE}/api/stats/${hostSubjectCode}`;
      // Use fetch to get the file, then trigger download
      fetch(url).then(resp => {
        if (!resp.ok) return resp.json().then(d => { throw new Error(d.error || 'Download failed'); });
        return resp.blob();
      }).then(blob => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = `Stats_${hostSubjectCode}.xlsx`;
        a.click();
        URL.revokeObjectURL(a.href);
      }).catch(err => {
        // Show error inline since alert() is blocked in sandbox
        newBtn.textContent = err.message || 'Download failed';
        setTimeout(() => { newBtn.textContent = 'Download Stats (Excel)'; }, 3000);
      });
    }
  });
}

function handleHostMessage(msg) {
  switch (msg.type) {
    case 'host_joined':
      updateHostLobby(msg.players);
      if (msg.phase === 'lobby') {
        showScreen('screen-host-lobby');
      }
      // Show/hide stats download button
      const statsBtn = $('#btn-download-stats');
      if (statsBtn) {
        statsBtn.hidden = !msg.hasStats;
      }
      // Warn if no questions
      if (!msg.hasQuestions) {
        const startBtn = $('#btn-start-game');
        if (startBtn) {
          startBtn.disabled = true;
          startBtn.textContent = 'No questions loaded';
        }
      }
      break;

    case 'player_update':
      updateHostLobby(msg.players);
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
      break;
  }
}

function updateHostLobby(players) {
  const count = players ? players.length : 0;
  $('#host-player-count').textContent = count;

  const startBtn = $('#btn-start-game');
  // Only enable if there are players AND there are questions
  if (selectedSubject && selectedSubject.questionCount > 0) {
    startBtn.disabled = count === 0;
    startBtn.textContent = `Start Game (${count} players)`;
  }

  const list = $('#host-player-list');
  if (!players || players.length === 0) {
    list.innerHTML = '<p class="empty-msg">Waiting for students to join...</p>';
    return;
  }
  list.innerHTML = '';
  players.forEach(p => {
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

let hostTimerInterval = null;
var hostCorrectAnswer = -1;
var hostCurrentOptions = [];
var hostCurrentQuestion = '';

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
    $('#host-timer-bar').style.width = pct + '%';
    $('#host-timer').textContent = Math.ceil(hostTimeLeft);
  }, 100);
}

function hostShowReveal(msg) {
  if (hostTimerInterval) { clearInterval(hostTimerInterval); hostTimerInterval = null; }
  showScreen('screen-host-reveal');

  const answerEl = $('#host-reveal-answer');
  if (answerEl && hostCurrentOptions.length > 0) {
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
  }

  $('#host-reveal-explanation').textContent = msg.explanation;
  renderLeaderboardList($('#host-reveal-leaderboard'), msg.leaderboard, null);

  const btn = $('#btn-next-question');
  if (btn) btn.style.display = 'none';

  let autoCountdown = 5;
  const countdownEl = $('#host-auto-countdown');
  if (countdownEl) {
    countdownEl.textContent = `Next question in ${autoCountdown}s...`;
    countdownEl.style.display = 'block';
    const iv = setInterval(() => {
      autoCountdown--;
      if (autoCountdown <= 0) {
        clearInterval(iv);
        countdownEl.textContent = 'Loading next question...';
      } else {
        countdownEl.textContent = `Next question in ${autoCountdown}s...`;
      }
    }, 1000);
  }
}

function hostShowFinal(lb) {
  if (hostTimerInterval) { clearInterval(hostTimerInterval); hostTimerInterval = null; }
  showScreen('screen-host-final');
  renderPodium($('#host-final-podium'), lb);
  renderFullList($('#host-final-list'), lb.slice(3), null, 4);
}


// ════════════════════════════════════════════════════════════
//  PASSCODE MODAL
// ════════════════════════════════════════════════════════════

function hasHostSession() {
  return sessionStorage.getItem(HOST_AUTH_KEY) === '1';
}

function setHostSession() {
  sessionStorage.setItem(HOST_AUTH_KEY, '1');
}

function clearHostSession() {
  sessionStorage.removeItem(HOST_AUTH_KEY);
}

function openPasscodeModal(afterSuccess) {
  const modal = $('#passcode-modal');
  const passInput = $('#passcode-input');
  const passError = $('#passcode-error');
  if (!modal || !passInput || !passError) return;

  passInput.value = '';
  passError.hidden = true;
  modal.hidden = false;
  modal.dataset.afterSuccess = afterSuccess || 'host';
  setTimeout(() => passInput.focus(), 0);
}

function setupPasscodeModal() {
  const modal = $('#passcode-modal');
  const passInput = $('#passcode-input');
  const passError = $('#passcode-error');
  const passSubmit = $('#btn-passcode-submit');
  const passCancel = $('#btn-passcode-cancel');

  $('#btn-host-link').addEventListener('click', () => {
    if (hasHostSession()) {
      location.hash = '#host';
      initHost();
      return;
    }
    openPasscodeModal('host');
  });

  passCancel.addEventListener('click', () => { modal.hidden = true; });
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.hidden = true; });

  function submitPasscode() {
    if (passInput.value.trim() === HOST_PASSCODE) {
      setHostSession();
      modal.hidden = true;
      location.hash = '#host';
      initHost();
    } else {
      passError.hidden = false;
      passInput.value = '';
      passInput.focus();
    }
  }

  passSubmit.addEventListener('click', submitPasscode);
  passInput.addEventListener('keydown', (e) => { if (e.key === 'Enter') submitPasscode(); });
}


// ════════════════════════════════════════════════════════════
//  SHARED RENDERING
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

  order.forEach(pos => {
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


// ═══════════════════════════════════════════════
//  INIT
// ═══════════════════════════════════════════════

window.addEventListener('DOMContentLoaded', () => {
  setupPasscodeModal();

  if (location.hash === '#host') {
    if (hasHostSession()) {
      initHost();
    } else {
      initPlayer();
      openPasscodeModal('host');
    }
  } else {
    initPlayer();
  }
});
