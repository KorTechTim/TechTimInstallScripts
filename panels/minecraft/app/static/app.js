const $ = id => document.getElementById(id);
const terminal = $('terminal');
const settingsDialog = $('settingsDialog');
const filesDialog = $('filesDialog');
const playerManagerDialog = $('playerManagerDialog');
const backupManagerDialog = $('backupManagerDialog');
const eulaDialog = $('eulaDialog');
const deleteServerDialog = $('deleteServerDialog');
const panelUpdateDialog = $('panelUpdateDialog');
let logMode = 'install';
let currentPath = '';
let currentParent = '';
let writeLocked = false;
const selectedFilePaths = new Set();
let panelUpdateRequested = false;
let panelUpdateProgressVisible = false;
let panelUpdatePollTimer = null;
let panelUpdateReloadScheduled = false;
let commandSending = false;
let stopRequestActive = false;
let playerManagerBusy = false;
let backupManagerBusy = false;
let backupPollTimer = null;
let serverIconObjectUrl = '';
let serverIconBusy = false;

const statusKo = {
  not_started: '미설치', running: '실행 중', completed: '설치 완료', failed: '설치 실패',
  created: '생성됨', exited: '중지됨', dead: '오류', restarting: '재시작 중',
  not_created: '생성되지 않음', paused: '일시 정지', removing: '삭제 중'
};

function settingsSummary(config) {
  const javaVersion = config.JavaVersion === 'AUTO' ? 'JAVA AUTO' : `JAVA ${config.JavaVersion}`;
  return `${config.Type} · ${config.Version} · ${javaVersion} · ${config.Memory}`;
}

function toast(message, error = false) {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(el.timer);
  el.timer = setTimeout(() => el.className = 'toast', 2500);
}

function showButtonBubble(anchor, message, error = true) {
  const el = $('actionBubble');
  const rect = anchor.getBoundingClientRect();
  const halfWidth = Math.min(170, (window.innerWidth - 24) / 2);
  const center = Math.max(12 + halfWidth, Math.min(window.innerWidth - 12 - halfWidth, rect.left + rect.width / 2));
  el.textContent = message;
  el.classList.toggle('success', !error);
  el.style.left = `${center}px`;
  el.style.bottom = `${window.innerHeight - rect.top + 12}px`;
  el.classList.add('show');
  clearTimeout(el.timer);
  el.timer = setTimeout(() => el.classList.remove('show'), 2500);
}

async function api(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || '요청을 처리하지 못했습니다.');
  return data;
}

async function loadMinecraftVersions() {
  try {
    const data = await api('/api/minecraft/versions');
    const list = $('minecraftVersionSelect');
    const versions = Array.isArray(data.versions) ? data.versions : [];
    if (!list || !versions.length) return;

    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = '목록 선택';
    list.replaceChildren(placeholder, ...versions.map(version => {
      const option = document.createElement('option');
      option.value = version;
      option.textContent = version === 'LATEST' ? `LATEST (최신 ${data.latest})` : version;
      return option;
    }));
  } catch (_) {
    // The embedded options remain available if Mojang's manifest cannot be reached.
  }
}

function setStatusIcon(el, state, goodStates) {
  el.className = 'status-icon';
  if (goodStates.includes(state)) { el.classList.add('ok'); el.textContent = '✓'; }
  else if (['running', 'restarting', 'created'].includes(state)) { el.classList.add('busy'); el.textContent = '…'; }
  else el.textContent = '×';
}

async function refreshStatus() {
  try {
    const [install, server] = await Promise.all([api('/api/install/status'), api('/api/server/status')]);
    $('installStatus').textContent = statusKo[install.status] || install.status;
    $('serverStatus').textContent = statusKo[server.status] || server.status;
    setStatusIcon($('installIcon'), install.status, ['completed']);
    setStatusIcon($('serverIcon'), server.status, ['running']);
    const serverActive = Boolean(server.running || server.stoppable);
    $('startButton').disabled = serverActive || !install.installed;
    $('stopButton').disabled = !server.stoppable || stopRequestActive;
    $('deleteServerButton').disabled = serverActive || install.status === 'running' || (!install.installed && server.status === 'not_created');
    $('installButton').disabled = serverActive || install.status === 'running' || install.install_locked;
    $('settingsEntry').classList.toggle('locked', serverActive);
    $('settingsButton').disabled = serverActive;
    $('consoleCommandInput').disabled = !server.running || commandSending;
    if (server.running && logMode !== 'server') selectLog('server');
  } catch (error) { toast(error.message, true); }
}

async function refreshLog() {
  try {
    const data = await api(logMode === 'install' ? '/api/install/log' : '/api/server/log');
    const nearBottom = terminal.scrollHeight - terminal.scrollTop - terminal.clientHeight < 80;
    terminal.textContent = data.log || (logMode === 'install' ? '설치 작업을 시작하면 로그가 표시됩니다.' : '서버가 시작되면 로그가 표시됩니다.');
    if (nearBottom) terminal.scrollTop = terminal.scrollHeight;
  } catch (error) { terminal.textContent = error.message; }
}

function selectLog(mode) {
  logMode = mode;
  document.querySelectorAll('[data-log]').forEach(button => button.classList.toggle('active', button.dataset.log === mode));
  $('logCaption').textContent = mode === 'install' ? '설치 로그' : '서버 로그';
  refreshLog();
}

function syncModalScrollLock() {
  document.body.classList.toggle('modal-open', Boolean(document.querySelector('dialog[open]')));
}

function showDialog(dialog) {
  dialog.showModal();
  syncModalScrollLock();
}

function closeDialog(dialog) {
  dialog.close();
  syncModalScrollLock();
}

document.querySelectorAll('[data-log]').forEach(button => button.onclick = () => selectLog(button.dataset.log));
document.querySelectorAll('[data-close]').forEach(button => button.onclick = () => closeDialog($(button.dataset.close)));
document.querySelectorAll('[data-placeholder-menu]').forEach(button => {
  button.onclick = () => showButtonBubble(button, '준비 중인 메뉴입니다.', false);
});
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', syncModalScrollLock));

