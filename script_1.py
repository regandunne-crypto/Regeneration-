from pathlib import Path
import re
from textwrap import dedent

app_js = Path('app.js').read_text()
index_html = Path('index.html').read_text()
style_css = Path('style.css').read_text()
server_py = Path('server.py').read_text()

# ---------------- index.html ----------------
index_html = re.sub(r'\s*<!-- .*?Perplexity Computer.*?-->\s*', '\n', index_html, flags=re.S)
index_html = re.sub(r'\s*<meta name="generator"[^>]*>\s*', '\n', index_html)
index_html = re.sub(r'\s*<meta name="author"[^>]*>\s*', '\n', index_html)
index_html = re.sub(r'\s*<meta property="og:see_also"[^>]*>\s*', '\n', index_html)
index_html = re.sub(r'\s*<link rel="author"[^>]*>\s*', '\n', index_html)
index_html = re.sub(r'\s*<footer class="site-footer">.*?</footer>\s*', '\n', index_html, flags=re.S)

index_html = index_html.replace(
    '<p class="host-subtitle">Create your lecturer account or sign in to manage saved tests.</p>',
    '<p id="host-auth-subtitle" class="host-subtitle">Sign in to manage your saved tests.</p>'
)

index_html = re.sub(
    r'\s*<div class="auth-toggle".*?</div>\s*<p id="host-auth-status" class="inline-status hidden"></p>',
    '\n            <p id="host-auth-status" class="inline-status hidden"></p>',
    index_html,
    flags=re.S,
)

index_html = index_html.replace(
    '</form>\n            <form id="host-signup-form" class="auth-form hidden">',
    '</form>\n            <p id="auth-switch-login" class="auth-switch">Need an account? <button id="btn-show-signup" class="auth-switch-btn" type="button">Create one</button></p>\n            <form id="host-signup-form" class="auth-form hidden">'
)
index_html = index_html.replace(
    '</form>\n            <button id="btn-auth-back" class="back-link">&larr; Back to student view</button>',
    '</form>\n            <p id="auth-switch-signup" class="auth-switch" hidden>Already have an account? <button id="btn-show-login" class="auth-switch-btn" type="button">Sign in</button></p>\n            <button id="btn-auth-back" class="back-link">&larr; Back to student view</button>'
)

# ---------------- style.css ----------------
if '[hidden] {' not in style_css:
    style_css = style_css.replace(
        'input {\n    font: inherit;\n    color: inherit;\n}\n',
        'input {\n    font: inherit;\n    color: inherit;\n}\n\n[hidden] {\n    display: none !important;\n}\n'
    )

style_css = style_css.replace(
    '.auth-form {\n    display: flex;\n    flex-direction: column;\n    gap: var(--space-3);\n}\n',
    '.auth-form {\n    display: flex;\n    flex-direction: column;\n    gap: var(--space-3);\n}\n\n.auth-switch {\n    font-size: var(--text-sm);\n    color: var(--color-text-muted);\n    text-align: center;\n}\n\n.auth-switch-btn {\n    border: none;\n    background: none;\n    color: var(--accent-blue);\n    font: inherit;\n    font-weight: 700;\n    text-decoration: underline;\n    text-underline-offset: 3px;\n}\n\n.auth-switch-btn:hover {\n    opacity: 0.9;\n}\n'
)

# ---------------- app.js ----------------
set_auth_mode_replacement = dedent('''
function setAuthMode(mode) {
  const loginForm = sel("#host-login-form");
  const signupForm = sel("#host-signup-form");
  const loginSwitch = sel("#auth-switch-login");
  const signupSwitch = sel("#auth-switch-signup");
  const loginTab = sel("#btn-auth-tab-login");
  const signupTab = sel("#btn-auth-tab-signup");
  const authTitle = sel("#host-auth-title");
  const authSubtitle = sel("#host-auth-subtitle");
  const isLogin = mode !== "signup";

  if (loginTab) loginTab.classList.toggle("active", isLogin);
  if (signupTab) signupTab.classList.toggle("active", !isLogin);
  if (loginForm) loginForm.hidden = !isLogin;
  if (signupForm) signupForm.hidden = isLogin;
  if (loginSwitch) loginSwitch.hidden = !isLogin;
  if (signupSwitch) signupSwitch.hidden = isLogin;
  if (authTitle) authTitle.textContent = isLogin ? "Lecturer Sign In" : "Create Lecturer Account";
  if (authSubtitle) {
    authSubtitle.textContent = isLogin
      ? "Sign in to manage your saved tests."
      : "Create a lecturer account to manage your saved tests.";
  }
}
''')
app_js, n = re.subn(
    r'function setAuthMode\(mode\) \{.*?\n\}',
    set_auth_mode_replacement.rstrip(),
    app_js,
    count=1,
    flags=re.S,
)
assert n == 1, 'setAuthMode replacement failed'

