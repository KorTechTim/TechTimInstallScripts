const $ = id => document.getElementById(id);
const terminal = $('terminal');
const settingsDialog = $('settingsDialog');
const filesDialog = $('filesDialog');
const eulaDialog = $('eulaDialog');
const deleteServerDialog = $('deleteServerDialog');
const panelUpdateDialog = $('panelUpdateDialog');
const curseforgeDialog = $('curseforgeDialog');
let logMode = 'install';
let currentPath = '';
let currentParent = '';
let writeLocked = false;
let panelUpdateRequested = false;
let panelUpdatePollTimer = null;
let panelUpdateReloadScheduled = false;
let commandSending = false;
let selectedCurseForgeProject = null;

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
    $('startButton').disabled = server.running || !install.installed;
    $('stopButton').disabled = !server.running;
    $('deleteServerButton').disabled = install.status === 'running' || (!install.installed && server.status === 'not_created');
    $('installButton').disabled = install.status === 'running' || install.install_locked;
    $('settingsEntry').classList.toggle('locked', server.running);
    $('settingsButton').disabled = server.running;
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
document.querySelectorAll('dialog').forEach(dialog => dialog.addEventListener('close', syncModalScrollLock));

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
  showDialog(panelUpdateDialog);
  loadPanelUpdateStatus();
};
$('panelUpdateConfirm').onclick = requestPanelUpdate;
$('installButton').onclick = async () => {
  try {
    const server = await api('/api/server/status');
    if (server.running) {
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
  try {
    const options = {method: 'POST'};
    if (payload) {
      options.headers = {'Content-Type': 'application/json'};
      options.body = JSON.stringify(payload);
    }
    await api(`/api/server/${action}`, options);
    selectLog('server');
    toast({start: '서버 시작을 요청했습니다.', stop: '서버가 종료되었습니다.', restart: '서버를 재시작했습니다.'}[action]);
    await refreshStatus();
  } catch (error) { toast(error.message, true); }
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
    if (server.running) {
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

const fields = ['Type','Version','JavaVersion','Memory','ServerName','Motd','Level','Seed','Difficulty','GameMode','MaxPlayers','OnlineMode','Pvp','AllowFlight','EnableCommandBlock','ViewDistance','SimulationDistance','SpawnProtection','Whitelist','Ops','ModrinthProjects','ModpackUrl','ModpackSource','CurseForgeProjectId','CurseForgeFileId','CurseForgeSlug','CurseForgeProjectName','CurseForgeFileName','CurseForgePageUrl','CurseForgeGameVersion','CurseForgeLoader'];
const checkFields = new Set(['OnlineMode','Pvp','AllowFlight','EnableCommandBlock']);
const numberFields = new Set(['MaxPlayers','ViewDistance','SimulationDistance','SpawnProtection']);

function updateTypeFields() {
  $('loaderModpackSection').classList.toggle('visible', ['FORGE', 'NEOFORGE', 'FABRIC'].includes($('Type').value));
}

function renderCurseForgeSelection() {
  const selected = $('ModpackSource').value === 'curseforge' && Boolean($('CurseForgeFileId').value);
  $('curseforgeSelection').hidden = !selected;
  if (!selected) return;
  $('curseforgeSelectionName').textContent = $('CurseForgeProjectName').value || 'CurseForge 모드팩';
  const details = [$('CurseForgeFileName').value, $('CurseForgeGameVersion').value, $('CurseForgeLoader').value].filter(Boolean);
  $('curseforgeSelectionDetail').textContent = details.join(' · ');
}

function clearCurseForgeSelection(clearUrl = false) {
  $('ModpackSource').value = 'manual';
  ['CurseForgeProjectId','CurseForgeFileId','CurseForgeSlug','CurseForgeProjectName','CurseForgeFileName','CurseForgePageUrl','CurseForgeGameVersion','CurseForgeLoader'].forEach(key => { $(key).value = ''; });
  if (clearUrl) $('ModpackUrl').value = '';
  renderCurseForgeSelection();
}

function curseForgeLoaderCode(type) {
  return {FORGE: '1', FABRIC: '4', NEOFORGE: '6'}[type] || '';
}

function curseForgeLoaderName(gameVersions) {
  const values = new Set((gameVersions || []).map(value => String(value).toLowerCase()));
  if (values.has('neoforge')) return 'NeoForge';
  if (values.has('fabric')) return 'Fabric';
  if (values.has('forge')) return 'Forge';
  if (values.has('quilt')) return 'Quilt';
  return '';
}

function curseForgeMinecraftVersion(gameVersions) {
  const requested = $('curseforgeGameVersion').value.trim();
  const versions = (gameVersions || []).map(String);
  if (requested && versions.includes(requested)) return requested;
  return versions.find(value => /^\d+(?:\.\d+)+$/.test(value)) || '';
}

function formatDownloads(value) {
  const count = Number(value) || 0;
  return new Intl.NumberFormat('ko-KR', {notation: count >= 10000 ? 'compact' : 'standard', maximumFractionDigits: 1}).format(count);
}

function setCurseForgeLoading(message) {
  const loading = document.createElement('div');
  loading.className = 'curseforge-empty';
  loading.textContent = message;
  $('curseforgeResults').replaceChildren(loading);
}

function renderCurseForgeProjects(projects) {
  const results = $('curseforgeResults');
  results.textContent = '';
  if (!projects.length) {
    setCurseForgeLoading('검색 조건에 맞는 모드팩이 없습니다.');
    return;
  }
  projects.forEach(project => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'curseforge-project';
    const visual = document.createElement('span');
    visual.className = 'curseforge-project-logo';
    if (project.logoUrl) {
      const image = document.createElement('img');
      image.src = project.logoUrl;
      image.alt = '';
      image.loading = 'lazy';
      visual.append(image);
    } else {
      visual.textContent = 'CF';
    }
    const copy = document.createElement('span');
    copy.className = 'curseforge-project-copy';
    const title = document.createElement('strong');
    title.textContent = project.name;
    const summary = document.createElement('small');
    summary.textContent = project.summary || '설명 없음';
    const meta = document.createElement('span');
    const author = Array.isArray(project.authors) && project.authors.length ? project.authors.join(', ') : 'CurseForge';
    meta.textContent = `${author} · 다운로드 ${formatDownloads(project.downloadCount)}`;
    copy.append(title, summary, meta);
    const arrow = document.createElement('b');
    arrow.className = 'curseforge-project-arrow';
    arrow.textContent = '›';
    button.append(visual, copy, arrow);
    button.onclick = () => loadCurseForgeFiles(project);
    results.append(button);
  });
}

async function loadCurseForgeProjects() {
  const submit = $('curseforgeSearchButton');
  submit.disabled = true;
  submit.textContent = '검색 중';
  selectedCurseForgeProject = null;
  $('curseforgeBack').hidden = true;
  $('curseforgeViewTitle').textContent = $('curseforgeQuery').value.trim() ? '검색 결과' : '인기 모드팩';
  $('curseforgeViewCaption').textContent = '설치할 모드팩을 선택하세요.';
  setCurseForgeLoading('CurseForge에서 모드팩을 불러오고 있습니다.');
  const params = new URLSearchParams({
    query: $('curseforgeQuery').value.trim(),
    game_version: $('curseforgeGameVersion').value.trim(),
    mod_loader_type: $('curseforgeLoader').value,
  });
  try {
    const data = await api(`/api/curseforge/search?${params}`);
    renderCurseForgeProjects(Array.isArray(data.projects) ? data.projects : []);
  } catch (error) {
    setCurseForgeLoading(error.message);
  } finally {
    submit.disabled = false;
    submit.textContent = '검색';
  }
}

function selectCurseForgeFile(project, file) {
  const pageUrl = `https://www.curseforge.com/minecraft/modpacks/${encodeURIComponent(project.slug)}/files/${file.id}`;
  const loader = curseForgeLoaderName(file.gameVersions);
  const gameVersion = curseForgeMinecraftVersion(file.gameVersions);
  $('ModpackUrl').value = pageUrl;
  $('ModpackSource').value = 'curseforge';
  $('CurseForgeProjectId').value = project.id;
  $('CurseForgeFileId').value = file.id;
  $('CurseForgeSlug').value = project.slug;
  $('CurseForgeProjectName').value = project.name;
  $('CurseForgeFileName').value = file.displayName || file.fileName;
  $('CurseForgePageUrl').value = pageUrl;
  $('CurseForgeGameVersion').value = gameVersion;
  $('CurseForgeLoader').value = loader;
  if (gameVersion) $('Version').value = gameVersion;
  if (loader === 'Forge') $('Type').value = 'FORGE';
  if (loader === 'NeoForge') $('Type').value = 'NEOFORGE';
  if (loader === 'Fabric') $('Type').value = 'FABRIC';
  updateTypeFields();
  renderCurseForgeSelection();
  closeDialog(curseforgeDialog);
}

function renderCurseForgeFiles(project, files) {
  const results = $('curseforgeResults');
  results.textContent = '';
  if (!files.length) {
    setCurseForgeLoading('선택한 조건에 맞는 일반 모드팩 파일이 없습니다. 버전 또는 로더 조건을 비워보세요.');
    return;
  }
  files.forEach(file => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'curseforge-file';
    const copy = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = file.displayName || file.fileName;
    const tags = [curseForgeMinecraftVersion(file.gameVersions), curseForgeLoaderName(file.gameVersions)].filter(Boolean);
    const meta = document.createElement('small');
    const release = {1: '정식', 2: '베타', 3: '알파'}[file.releaseType] || '배포';
    meta.textContent = `${tags.join(' · ') || '버전 정보 없음'} · ${release} · 다운로드 ${formatDownloads(file.downloadCount)}`;
    copy.append(title, meta);
    const action = document.createElement('b');
    action.textContent = '이 버전 선택';
    button.append(copy, action);
    button.onclick = () => selectCurseForgeFile(project, file);
    results.append(button);
  });
}

async function loadCurseForgeFiles(project) {
  selectedCurseForgeProject = project;
  $('curseforgeBack').hidden = false;
  $('curseforgeViewTitle').textContent = project.name;
  $('curseforgeViewCaption').textContent = '서버에 설치할 일반 모드팩 파일을 선택하세요.';
  setCurseForgeLoading('사용 가능한 모드팩 버전을 불러오고 있습니다.');
  const params = new URLSearchParams({
    game_version: $('curseforgeGameVersion').value.trim(),
    mod_loader_type: $('curseforgeLoader').value,
  });
  try {
    const data = await api(`/api/curseforge/projects/${project.id}/files?${params}`);
    renderCurseForgeFiles(project, Array.isArray(data.files) ? data.files : []);
  } catch (error) {
    setCurseForgeLoading(error.message);
  }
}

$('curseforgeBrowseButton').onclick = () => {
  const version = $('Version').value.trim();
  $('curseforgeGameVersion').value = version && version !== 'LATEST' ? version : '';
  $('curseforgeLoader').value = curseForgeLoaderCode($('Type').value);
  $('curseforgeQuery').value = '';
  showDialog(curseforgeDialog);
  loadCurseForgeProjects();
};
$('curseforgeSearchForm').onsubmit = event => { event.preventDefault(); loadCurseForgeProjects(); };
$('curseforgeBack').onclick = () => loadCurseForgeProjects();

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
$('ModpackUrl').addEventListener('input', () => {
  if ($('ModpackSource').value === 'curseforge' && $('ModpackUrl').value !== $('CurseForgePageUrl').value) {
    clearCurseForgeSelection(false);
  }
});
$('curseforgeSelectionClear').onclick = () => clearCurseForgeSelection(true);

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
    $('ExtraEnv').value = JSON.stringify(data.config.ExtraEnv || {}, null, 2);
    updateTypeFields();
    renderCurseForgeSelection();
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
    $('filePath').textContent = `/data${currentPath ? '/' + currentPath : ''}`;
    $('newFolder').disabled = writeLocked; $('fileUpload').disabled = writeLocked; $('folderUpload').disabled = writeLocked;
    const list = $('fileList'); list.textContent = '';
    if (!data.entries.length) { list.innerHTML = '<div class="file-row"><span>폴더가 비어 있습니다.</span></div>'; return; }
    data.entries.forEach(entry => {
      const row = document.createElement('div'); row.className = 'file-row';
      const name = document.createElement('button'); name.className = 'file-name';
      const label = document.createElement('span'); label.className = 'file-name-label'; label.textContent = entry.name;
      name.append(createFileIcon(entry.type), label);
      name.onclick = () => entry.type === 'dir' ? loadFiles(entry.path) : location.href = `/api/files/download?path=${encodeURIComponent(entry.path)}`;
      const size = document.createElement('span'); size.textContent = entry.type === 'dir' ? '폴더' : humanSize(entry.size);
      const modified = document.createElement('span'); modified.className = 'modified'; modified.textContent = entry.modified.replace('T',' ');
      const del = document.createElement('button'); del.className = 'delete-file'; del.textContent = '×'; del.disabled = writeLocked; del.title = '삭제';
      del.onclick = async () => { if (!confirm(`${entry.name} 항목을 삭제할까요?`)) return; try { await api('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:entry.path})}); loadFiles(currentPath); } catch(error){ toast(error.message,true); } };
      row.append(name,size,modified,del); list.append(row);
    });
  } catch (error) { toast(error.message, true); }
}

$('filesButton').onclick = async () => {
  try {
    const server = await api('/api/server/status');
    if (server.running) {
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

function setPanelUpdateProgress(progress, status = 'idle') {
  const value = Math.max(0, Math.min(100, Math.round(Number(progress) || 0)));
  const wrapper = $('panelUpdateProgress');
  const track = wrapper.querySelector('[role="progressbar"]');
  wrapper.dataset.status = status;
  $('panelUpdatePercent').textContent = `${value}%`;
  $('panelUpdateProgressBar').style.width = `${value}%`;
  track.setAttribute('aria-valuenow', String(value));
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

async function refreshResources() {
  try {
    const data = await api('/api/server/resources');
    setResourceMeter('resourceCpu', 'resourceCpuBar', data.cpu_percent);
    setResourceMeter('resourceMemory', 'resourceMemoryBar', data.memory_percent);
    setResourceMeter('resourceDisk', 'resourceDiskBar', data.disk_percent);
    $('resourceMemoryDetail').textContent = data.memory_limit
      ? `${formatResourceBytes(data.memory_used)} / ${formatResourceBytes(data.memory_limit)}`
      : '서버 시작 후 표시';
    $('resourceDiskDetail').textContent = `${formatResourceBytes(data.disk_used)} / ${formatResourceBytes(data.disk_total)}`;
    $('resourceNetworkDown').textContent = `↓ ${formatResourceBytes(data.network_received_per_second, true)}`;
    $('resourceNetworkUp').textContent = `↑ ${formatResourceBytes(data.network_sent_per_second, true)}`;
    $('resourceNetworkState').textContent = data.running ? '활성' : '대기';
    $('resourceStatus').textContent = data.error ? '일부 조회 실패 : 5초' : '새로고침 : 5초';
  } catch (_) {
    $('resourceStatus').textContent = '조회 실패 : 5초';
  }
}

async function initialize() {
  loadMinecraftVersions();
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