const playerActionLabels = {
  op: 'OP 부여', deop: 'OP 해제', whitelist_add: '허용 추가', whitelist_remove: '허용 제거',
  kick: '추방', ban: '차단', pardon: '차단 해제'
};

function playerActionButton(action, player, tone = '') {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = `player-row-action${tone ? ` ${tone}` : ''}`;
  button.textContent = playerActionLabels[action];
  button.disabled = playerManagerBusy;
  button.onclick = () => runPlayerAction(action, player);
  return button;
}

function renderPlayerList(listId, countId, entries, type, running) {
  const list = $(listId);
  const values = Array.isArray(entries) ? entries : [];
  list.textContent = '';
  $(countId).textContent = String(values.length);
  if (!values.length) {
    const empty = document.createElement('p');
    empty.className = 'player-list-empty';
    empty.textContent = type === 'online' && !running ? '서버가 실행 중이 아닙니다.' : '등록된 플레이어가 없습니다.';
    list.append(empty);
    return;
  }
  values.forEach(entry => {
    const player = typeof entry === 'string' ? entry : entry.name;
    const row = document.createElement('div');
    row.className = 'player-row';
    const identity = document.createElement('div');
    identity.className = 'player-row-identity';
    const avatar = document.createElement('span');
    avatar.className = 'player-avatar';
    avatar.textContent = player.slice(0, 1).toUpperCase();
    const copy = document.createElement('span');
    const name = document.createElement('strong');
    name.textContent = player;
    copy.append(name);
    if (type === 'banned' && entry.reason) {
      const reason = document.createElement('small');
      reason.textContent = entry.reason;
      copy.append(reason);
    }
    identity.append(avatar, copy);
    const actions = document.createElement('div');
    actions.className = 'player-row-actions';
    if (type === 'online') {
      actions.append(playerActionButton('op', player), playerActionButton('whitelist_add', player), playerActionButton('kick', player, 'warn'), playerActionButton('ban', player, 'danger'));
    } else if (type === 'ops') {
      actions.append(playerActionButton('deop', player, 'warn'));
    } else if (type === 'whitelist') {
      actions.append(playerActionButton('whitelist_remove', player, 'warn'));
    } else if (type === 'banned') {
      actions.append(playerActionButton('pardon', player));
    }
    actions.querySelectorAll('button').forEach(button => button.disabled = !running || playerManagerBusy);
    row.append(identity, actions);
    list.append(row);
  });
}

function renderPlayerManager(data) {
  const running = Boolean(data.running);
  const state = $('playerServerState');
  state.textContent = running ? '서버 실행 중' : '서버 중지됨';
  state.classList.toggle('online', running);
  $('playerOnlineSummary').textContent = running
    ? `${data.online.length} / ${data.max_players}명 접속 중`
    : '서버를 시작하면 실시간 플레이어 관리가 활성화됩니다.';
  $('playerManagerError').textContent = data.error || '';
  renderPlayerList('onlinePlayerList', 'onlinePlayerCount', data.online, 'online', running);
  renderPlayerList('opPlayerList', 'opPlayerCount', data.ops, 'ops', running);
  renderPlayerList('whitelistPlayerList', 'whitelistPlayerCount', data.whitelist, 'whitelist', running);
  renderPlayerList('bannedPlayerList', 'bannedPlayerCount', data.banned, 'banned', running);
  $('playerActionForm').querySelectorAll('input,select,button').forEach(control => control.disabled = !running || playerManagerBusy);
}

async function loadPlayerManager() {
  if (playerManagerBusy) return;
  $('playerRefreshButton').disabled = true;
  try {
    const data = await api('/api/players');
    renderPlayerManager(data);
  } catch (error) {
    $('playerManagerError').textContent = error.message;
  } finally {
    $('playerRefreshButton').disabled = false;
  }
}

async function runPlayerAction(action, player, reason = '') {
  if (playerManagerBusy) return;
  if (['kick', 'ban', 'pardon'].includes(action) && !confirm(`${player} 플레이어에게 '${playerActionLabels[action]}' 작업을 실행할까요?`)) return;
  playerManagerBusy = true;
  $('playerActionForm').querySelectorAll('input,select,button').forEach(control => control.disabled = true);
  try {
    const data = await api('/api/players/action', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action, player, reason})
    });
    toast(data.message || '플레이어 관리 작업이 완료되었습니다.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    playerManagerBusy = false;
    await loadPlayerManager();
  }
}

$('playerManagerButton').onclick = () => {
  showDialog(playerManagerDialog);
  loadPlayerManager();
};
$('playerRefreshButton').onclick = loadPlayerManager;
$('playerActionForm').onsubmit = event => {
  event.preventDefault();
  runPlayerAction($('playerActionType').value, $('playerActionName').value.trim(), $('playerActionReason').value.trim());
};

function formatBackupBytes(value) {
  let bytes = Math.max(0, Number(value) || 0);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  while (bytes >= 1024 && index < units.length - 1) { bytes /= 1024; index += 1; }
  return `${bytes.toFixed(index && bytes < 10 ? 1 : 0)} ${units[index]}`;
}

function formatBackupTime(value) {
  if (!value) return '기록 없음';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', hour12: false});
}

function setBackupFormDisabled(disabled) {
  $('backupConfigForm').querySelectorAll('input,select,button').forEach(control => { control.disabled = disabled; });
  $('backupNowButton').disabled = disabled;
  $('backupRefreshButton').disabled = disabled;
}