bind_host_replacement = dedent('''
function bindHostAuthUI() {
  if (authUiBound) return;
  authUiBound = true;

  const hostLink = sel("#btn-host-link");
  if (hostLink) hostLink.addEventListener("click", enterHostArea);

  const clearAuthStatus = () => showInlineStatus("#host-auth-status", "", false);

  const loginTabBtn = sel("#btn-auth-tab-login");
  if (loginTabBtn) {
    loginTabBtn.addEventListener("click", () => {
      setAuthMode("login");
      clearAuthStatus();
    });
  }

  const signupTabBtn = sel("#btn-auth-tab-signup");
  if (signupTabBtn) {
    signupTabBtn.addEventListener("click", () => {
      setAuthMode("signup");
      clearAuthStatus();
    });
  }

  const showSignupBtn = sel("#btn-show-signup");
  if (showSignupBtn) {
    showSignupBtn.addEventListener("click", () => {
      setAuthMode("signup");
      clearAuthStatus();
    });
  }

  const showLoginBtn = sel("#btn-show-login");
  if (showLoginBtn) {
    showLoginBtn.addEventListener("click", () => {
      setAuthMode("login");
      clearAuthStatus();
    });
  }

  const authBackBtn = sel("#btn-auth-back");
  if (authBackBtn) authBackBtn.addEventListener("click", resetToStudentView);

  const loginForm = sel("#host-login-form");
  if (loginForm) {
    loginForm.addEventListener("submit", async e => {
      e.preventDefault();
      clearAuthStatus();
      const btn = sel("#btn-login-submit");
      btn.disabled = true;
      btn.textContent = "Signing In...";
      try {
        await apiPost("/api/lecturer/login", {
          email: sel("#login-email-input").value.trim(),
          password: sel("#login-password-input").value,
        });
        await fetchLecturerSession();
        sel("#login-password-input").value = "";
        await initHost();
      } catch (e) {
        showInlineStatus("#host-auth-status", e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Sign In";
      }
    });
  }

  const signupForm = sel("#host-signup-form");
  if (signupForm) {
    signupForm.addEventListener("submit", async e => {
      e.preventDefault();
      clearAuthStatus();
      const password = sel("#signup-password-input").value;
      const confirm = sel("#signup-password-confirm-input").value;
      if (password !== confirm) {
        showInlineStatus("#host-auth-status", "Passwords do not match.", true);
        return;
      }
      const btn = sel("#btn-signup-submit");
      btn.disabled = true;
      btn.textContent = "Creating Account...";
      try {
        await apiPost("/api/lecturer/signup", {
          name: sel("#signup-name-input").value.trim(),
          email: sel("#signup-email-input").value.trim(),
          password,
        });
        await fetchLecturerSession();
        sel("#signup-password-input").value = "";
        sel("#signup-password-confirm-input").value = "";
        await initHost();
      } catch (e) {
        showInlineStatus("#host-auth-status", e.message, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "Create Account";
      }
    });
  }

  const logoutBtn = sel("#btn-host-logout");
  if (logoutBtn) {
    logoutBtn.addEventListener("click", async () => {
      try {
        await apiPost("/api/lecturer/logout", {});
      } catch (e) {
        console.error(e);
      }
      lecturerSession = null;
      updateHostAccountBar();
      showHostAuthScreen("login", "Signed out.");
    });
  }
}
''')
app_js, n = re.subn(
    r'function bindHostAuthUI\(\) \{.*?\n\}\n\nasync function initHost\(\)',
    bind_host_replacement.rstrip() + '\n\nasync function initHost()',
    app_js,
    count=1,
    flags=re.S,
)
assert n == 1, 'bindHostAuthUI replacement failed'

