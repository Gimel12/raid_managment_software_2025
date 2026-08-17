'use strict';

const state = {
  data: { disks: [], arrays: [], levels: {}, filesystems: {} },
  wizard: { step: 1, level: null, devices: [] },
  filter: 'all',
  mountTarget: null,
  deleteTarget: null,
  loading: false,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
let cockpitHttp = null;

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (character) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
  })[character]);
}

function titleCase(value) {
  return String(value || '').replace(/[-_]/g, ' ').replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function normalizeArrayName(value) {
  return String(value || '')
    .trim()
    .replace(/[^A-Za-z0-9_-]+/g, '-')
    .replace(/^[-_]+|[-_]+$/g, '')
    .slice(0, 32);
}

function formatNumber(value) {
  if (value === null || value === undefined) return '—';
  return Number(value).toLocaleString();
}

function formatHours(value) {
  if (value === null || value === undefined) return '—';
  if (value < 24) return `${formatNumber(value)} hours`;
  return `${formatNumber(Math.round(value / 24))} days`;
}

function toast(title, message = '', kind = 'success') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.innerHTML = `
    <span class="toast-icon">${kind === 'error' ? '!' : '✓'}</span>
    <div><strong>${escapeHtml(title)}</strong>${message ? `<p>${escapeHtml(message)}</p>` : ''}</div>`;
  $('#toast-region').appendChild(node);
  window.setTimeout(() => {
    node.style.opacity = '0';
    node.style.transform = 'translateX(12px)';
    window.setTimeout(() => node.remove(), 220);
  }, 4600);
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (options.method && options.method !== 'GET') headers['X-RAID-Studio'] = '1';
  if (options.body) headers['Content-Type'] = 'application/json';

  if (window.cockpit && typeof window.cockpit.http === 'function') {
    headers['X-RAID-Studio-Envelope'] = '1';
    cockpitHttp ||= window.cockpit.http('/run/raid-studio/raid-studio.sock', { superuser: 'require' });
    let status = 0;
    let responseText = '';
    const request = cockpitHttp.request({
      path: url,
      method: options.method || 'GET',
      headers,
      body: options.body || '',
    });
    request.response((responseStatus) => { status = responseStatus; });
    request.stream((chunk) => {
      responseText += chunk;
      return chunk.length;
    });
    try {
      await request;
    } catch (error) {
      let payload = {};
      try { payload = JSON.parse(responseText || '{}'); } catch (_) { payload = {}; }
      throw new Error(payload.error || error.message || `Request failed (${status || 'connection error'})`);
    }
    let payload = {};
    try { payload = JSON.parse(responseText || '{}'); } catch (_) { payload = {}; }
    if (status >= 400) throw new Error(payload.error || `Request failed (${status})`);
    if (payload.ok === false) throw new Error(payload.error || `Request failed (${payload.status || 'unknown error'})`);
    return payload;
  }

  const response = await fetch(url, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
  return payload;
}

async function loadData({ quiet = false } = {}) {
  if (state.loading) return;
  state.loading = true;
  $('#refresh-button').classList.add('loading');
  try {
    state.data = await api('/api/overview');
    renderAll();
  } catch (error) {
    if (!quiet) toast('Could not load storage data', error.message, 'error');
    $('#updated-at').textContent = 'Connection unavailable';
  } finally {
    state.loading = false;
    $('#refresh-button').classList.remove('loading');
  }
}

function renderAll() {
  const data = state.data;
  $('#side-hostname').textContent = data.hostname || 'Linux host';
  $('#stat-arrays').textContent = data.arrays.length;
  $('#stat-array-note').textContent = data.arrays.length
    ? `${data.arrays.filter((array) => array.health === 'healthy').length} reporting healthy`
    : 'Ready for your first array';
  $('#stat-drives').textContent = data.disks.length;
  $('#stat-drive-note').textContent = `${data.available_count} available for an array`;
  $('#stat-capacity').textContent = data.array_capacity_h || '0 B';
  $('#stat-raw-note').textContent = `${data.total_raw_h || '—'} raw capacity`;
  $('#stat-health').textContent = `${data.healthy_count}/${data.disks.length}`;
  const attention = data.disks.filter((disk) => disk.health.status === 'warning').length;
  $('#stat-health-note').textContent = attention ? `${attention} drive${attention === 1 ? '' : 's'} need attention` : 'SMART checks look good';
  $('#updated-at').textContent = `Updated ${new Date(data.refreshed_at * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}`;

  const notice = $('#system-notice');
  if (!data.mdadm_available) {
    notice.textContent = 'mdadm is not installed. Install it before creating software RAID arrays.';
    notice.classList.remove('hidden');
  } else if (data.available_count < 2) {
    notice.textContent = `Creating an array needs at least two completely unused drives. ${data.available_count} ${data.available_count === 1 ? 'drive is' : 'drives are'} available right now; protected system drives can never be selected.`;
    notice.classList.remove('hidden');
  } else {
    notice.classList.add('hidden');
  }

  renderArrays();
  renderDrives();
}

function displayArrayName(array) {
  if (!array.array_name) return array.name;
  return array.array_name.includes(':') ? array.array_name.split(':').pop() : array.array_name;
}

function renderArrays() {
  const container = $('#arrays');
  container.classList.remove('skeleton-grid');
  container.innerHTML = '';
  $('#arrays-empty').classList.toggle('hidden', state.data.arrays.length > 0);
  if (!state.data.arrays.length) return;

  state.data.arrays.forEach((array) => {
    const memberDots = array.members.length
      ? array.members.map((member) => `<span class="drive-dot ${escapeHtml(member.state)}" title="${escapeHtml(member.path)} · ${escapeHtml(member.state)}">▮</span>`).join('')
      : '<span class="member-caption">No member details</span>';
    const sync = array.sync && array.sync.percent !== null ? `
      <div class="sync-block">
        <div class="sync-meta"><span>${escapeHtml(titleCase(array.sync.operation))} ${Number(array.sync.percent).toFixed(1)}%</span><span>${escapeHtml(array.sync.speed || '')} · ETA ${escapeHtml(array.sync.eta || '—')}</span></div>
        <div class="sync-track"><i style="width:${Math.max(0, Math.min(100, Number(array.sync.percent)))}%"></i></div>
      </div>` : '';
    const mounted = Boolean(array.mountpoint);
    const mountButton = !array.protected && array.fstype ? `
      <button class="button secondary small" type="button" data-array-action="${mounted ? 'unmount' : 'mount'}" data-array="${escapeHtml(array.name)}">
        ${mounted ? 'Unmount' : 'Mount'}
      </button>` : '';
    const arrayActions = array.protected
      ? `<span class="system-array-badge" title="${escapeHtml(array.protection_reason || 'Protected system array')}">Protected system array</span>`
      : `${mountButton}<button class="button secondary small" type="button" data-array-action="delete" data-array="${escapeHtml(array.name)}">Delete</button>`;
    const card = document.createElement('article');
    card.className = `array-card ${escapeHtml(array.health)}`;
    card.innerHTML = `
      <div class="array-card-head">
        <div class="array-identity">
          <div class="array-symbol">▤</div>
          <div class="array-title"><h3>${escapeHtml(displayArrayName(array))}</h3><p>${escapeHtml(array.dev)}</p></div>
        </div>
        <span class="health-badge ${escapeHtml(array.health)}">${escapeHtml(array.health)}</span>
      </div>
      <div class="array-stats">
        <div class="array-stat"><small>RAID level</small><strong>${escapeHtml(String(array.level).toUpperCase())}</strong></div>
        <div class="array-stat"><small>Usable capacity</small><strong>${escapeHtml(array.size_h)}</strong></div>
        <div class="array-stat"><small>Filesystem</small><strong>${escapeHtml(array.fstype || 'Not formatted')}</strong></div>
      </div>
      <div class="member-strip">
        <div class="drive-dots">${memberDots}</div>
        <span class="member-caption">${escapeHtml(array.active)}/${escapeHtml(array.raid_devices)} active${array.spare ? ` · ${escapeHtml(array.spare)} spare` : ''}</span>
      </div>
      ${sync}
      <div class="array-footer">
        <div class="mount-state ${mounted ? '' : 'unmounted'}"><span class="mount-dot"></span><span>${mounted ? `Mounted at ${escapeHtml(array.mountpoint)}` : array.fstype ? 'Not mounted' : 'Raw array'}</span></div>
        <div class="array-actions">
          ${arrayActions}
        </div>
      </div>`;
    container.appendChild(card);
  });
}

function filteredDisks() {
  if (state.filter === 'available') return state.data.disks.filter((disk) => disk.available);
  if (state.filter === 'attention') return state.data.disks.filter((disk) => disk.health.status === 'warning' || disk.health.status === 'unknown');
  return state.data.disks;
}

function driveType(disk) {
  return [disk.tran, disk.ssd ? 'SSD' : 'HDD'].filter(Boolean).join(' · ');
}

function renderDrives() {
  const container = $('#drives');
  container.classList.remove('skeleton-list');
  container.innerHTML = '';
  const disks = filteredDisks();
  if (!disks.length) {
    container.innerHTML = '<div class="empty-state"><h3>No drives match this filter</h3><p>Choose another filter to see the rest of the machine’s storage.</p></div>';
    return;
  }
  disks.forEach((disk) => {
    const health = disk.health || {};
    const item = document.createElement('article');
    item.className = 'drive-item';
    item.dataset.path = disk.path;
    item.innerHTML = `
      <div class="drive-row-main">
        <div class="drive-identity"><span class="disk-symbol"></span><div><strong>${escapeHtml(disk.model)}</strong><small>${escapeHtml(disk.path)}${disk.serial ? ` · ${escapeHtml(disk.serial)}` : ''}</small></div></div>
        <span class="cell-value">${escapeHtml(disk.size_h)}<small class="cell-sub">${escapeHtml(driveType(disk))}</small></span>
        <span class="drive-health ${escapeHtml(health.status)}">${escapeHtml(health.label || 'Unavailable')}</span>
        <span class="cell-value">${health.temperature !== null && health.temperature !== undefined ? `${escapeHtml(health.temperature)}°C` : '—'}<small class="cell-sub">${health.temperature >= 55 ? 'Warm' : health.temperature !== null ? 'Normal' : 'No reading'}</small></span>
        <span><i class="usage-badge ${escapeHtml(disk.status)}">${escapeHtml(titleCase(disk.status))}</i></span>
        <button class="expand-button" type="button" aria-label="Show details" aria-expanded="false">⌄</button>
      </div>
      <div class="drive-details hidden">
        <div class="detail-item"><small>SMART status</small><strong>${escapeHtml(health.message || 'Unavailable')}</strong></div>
        <div class="detail-item"><small>Power-on time</small><strong>${escapeHtml(formatHours(health.power_on_hours))}</strong></div>
        <div class="detail-item"><small>Life used</small><strong>${health.life_used !== null && health.life_used !== undefined ? `${escapeHtml(health.life_used)}%` : '—'}</strong></div>
        <div class="detail-item"><small>Media errors</small><strong>${escapeHtml(formatNumber(health.media_errors))}</strong></div>
        <div class="detail-item"><small>Usage details</small><strong>${escapeHtml(disk.reasons.join(' · ') || 'Ready for a new array')}</strong></div>
      </div>`;
    container.appendChild(item);
  });
}

function openOverlay(id) {
  $(`#${id}`).classList.remove('hidden');
  document.body.style.overflow = 'hidden';
}

function closeOverlay(id) {
  $(`#${id}`).classList.add('hidden');
  if (!$$('.overlay:not(.hidden)').length) document.body.style.overflow = '';
}

function resetWizard() {
  state.wizard = { step: 1, level: null, devices: [] };
  $('#option-name').value = '';
  $('#option-chunk').value = '';
  $('#option-format').checked = true;
  $('#option-mount').checked = true;
  $('#option-mountpoint').value = '';
  $('#erase-confirmation').value = '';
  populateFilesystems();
  renderLevels();
  goToStep(1);
}

function openWizard() {
  resetWizard();
  openOverlay('wizard');
}

function populateFilesystems() {
  const select = $('#option-filesystem');
  select.innerHTML = Object.entries(state.data.filesystems || {}).map(([name, details]) => (
    `<option value="${escapeHtml(name)}">${escapeHtml(details.label)} — ${escapeHtml(details.description)}</option>`
  )).join('');
  if (state.data.filesystems.ext4) select.value = 'ext4';
}

function renderLevels() {
  const container = $('#level-cards');
  container.innerHTML = '';
  Object.entries(state.data.levels).forEach(([key, level]) => {
    const available = state.data.available_count >= level.min;
    const card = document.createElement('button');
    card.type = 'button';
    card.className = `level-card ${state.wizard.level === key ? 'selected' : ''} ${available ? '' : 'disabled'}`;
    card.disabled = !available;
    card.dataset.level = key;
    card.innerHTML = `
      <div class="level-card-head"><h4>${escapeHtml(level.name)}</h4><span class="level-tag">${escapeHtml(level.label)}</span></div>
      <p>${escapeHtml(level.desc)}</p>
      <div class="level-meta"><span>${escapeHtml(level.min)}+ drives</span><span>${escapeHtml(level.efficiency)}</span></div>
      <span class="level-check">✓</span>`;
    container.appendChild(card);
  });
}

function renderDrivePicker() {
  const level = state.data.levels[state.wizard.level];
  const container = $('#drive-picker');
  container.innerHTML = '';
  state.data.disks.forEach((disk) => {
    const selected = state.wizard.devices.includes(disk.path);
    const row = document.createElement('label');
    row.className = `drive-choice ${selected ? 'selected' : ''} ${disk.available ? '' : 'disabled'}`;
    row.innerHTML = `
      <input type="checkbox" value="${escapeHtml(disk.path)}" ${selected ? 'checked' : ''} ${disk.available ? '' : 'disabled'}>
      <span class="disk-symbol"></span>
      <span class="choice-copy"><strong>${escapeHtml(disk.model)}</strong><small>${escapeHtml(disk.path)}${disk.available ? ' · Healthy and unused' : ` · ${escapeHtml(disk.reasons[0] || 'In use')}`}</small></span>
      <span class="choice-size">${escapeHtml(disk.size_h)}<small>${escapeHtml(driveType(disk))}</small></span>`;
    container.appendChild(row);
  });
  const count = state.wizard.devices.length;
  $('#drive-selection-summary').innerHTML = `<span><strong>${count}</strong> selected · ${level ? `minimum ${escapeHtml(level.min)}` : 'choose a level first'}</span><span>Estimated capacity: <strong>${escapeHtml(estimateCapacity())}</strong></span>`;
}

function estimateCapacity() {
  const chosen = state.wizard.devices.map((path) => state.data.disks.find((disk) => disk.path === path)).filter(Boolean);
  if (!chosen.length || !state.wizard.level) return '—';
  const sizes = chosen.map((disk) => disk.size);
  const smallest = Math.min(...sizes);
  const count = chosen.length;
  let bytes = 0;
  if (state.wizard.level === 'raid0') bytes = sizes.reduce((sum, size) => sum + size, 0);
  if (state.wizard.level === 'raid1') bytes = smallest;
  if (state.wizard.level === 'raid5') bytes = smallest * Math.max(0, count - 1);
  if (state.wizard.level === 'raid6') bytes = smallest * Math.max(0, count - 2);
  if (state.wizard.level === 'raid10') bytes = smallest * Math.floor(count / 2);
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(1)} ${units[unit]}`;
}

function updateFormatOptions() {
  const format = $('#option-format').checked;
  const mount = $('#option-mount');
  if (!format) mount.checked = false;
  mount.disabled = !format;
  $('#filesystem-field').classList.toggle('hidden', !format);
  $('#mountpoint-field').classList.toggle('hidden', !mount.checked);
  $('#chunk-field').classList.toggle('hidden', state.wizard.level === 'raid1');
}

function renderReview() {
  const level = state.data.levels[state.wizard.level];
  const selected = state.wizard.devices.map((path) => state.data.disks.find((disk) => disk.path === path)).filter(Boolean);
  const filesystem = $('#option-format').checked ? $('#option-filesystem').value : 'Raw array';
  const mountpoint = $('#option-mount').checked ? ($('#option-mountpoint').value.trim() || 'Automatic (/mnt/mdX)') : 'Not mounted';
  $('#review-summary').innerHTML = `
    <div class="review-hero"><div><small>Configuration</small><strong>${escapeHtml(level.name)} · ${escapeHtml(level.label)}</strong></div><div class="review-capacity"><small>Estimated usable</small><strong>${escapeHtml(estimateCapacity())}</strong></div></div>
    <div class="review-rows">
      <div class="review-row"><span>Physical drives</span><span>${selected.map((disk) => escapeHtml(disk.path)).join(', ')}</span></div>
      <div class="review-row"><span>Fault tolerance</span><span>${level.redundancy ? `Up to ${escapeHtml(level.redundancy)} drive failure${level.redundancy === 1 ? '' : 's'}` : 'None'}</span></div>
      <div class="review-row"><span>Array label</span><span>${escapeHtml(normalizeArrayName($('#option-name').value) || 'Automatic')}</span></div>
      <div class="review-row"><span>Filesystem</span><span>${escapeHtml(filesystem)}</span></div>
      <div class="review-row"><span>Mount point</span><span>${escapeHtml(mountpoint)}</span></div>
    </div>`;
}

function goToStep(step) {
  state.wizard.step = step;
  $$('.wizard-page').forEach((page) => page.classList.toggle('hidden', Number(page.dataset.page) !== step));
  $$('.stepper .step').forEach((item) => {
    const itemStep = Number(item.dataset.step);
    item.classList.toggle('active', itemStep === step);
    item.classList.toggle('done', itemStep < step);
    const bubble = $('span', item);
    bubble.textContent = itemStep < step ? '✓' : String(itemStep);
  });
  $('#wizard-back').style.visibility = step === 1 ? 'hidden' : 'visible';
  $('#wizard-next').textContent = step === 4 ? 'Create array' : 'Continue';
  if (step === 2) renderDrivePicker();
  if (step === 3) updateFormatOptions();
  if (step === 4) renderReview();
  updateWizardButton();
  $('.drawer-body').scrollTop = 0;
}

function updateWizardButton() {
  const next = $('#wizard-next');
  if (state.wizard.step === 1) next.disabled = !state.wizard.level;
  else if (state.wizard.step === 2) {
    const level = state.data.levels[state.wizard.level];
    next.disabled = !level || state.wizard.devices.length < level.min || (state.wizard.level === 'raid10' && state.wizard.devices.length % 2 !== 0);
  } else if (state.wizard.step === 4) next.disabled = $('#erase-confirmation').value.trim() !== 'ERASE';
  else next.disabled = false;
}

async function createArray() {
  const body = {
    level: state.wizard.level,
    devices: state.wizard.devices,
    name: normalizeArrayName($('#option-name').value),
    chunk: $('#option-chunk').value || null,
    format: $('#option-format').checked,
    fstype: $('#option-filesystem').value,
    mount: $('#option-mount').checked,
    mountpoint: $('#option-mountpoint').value.trim(),
    confirm: $('#erase-confirmation').value.trim(),
  };
  closeOverlay('wizard');
  showProgress('Creating your array', 'Please keep this page open while storage is prepared.');
  progressMessage('Validating drives and creating the mdadm array…');
  try {
    const plan = await api('/api/plan', { method: 'POST', body: JSON.stringify(body) });
    body.plan = plan.plan;
    const result = await api('/api/create', { method: 'POST', body: JSON.stringify(body) });
    progressMessage(`Array ${result.md} created`);
    if (result.warning) progressMessage(`Warning: ${result.warning}`);
    if (result.task) await pollTask(result.task);
    finishProgress('Array is ready', 'Your new software RAID array was created successfully.');
    await loadData({ quiet: true });
  } catch (error) {
    failProgress(error.message);
  }
}

async function pollTask(taskId) {
  let lastLogLength = 0;
  while (true) {
    await new Promise((resolve) => window.setTimeout(resolve, 1200));
    const task = await api(`/api/task/${encodeURIComponent(taskId)}`);
    (task.log || []).slice(lastLogLength).forEach(progressMessage);
    lastLogLength = (task.log || []).length;
    if (task.status === 'error') throw new Error(task.error || 'Storage preparation failed');
    if (task.done || task.status === 'done') break;
  }
}

function showProgress(title, subtitle) {
  $('#progress-title').textContent = title;
  $('#progress-subtitle').textContent = subtitle;
  $('#progress-log').innerHTML = '';
  $('#progress-spinner').classList.remove('hidden');
  $('#progress-success').classList.add('hidden');
  $('#progress-footer').classList.add('hidden');
  openOverlay('progress-dialog');
}

function progressMessage(message) {
  const line = document.createElement('div');
  line.textContent = `› ${message}`;
  $('#progress-log').appendChild(line);
  $('#progress-log').scrollTop = $('#progress-log').scrollHeight;
}

function finishProgress(title, subtitle) {
  $('#progress-spinner').classList.add('hidden');
  $('#progress-success').classList.remove('hidden');
  $('#progress-title').textContent = title;
  $('#progress-subtitle').textContent = subtitle;
  $('#progress-footer').classList.remove('hidden');
}

function failProgress(message) {
  $('#progress-spinner').classList.add('hidden');
  $('#progress-success').classList.remove('hidden');
  $('#progress-success').textContent = '!';
  $('#progress-success').style.color = 'var(--red)';
  $('#progress-success').style.background = 'var(--red-soft)';
  $('#progress-title').textContent = 'Could not complete the operation';
  $('#progress-subtitle').textContent = message;
  $('#progress-footer').classList.remove('hidden');
  progressMessage(message);
}

function openMountDialog(name) {
  state.mountTarget = name;
  $('#mount-title').textContent = `Mount ${name}`;
  $('#mount-dialog-path').value = `/mnt/${name}`;
  openOverlay('mount-dialog');
  $('#mount-dialog-path').focus();
}

async function mountArray() {
  const name = state.mountTarget;
  if (!name) return;
  const mountpoint = $('#mount-dialog-path').value.trim();
  $('#mount-confirm').disabled = true;
  try {
    await api(`/api/array/${encodeURIComponent(name)}/mount`, { method: 'POST', body: JSON.stringify({ mountpoint }) });
    closeOverlay('mount-dialog');
    toast('Array mounted', `${name} is available at ${mountpoint}`);
    await loadData({ quiet: true });
  } catch (error) {
    toast('Could not mount array', error.message, 'error');
  } finally {
    $('#mount-confirm').disabled = false;
  }
}

async function unmountArray(name) {
  try {
    await api(`/api/array/${encodeURIComponent(name)}/unmount`, { method: 'POST', body: '{}' });
    toast('Array unmounted', `${name} was safely unmounted`);
    await loadData({ quiet: true });
  } catch (error) {
    toast('Could not unmount array', error.message, 'error');
  }
}

function openDeleteDialog(name) {
  const array = state.data.arrays.find((item) => item.name === name);
  state.deleteTarget = name;
  $('#confirm-title').textContent = `Delete ${name}?`;
  $('#confirm-content').innerHTML = `
    <p class="confirm-copy">This will stop <strong>${escapeHtml(array?.dev || `/dev/${name}`)}</strong>, remove its automatic mount, and erase RAID metadata from every member drive. This cannot be undone.</p>
    <label class="confirm-input-label">Type <strong>${escapeHtml(name)}</strong> to confirm<input id="delete-confirmation" placeholder="${escapeHtml(name)}" autocomplete="off" spellcheck="false"></label>`;
  $('#confirm-action').disabled = true;
  openOverlay('confirm-dialog');
  $('#delete-confirmation').focus();
}

async function deleteArray() {
  const name = state.deleteTarget;
  const confirmation = $('#delete-confirmation').value.trim();
  $('#confirm-action').disabled = true;
  try {
    await api(`/api/array/${encodeURIComponent(name)}/delete`, { method: 'POST', body: JSON.stringify({ confirm: confirmation }) });
    closeOverlay('confirm-dialog');
    toast('Array deleted', `${name} was removed and its drives were released`);
    await loadData({ quiet: true });
  } catch (error) {
    toast('Could not delete array', error.message, 'error');
    $('#confirm-action').disabled = confirmation !== name;
  }
}

$('#refresh-button').addEventListener('click', () => loadData());
['#create-array-button', '#section-create-button', '#empty-create-button'].forEach((selector) => $(selector).addEventListener('click', openWizard));

$$('[data-close]').forEach((button) => button.addEventListener('click', () => closeOverlay(button.dataset.close)));

$('#level-cards').addEventListener('click', (event) => {
  const card = event.target.closest('[data-level]');
  if (!card || card.disabled) return;
  state.wizard.level = card.dataset.level;
  state.wizard.devices = [];
  renderLevels();
  updateWizardButton();
});

$('#drive-picker').addEventListener('change', (event) => {
  if (!event.target.matches('input[type="checkbox"]')) return;
  const path = event.target.value;
  if (event.target.checked) state.wizard.devices.push(path);
  else state.wizard.devices = state.wizard.devices.filter((device) => device !== path);
  renderDrivePicker();
  updateWizardButton();
});

$('#wizard-back').addEventListener('click', () => {
  if (state.wizard.step > 1) goToStep(state.wizard.step - 1);
});

$('#wizard-next').addEventListener('click', () => {
  if (state.wizard.step === 4) createArray();
  else goToStep(state.wizard.step + 1);
});

$('#erase-confirmation').addEventListener('input', updateWizardButton);
$('#option-format').addEventListener('change', updateFormatOptions);
$('#option-mount').addEventListener('change', updateFormatOptions);

$('.segmented').addEventListener('click', (event) => {
  const button = event.target.closest('[data-filter]');
  if (!button) return;
  state.filter = button.dataset.filter;
  $$('.segmented button').forEach((item) => item.classList.toggle('active', item === button));
  renderDrives();
});

$('#drives').addEventListener('click', (event) => {
  const button = event.target.closest('.expand-button');
  if (!button) return;
  const item = button.closest('.drive-item');
  const details = $('.drive-details', item);
  const expanded = !details.classList.contains('hidden');
  details.classList.toggle('hidden', expanded);
  item.classList.toggle('expanded', !expanded);
  button.setAttribute('aria-expanded', String(!expanded));
});

$('#arrays').addEventListener('click', (event) => {
  const button = event.target.closest('[data-array-action]');
  if (!button) return;
  const name = button.dataset.array;
  if (button.dataset.arrayAction === 'mount') openMountDialog(name);
  if (button.dataset.arrayAction === 'unmount') unmountArray(name);
  if (button.dataset.arrayAction === 'delete') openDeleteDialog(name);
});

$('#mount-confirm').addEventListener('click', mountArray);
$('#confirm-action').addEventListener('click', deleteArray);
$('#confirm-content').addEventListener('input', (event) => {
  if (event.target.id === 'delete-confirmation') $('#confirm-action').disabled = event.target.value.trim() !== state.deleteTarget;
});
$('#progress-close').addEventListener('click', () => {
  $('#progress-success').textContent = '✓';
  $('#progress-success').removeAttribute('style');
  closeOverlay('progress-dialog');
});

document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  const active = $$('.overlay:not(.hidden)').pop();
  if (active && active.id !== 'progress-dialog') closeOverlay(active.id);
});

loadData();
window.setInterval(() => {
  if (!$$('.overlay:not(.hidden)').length && document.visibilityState === 'visible') loadData({ quiet: true });
}, 30000);