function backupRow(entry, serverActive, active) {
  const row = document.createElement('div');
  row.className = 'backup-row';
  const icon = document.createElement('span');
  icon.className = 'backup-row-icon';
  icon.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><ellipse cx="12" cy="5" rx="8" ry="3"></ellipse><path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5"></path><path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"></path></svg>';
  const copy = document.createElement('span');
  copy.className = 'backup-row-copy';
  const name = document.createElement('strong');
  name.textContent = entry.name;
  const detail = document.createElement('small');
  detail.textContent = `${formatBackupTime(entry.created_at)} · ${formatBackupBytes(entry.size)}`;
  copy.append(name, detail);
  const actions = document.createElement('span');
  actions.className = 'backup-row-actions';
  const download = document.createElement('a');
  download.className = 'backup-row-action';
  download.textContent = '다운로드';
  download.href = `/api/backups/${encodeURIComponent(entry.name)}/download`;
  const restore = document.createElement('button');
  restore.type = 'button';
  restore.className = 'backup-row-action restore';
  restore.textContent = '복원';
  restore.disabled = serverActive || active;
  restore.title = serverActive ? '서버를 중지한 후 복원할 수 있습니다.' : '이 백업으로 서버 데이터를 복원합니다.';
  restore.onclick = () => restoreBackup(entry.name);
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'backup-row-action danger';
  remove.textContent = '삭제';
  remove.disabled = active;
  remove.onclick = () => deleteBackup(entry.name);
  actions.append(download, restore, remove);
  row.append(icon, copy, actions);
  return row;
}

function renderBackupManager(data) {
  const config = data.config || {};
  const status = data.status || {};
  const entries = Array.isArray(data.backups) ? data.backups : [];
  const active = Boolean(data.active);
  $('backupEnabled').checked = Boolean(config.enabled);
  $('backupEnabledLabel').textContent = config.enabled ? '사용 중' : '사용 안 함';
  $('backupIntervalHours').value = String(config.interval_hours || 6);
  $('backupRetentionCount').value = String(config.retention_count || 7);
  const autoState = $('backupAutoState');
  autoState.textContent = config.enabled ? '자동 백업 사용 중' : '자동 백업 꺼짐';
  autoState.classList.toggle('online', Boolean(config.enabled));
  $('backupSummary').textContent = entries.length
    ? `${entries.length}개의 백업 · 최근 ${formatBackupTime(entries[0].created_at)}`
    : '저장된 백업이 없습니다.';
  $('backupNextRun').textContent = config.enabled
    ? `다음 자동 백업: ${formatBackupTime(config.next_run_at)}`
    : `최근 백업: ${formatBackupTime(config.last_backup_at)}`;
  const statusBox = $('backupManagerStatus');
  statusBox.dataset.status = status.status || 'idle';
  statusBox.textContent = status.message || '백업 기능을 사용할 수 있습니다.';
  $('backupCount').textContent = String(entries.length);
  const list = $('backupList');
  list.textContent = '';
  if (!entries.length) {
    const empty = document.createElement('p');
    empty.className = 'backup-list-empty';
    empty.textContent = '즉시 백업을 실행하거나 자동 백업을 설정해주세요.';
    list.append(empty);
  } else {
    entries.forEach(entry => list.append(backupRow(entry, Boolean(data.server_active), active)));
  }
  setBackupFormDisabled(active || backupManagerBusy);
  if (active) startBackupPolling(); else stopBackupPolling();
}

async function loadBackupManager() {
  if (backupManagerBusy) return;
  $('backupRefreshButton').disabled = true;
  try {
    renderBackupManager(await api('/api/backups'));
  } catch (error) {
    $('backupManagerStatus').dataset.status = 'failed';
    $('backupManagerStatus').textContent = error.message;
  } finally {
    if (!backupManagerBusy) $('backupRefreshButton').disabled = false;
  }
}

function startBackupPolling() {
  if (backupPollTimer) return;
  backupPollTimer = setInterval(loadBackupManager, 1500);
}

function stopBackupPolling() {
  if (!backupPollTimer) return;
  clearInterval(backupPollTimer);
  backupPollTimer = null;
}

async function runBackupNow() {
  if (backupManagerBusy) return;
  backupManagerBusy = true;
  setBackupFormDisabled(true);
  try {
    const data = await api('/api/backups/run', {method: 'POST'});
    toast(data.message || '즉시 백업을 시작했습니다.');
    startBackupPolling();
  } catch (error) {
    toast(error.message, true);
  } finally {
    backupManagerBusy = false;
    await loadBackupManager();
  }
}

async function restoreBackup(filename) {
  if (!confirm(`${filename}\n\n현재 서버 데이터를 이 백업으로 교체할까요?`)) return;
  backupManagerBusy = true;
  setBackupFormDisabled(true);
  try {
    const data = await api(`/api/backups/${encodeURIComponent(filename)}/restore`, {method: 'POST'});
    toast(data.message || '백업 복원을 시작했습니다.');
    startBackupPolling();
  } catch (error) {
    toast(error.message, true);
  } finally {
    backupManagerBusy = false;
    await loadBackupManager();
  }
}

async function deleteBackup(filename) {
  if (!confirm(`${filename} 백업 파일을 삭제할까요?`)) return;
  backupManagerBusy = true;
  try {
    const data = await api(`/api/backups/${encodeURIComponent(filename)}/delete`, {method: 'POST'});
    toast(data.message || '백업 파일을 삭제했습니다.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    backupManagerBusy = false;
    await loadBackupManager();
  }
}

$('backupManagerButton').onclick = () => {
  showDialog(backupManagerDialog);
  loadBackupManager();
};
$('backupRefreshButton').onclick = loadBackupManager;
$('backupNowButton').onclick = runBackupNow;
$('backupEnabled').onchange = event => { $('backupEnabledLabel').textContent = event.target.checked ? '사용 중' : '사용 안 함'; };
$('backupConfigForm').onsubmit = async event => {
  event.preventDefault();
  if (backupManagerBusy) return;
  backupManagerBusy = true;
  setBackupFormDisabled(true);
  try {
    const data = await api('/api/backups/config', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
        enabled: $('backupEnabled').checked,
        interval_hours: Number($('backupIntervalHours').value),
        retention_count: Number($('backupRetentionCount').value)
      })
    });
    toast(data.message || '자동 백업 설정이 저장되었습니다.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    backupManagerBusy = false;
    await loadBackupManager();
  }
};
backupManagerDialog.addEventListener('close', stopBackupPolling);