update_host_lobby_replacement = dedent('''
function updateHostLobby(players, activeTest = selectedTest) {
  const allPlayers = Array.isArray(players) ? players : [];
  const connectedPlayers = allPlayers.filter(p => p.connected);
  const connectedCount = connectedPlayers.length;
  const hostPlayerCount = sel("#host-player-count");
  if (hostPlayerCount) hostPlayerCount.textContent = connectedCount;

  const startBtn = sel("#btn-start-game");
  const qCount = activeTest && activeTest.questionCount ? activeTest.questionCount : 0;
  if (qCount > 0) {
    startBtn.disabled = connectedCount === 0;
    startBtn.textContent = connectedCount === 0 ? "Waiting for players" : `Start Game (${connectedCount} connected)`;
  } else {
    startBtn.disabled = true;
    startBtn.textContent = "This test has no questions";
  }

  const list = sel("#host-player-list");
  if (allPlayers.length === 0) {
    list.innerHTML = '<p class="empty-msg">Waiting for students to join...</p>';
    return;
  }

  list.innerHTML = "";
  allPlayers.forEach(p => {
    const el = document.createElement("div");
    el.className = "host-player-item";
    el.innerHTML = `
      <span class="host-player-dot ${p.connected ? "connected" : "disconnected"}"></span>
      <span class="host-player-name-text">${escapeHtml(p.name)}</span>
    `;
    list.appendChild(el);
  });

  if (allPlayers.some(p => !p.connected)) {
    const note = document.createElement("p");
    note.className = "empty-msg";
    note.textContent = "Disconnected students can reconnect using the same device.";
    list.appendChild(note);
  }
}
''')
app_js, n = re.subn(
    r'function updateHostLobby\(players, activeTest = selectedTest\) \{.*?\n\}\n\nfunction hostGetReady\(',
    update_host_lobby_replacement.rstrip() + '\n\nfunction hostGetReady(',
    app_js,
    count=1,
    flags=re.S,
)
assert n == 1, 'updateHostLobby replacement failed'

# ---------------- server.py ----------------
helper_insertion = dedent('''

def require_durable_storage_for_management() -> None:
    status = repo.get_storage_status()
    if status.get("supabaseConfigured") and status.get("mode") != "supabase":
        raise HTTPException(
            status_code=503,
            detail="Supabase storage is temporarily unavailable, so test changes are disabled to prevent data loss. Apply the Supabase schema and confirm the Render environment variables before creating or editing tests.",
        )
''')
server_py, n = re.subn(
    r'(def require_lecturer\(request: Request\) -> dict\[str, Any\]:\n    lecturer = current_lecturer_from_request\(request\)\n    if not lecturer:\n        raise HTTPException\(status_code=401, detail="Lecturer sign-in required"\)\n    return lecturer\n)',
    r'\1' + helper_insertion,
    server_py,
    count=1,
)
assert n == 1, 'require_durable_storage_for_management insertion failed'

for sig in [
    'def create_test(subject_code: str, payload: dict[str, Any], request: Request):',
    'def update_test(subject_code: str, test_id: str, payload: dict[str, Any], request: Request):',
    'def save_test_draft(subject_code: str, payload: dict[str, Any], request: Request):',
    'def clear_test_draft(subject_code: str, request: Request):',
]:
    if sig in server_py and 'require_durable_storage_for_management()' not in server_py.split(sig,1)[1][:220]:
        server_py = server_py.replace(sig, sig + '\n    require_durable_storage_for_management()', 1)

old_disconnect = dedent('''
        elif role == "player":
            if room.phase == "lobby":
                room.players.pop(visitor_id, None)
            else:
                player = room.players.get(visitor_id)
                if player:
                    player["ws"] = None
''')
new_disconnect = dedent('''
        elif role == "player":
            player = room.players.get(visitor_id)
            if player:
                player["ws"] = None
''')
if old_disconnect in server_py:
    server_py = server_py.replace(old_disconnect, new_disconnect, 1)
else:
    server_py = re.sub(
        r'elif role == "player":\n\s+if room\.phase == "lobby":\n\s+room\.players\.pop\(visitor_id, None\)\n\s+else:\n\s+player = room\.players\.get\(visitor_id\)\n\s+if player:\n\s+player\["ws"\] = None',
        'elif role == "player":\n            player = room.players.get(visitor_id)\n            if player:\n                player["ws"] = None',
        server_py,
        count=1,
    )

# ---------------- write updated files ----------------
outdir = Path('updated_project')
outdir.mkdir(exist_ok=True)
(outdir / 'index.html').write_text(index_html)
(outdir / 'style.css').write_text(style_css)
(outdir / 'app.js').write_text(app_js)
(outdir / 'server.py').write_text(server_py)

(outdir / 'UPDATE_NOTES.txt').write_text(dedent('''
Updated files generated:
- index.html
- style.css
- app.js
- server.py

Key changes:
- Removed Perplexity footer/meta tags
- Split lecturer auth into clearer sign-in/create-account modes
- Added stronger hidden element handling in CSS
- Prevented test changes when Supabase is configured but unavailable
- Kept disconnected students in room state so they can reconnect
- Host lobby now counts connected students for starting a game
''').strip() + '\n')

# basic validation
import py_compile
py_compile.compile(str(outdir / 'server.py'), doraise=True)

print('Updated files written to', outdir)
print('Files:', sorted(p.name for p in outdir.iterdir()))
