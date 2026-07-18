const $ = id => document.getElementById(id);
const terminal = $('terminal');
const settingsDialog = $('settingsDialog');
const filesDialog = $('filesDialog');
const panelUpdateDialog = $('panelUpdateDialog');
let logMode = 'install';
let currentPath = '';
let currentParent = '';
let writeLocked = false;
let panelUpdateRequested = false;
let panelUpdatePollTimer = null;
let panelUpdateReloadScheduled = false;
let commandSending = false;

const statusKo = {
  not_started: '미설치', running: '실행 중', completed: '설치 완료', failed: '설치 실패',
  created: '생성됨', exited: '중지됨', dead: '오류', restarting: '재시작 중',
  not_created: '생성되지 않음', paused: '일시 정지', removing: '삭제 중'
};

function toast(message, error = false) {
  const el = $('toast');
  el.textContent = message;
  el.className = `toast show${error ? ' error' : ''}`;
  clearTimeout(el.timer);
  el.timer = setTimeout(() => el.className = 'toast', 2500);
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
    $('startButton').disabled = server.running || !install.installed;
    $('stopButton').disabled = !server.running;
    $('restartButton').disabled = !server.running;
    $('installButton').disabled = install.status === 'running';
    $('settingsEntry').classList.toggle('locked', server.running);
    $('settingsButton').disabled = server.running;
    $('consoleCommandInput').disabled = !server.running || commandSending;
    $('consoleCommandSend').disabled = !server.running || commandSending;
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
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', syncModalScrollLock));

$('consoleCommandForm').onsubmit = async event => {
  event.preventDefault();
  const input = $('consoleCommandInput');
  const command = input.value.trim();
  if (!command || commandSending) return;
  commandSending = true;
  input.disabled = true;
  $('consoleCommandSend').disabled = true;
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
  showDialog(panelUpdateDialog);
  loadPanelUpdateStatus();
};
$('panelUpdateConfirm').onclick = requestPanelUpdate;
$('installButton').onclick = async () => {
  try { await api('/api/install', {method: 'POST'}); selectLog('install'); toast('서버 설치를 시작했습니다.'); refreshStatus(); }
  catch (error) { toast(error.message, true); }
};

async function serverAction(action) {
  try {
    await api(`/api/server/${action}`, {method: 'POST'});
    selectLog('server');
    toast({start: '서버 시작을 요청했습니다.', stop: '서버가 종료되었습니다.', restart: '서버를 재시작했습니다.'}[action]);
    await refreshStatus();
  } catch (error) { toast(error.message, true); }
}
$('startButton').onclick = () => serverAction('start');
$('stopButton').onclick = () => serverAction('stop');
$('restartButton').onclick = () => serverAction('restart');

const fields = ['Type','Version','Memory','ServerName','Motd','Level','Seed','Difficulty','GameMode','MaxPlayers','OnlineMode','Pvp','AllowFlight','EnableCommandBlock','ViewDistance','SimulationDistance','SpawnProtection','Whitelist','Ops','ModrinthProjects','ModrinthModpack','ModrinthLoader','CurseForgePageUrl','CurseForgeSlug','CurseForgeApiKey','EulaAccepted'];
const checkFields = new Set(['OnlineMode','Pvp','AllowFlight','EnableCommandBlock','EulaAccepted']);
const numberFields = new Set(['MaxPlayers','ViewDistance','SimulationDistance','SpawnProtection']);

function updateTypeFields() {
  $('modrinthSection').classList.toggle('visible', $('Type').value === 'MODRINTH');
  $('curseforgeSection').classList.toggle('visible', $('Type').value === 'AUTO_CURSEFORGE');
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
      else if (key !== 'CurseForgeApiKey') input.value = data.config[key] ?? '';
      updateRange(input);
    });
    $('CurseForgeApiKey').value = '';
    $('ExtraEnv').value = JSON.stringify(data.config.ExtraEnv || {}, null, 2);
    updateTypeFields();
    showDialog(settingsDialog);
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
    try { body.ExtraEnv = JSON.parse($('ExtraEnv').value || '{}'); }
    catch { throw new Error('추가 환경 변수는 올바른 JSON 형식이어야 합니다.'); }
    await api('/api/config', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    $('settingsMode').textContent = `${body.Type} · ${body.Version} · ${body.Memory}`;
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

async function loadFiles(path = '') {
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    currentPath = data.path; currentParent = data.parent; writeLocked = data.write_locked;
    $('filePath').textContent = `/data${currentPath ? '/' + currentPath : ''}`;
    $('newFolder').disabled = writeLocked; $('fileUpload').disabled = writeLocked;
    const list = $('fileList'); list.textContent = '';
    if (!data.entries.length) { list.innerHTML = '<div class="file-row"><span>폴더가 비어 있습니다.</span></div>'; return; }
    data.entries.forEach(entry => {
      const row = document.createElement('div'); row.className = 'file-row';
      const name = document.createElement('button'); name.className = 'file-name'; name.textContent = `${entry.type === 'dir' ? '▣' : '▤'}  ${entry.name}`;
      name.onclick = () => entry.type === 'dir' ? loadFiles(entry.path) : location.href = `/api/files/download?path=${encodeURIComponent(entry.path)}`;
      const size = document.createElement('span'); size.textContent = entry.type === 'dir' ? '폴더' : humanSize(entry.size);
      const modified = document.createElement('span'); modified.className = 'modified'; modified.textContent = entry.modified.replace('T',' ');
      const del = document.createElement('button'); del.className = 'delete-file'; del.textContent = '×'; del.disabled = writeLocked; del.title = '삭제';
      del.onclick = async () => { if (!confirm(`${entry.name} 항목을 삭제할까요?`)) return; try { await api('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:entry.path})}); loadFiles(currentPath); } catch(error){ toast(error.message,true); } };
      row.append(name,size,modified,del); list.append(row);
    });
  } catch (error) { toast(error.message, true); }
}

$('filesButton').onclick = () => { showDialog(filesDialog); loadFiles(''); };
$('fileUp').onclick = () => loadFiles(currentParent);
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

function schedulePanelUpdateStatus(delay = 2000) {
  clearTimeout(panelUpdatePollTimer);
  panelUpdatePollTimer = setTimeout(loadPanelUpdateStatus, delay);
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
      setTimeout(() => location.reload(), 1800);
      return;
    }
    if (active) schedulePanelUpdateStatus();
  } catch (error) {
    if (panelUpdateRequested) {
      statusBox.dataset.status = 'restarting';
      statusBox.textContent = '패널 컨테이너를 교체하고 있습니다. 잠시 후 자동으로 다시 연결합니다.';
      updateButton.disabled = true;
      updateButton.textContent = '업데이트 진행 중';
      schedulePanelUpdateStatus(1800);
    } else {
      statusBox.dataset.status = 'failed';
      statusBox.textContent = `업데이트 상태 조회 실패: ${error.message}`;
    }
  }
}

async function requestPanelUpdate() {
  if (!confirm('TechTim 구동기를 확인하고 필요하면 패널 컨테이너를 업데이트할까요?')) return;

  const statusBox = $('panelUpdateStatus');
  const updateButton = $('panelUpdateConfirm');
  panelUpdateRequested = true;
  panelUpdateReloadScheduled = false;
  updateButton.disabled = true;
  updateButton.textContent = '업데이트 확인 중';
  statusBox.dataset.status = 'checking';
  statusBox.textContent = 'TechTim 구동기 업데이트 확인을 요청하고 있습니다.';

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
  }
}

async function initialize() {
  loadMinecraftVersions();
  await refreshStatus();
  const config = await api('/api/config').catch(() => null);
  if (config) $('settingsMode').textContent = `${config.config.Type} · ${config.config.Version} · ${config.config.Memory}`;
  await refreshLog();
  setInterval(refreshStatus, 3000);
  setInterval(refreshLog, 2000);
}
initialize();