$('consoleCommandForm').onsubmit = async event => {
  event.preventDefault();
  const input = $('consoleCommandInput');
  const command = input.value.trim();
  if (!command || commandSending) return;
  commandSending = true;
  input.disabled = true;
  try {
    const data = await api('/api/server/command', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({command})
    });
    input.value = '';
    selectLog('server');
    toast(data.message || '명령어를 전송했습니다.');
    await refreshLog();
  } catch (error) {
    toast(error.message, true);
  } finally {
    commandSending = false;
    await refreshStatus();
    if (!input.disabled) input.focus();
  }
};

$('logout').onclick = async () => { await api('/api/auth/logout', {method: 'POST'}); location.href = '/login'; };
$('panelUpdateButton').onclick = () => {
  $('panelUpdateNotice').classList.remove('show');
  setPanelUpdateProgressVisible(false);
  showDialog(panelUpdateDialog);
  loadPanelUpdateStatus();
};
$('panelUpdateConfirm').onclick = requestPanelUpdate;
$('installButton').onclick = async () => {
  try {
    const server = await api('/api/server/status');
    if (server.running || server.stoppable) {
      showButtonBubble($('installButton'), '서버 기동 중에는 엔진을 설치할 수 없습니다.');
      return;
    }
    await api('/api/install', {method: 'POST'});
    selectLog('install');
    showButtonBubble($('installButton'), '서버 설치를 시작했습니다.', false);
    refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
};

async function serverAction(action, payload = null) {
  const stopping = action === 'stop';
  if (stopping) {
    if (stopRequestActive) return;
    stopRequestActive = true;
    $('stopButton').disabled = true;
    showButtonBubble($('stopButton'), '서버를 즉시 종료하고 있습니다.', false);
  }
  try {
    const options = {method: 'POST'};
    if (payload) {
      options.headers = {'Content-Type': 'application/json'};
      options.body = JSON.stringify(payload);
    }
    const result = await api(`/api/server/${action}`, options);
    selectLog('server');
    toast(result.message || {start: '서버 시작을 요청했습니다.', stop: '서버가 즉시 종료되었습니다.', restart: '서버를 재시작했습니다.'}[action]);
  } catch (error) {
    toast(error.message, true);
  } finally {
    if (stopping) stopRequestActive = false;
    await refreshStatus();
  }
}
$('startButton').onclick = () => showDialog(eulaDialog);
$('eulaAgreeButton').onclick = async event => {
  const button = event.currentTarget;
  button.disabled = true;
  closeDialog(eulaDialog);
  await serverAction('start', {eula_accepted: true});
  button.disabled = false;
};
$('stopButton').onclick = () => serverAction('stop');
$('deleteServerButton').onclick = async () => {
  try {
    const server = await api('/api/server/status');
    if (server.running || server.stoppable) {
      showButtonBubble($('deleteServerButton'), '서버 기동 중에는 서버를 삭제할 수 없습니다.');
      return;
    }
    showDialog(deleteServerDialog);
  } catch (error) {
    toast(error.message, true);
  }
};
$('deleteServerConfirm').onclick = async event => {
  const button = event.currentTarget;
  button.disabled = true;
  button.textContent = '서버 삭제 중...';
  try {
    const data = await api('/api/server/delete', {method: 'POST'});
    closeDialog(deleteServerDialog);
    selectLog('install');
    const config = await api('/api/config');
    $('settingsMode').textContent = settingsSummary(config.config);
    await refreshStatus();
    await refreshLog();
    toast(data.message || '서버 데이터가 삭제되었습니다.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = '모든 서버 데이터 삭제';
  }
};

const fields = ['Type','Version','JavaVersion','Memory','ServerName','Motd','Level','Seed','Difficulty','GameMode','MaxPlayers','OnlineMode','Pvp','AllowFlight','EnableCommandBlock','ViewDistance','SimulationDistance','SpawnProtection','Whitelist','Ops','ModpackUrl'];
const checkFields = new Set(['OnlineMode','Pvp','AllowFlight','EnableCommandBlock']);
const numberFields = new Set(['MaxPlayers','ViewDistance','SimulationDistance','SpawnProtection']);
const motdColors = {
  0:'#000000',1:'#0000aa',2:'#00aa00',3:'#00aaaa',4:'#aa0000',5:'#aa00aa',6:'#ffaa00',7:'#aaaaaa',
  8:'#555555',9:'#5555ff',a:'#55ff55',b:'#55ffff',c:'#ff5555',d:'#ff55ff',e:'#ffff55',f:'#ffffff'
};

function renderMotd(value, target) {
  target.textContent = '';
  let style = {};
  let buffer = '';
  const flush = () => {
    if (!buffer) return;
    const span = document.createElement('span');
    span.textContent = buffer;
    Object.assign(span.style, style);
    target.append(span);
    buffer = '';
  };
  const text = String(value || '');
  for (let index = 0; index < text.length; index += 1) {
    if (text[index] === '§' && index + 1 < text.length) {
      const code = text[index + 1].toLowerCase();
      if (motdColors[code]) {
        flush();
        style = {color: motdColors[code]};
        index += 1;
        continue;
      }
      if ('l o n m r'.replaceAll(' ', '').includes(code)) {
        flush();
        if (code === 'l') style = {...style, fontWeight: '800'};
        if (code === 'o') style = {...style, fontStyle: 'italic'};
        if (code === 'n') style = {...style, textDecoration: `${style.textDecoration || ''} underline`.trim()};
        if (code === 'm') style = {...style, textDecoration: `${style.textDecoration || ''} line-through`.trim()};
        if (code === 'r') style = {};
        index += 1;
        continue;
      }
    }
    if (text[index] === '\n') {
      flush();
      target.append(document.createElement('br'));
    } else {
      buffer += text[index];
    }
  }
  flush();
  if (!target.childNodes.length) target.textContent = '서버 메시지를 입력하세요.';
}

function updateMotdPreview() {
  const motd = $('Motd').value || '';
  $('motdPreviewServerName').textContent = $('ServerName').value.trim() || 'Minecraft Server';
  $('motdCharacterCount').textContent = `${motd.length} / 300`;
  renderMotd(motd, $('motdPreviewText'));
}

function insertMotdCode(code) {
  const input = $('Motd');
  const start = input.selectionStart ?? input.value.length;
  const end = input.selectionEnd ?? start;
  const selected = input.value.slice(start, end);
  const insertion = selected ? `${code}${selected}§r` : code;
  input.setRangeText(insertion, start, end, 'end');
  input.focus();
  updateMotdPreview();
}

function showServerIcon(url = '') {
  if (serverIconObjectUrl) URL.revokeObjectURL(serverIconObjectUrl);
  serverIconObjectUrl = url;
  [$('serverIconPreview'), $('motdPreviewIcon')].forEach(image => {
    image.hidden = !url;
    if (url) image.src = url;
    else image.removeAttribute('src');
  });
  $('serverIconPlaceholder').hidden = Boolean(url);
  $('motdPreviewPlaceholder').hidden = Boolean(url);
  $('serverIconDelete').disabled = !url || serverIconBusy;
}

async function refreshServerIconPreview() {
  try {
    const response = await fetch(`/api/server-icon?ts=${Date.now()}`, {cache: 'no-store'});
    if (response.status === 404) {
      showServerIcon();
      $('serverIconStatus').textContent = '등록된 서버 아이콘이 없습니다.';
      return;
    }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || '서버 아이콘을 불러오지 못했습니다.');
    }
    const url = URL.createObjectURL(await response.blob());
    showServerIcon(url);
    $('serverIconStatus').textContent = '현재 서버 아이콘이 적용되어 있습니다.';
  } catch (error) {
    showServerIcon();
    $('serverIconStatus').textContent = error.message;
  }
}

async function resizeServerIcon(file) {
  if (!file || file.type !== 'image/png') throw new Error('PNG 이미지만 업로드할 수 있습니다.');
  const sourceUrl = URL.createObjectURL(file);
  try {
    const image = new Image();
    image.src = sourceUrl;
    await image.decode();
    const canvas = document.createElement('canvas');
    canvas.width = 64;
    canvas.height = 64;
    const context = canvas.getContext('2d');
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = 'high';
    const scale = Math.max(64 / image.naturalWidth, 64 / image.naturalHeight);
    const width = image.naturalWidth * scale;
    const height = image.naturalHeight * scale;
    context.drawImage(image, (64 - width) / 2, (64 - height) / 2, width, height);
    return await new Promise((resolve, reject) => canvas.toBlob(
      blob => blob ? resolve(blob) : reject(new Error('PNG 변환에 실패했습니다.')),
      'image/png'
    ));
  } finally {
    URL.revokeObjectURL(sourceUrl);
  }
}

async function uploadServerIcon(file) {
  if (serverIconBusy) return;
  serverIconBusy = true;
  $('serverIconUpload').disabled = true;
  $('serverIconDelete').disabled = true;
  $('serverIconStatus').textContent = 'PNG 이미지를 64×64 크기로 변환하고 있습니다.';
  try {
    const blob = await resizeServerIcon(file);
    const form = new FormData();
    form.append('icon', blob, 'server-icon.png');
    const data = await api('/api/server-icon', {method: 'POST', body: form});
    await refreshServerIconPreview();
    toast(data.message || '서버 아이콘이 저장되었습니다.');
  } catch (error) {
    $('serverIconStatus').textContent = error.message;
    toast(error.message, true);
  } finally {
    serverIconBusy = false;
    $('serverIconUpload').disabled = false;
    $('serverIconUpload').value = '';
    $('serverIconDelete').disabled = !serverIconObjectUrl;
  }
}

document.querySelectorAll('[data-motd-code]').forEach(button => {
  button.onclick = () => insertMotdCode(button.dataset.motdCode);
});
$('Motd').addEventListener('input', updateMotdPreview);
$('ServerName').addEventListener('input', updateMotdPreview);
$('serverIconUpload').onchange = event => uploadServerIcon(event.target.files?.[0]);
$('serverIconDelete').onclick = async () => {
  if (serverIconBusy || !serverIconObjectUrl) return;
  serverIconBusy = true;
  try {
    const data = await api('/api/server-icon', {method: 'DELETE'});
    showServerIcon();
    $('serverIconStatus').textContent = '등록된 서버 아이콘이 없습니다.';
    toast(data.message || '서버 아이콘이 삭제되었습니다.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    serverIconBusy = false;
  }
};

function updateTypeFields() {
  $('loaderModpackSection').classList.toggle('visible', ['FORGE', 'NEOFORGE', 'FABRIC'].includes($('Type').value));
}

function updateRange(input) {
  const output = document.querySelector(`output[data-for="${input.id}"]`);
  if (output) output.value = input.value;
}
document.querySelectorAll('input[type=range]').forEach(input => input.oninput = () => updateRange(input));
$('minecraftVersionSelect').onchange = event => {
  if (!event.target.value) return;
  $('Version').value = event.target.value;
  event.target.value = '';
};
$('Memory').oninput = event => { event.target.value = event.target.value.replace(/\D/g, ''); };
$('Type').onchange = updateTypeFields;

async function openSettings(targetId = '') {
  try {
    const data = await api('/api/config');
    if (data.locked) { toast('서버 실행 중에는 설정을 변경할 수 없습니다.', true); return; }
    fields.forEach(key => {
      const input = $(key);
      if (!input) return;
      if (checkFields.has(key)) input.checked = Boolean(data.config[key]);
      else if (key === 'Memory') input.value = String(data.config[key] || '4G').replace(/G$/i, '');
      else input.value = data.config[key] ?? '';
      updateRange(input);
    });
    $('JavaVersion').disabled = Boolean(data.engine_installed);
    updateTypeFields();
    updateMotdPreview();
    showDialog(settingsDialog);
    refreshServerIconPreview();
    if (targetId) {
      requestAnimationFrame(() => {
        const target = $(targetId);
        if (!target) return;
        target.scrollIntoView({behavior: 'smooth', block: 'center'});
        target.classList.add('settings-target-flash');
        if (target.matches('input,select,textarea')) target.focus({preventScroll: true});
        setTimeout(() => target.classList.remove('settings-target-flash'), 1400);
      });
    }
  } catch (error) { toast(error.message, true); }
}
$('settingsButton').onclick = () => openSettings();

$('settingsForm').onsubmit = async event => {
  event.preventDefault();
  try {
    const body = {};
    fields.forEach(key => {
      const input = $(key);
      body[key] = checkFields.has(key) ? input.checked : key === 'Memory' ? `${input.value}G` : numberFields.has(key) ? Number(input.value) : input.value;
    });
    if (!/^[1-9]\d*$/.test($('Memory').value)) throw new Error('메모리는 1 이상의 숫자로 입력해주세요.');
    await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    $('settingsMode').textContent = settingsSummary(body);
    $('saveToast').classList.add('show');
    setTimeout(() => { $('saveToast').classList.remove('show'); settingsDialog.close(); }, 1000);
  } catch (error) { toast(error.message, true); }
};

function humanSize(bytes) {
  if (!bytes) return '-';
  const units = ['B','KB','MB','GB']; let value = bytes; let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${value.toFixed(unit ? 1 : 0)} ${units[unit]}`;
}

function createFileIcon(type) {
  const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  icon.setAttribute('viewBox', '0 0 24 24');
  icon.setAttribute('fill', 'none');
  icon.setAttribute('stroke', 'currentColor');
  icon.setAttribute('stroke-width', '2');
  icon.setAttribute('stroke-linecap', 'round');
  icon.setAttribute('stroke-linejoin', 'round');
  icon.setAttribute('aria-hidden', 'true');
  icon.classList.add('file-entry-icon', type === 'dir' ? 'folder' : 'document');
  icon.innerHTML = type === 'dir'
    ? '<path d="M3 5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"></path>'
    : '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"></path><path d="M14 2v6h6"></path>';
  return icon;
}

async function loadFiles(path = '') {
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    currentPath = data.path; currentParent = data.parent; writeLocked = data.write_locked;
    selectedFilePaths.clear();
    updateSelectedDownloadButton();
    $('filePath').textContent = `/data${currentPath ? '/' + currentPath : ''}`;
    $('newFolder').disabled = writeLocked; $('fileUpload').disabled = writeLocked; $('folderUpload').disabled = writeLocked;
    const list = $('fileList'); list.textContent = '';
    if (!data.entries.length) { list.innerHTML = '<div class="file-empty">폴더가 비어 있습니다.</div>'; return; }
    data.entries.forEach(entry => {
      const row = document.createElement('div'); row.className = 'file-row';
      const select = document.createElement('input'); select.type = 'checkbox'; select.className = 'file-select'; select.setAttribute('aria-label', `${entry.name} 선택`);
      select.onchange = () => {
        if (select.checked) selectedFilePaths.add(entry.path); else selectedFilePaths.delete(entry.path);
        row.classList.toggle('selected', select.checked);
        updateSelectedDownloadButton();
      };
      const name = document.createElement('button'); name.className = 'file-name';
      const label = document.createElement('span'); label.className = 'file-name-label'; label.textContent = entry.name;
      name.append(createFileIcon(entry.type), label);
      name.onclick = () => entry.type === 'dir' ? loadFiles(entry.path) : location.href = `/api/files/download?path=${encodeURIComponent(entry.path)}`;
      const size = document.createElement('span'); size.textContent = entry.type === 'dir' ? '폴더' : humanSize(entry.size);
      const modified = document.createElement('span'); modified.className = 'modified'; modified.textContent = entry.modified.replace('T',' ');
      const del = document.createElement('button'); del.className = 'delete-file'; del.textContent = '×'; del.disabled = writeLocked; del.title = '삭제';
      del.onclick = async () => { if (!confirm(`${entry.name} 항목을 삭제할까요?`)) return; try { await api('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:entry.path})}); loadFiles(currentPath); } catch(error){ toast(error.message,true); } };
      row.append(select,name,size,modified,del); list.append(row);
    });
  } catch (error) { toast(error.message, true); }
}

function updateSelectedDownloadButton() {
  $('fileDownloadSelected').disabled = selectedFilePaths.size === 0;
}

function responseFilename(response, fallback) {
  const disposition = response.headers.get('Content-Disposition') || '';
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encoded) {
    try { return decodeURIComponent(encoded[1]); } catch (_) { return encoded[1]; }
  }
  return disposition.match(/filename="?([^";]+)"?/i)?.[1] || fallback;
}

async function downloadSelectedFiles() {
  if (!selectedFilePaths.size) return;
  const button = $('fileDownloadSelected');
  button.disabled = true;
  button.textContent = 'ZIP 준비 중...';
  try {
    const response = await fetch('/api/files/download-selected', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({paths: Array.from(selectedFilePaths)})
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.detail || '선택 항목을 다운로드하지 못했습니다.');
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a');
    link.href = url;
    link.download = responseFilename(response, 'minecraft-files.zip');
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    selectedFilePaths.clear();
    document.querySelectorAll('.file-select').forEach(input => { input.checked = false; input.closest('.file-row')?.classList.remove('selected'); });
    toast('선택한 항목의 ZIP 다운로드가 시작되었습니다.');
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.textContent = '↓ 선택 다운로드';
    updateSelectedDownloadButton();
  }
}

$('filesButton').onclick = async () => {
  try {
    const server = await api('/api/server/status');
    if (server.running || server.stoppable) {
      showButtonBubble($('filesButton'), '서버 기동 중에는 파일 핸들링이 불가능합니다.');
      return;
    }
    showDialog(filesDialog);
    loadFiles('');
  } catch (error) {
    toast(error.message, true);
  }
};
$('fileUp').onclick = () => loadFiles(currentParent);
$('fileDownloadSelected').onclick = downloadSelectedFiles;
$('newFolder').onclick = async () => {
  const name = prompt('새 폴더 이름'); if (!name) return;
  try { await api('/api/files/mkdir',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:currentPath,name})}); loadFiles(currentPath); }
  catch(error){ toast(error.message,true); }
};
$('fileUpload').onchange = async event => {
  try {
    for (const file of event.target.files) { const form = new FormData(); form.append('file',file); await api(`/api/files/upload?path=${encodeURIComponent(currentPath)}`,{method:'POST',body:form}); }
    toast('파일 업로드가 완료되었습니다.'); loadFiles(currentPath);
  } catch(error){ toast(error.message,true); } finally { event.target.value=''; }
};
$('folderUpload').onchange = async event => {
  const files = Array.from(event.target.files || []);
  if (!files.length) return;
  try {
    const form = new FormData();
    files.forEach(file => {
      form.append('files', file, file.name);
      form.append('relative_paths', file.webkitRelativePath || file.name);
    });
    const result = await api(`/api/files/upload-folder?path=${encodeURIComponent(currentPath)}`, {method:'POST', body:form});
    toast(`폴더 업로드가 완료되었습니다. 파일 ${result.uploaded_count}개`);
    loadFiles(currentPath);
  } catch(error) {
    toast(error.message, true);
  } finally {
    event.target.value = '';
  }
};

function schedulePanelUpdateStatus(delay = 2000) {
  clearTimeout(panelUpdatePollTimer);
  panelUpdatePollTimer = setTimeout(loadPanelUpdateStatus, delay);
}

async function checkPanelUpdateOnLoad() {
  try {
    const data = await api('/api/panel/update/check');
    $('panelUpdateNotice').classList.toggle('show', Boolean(data.update_available));
  } catch (_) {
    $('panelUpdateNotice').classList.remove('show');
  }
}

function setPanelUpdateProgress(progress, status = 'idle') {
  const value = Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));
  const wrapper = $('panelUpdateProgress');
  const track = wrapper.querySelector('[role="progressbar"]');
  wrapper.dataset.status = status;
  $('panelUpdatePercent').textContent = `${value}%`;
  $('panelUpdateProgressBar').style.width = `${value}%`;
  track.setAttribute('aria-valuenow', String(value));
}

function setPanelUpdateProgressVisible(visible) {
  panelUpdateProgressVisible = Boolean(visible);
  $('panelUpdateProgress').hidden = !panelUpdateProgressVisible;
}

async function loadPanelUpdateStatus() {
  const statusBox = $('panelUpdateStatus');
  const updateButton = $('panelUpdateConfirm');
  try {
    const response = await fetch('/api/panel/update/status', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || '업데이트 상태 조회 실패');

    const status = (data.status || 'idle').toLowerCase();
    const active = ['checking', 'downloading', 'restarting', 'running', 'started'].includes(status);
    statusBox.dataset.status = status;
    statusBox.textContent = data.message || '업데이트 상태를 확인할 수 없습니다.';
    const fallbackProgress = {started: 2, running: 5, checking: 5, downloading: 10, restarting: 90, completed: 100, not_required: 100};
    const reportedProgress = Number(data.progress);
    setPanelUpdateProgress(reportedProgress > 0 ? reportedProgress : fallbackProgress[status] ?? 0, status);
    updateButton.disabled = active;
    updateButton.textContent = active ? '업데이트 진행 중' : '업데이트 확인 및 설치';

    if (panelUpdateRequested && status === 'not_required') {
      panelUpdateRequested = false;
      alert('이미 최신 버전의 TechTim 구동기를 사용하고 있어 업데이트가 필요하지 않습니다.');
    } else if (panelUpdateRequested && status === 'failed') {
      panelUpdateRequested = false;
      alert(data.message || 'TechTim 구동기 업데이트에 실패했습니다.');
    }

    if (panelUpdateRequested && status === 'completed' && !panelUpdateReloadScheduled) {
      panelUpdateRequested = false;
      panelUpdateReloadScheduled = true;
      statusBox.textContent = 'TechTim 구동기 업데이트가 완료되었습니다. 화면을 새로고침합니다.';
      setPanelUpdateProgress(100, 'completed');
      setTimeout(() => location.reload(), 1800);
      return;
    }
    if (active) schedulePanelUpdateStatus();
  } catch (error) {
    if (panelUpdateRequested) {
      statusBox.dataset.status = 'restarting';
      statusBox.textContent = '패널 컨테이너를 교체하고 있습니다. 잠시 후 자동으로 다시 연결합니다.';
      const currentProgress = Number($('panelUpdateProgress').querySelector('[role="progressbar"]').getAttribute('aria-valuenow')) || 0;
      setPanelUpdateProgress(Math.max(currentProgress, 92), 'restarting');
      updateButton.disabled = true;
      updateButton.textContent = '업데이트 진행 중';
      schedulePanelUpdateStatus(1800);
    } else {
      statusBox.dataset.status = 'failed';
      statusBox.textContent = `업데이트 상태 조회 실패: ${error.message}`;
      setPanelUpdateProgress(0, 'failed');
    }
  }
}

async function requestPanelUpdate() {
  if (!confirm('TechTim 구동기를 확인하고 필요하면 패널 컨테이너를 업데이트할까요?')) return;

  const statusBox = $('panelUpdateStatus');
  const updateButton = $('panelUpdateConfirm');
  panelUpdateRequested = true;
  panelUpdateReloadScheduled = false;
  setPanelUpdateProgressVisible(true);
  updateButton.disabled = true;
  updateButton.textContent = '업데이트 확인 중';
  statusBox.dataset.status = 'checking';
  statusBox.textContent = 'TechTim 구동기 업데이트 확인을 요청하고 있습니다.';
  setPanelUpdateProgress(2, 'checking');

  try {
    const response = await fetch('/api/panel/update', {method: 'POST'});
    const data = await response.json();
    if (!response.ok) {
      panelUpdateRequested = false;
      throw new Error(data.detail || '업데이트 요청 실패');
    }
    statusBox.textContent = data.message || '업데이트 확인을 시작했습니다.';
    schedulePanelUpdateStatus(700);
  } catch (error) {
    panelUpdateRequested = false;
    updateButton.disabled = false;
    updateButton.textContent = '업데이트 확인 및 설치';
    statusBox.dataset.status = 'failed';
    statusBox.textContent = `TechTim 구동기 업데이트 요청 실패: ${error.message}`;
    setPanelUpdateProgress(0, 'failed');
  }
}

function formatResourceBytes(value, perSecond = false) {
  const bytes = Math.max(0, Number(value) || 0);
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let unit = 0;
  let amount = bytes;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit++;
  }
  const digits = unit === 0 || amount >= 100 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(digits)} ${units[unit]}${perSecond ? '/s' : ''}`;
}

function setResourceMeter(valueId, barId, percent) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  $(valueId).textContent = `${value.toFixed(value % 1 ? 1 : 0)}%`;
  $(barId).style.width = `${value}%`;
}

function renderCpuThreads(threads) {
  const root = $('resourceCpuThreads');
  const values = Array.isArray(threads) ? threads : [];
  root.textContent = '';
  $('resourceCpuThreadCount').textContent = values.length ? `${values.length} Threads` : '미지원';
  if (!values.length) {
    const empty = document.createElement('span');
    empty.className = 'cpu-thread-empty';
    empty.textContent = 'OS CPU 통계를 불러올 수 없습니다.';
    root.append(empty);
    return;
  }
  values.forEach(item => {
    const percent = Math.max(0, Math.min(100, Number(item.percent) || 0));
    const thread = document.createElement('div');
    thread.className = 'cpu-thread';
    const heading = document.createElement('div');
    heading.className = 'cpu-thread-head';
    const name = document.createElement('span');
    name.textContent = `CPU${item.thread}`;
    const value = document.createElement('strong');
    value.textContent = `${percent.toFixed(percent % 1 ? 1 : 0)}%`;
    heading.append(name, value);
    const meter = document.createElement('div');
    meter.className = 'cpu-thread-meter';
    const bar = document.createElement('span');
    bar.style.width = `${percent}%`;
    meter.append(bar);
    thread.append(heading, meter);
    root.append(thread);
  });
}

function publicAddressFallback() {
  const hostname = location.hostname;
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(hostname)) return '';
  if (hostname === '127.0.0.1' || hostname.startsWith('10.') || hostname.startsWith('192.168.')) return '';
  const second = Number(hostname.split('.')[1]);
  if (hostname.startsWith('172.') && second >= 16 && second <= 31) return '';
  return hostname;
}

async function copyToClipboard(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement('textarea');
  input.value = value;
  input.setAttribute('readonly', '');
  input.style.position = 'fixed';
  input.style.opacity = '0';
  document.body.appendChild(input);
  input.select();
  const copied = document.execCommand('copy');
  input.remove();
  if (!copied) throw new Error('클립보드 복사를 지원하지 않는 브라우저입니다.');
}

$('resourcePublicAddress').onclick = async event => {
  const button = event.currentTarget;
  const address = button.dataset.address || '';
  if (!address) return;
  try {
    await copyToClipboard(address);
    showButtonBubble(button, '공인 IP가 복사되었습니다.', false);
  } catch (error) {
    showButtonBubble(button, error.message);
  }
};

async function refreshResources() {
  try {
    const data = await api('/api/server/resources');
    renderCpuThreads(data.cpu_threads);
    setResourceMeter('resourceMemory', 'resourceMemoryBar', data.memory_percent);
    setResourceMeter('resourceDisk', 'resourceDiskBar', data.disk_percent);
    $('resourceMemoryDetail').textContent = data.memory_limit
      ? `${formatResourceBytes(data.memory_used)} / ${formatResourceBytes(data.memory_limit)}`
      : '서버 시작 후 표시';
    $('resourceDiskDetail').textContent = `${formatResourceBytes(data.disk_used)} / ${formatResourceBytes(data.disk_total)}`;
    $('resourceNetworkDown').textContent = `↓ ${formatResourceBytes(data.network_received_per_second, true)}`;
    $('resourceNetworkUp').textContent = `↑ ${formatResourceBytes(data.network_sent_per_second, true)}`;
    $('resourceNetworkState').textContent = data.running ? '활성' : '대기';
    const publicAddress = data.public_ip || publicAddressFallback();
    const publicAddressButton = $('resourcePublicAddress');
    publicAddressButton.dataset.address = publicAddress;
    publicAddressButton.disabled = !publicAddress;
    $('resourcePublicAddressText').textContent = publicAddress || '확인 불가';
    $('resourceStatus').textContent = data.error ? '일부 조회 실패 : 5초' : '새로고침 : 5초';
  } catch (_) {
    $('resourceStatus').textContent = '조회 실패 : 5초';
  }
}

async function initialize() {
  loadMinecraftVersions();
  checkPanelUpdateOnLoad();
  await refreshStatus();
  const config = await api('/api/config').catch(() => null);
  if (config) $('settingsMode').textContent = settingsSummary(config.config);
  await refreshLog();
  await refreshResources();
  setInterval(refreshStatus, 3000);
  setInterval(refreshLog, 2000);
  setInterval(refreshResources, 5000);
}
initialize();
