/* ============================================================
   MediaFix · app.js
   业务逻辑（依赖 fx.js 提供的 window.FX 工具）
   分三段：本段为第一段（状态 / API / 登录 / 顶栏 / 统计卡片）
   ============================================================ */

(function () {
  'use strict';

  const { toast, showLoading, hideLoading, openModal, closeModal,
          switchView, switchAutoPage, confirmDialog, animateNumber,
          bindRangeHint, $, $$ } = window.FX;

  /* ==================== 全局状态 ==================== */
  const State = {
    token: localStorage.getItem('mf_token') || '',
    user: null,               // {username, nickname, avatar, role, today_used, quota_limit}
    stats: { total_renamed: 0 },
    activeApi: null,          // {id, name, provider, model_name}
    providers: {},            // 供应商字典

    // 网盘 & 目录浏览
    sources: [],              // [{id,name,url,username,password_masked}]
    allSources: [],           // 所有源（WebDAV + 123），任务配置用
    currentSourceId: '',
    currentPath: '/',

    // 待整理视频列表 [{name, path, standard_name, status, checked}]
    videoList: [],

    // 自动任务
    autoConfigs: [],          // [{task_id, name, enabled, ...}]
    autoRunning: {},          // {task_id: true}
    autoState: {},            // {task_id: {running, current_file, total, ...}}
    currentTaskId: '',        // 当前详情页查看的任务
    editingTaskId: '',        // 当前编辑的任务（空=新建）
    autoPollTimer: null,      // 自动任务轮询定时器

    // 文件夹选择器
    pickerTarget: '',         // 回填到哪个 input 的 id
    pickerPath: '/',

    // AI API 编辑
    editingApiId: '',
  };

  /* ==================== 工具函数 ==================== */
  function escapeHtml(s) {
    if (s === null || s === undefined) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function formatTime(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} `
         + `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  }

  function formatDate(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const pad = n => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  }

  function ellipsisAttr(text) {
    return `title="${escapeHtml(text || '')}"`;
  }

  // 首字母（用于头像兜底）
  function initialOf(name) {
    if (!name) return 'M';
    return String(name).trim().charAt(0).toUpperCase();
  }

  // 简单 DOM 选择
  function el(id) { return document.getElementById(id); }

  /* ==================== API 封装 ==================== */

  // 统一 fetch 请求
  async function api(path, options) {
    options = options || {};
    const headers = Object.assign({}, options.headers || {});
    if (State.token) headers['Authorization'] = 'Bearer ' + State.token;
    if (options.body && !(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      if (typeof options.body !== 'string') options.body = JSON.stringify(options.body);
    }

    let res;
    try {
      res = await fetch(path, Object.assign({}, options, { headers }));
    } catch (e) {
      throw new Error('网络请求失败，请检查服务器连接');
    }

    // 401：token 失效 → 跳登录页
    if (res.status === 401) {
      logoutLocal();
      switchView('auth-view');
      throw new Error('登录已过期，请重新登录');
    }

    let data = null;
    const ct = res.headers.get('content-type') || '';
    try {
      if (ct.includes('application/json')) data = await res.json();
      else data = await res.text();
    } catch (e) { data = null; }

    if (!res.ok) {
      // FastAPI 错误格式：{detail: 'xxx'} 或 {detail: [{msg: 'xxx'}, ...]}
      let msg = '请求失败';
      if (data && typeof data === 'object' && data.detail) {
        if (typeof data.detail === 'string') {
          msg = data.detail;
        } else if (Array.isArray(data.detail)) {
          msg = data.detail.map(x => x.msg || JSON.stringify(x)).join('；');
        } else {
          msg = JSON.stringify(data.detail);
        }
      } else if (typeof data === 'string' && data) {
        msg = data;
      } else if (res.status) {
        msg = `HTTP ${res.status}`;
      }
      throw new Error(msg);
    }
    return data;
  }

  // SSE 流式请求（POST + Authorization）
  // onChunk 回调，用于每条消息处理
  // 返回一个 promise，流结束时 resolve
  async function streamSSE(path, body, onChunk) {
    const headers = {
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
    };
    if (State.token) headers['Authorization'] = 'Bearer ' + State.token;

    let res;
    try {
      res = await fetch(path, { method: 'POST', headers, body: JSON.stringify(body) });
    } catch (e) {
      throw new Error('无法建立连接');
    }

    if (res.status === 401) {
      logoutLocal();
      switchView('auth-view');
      throw new Error('登录已过期');
    }
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const t = await res.text();
        const j = JSON.parse(t);
        if (j.detail) msg = j.detail;
      } catch (e) {}
      throw new Error(msg);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder('utf-8');
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 消息以 \n\n 分割
      let idx;
      while ((idx = buffer.indexOf('\n\n')) >= 0) {
        const raw = buffer.slice(0, idx);
        buffer = buffer.slice(idx + 2);

        // 解析 data: 行
        const lines = raw.split('\n');
        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const payload = line.slice(6);
            try {
              const msg = JSON.parse(payload);
              onChunk(msg);
            } catch (e) {
              // 忽略无法解析的行
            }
          }
        }
      }
    }
  }

  /* ==================== 会话管理 ==================== */
  function logoutLocal() {
    State.token = '';
    State.user = null;
    State.activeApi = null;
    State.sources = [];
    State.videoList = [];
    State.currentSourceId = '';
    State.currentPath = '/';
    State.autoConfigs = [];
    State.autoRunning = {};
    State.autoState = {};
    stopAutoPoll();
    localStorage.removeItem('mf_token');
  }

  function saveToken(t) {
    State.token = t;
    localStorage.setItem('mf_token', t);
  }

  async function doLogin(username, password) {
    const data = await api('/api/login', {
      method: 'POST',
      body: { username, password },
    });
    saveToken(data.token);
    return data;
  }

  async function doRegister(username, password, inviteCode) {
    return await api('/api/register', {
      method: 'POST',
      body: { username, password, invite_code: inviteCode },
    });
  }

  /* ==================== 登录 / 注册页交互 ==================== */
  let isRegisterMode = false;

  function bindAuthPage() {
    const form = el('auth-form');
    const switchBtn = el('auth-switch-btn');
    const submitBtn = el('auth-submit');
    const inviteField = document.querySelector('.auth-invite-field');
    const errorBox = el('auth-error');

    function setError(msg) {
      if (!msg) {
        errorBox.style.display = 'none';
        errorBox.textContent = '';
      } else {
        errorBox.style.display = 'block';
        errorBox.textContent = msg;
      }
    }

    switchBtn.addEventListener('click', () => {
      isRegisterMode = !isRegisterMode;
      setError('');
      if (isRegisterMode) {
        submitBtn.textContent = '注册';
        switchBtn.textContent = '已有账号？返回登录';
        inviteField.style.display = '';
      } else {
        submitBtn.textContent = '登录';
        switchBtn.textContent = '没有账号？使用邀请码注册';
        inviteField.style.display = 'none';
      }
    });

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      setError('');
      const username = el('auth-username').value.trim();
      const password = el('auth-password').value;
      const invite = el('auth-invite').value.trim();

      if (!username) return setError('请输入用户名');
      if (!password) return setError('请输入密码');
      if (isRegisterMode && !invite) return setError('请输入邀请码');

      submitBtn.disabled = true;
      submitBtn.textContent = isRegisterMode ? '注册中…' : '登录中…';

      try {
        if (isRegisterMode) {
          await doRegister(username, password, invite);
          toast('注册成功，请登录', 'success');
          isRegisterMode = true;
          // 切回登录模式
          switchBtn.click();
          el('auth-password').value = '';
        } else {
          await doLogin(username, password);
          toast('登录成功', 'success');
          await enterApp();
        }
      } catch (err) {
        setError(err.message || '操作失败');
      } finally {
        submitBtn.disabled = false;
        submitBtn.textContent = isRegisterMode ? '注册' : '登录';
      }
    });
  }

  /* ==================== 进入主应用 ==================== */
  async function enterApp() {
    switchView('main-app');
    // 并行加载状态 + 网盘列表
    await Promise.all([
      refreshStatus(),
      refreshSources(),
      loadHistory(),
    ]);
    // 若是管理员，权限相关的 UI 显示
    applyRoleUI();
  }

  // 根据角色调整 UI（普通用户看不到设置按钮 / 自动任务入口等）
  function applyRoleUI() {
    if (!State.user) return;
    const role = State.user.role;
    const settingsBtn = el('btn-settings');
    if (role === 'admin') {
      settingsBtn.style.display = '';
    } else {
      settingsBtn.style.display = 'none';
    }
  }

  /* ==================== 顶栏 / 用户信息 ==================== */
  function renderTopbar() {
    if (!State.user) return;
    const u = State.user;

    el('topbar-nickname').textContent = u.nickname || u.username;
    el('topbar-username').textContent = '@' + u.username;
    el('topbar-role').textContent = u.role;

    // 头像
    const img = el('topbar-avatar-img');
    const txt = el('topbar-avatar-text');
    if (u.avatar) {
      img.src = u.avatar;
      img.style.display = '';
      txt.style.display = 'none';
    } else {
      img.style.display = 'none';
      txt.style.display = '';
      txt.textContent = initialOf(u.nickname || u.username);
    }

    // 额度徽章
    const limit = u.quota_limit;
    const used = u.today_used;
    const badge = el('quota-badge');
    if (limit >= 999999) {
      badge.textContent = '管理员 · 无限额度';
    } else {
      badge.textContent = `今日额度 ${used} / ${limit}`;
    }
  }

  /* ==================== 状态刷新 ==================== */
  async function refreshStatus() {
    try {
      const data = await api('/api/status');
      State.user = data.user;
      State.stats = data.stats || { total_renamed: 0 };
      State.activeApi = data.active_api || null;
      State.providers = data.providers || {};

      renderTopbar();
      renderStatsCards();
      renderAiCard();
    } catch (e) {
      // 401 已由 api 处理；其他错误静默
      console.warn('refreshStatus failed:', e.message);
    }
  }

  function renderStatsCards() {
    const total = (State.stats && State.stats.total_renamed) || 0;
    animateNumber(el('stat-total'), total, 600);
  }

  function renderAiCard() {
    const api = State.activeApi;
    if (!api) {
      el('ai-name').textContent = 'AI 未配置';
      el('ai-model').textContent = '请到设置中配置';
      el('ai-balance').textContent = '--';
      return;
    }
    el('ai-name').textContent = api.name || api.provider;
    el('ai-model').textContent = api.model_name || '';
    // 余额异步加载
    loadAiBalance(api.id);
  }

  async function loadAiBalance(apiId) {
    const balEl = el('ai-balance');
    balEl.textContent = '加载中…';
    try {
      const data = await api(`/api/api_configs/balance/${apiId}`);
      // 不同供应商返回结构不同，尽力解析
      let val = '--';

      let warn = false;

      if (data && !data.error) {

        let symbol = '¥';

        if (data.currency === 'USD' || data.currency === '$') symbol = '$';



        let amount = null;

        if (typeof data.balance === 'number') amount = data.balance;

        else if (data.balance_infos && data.balance_infos.length) {

          amount = data.balance_infos.reduce((a, b) => a + parseFloat(b.total_balance || 0), 0);

          if (data.balance_infos[0].currency === 'USD') symbol = '$';

        } else if (typeof data.total_available === 'number') {

          amount = data.total_available;

          symbol = '$';

        } else if (data.data && typeof data.data.balance === 'number') {

          amount = data.data.balance;

        }



        if (amount !== null) {

          if (amount > 0) val = symbol + amount.toFixed(2);

          else { val = '余额不足'; warn = true; }

        } else {

          val = '已连接';

        }

      }

      balEl.textContent = val;

      balEl.style.color = warn ? 'var(--c-danger)' : '';
    } catch (e) {
      balEl.textContent = '--';
    }
  }
    /* ==================== 网盘列表 ==================== */
  async function loadAllSources() {
    try {
      const res = await api('/api/sources/list');
      State.allSources = (res && res.sources) || [];
    } catch (e) {
      console.warn('loadAllSources failed:', e.message);
      State.allSources = [];
    }
  }

  async function refreshSources() {
    try {
      // 调新接口，返回所有源（WebDAV + 123）
      const res = await api('/api/sources/list');
      const list = (res && res.sources) || [];
      State.sources = list;
      // 同步更新 allSources（任务配置用）
      State.allSources = list;
      renderSources();
    } catch (e) {
      console.warn('refreshSources failed:', e.message);
      State.sources = [];
      renderSources();
    }
  }

  function renderSources() {
    const box = el('source-list');
    if (!State.sources.length) {
      box.innerHTML = '<div class="empty-hint">还没有网盘，点右上角添加</div>';
      return;
    }
    box.innerHTML = State.sources.map(s => {
      const active = s.id === State.currentSourceId ? ' active' : '';
      const isPan123 = s.type === 'pan123';
      const icon = isPan123 ? '⚡' : '☁️';
      const tag = isPan123 ? '<span class="source-tag">123</span>' : '';
      // 123 源不显示编辑/删除（在"网盘管理"里操作）
      const actions = isPan123 ? '' : `
            <button class="icon-btn icon-btn-press" data-action="edit" title="编辑">✎</button>
            <button class="icon-btn icon-btn-press" data-action="delete" title="删除">🗑</button>`;
      return `
        <div class="source-item${active}" data-source-id="${escapeHtml(s.id)}">
          <span class="browser-item-icon">${icon}</span>
          <span class="source-item-name" ${ellipsisAttr(s.name)}>${escapeHtml(s.name)}${tag}</span>
          <div class="source-item-actions">${actions}</div>
        </div>
      `;
    }).join('');

    // 绑定点击
    box.querySelectorAll('.source-item').forEach(item => {
      item.addEventListener('click', (e) => {
        const act = e.target.closest('[data-action]');
        const sid = item.getAttribute('data-source-id');
        if (act) {
          e.stopPropagation();
          const action = act.getAttribute('data-action');
          if (action === 'edit') openSourceModal(sid);
          else if (action === 'delete') deleteSource(sid);
          return;
        }
        selectSource(sid);
      });
    });
  }

  async function selectSource(sid) {
    State.currentSourceId = sid;
    State.currentPath = '/';
    renderSources();
    el('panel-browser').style.display = '';
    await browsePath('/');
  }

  /* ==================== 网盘编辑弹窗 ==================== */
  let editingSourceId = '';

  function openSourceModal(sid) {
    editingSourceId = sid || '';
    const modal = el('source-modal');
    el('source-modal-title').textContent = sid ? '编辑网盘' : '添加网盘';

    if (sid) {
      const s = State.sources.find(x => x.id === sid);
      if (s) {
        el('source-name').value = s.name || '';
        el('source-url').value = s.url || '';
        el('source-username').value = s.username || '';
        el('source-password').value = ''; // 密码不回填（用 * 掩码占位）
        el('source-password').placeholder = s.password_masked || '留空则不修改';
      }
    } else {
      el('source-name').value = '';
      el('source-url').value = '';
      el('source-username').value = '';
      el('source-password').value = '';
      el('source-password').placeholder = '';
    }
    openModal(modal);
  }

  async function saveSource() {
    const name = el('source-name').value.trim();
    const url = el('source-url').value.trim();
    const username = el('source-username').value.trim();
    const password = el('source-password').value;

    if (!name) return toast('请输入网盘名称', 'error');
    if (!url) return toast('请输入 WebDAV URL', 'error');
    if (!username) return toast('请输入用户名', 'error');
    if (!editingSourceId && !password) return toast('请输入密码', 'error');

    const body = {
      name, url, username,
      password: password || '',
      id: editingSourceId || '',
    };
    try {
      showLoading('保存中…');
      await api('/api/webdav/save', { method: 'POST', body });
      hideLoading();
      closeModal('source-modal');
      toast('保存成功', 'success');
      await refreshSources();
    } catch (e) {
      hideLoading();
      toast(e.message || '保存失败', 'error');
    }
  }

  async function testSource() {
    const url = el('source-url').value.trim();
    const username = el('source-username').value.trim();
    const password = el('source-password').value;
    if (!url || !username || !password) return toast('请填写 URL、用户名和密码', 'warn');

    try {
      showLoading('测试连接中…');
      const res = await api('/api/webdav/test', {
        method: 'POST',
        body: { url, username, password },
      });
      hideLoading();
      if (res.status === 'ok') toast(res.message || '连接成功', 'success');
      else toast(res.message || '连接失败', 'error');
    } catch (e) {
      hideLoading();
      toast(e.message || '测试失败', 'error');
    }
  }

  async function deleteSource(sid) {
    const s = State.sources.find(x => x.id === sid);
    const ok = await confirmDialog('删除网盘', `确定要删除「${s ? s.name : ''}」吗？`, '删除', 'btn-danger');
    if (!ok) return;
    try {
      await api(`/api/webdav/delete/${sid}`, { method: 'DELETE' });
      toast('已删除', 'success');
      if (State.currentSourceId === sid) {
        State.currentSourceId = '';
        el('panel-browser').style.display = 'none';
      }
      await refreshSources();
    } catch (e) {
      toast(e.message || '删除失败', 'error');
    }
  }

  /* ==================== 目录浏览 ==================== */
  async function browsePath(path) {
    if (!State.currentSourceId) return;
    State.currentPath = path || '/';
    el('browser-path').textContent = State.currentPath;
    el('browser-path').setAttribute('title', State.currentPath);

    const list = el('browser-list');
    list.innerHTML = '<div class="empty-hint">加载中…</div>';

    try {
      const data = await api('/api/browse', {
        method: 'POST',
        body: { source_id: State.currentSourceId, path: State.currentPath },
      });
      renderBrowser(data);
    } catch (e) {
      list.innerHTML = `<div class="empty-hint">${escapeHtml(e.message)}</div>`;
    }
  }

  function renderBrowser(data) {
    const list = el('browser-list');
    const dirs = data.dirs || [];
    const files = data.files || [];
    if (!dirs.length && !files.length) {
      list.innerHTML = '<div class="empty-hint">此目录为空</div>';
      return;
    }

    let html = '';
    dirs.forEach(d => {
      html += `
        <div class="browser-item" data-dir="${escapeHtml(d.path)}">
          <span class="browser-item-icon">📁</span>
          <span class="browser-item-name" ${ellipsisAttr(d.name)}>${escapeHtml(d.name)}</span>
          <span style="color:#8F959E;font-size:12px;">›</span>
        </div>
      `;
    });
    files.forEach(f => {
      html += `
        <div class="browser-item browser-item-video">
          <span class="browser-item-icon">🎬</span>
          <span class="browser-item-name" ${ellipsisAttr(f.name)}>${escapeHtml(f.name)}</span>
        </div>
      `;
    });
    list.innerHTML = html;

    list.querySelectorAll('[data-dir]').forEach(item => {
      item.addEventListener('click', () => browsePath(item.getAttribute('data-dir')));
    });
  }

  function browserUp() {
    if (!State.currentPath || State.currentPath === '/') return;
    const parts = State.currentPath.split('/').filter(Boolean);
    parts.pop();
    browsePath('/' + parts.join('/'));
  }

  /* ==================== 扫描视频（SSE） ==================== */
  let scanning = false;

  async function scanVideos() {
    if (!State.currentSourceId) return toast('请先选择网盘', 'warn');
    if (scanning) return toast('正在扫描中…', 'warn');
    scanning = true;

    // 清空之前的列表
    State.videoList = [];
    renderVideoTable();

    const progressBox = el('scan-progress');
    const progressFill = el('scan-progress-fill');
    const progressText = el('scan-progress-text');
    progressBox.style.display = '';
    progressFill.style.width = '0%';
    progressText.textContent = '正在扫描 ' + State.currentPath + ' …';

    let count = 0;
    try {
      await streamSSE('/api/scan_stream',
        { source_id: State.currentSourceId, path: State.currentPath },
        (msg) => {
          if (msg.error) {
            toast(msg.error, 'warn');
            return;
          }
          if (msg.done) return;
          if (msg.name && msg.path) {
            count++;
            State.videoList.push({
              name: msg.name,
              path: msg.path,
              standard_name: '',
              status: 'pending',
              checked: false,
            });
            // 实时追加行
            appendVideoRow(State.videoList[State.videoList.length - 1], State.videoList.length - 1);
            progressText.textContent = `已发现 ${count} 个视频 · ${msg.name}`;
          }
        }
      );
      progressFill.style.width = '100%';
      progressText.textContent = `扫描完成，共发现 ${count} 个视频`;
      toast(`扫描完成，共 ${count} 个视频`, 'success');
    } catch (e) {
      progressText.textContent = '扫描失败：' + e.message;
      toast(e.message || '扫描失败', 'error');
    } finally {
      scanning = false;
      setTimeout(() => { progressBox.style.display = 'none'; }, 2000);
    }
  }

  /* ==================== 视频表格渲染 ==================== */
  function renderVideoTable() {
    const tbody = el('video-tbody');
    if (!State.videoList.length) {
      tbody.innerHTML = '<tr><td colspan="4"><div class="empty-hint">还没有扫描任何文件</div></td></tr>';
      return;
    }
    tbody.innerHTML = '';
    State.videoList.forEach((v, i) => appendVideoRow(v, i));
    updateCheckAllState();
  }

  function appendVideoRow(v, index) {
    const tbody = el('video-tbody');
    // 去掉空状态提示行
    const emptyRow = tbody.querySelector('.empty-hint');
    if (emptyRow) tbody.innerHTML = '';

    const tr = document.createElement('tr');
    tr.setAttribute('data-index', index);
    tr.innerHTML = `
      <td><input type="checkbox" data-check-index="${index}" ${v.checked ? 'checked' : ''}></td>
      <td class="cell-name" ${ellipsisAttr(v.name)}>${escapeHtml(v.name)}</td>
      <td class="cell-standard" ${ellipsisAttr(v.standard_name || '')}>${escapeHtml(v.standard_name || '—')}</td>
      <td><span class="status-dot ${v.status}">${statusLabel(v.status)}</span></td>
    `;
    tbody.appendChild(tr);

    tr.querySelector('input[type=checkbox]').addEventListener('change', (e) => {
      State.videoList[index].checked = e.target.checked;
      updateCheckAllState();
    });
  }

  function updateVideoRow(index) {
    const tr = el('video-tbody').querySelector(`tr[data-index="${index}"]`);
    if (!tr) return;
    const v = State.videoList[index];

    const stdCell = tr.querySelector('.cell-standard');
    if (stdCell) {
      stdCell.textContent = v.standard_name || '—';
      stdCell.setAttribute('title', v.standard_name || '');
    }
    const dotCell = tr.querySelector('.status-dot');
    if (dotCell) {
      dotCell.className = 'status-dot ' + v.status;
      dotCell.textContent = statusLabel(v.status);
    }
    const cb = tr.querySelector('input[type=checkbox]');
    if (cb) cb.checked = !!v.checked;
  }

  function statusLabel(s) {
    return {
      pending: '待处理',
      processing: '解析中',
      success: '成功',
      fail: '失败',
      skip: '跳过',
    }[s] || s;
  }

  function updateCheckAllState() {
    const all = State.videoList.length;
    const checked = State.videoList.filter(v => v.checked).length;
    const cb = el('check-all');
    if (cb) {
      cb.checked = all > 0 && checked === all;
      cb.indeterminate = checked > 0 && checked < all;
    }
  }

  function checkAllVideos(val) {
    State.videoList.forEach((v, i) => {
      v.checked = val;
      updateVideoRow(i);
    });
    updateCheckAllState();
  }

  function invertVideos() {
    State.videoList.forEach((v, i) => {
      v.checked = !v.checked;
      updateVideoRow(i);
    });
    updateCheckAllState();
  }

  function clearVideoList() {
    State.videoList = [];
    renderVideoTable();
  }

  /* ==================== AI 解析 + 重命名 ==================== */
  let renaming = false;

  async function runRename() {
    const selected = State.videoList.filter(v => v.checked);
    if (!selected.length) return toast('请先勾选要处理的文件', 'warn');
    if (renaming) return toast('正在处理中…', 'warn');

    const ok = await confirmDialog(
      '确认执行重命名',
      `将对勾选的 ${selected.length} 个文件调用 AI 解析，然后创建后台任务执行重命名。确认继续？`,
      '开始执行'
    );
    if (!ok) return;

    renaming = true;
    const progressBox = el('scan-progress');
    const progressFill = el('scan-progress-fill');
    const progressText = el('scan-progress-text');
    progressBox.style.display = '';
    progressFill.style.width = '0%';

    try {
      const total = selected.length;
      selected.forEach(v => {
        v.status = 'processing';
        const idx = State.videoList.indexOf(v);
        if (idx >= 0) updateVideoRow(idx);
      });
      progressText.textContent = `批量 AI 解析中（共 ${total} 个）…`;

      let parsed = 0;
      await streamSSE('/api/parse_batch_stream',
        {
          source_id: State.currentSourceId,
          files: selected.map(v => ({ name: v.name, path: v.path })),
          batch_size: 15,
        },
        (msg) => {
          if (msg.error) { toast(msg.error, 'error'); return; }
          if (msg.done) return;
          const v = selected[msg.index];
          if (!v) return;
          if (msg.success) {
            v.standard_name = msg.standard_name || '';
            v.status = 'pending';
          } else {
            v.status = 'fail';
            v.standard_name = '解析失败：' + (msg.reason || '未知');
          }
          const idx = State.videoList.indexOf(v);
          if (idx >= 0) updateVideoRow(idx);
          parsed++;
          progressText.textContent = `AI 解析中 ${parsed}/${total} · ${v.name}`;
          progressFill.style.width = (parsed / total * 50) + '%';
        }
      );

      const renameFiles = selected
        .filter(v => v.status !== 'fail' && v.standard_name)
        .map(v => ({
          name: v.name,
          path: v.path,
          standard_name: v.standard_name.split('/').pop(),
        }));

      if (!renameFiles.length) {
        toast('没有可重命名的文件（全部解析失败）', 'warn');
        return;
      }

      progressText.textContent = `创建后台任务（${renameFiles.length} 个文件）…`;
      progressFill.style.width = '60%';

      const createRes = await api('/api/rename_tasks/create', {
        method: 'POST',
        body: {
          source_id: State.currentSourceId,
          files: renameFiles,
        },
      });

      progressText.textContent = '启动任务…';
      await api(`/api/rename_tasks/${createRes.task_id}/run`, { method: 'POST' });

      progressFill.style.width = '100%';
      progressText.textContent = '任务已启动，正在跳转到任务详情…';
      toast('任务已启动，可在“重命名任务”中查看进度', 'success');

      await refreshStatus();
      await loadHistory();

      setTimeout(() => {
        progressBox.style.display = 'none';
        openRenameDetail(createRes.task_id);
      }, 600);
    } catch (e) {
      toast(e.message || '执行失败', 'error');
    } finally {
      renaming = false;
      setTimeout(() => { progressBox.style.display = 'none'; }, 3000);
    }
  }

  /* ==================== 操作历史 ==================== */
  async function loadHistory() {
    try {
      const list = await api('/api/history');
      renderHistory(Array.isArray(list) ? list : []);
    } catch (e) {
      console.warn('loadHistory failed:', e.message);
    }
  }

  function renderHistory(list) {
    const box = el('history-list');
    if (!list.length) {
      box.innerHTML = '<div class="empty-hint">暂无操作记录</div>';
      return;
    }
    // 最新的在前，取前 3 条
    const recent = list.slice(-3).reverse();
    box.innerHTML = recent.map(batch => {
      const ops = (batch.operations || []).slice(0, 3);
      const more = (batch.operations || []).length - ops.length;
      return `
        <div class="history-item">
          <div class="history-time">${escapeHtml(formatTime(batch.time))}</div>
          <div class="history-ops">
            ${ops.map(o => {
              const oldName = o.old.split('/').pop();
              const newName = o.new.split('/').pop();
              return `<div class="history-op" ${ellipsisAttr(oldName + ' → ' + newName)}>
                ${escapeHtml(oldName)}<span class="history-op-arrow">→</span>${escapeHtml(newName)}
              </div>`;
            }).join('')}
            ${more > 0 ? `<div class="history-op" style="color:#8F959E;">…还有 ${more} 条</div>` : ''}
          </div>
        </div>
      `;
    }).join('');
  }

  function openHistoryFull() {
    api('/api/history').then(list => {
      list = Array.isArray(list) ? list : [];
      const box = el('history-full-list');
      if (!list.length) {
        box.innerHTML = '<div class="empty-hint">暂无操作记录</div>';
      } else {
        const reversed = list.slice().reverse();
        box.innerHTML = reversed.map(batch => {
          const ops = batch.operations || [];
          return `
            <div class="history-item">
              <div class="history-time">${escapeHtml(formatTime(batch.time))} · 共 ${ops.length} 条</div>
              <div class="history-ops">
                ${ops.map(o => {
                  const oldName = o.old.split('/').pop();
                  const newName = o.new.split('/').pop();
                  return `<div class="history-op" ${ellipsisAttr(oldName + ' → ' + newName)}>
                    ${escapeHtml(oldName)}<span class="history-op-arrow">→</span>${escapeHtml(newName)}
                  </div>`;
                }).join('')}
              </div>
            </div>
          `;
        }).join('');
      }
      openModal('history-full-modal');
    }).catch(e => toast(e.message, 'error'));
  }
    /* ==================== 文件夹选择器 ==================== */
    async function openFolderPicker(targetInputId) {
    State.pickerTarget = targetInputId;
    State.pickerPath = '/';
    var fs = document.getElementById('form-source');
    var fv = document.getElementById('auto-form-view');
    if (fs && fs.value && fv && fv.classList.contains('auto-page-active')) {
      State.currentSourceId = fs.value;
    }
    el('picker-path').textContent = '/';
    await loadPickerDir('/');
    openModal('folder-picker-modal');
  }

  async function loadPickerDir(path) {
    State.pickerPath = path || '/';
    el('picker-path').textContent = State.pickerPath;
    el('picker-path').setAttribute('title', State.pickerPath);
    const list = el('picker-list');
    list.innerHTML = '<div class="empty-hint">加载中…</div>';
    if (!State.currentSourceId) {
      list.innerHTML = '<div class="empty-hint">请先在主界面选择网盘</div>';
      return;
    }
    try {
      const data = await api('/api/browse', {
        method: 'POST',
        body: { source_id: State.currentSourceId, path: State.pickerPath },
      });
      const dirs = data.dirs || [];
      if (!dirs.length) {
        list.innerHTML = '<div class="empty-hint">此目录无子文件夹</div>';
        return;
      }
      list.innerHTML = dirs.map(d => `
        <div class="browser-item" data-dir="${escapeHtml(d.path)}">
          <span class="browser-item-icon">📁</span>
          <span class="browser-item-name" ${ellipsisAttr(d.name)}>${escapeHtml(d.name)}</span>
          <span style="color:#8F959E;font-size:12px;">›</span>
        </div>
      `).join('');
      list.querySelectorAll('[data-dir]').forEach(item => {
        item.addEventListener('click', () => loadPickerDir(item.getAttribute('data-dir')));
      });
    } catch (e) {
      list.innerHTML = `<div class="empty-hint">${escapeHtml(e.message)}</div>`;
    }
  }

  function confirmFolderPicker() {
    if (State.pickerTarget) {
      const input = el(State.pickerTarget);
      if (input) input.value = State.pickerPath;
    }
    closeModal('folder-picker-modal');
  }

  /* ==================== 自动任务 - 列表 ==================== */
  async function openAutoModule() {
    // 角色检查
    if (!State.user || (State.user.role !== 'admin' && State.user.role !== 'member')) {
      openModal('permission-modal');
      return;
    }
    switchView('auto-module');
    switchAutoPage('auto-list-view');
    await loadAutoConfigs();
    startAutoPoll();
  }

  async function loadAutoConfigs() {
    try {
      const data = await api('/api/auto/configs');
      State.autoConfigs = data.configs || [];
      State.autoRunning = data.running || {};
      renderAutoList();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function renderAutoList() {
    const box = el('auto-list');
    if (!State.autoConfigs.length) {
      box.innerHTML = `
        <div class="empty-state">
          <div class="empty-state-icon">⏰</div>
          <div class="empty-state-text">暂无定时任务</div>
          <div class="empty-state-hint">点击右上角"新建"创建第一个自动任务</div>
        </div>`;
      return;
    }

    box.innerHTML = State.autoConfigs.map(t => {
      const running = State.autoRunning[t.task_id];
      const tag = running
        ? '<span class="auto-card-tag running">运行中</span>'
        : (t.enabled ? '<span class="auto-card-tag enabled">已启用</span>'
                     : '<span class="auto-card-tag disabled">已禁用</span>');
      const dot = running ? '<span class="run-dot running"></span>' : '<span class="run-dot"></span>';
      const rule = t.mode === 'daily'
        ? `每日 ${t.daily_time}`
        : `每 ${t.interval_hours} 小时`;
      const src = State.sources.find(s => s.id === t.source_id);
      const sourceName = src ? src.name : '(网盘已删除)';

      return `
        <div class="auto-card card-press" data-task-id="${escapeHtml(t.task_id)}">
          <div class="auto-card-head">
            ${dot}
            <div class="auto-card-name" ${ellipsisAttr(t.name)}>${escapeHtml(t.name)}</div>
            ${tag}
          </div>
          <div class="auto-card-meta">
            <div class="auto-card-meta-item"><span class="auto-card-meta-key">网盘</span><span class="auto-card-meta-val">${escapeHtml(sourceName)}</span></div>
            <div class="auto-card-meta-item"><span class="auto-card-meta-key">待处理</span><span class="auto-card-meta-val" ${ellipsisAttr(t.source_path)}>${escapeHtml(t.source_path)}</span></div>
            <div class="auto-card-meta-item"><span class="auto-card-meta-key">成品库</span><span class="auto-card-meta-val" ${ellipsisAttr(t.target_path)}>${escapeHtml(t.target_path)}</span></div>
            <div class="auto-card-meta-item"><span class="auto-card-meta-key">定时</span><span class="auto-card-meta-val">${escapeHtml(rule)}</span></div>
          </div>
          <div class="auto-card-foot">
            <button class="btn btn-ghost btn-press" data-action="edit">编辑</button>
            <button class="btn btn-primary btn-press" data-action="run" ${running ? 'disabled' : ''}>执行</button>
          </div>
        </div>
      `;
    }).join('');

    box.querySelectorAll('.auto-card').forEach(card => {
      card.addEventListener('click', (e) => {
        const act = e.target.closest('[data-action]');
        const tid = card.getAttribute('data-task-id');
        if (act) {
          e.stopPropagation();
          const a = act.getAttribute('data-action');
          if (a === 'edit') openAutoForm(tid);
          else if (a === 'run') runAutoNow(tid);
          return;
        }
        openAutoDetail(tid);
      });
    });
  }

  /* ==================== 自动任务 - 详情 ==================== */
  async function openAutoDetail(taskId) {
    State.currentTaskId = taskId;
    const task = State.autoConfigs.find(t => t.task_id === taskId);
    if (!task) return toast('任务不存在', 'error');

    switchAutoPage('auto-detail-view');
    el('detail-sub').textContent = task.name;
    renderAutoDetail(task);
    await loadTaskLogs(taskId);
  }

  function renderAutoDetail(task) {
    const running = State.autoRunning[task.task_id];
    const state = State.autoState[task.task_id] || {};
    const src = State.sources.find(s => s.id === task.source_id);
    const sourceName = src ? src.name : '(已删除)';
    const rule = task.mode === 'daily' ? `每日 ${task.daily_time}` : `每 ${task.interval_hours} 小时`;
    const esc = (s) => escapeHtml(String(s || ''));

    let progressHtml = '';
    if (running) {
      const phase = state.phase || 'scan';
      const _srcType = state.source_type || 'webdav';

      if (phase === 'move' && _srcType === 'pan123') {
        const _batchMsg = state.current_file || '批量处理中...';
        progressHtml = '<div class="auto-detail-progress">' +
          '<div class="auto-detail-progress-head"><span class="auto-detail-progress-phase">⚡ 123 批量归档</span></div>' +
          '<div class="auto-detail-progress-stats"><span>' + esc(_batchMsg) + '</span></div>' +
          '<div class="progress-bar"><div class="progress-fill" style="width:50%;"></div></div>' +
          '</div>';
      } else if (phase === 'scan') {
        const dirs = state.scan_dirs_visited || 0;
        const found = state.scan_files_found || 0;
        progressHtml = '<div class="auto-detail-progress">' +
          '<div class="auto-detail-progress-head"><span class="auto-detail-progress-phase">📂 正在扫描目录</span></div>' +
          '<div class="auto-detail-progress-stats"><span>已访问 <b>' + dirs + '</b> 个目录</span><span>发现 <b>' + found + '</b> 个视频</span></div>' +
          '<div class="auto-detail-progress-file" title="' + esc(state.current_file) + '">' + esc(state.current_file) + '</div>' +
          '</div>';
      } else if (phase === 'parse') {
        const bCur = state.parse_current_batch || 0;
        const bTot = state.parse_total_batches || 0;
        const done = state.parse_done || 0;
        const totalFound = state.scan_files_found || 0;
        const cached = state.parse_cached || 0;
        const percent = totalFound > 0 ? Math.round(done / totalFound * 100) : 0;
        progressHtml = '<div class="auto-detail-progress">' +
          '<div class="auto-detail-progress-head"><span class="auto-detail-progress-phase">🤖 正在 AI 批量解析</span><span class="auto-detail-progress-count">' + done + ' / ' + totalFound + '</span></div>' +
          '<div class="progress-bar"><div class="progress-fill" style="width:' + percent + '%"></div></div>' +
          '<div class="auto-detail-progress-stats"><span>批次 <b>' + bCur + '/' + bTot + '</b></span><span>缓存命中 <b>' + cached + '</b> 个</span></div>' +
          '<div class="auto-detail-progress-file" title="' + esc(state.current_file) + '">' + esc(state.current_file) + '</div>' +
          '</div>';
      } else if (phase === 'move') {
        const cur = state.move_current || 0;
        const tot = state.move_total || 0;
        const okN = state.move_success || 0;
        const failN = state.move_failed || 0;
        const percent = tot > 0 ? Math.round(cur / tot * 100) : 0;
        const oldName = state.current_file_old || '';
        const newName = state.current_file_new || '';
        let fileHtml = '';
        if (oldName) fileHtml += '<div class="auto-detail-progress-file"><span class="file-tag old">原</span><span title="' + esc(oldName) + '">' + esc(oldName) + '</span></div>';
        if (newName) fileHtml += '<div class="auto-detail-progress-file"><span class="file-tag new">新</span><span title="' + esc(newName) + '">' + esc(newName) + '</span></div>';
        progressHtml = '<div class="auto-detail-progress">' +
          '<div class="auto-detail-progress-head"><span class="auto-detail-progress-phase">📦 正在归档文件</span><span class="auto-detail-progress-count">' + cur + ' / ' + tot + '</span></div>' +
          '<div class="progress-bar"><div class="progress-fill" style="width:' + percent + '%"></div></div>' +
          '<div class="auto-detail-progress-stats"><span style="color:#52C41A;">✓ 成功 <b>' + okN + '</b></span><span style="color:#F5222D;">✗ 失败 <b>' + failN + '</b></span></div>' +
          fileHtml +
          '</div>';
      } else if (phase === 'cleanup') {
        const cleaned = state.cleaned_count || 0;
        progressHtml = '<div class="auto-detail-progress">' +
          '<div class="auto-detail-progress-head"><span class="auto-detail-progress-phase">🗑️ 正在清理空目录</span></div>' +
          '<div class="auto-detail-progress-stats"><span>已清理 <b>' + cleaned + '</b> 个空文件夹</span></div>' +
          '</div>';
      } else {
        progressHtml = '<div class="auto-detail-progress"><div class="auto-detail-progress-head"><span class="auto-detail-progress-phase">处理中…</span></div></div>';
      }
    }

    el('detail-info').innerHTML = progressHtml +
      '<div class="detail-info-item"><div class="detail-info-key">状态</div><div class="detail-info-val">' + (running ? '🟢 运行中' : (task.enabled ? '启用' : '禁用')) + '</div></div>' +
      '<div class="detail-info-item"><div class="detail-info-key">网盘</div><div class="detail-info-val">' + escapeHtml(sourceName) + '</div></div>' +
      '<div class="detail-info-item"><div class="detail-info-key">待处理目录</div><div class="detail-info-val mono" ' + ellipsisAttr(task.source_path) + '>' + escapeHtml(task.source_path) + '</div></div>' +
      '<div class="detail-info-item"><div class="detail-info-key">成品库目录</div><div class="detail-info-val mono" ' + ellipsisAttr(task.target_path) + '>' + escapeHtml(task.target_path) + '</div></div>' +
      '<div class="detail-info-item"><div class="detail-info-key">定时规则</div><div class="detail-info-val">' + escapeHtml(rule) + '</div></div>' +
      '<div class="detail-info-item"><div class="detail-info-key">单次上限</div><div class="detail-info-val">' + task.max_files + ' 个</div></div>';

    const runBtn = el('btn-detail-run');
    const editBtn = el('btn-detail-edit');
    const toggleBtn = el('btn-detail-toggle');
    const delBtn = el('btn-detail-delete');
    const cancelBtn = el('btn-detail-cancel');

    if (running) {
      if (runBtn) { runBtn.disabled = true; runBtn.textContent = '执行中…'; }
      if (editBtn) editBtn.disabled = true;
      if (toggleBtn) toggleBtn.disabled = true;
      if (delBtn) delBtn.disabled = true;
      if (cancelBtn) { cancelBtn.style.display = ''; cancelBtn.textContent = '中断当前任务'; }
    } else {
      if (runBtn) { runBtn.disabled = false; runBtn.textContent = '立即执行'; }
      if (editBtn) editBtn.disabled = false;
      if (toggleBtn) toggleBtn.disabled = false;
      if (delBtn) delBtn.disabled = false;
      if (cancelBtn) cancelBtn.style.display = 'none';
    }
  }

  async function loadTaskLogs(taskId) {
    const box = el('detail-logs');
    box.innerHTML = '<div class="empty-hint">加载中…</div>';
    try {
      const data = await api(`/api/auto/logs?task_id=${encodeURIComponent(taskId)}`);
      const logs = (data.logs || []).slice().reverse().slice(0, 5);
      if (!logs.length) {
        box.innerHTML = '<div class="empty-hint">暂无执行记录</div>';
        return;
      }
      box.innerHTML = logs.map((log, i) => renderLogCard(log, i)).join('');
      box.querySelectorAll('.log-card').forEach((card, i) => {
        card.addEventListener('click', () => openLogDetail(logs[i]));
      });
    } catch (e) {
      box.innerHTML = `<div class="empty-hint">${escapeHtml(e.message)}</div>`;
    }
  }

  function renderLogCard(log, idx) {
    const timeStr = log.time_str || formatTime(log.time);
    const _logSrcType = log.source_type || 'webdav';
    const _srcBadge = _logSrcType === 'pan123'
      ? '<span class="src-badge src-pan123">⚡ 123</span>'
      : '<span class="src-badge src-webdav">☁️ WebDAV</span>';
    return `
      <div class="log-card" data-log-index="${idx}">
        <div class="log-card-head">
          ${_srcBadge}
          <span>${escapeHtml(timeStr)}</span>
          <span class="spacer"></span>
          ${log.cancelled ? '<span style="color:#FAAD14;">已中断</span>' : ''}
          ${log.error ? '<span style="color:#F5222D;">⚠ 异常</span>' : (log.info ? '<span style="color:#52C41A;">✅ 已完成</span>' : '')}
        </div>
        <div class="log-card-stats">
          <span class="log-card-stat">扫描 <span class="log-card-stat-num">${log.total || 0}</span></span>
          <span class="log-card-stat">成功 <span class="log-card-stat-num ok">${log.success || 0}</span></span>
          <span class="log-card-stat">失败 <span class="log-card-stat-num bad">${log.failed || 0}</span></span>
          ${log.cleaned_empty_dirs ? `<span class="log-card-stat">清理 <span class="log-card-stat-num">${log.cleaned_empty_dirs}</span></span>` : ''}
        </div>
      </div>
    `;
  }

  /* ==================== 执行详情弹窗 ==================== */
    function openLogDetail(log) {
    const box = el('log-detail-content');
    const success = log.success_files || [];
    const failed = log.failed_files || [];
    const cleaned = log.cleaned_dirs || [];
    const skipped = log.skipped_files || [];
    const nonMedia = log.non_media_files || [];

    const metaItems = [
      ['执行时间', log.time_str || formatTime(log.time)],
      ['触发方式', log.cancelled ? '手动中断' : '自动/手动'],
      ['扫描耗时', (log.scan_elapsed_sec || 0) + ' 秒'],
      ['访问目录数', log.scan_dirs_visited || 0],
      ['跳过目录数', log.scan_dirs_skipped || 0],
      ['清理空目录', (log.cleaned_empty_dirs || 0) + ' 个'],
    ];

    let html = `
      <div class="log-detail-summary">
        <div class="log-detail-stat"><div class="log-detail-stat-num">${log.total || 0}</div><div class="log-detail-stat-label">扫描</div></div>
        <div class="log-detail-stat"><div class="log-detail-stat-num ok">${log.success || 0}</div><div class="log-detail-stat-label">成功</div></div>
        <div class="log-detail-stat"><div class="log-detail-stat-num skip">${log.skipped || 0}</div><div class="log-detail-stat-label">跳过</div></div>
        <div class="log-detail-stat"><div class="log-detail-stat-num nonmedia">${nonMedia.length}</div><div class="log-detail-stat-label">非影视</div></div>
        <div class="log-detail-stat"><div class="log-detail-stat-num bad">${log.failed || 0}</div><div class="log-detail-stat-label">失败</div></div>
      </div>
    `;
    if (log.error) {
      html += `<div class="log-detail-section failed" style="margin-bottom:12px;">
        <div class="log-detail-section-head">⚠ 错误信息</div>
        <div class="log-detail-section-body">${escapeHtml(log.error)}</div>
      </div>`;
    } else if (log.info) {
      html += `<div class="log-detail-section" style="margin-bottom:12px;background:var(--c-success-soft);color:var(--c-success);">
        <div class="log-detail-section-head">✅ 任务结果</div>
        <div class="log-detail-section-body">${escapeHtml(log.info)}</div>
      </div>`;
    }

    if (success.length) {
      html += `
        <div class="log-detail-section success collapsed">
          <div class="log-detail-section-head">✅ 成功文件清单（${success.length}）<span class="arrow">▼</span></div>
          <div class="log-detail-section-body">
            ${success.map(f => `
              <div class="log-detail-row">
                ${escapeHtml(f.old)}<span class="log-detail-row-arrow">→</span>${escapeHtml(f.new)}
                <span class="log-detail-row-path" ${ellipsisAttr(f.path)}>${escapeHtml(f.path)}</span>
              </div>
            `).join('')}
          </div>
        </div>
      `;
    }

    if (skipped.length) {
      html += `
        <div class="log-detail-section skipped collapsed">
          <div class="log-detail-section-head">⏭️ 跳过的文件（${skipped.length}）<span class="arrow">▼</span></div>
          <div class="log-detail-section-body">
            ${skipped.map(f => `
              <div class="log-detail-row">
                <b>${escapeHtml(f.name)}</b>
                <span class="log-detail-row-path">${escapeHtml(f.reason || '目标已存在')}</span>
              </div>
            `).join('')}
          </div>
        </div>
      `;
    }

      if (nonMedia.length) {
        html += `
          <div class="log-detail-section nonmedia collapsed">
            <div class="log-detail-section-head">🚫 非影视资源（${nonMedia.length}）<span class="arrow">▼</span></div>
            <div class="log-detail-section-body">
              ${nonMedia.map(f => `
                <div class="log-detail-row">
                  <b>${escapeHtml(f.name)}</b>
                  <span class="log-detail-row-path">${escapeHtml(f.reason || '非影视资源')}</span>
                </div>
              `).join('')}
            </div>
          </div>
        `;
      }


    if (cleaned.length) {
      html += `
        <div class="log-detail-section cleaned collapsed">
          <div class="log-detail-section-head">🗑️ 清理的空文件夹（${cleaned.length}）<span class="arrow">▼</span></div>
          <div class="log-detail-section-body">
            ${cleaned.map(p => `<div class="log-detail-row" ${ellipsisAttr(p)}>${escapeHtml(p)}</div>`).join('')}
          </div>
        </div>
      `;
    }

    if (failed.length) {
      html += `
        <div class="log-detail-section failed collapsed">
          <div class="log-detail-section-head">❌ 失败文件清单（${failed.length}）<span class="arrow">▼</span></div>
          <div class="log-detail-section-body">
            ${failed.map(f => `<div class="log-detail-row"><b>${escapeHtml(f.name)}</b><span class="log-detail-row-path">${escapeHtml(f.reason || '')}</span></div>`).join('')}
          </div>
        </div>
      `;
    }

    box.innerHTML = html;
    openModal('auto-log-detail-modal');
  }

  /* ==================== 自动任务 - 表单 ==================== */
  function openAutoForm(taskId) {
    State.editingTaskId = taskId || '';
    const isNew = !taskId;
    el('form-title').textContent = isNew ? '新建任务' : '编辑任务';

    // 填充网盘下拉（多源：WebDAV + 123）
    const sourceSel = el('form-source');
    if (!State.allSources.length) {
      sourceSel.innerHTML = '<option value="">请先在"网盘管理"里添加源</option>';
    } else {
      sourceSel.innerHTML = State.allSources.map(s => {
        const typeLabel = s.type === 'pan123' ? '123直连' : 'WebDAV';
        const display = '[' + typeLabel + '] ' + s.name;
        return `<option value="${escapeHtml(s.id)}">${escapeHtml(display)}</option>`;
      }).join('');
    }

    if (isNew) {
      el('form-name').value = '';
      el('form-source-path').value = '/待处理';
      el('form-target-path').value = '/成品影视库';
      document.querySelector('input[name=form-mode][value=interval]').checked = true;
      el('form-interval').value = 1;
      el('form-daily-time').value = '03:00';
      el('form-max').value = 300;
      if (State.allSources.length) sourceSel.value = State.allSources[0].id;
    } else {
      const t = State.autoConfigs.find(x => x.task_id === taskId);
      if (!t) return toast('任务不存在', 'error');
      el('form-name').value = t.name || '';
      sourceSel.value = t.source_id || '';
      el('form-source-path').value = t.source_path || '/待处理';
      el('form-target-path').value = t.target_path || '/成品影视库';
      document.querySelector(`input[name=form-mode][value="${t.mode || 'interval'}"]`).checked = true;
      el('form-interval').value = t.interval_hours || 1;
      el('form-daily-time').value = t.daily_time || '03:00';
      el('form-max').value = t.max_files || 300;
    }

    updateFormModeUI();
    bindRangeHint('form-max', 'form-max-hint', ' 个');
    switchAutoPage('auto-form-view');
  }

  function updateFormModeUI() {
    const mode = document.querySelector('input[name=form-mode]:checked').value;
    el('field-interval').style.display = mode === 'interval' ? '' : 'none';
    el('field-daily').style.display = mode === 'daily' ? '' : 'none';
  }

  async function saveAutoForm() {
    const name = el('form-name').value.trim();
    const sourceId = el('form-source').value;
    const sourcePath = el('form-source-path').value.trim();
    const targetPath = el('form-target-path').value.trim();
    const mode = document.querySelector('input[name=form-mode]:checked').value;
    const interval = parseFloat(el('form-interval').value) || 1;
    const dailyTime = el('form-daily-time').value || '03:00';
    const maxFiles = parseInt(el('form-max').value) || 300;

    if (!name) return toast('请输入任务名称', 'error');
    if (!sourceId) return toast('请选择网盘', 'error');
    if (!sourcePath) return toast('请输入待处理目录', 'error');
    if (!targetPath) return toast('请输入成品库目录', 'error');

    const body = {
      task_id: State.editingTaskId || '',
      name,
      enabled: true,
      mode,
      interval_hours: interval,
      daily_time: dailyTime,
      max_files: maxFiles,
      source_id: sourceId,
      source_path: sourcePath,
      target_id: sourceId,
      target_path: targetPath,
    };

    try {
      showLoading('保存中…');
      await api('/api/auto/config', { method: 'POST', body });
      hideLoading();
      toast('保存成功', 'success');
      switchAutoPage('auto-list-view');
      await loadAutoConfigs();
    } catch (e) {
      hideLoading();
      toast(e.message || '保存失败', 'error');
    }
  }

  async function runAutoNow(taskId) {
    const ok = await confirmDialog('立即执行', '确定立即执行该任务吗？执行过程会调用 AI 和网盘，可能持续较长时间。', '开始执行');
    if (!ok) return;
    try {
      await api('/api/auto/run_now', { method: 'POST', body: { task_id: taskId } });
      toast('任务已在后台启动', 'success');
      await loadAutoConfigs();
    } catch (e) {
      toast(e.message || '启动失败', 'error');
    }
  }

  async function cancelAutoTask(taskId) {
    try {
      await api('/api/auto/cancel', { method: 'POST', body: { task_id: taskId } });
      toast('中断信号已发送', 'success');
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  async function deleteAutoTask(taskId) {
    const ok = await confirmDialog('删除任务', '确定删除该自动任务吗？此操作不可恢复。', '删除', 'btn-danger');
    if (!ok) return;
    try {
      await api('/api/auto/delete', { method: 'POST', body: { task_id: taskId } });
      toast('已删除', 'success');
      switchAutoPage('auto-list-view');
      await loadAutoConfigs();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  async function toggleAutoTask(taskId) {
    const t = State.autoConfigs.find(x => x.task_id === taskId);
    if (!t) return;
    const body = Object.assign({}, t, { enabled: !t.enabled });
    try {
      await api('/api/auto/config', { method: 'POST', body });
      toast(t.enabled ? '已禁用' : '已启用', 'success');
      await loadAutoConfigs();
      // 刷新详情页
      if (State.currentTaskId === taskId) {
        const newT = State.autoConfigs.find(x => x.task_id === taskId);
        if (newT) renderAutoDetail(newT);
      }
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  /* ==================== 自动任务状态轮询 ==================== */
  function startAutoPoll() {
    if (State.autoPollTimer) return;
    State.autoPollTimer = setInterval(async () => {
      // 只在自动任务模块可见时轮询
      if (!el('auto-module').classList.contains('view-active')) return;
      try {
        const data = await api('/api/auto/status');
        const prevRunning = State.autoRunning;
        State.autoRunning = data.running || {};
        State.autoState = data.state || {};

        // 若运行状态变化或详情页处于打开状态，重绘
        const listVisible = el('auto-list-view').classList.contains('auto-page-active');
        if (listVisible) {
          // 只更新卡片上的运行标签，避免整体重绘造成闪烁
          State.autoConfigs.forEach(t => {
            const card = document.querySelector(`.auto-card[data-task-id="${t.task_id}"]`);
            if (!card) return;
            const running = !!State.autoRunning[t.task_id];
            const wasRunning = !!prevRunning[t.task_id];
            if (running !== wasRunning) {
              // 状态变了，整体重绘列表
              renderAutoList();
            }
          });
        }

        // 详情页实时刷新
        if (el('auto-detail-view').classList.contains('auto-page-active') && State.currentTaskId) {
          const t = State.autoConfigs.find(x => x.task_id === State.currentTaskId);
          if (t) renderAutoDetail(t);
        }
      } catch (e) { /* 忽略轮询错误 */ }
    }, 5000);
  }

  function stopAutoPoll() {
    if (State.autoPollTimer) {
      clearInterval(State.autoPollTimer);
      State.autoPollTimer = null;
    }
  }

  /* ==================== 设置页 ==================== */
  function openSettings() {
    switchView('settings-view');
    if (State.user && State.user.role === 'admin') {
      loadApiConfigs();
      loadAdminUsers();
      loadInvites();
    } else {
      // 非管理员：只显示基本设置
      ['settings-panel-api', 'settings-panel-users', 'settings-panel-invites', 'settings-panel-system']
        .forEach(id => { const p = el(id); if (p) p.style.display = 'none'; });
    }
  }

  /* ==================== AI API 管理 ==================== */
  async function loadApiConfigs() {
    try {
      const list = await api('/api/api_configs');
      renderApiList(el('api-list'), list || [], false);
      renderApiList(el('api-modal-list'), list || [], true);
    } catch (e) {
      console.warn('loadApiConfigs failed:', e.message);
    }
  }

  function renderApiList(box, list, inModal) {
    if (!box) return;
    if (!list.length) {
      box.innerHTML = '<div class="empty-hint">还没有配置任何 API</div>';
      return;
    }
    const activeId = State.activeApi ? State.activeApi.id : '';
    box.innerHTML = list.map(a => {
      const isActive = a.id === activeId;
      return `
        <div class="api-item${isActive ? ' active' : ''}" data-api-id="${escapeHtml(a.id)}">
          <div class="api-item-main">
            <div class="api-item-name" ${ellipsisAttr(a.name)}>
              ${escapeHtml(a.name)}
              ${isActive ? '<span class="api-item-badge">使用中</span>' : ''}
            </div>
            <div class="api-item-meta" ${ellipsisAttr(a.provider + ' · ' + a.model_name)}>
              ${escapeHtml(a.provider)} · ${escapeHtml(a.model_name)} · ${escapeHtml(a.api_key_masked)}
            </div>
          </div>
          <div class="api-item-actions">
            ${isActive ? '' : `<button class="btn btn-text btn-press" data-act="activate">激活</button>`}
            <button class="btn btn-text btn-press" data-act="edit">编辑</button>
            <button class="btn btn-text btn-press" data-act="delete" style="color:#F5222D;">删除</button>
          </div>
        </div>
      `;
    }).join('');

    box.querySelectorAll('.api-item').forEach(item => {
      item.addEventListener('click', (e) => {
        const btn = e.target.closest('[data-act]');
        if (!btn) return;
        e.stopPropagation();
        const id = item.getAttribute('data-api-id');
        const act = btn.getAttribute('data-act');
        if (act === 'activate') activateApi(id);
        else if (act === 'edit') openApiForm(id);
        else if (act === 'delete') deleteApi(id);
      });
    });
  }

  async function activateApi(id) {
    try {
      await api(`/api/api_configs/activate/${id}`, { method: 'POST' });
      toast('已激活', 'success');
      await refreshStatus();
      await loadApiConfigs();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  async function deleteApi(id) {
    const ok = await confirmDialog('删除 API', '确定删除该 API 配置吗？', '删除', 'btn-danger');
    if (!ok) return;
    try {
      await api(`/api/api_configs/delete/${id}`, { method: 'DELETE' });
      toast('已删除', 'success');
      await refreshStatus();
      await loadApiConfigs();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function openApiForm(id) {
    State.editingApiId = id || '';
    el('api-form-title').textContent = id ? '编辑 API' : '添加 API';

    // 填充供应商下拉
    const sel = el('api-form-provider');
    sel.innerHTML = Object.entries(State.providers).map(([k, v]) =>
      `<option value="${escapeHtml(k)}">${escapeHtml(v.name)}</option>`
    ).join('');

    if (id) {
      api('/api/api_configs').then(list => {
        const c = (list || []).find(x => x.id === id);
        if (!c) return;
        el('api-form-name').value = c.name;
        el('api-form-provider').value = c.provider;
        el('api-form-key').value = c.api_key_masked || '';
        el('api-form-model').value = c.model_name;
        el('api-form-custom-url').value = c.custom_base_url || '';
        updateApiFormProviderUI();
        openModal('api-form-modal');
      }).catch(e => toast(e.message, 'error'));
    } else {
      el('api-form-name').value = '';
      el('api-form-provider').value = 'deepseek';
      el('api-form-key').value = '';
      el('api-form-model').value = '';
      el('api-form-custom-url').value = '';
      updateApiFormProviderUI();
      openModal('api-form-modal');
    }
  }

  function updateApiFormProviderUI() {
    const p = el('api-form-provider').value;
    el('field-custom-url').style.display = p === 'custom' ? '' : 'none';

    // 更新模型名候选列表（点击输入框即可看到并选择）
    const prov = State.providers[p];
    const datalist = el('api-model-datalist');
    if (datalist) {
      datalist.innerHTML = '';
      if (prov && prov.models && prov.models.length) {
        prov.models.forEach(function (m) {
          const opt = document.createElement('option');
          opt.value = m;
          datalist.appendChild(opt);
        });
        el('api-form-model').placeholder = prov.models[0];
      }
    }
  }

  async function saveApiForm() {
    const name = el('api-form-name').value.trim();
    const provider = el('api-form-provider').value;
    const apiKey = el('api-form-key').value.trim();
    const modelName = el('api-form-model').value.trim();
    const customUrl = el('api-form-custom-url').value.trim();

    if (!name) return toast('请输入名称', 'error');
    if (!apiKey) return toast('请输入 API Key', 'error');
    if (!modelName) return toast('请输入模型名', 'error');
    if (provider === 'custom' && !customUrl) return toast('请输入自定义 Base URL', 'error');

    const body = {
      id: State.editingApiId || '',
      name,
      provider,
      api_key: apiKey,
      model_name: modelName,
      custom_base_url: customUrl,
    };
    try {
      showLoading('保存中…');
      await api('/api/api_configs/save', { method: 'POST', body });
      hideLoading();
      closeModal('api-form-modal');
      toast('保存成功', 'success');
      await refreshStatus();
      await loadApiConfigs();
    } catch (e) {
      hideLoading();
      toast(e.message || '保存失败', 'error');
    }
  }

  async function testApiForm() {
    const name = el('api-form-name').value.trim();
    const provider = el('api-form-provider').value;
    const apiKey = el('api-form-key').value.trim();
    const modelName = el('api-form-model').value.trim();
    const customUrl = el('api-form-custom-url').value.trim();

    if (!provider) return toast('请选择供应商', 'error');
    if (!apiKey) return toast('请输入 API Key', 'error');
    if (!modelName) return toast('请输入模型名', 'error');
    if (provider === 'custom' && !customUrl) return toast('请输入自定义 Base URL', 'error');

    const btn = el('btn-api-form-test');
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = '测试中…';

    try {
      const res = await api('/api/api_configs/test', {
        method: 'POST',
        body: {
          provider,
          api_key: apiKey,
          model_name: modelName,
          custom_base_url: customUrl,
        },
      });
      if (res.status === 'ok') {
        toast('✅ ' + (res.message || '连接成功'), 'success');
      } else {
        toast('❌ ' + (res.message || '连接失败'), 'error');
      }
    } catch (e) {
      toast('❌ ' + (e.message || '测试失败'), 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }

  /* ==================== 用户管理 ==================== */
  async function loadAdminUsers() {
    try {
      const list = await api('/api/admin/users');
      renderUsersList(list || []);
      el('users-count').textContent = (list || []).length + ' 位用户';
    } catch (e) {
      console.warn('loadAdminUsers failed:', e.message);
    }
  }

  function renderUsersList(list) {
    const box = el('users-list');
    if (!list.length) {
      box.innerHTML = '<div class="empty-hint">暂无用户</div>';
      return;
    }
    box.innerHTML = list.map(u => {
      const roleTag = `<span class="role-tag ${u.role}">${u.role}</span>`;
      const isSelf = State.user && u.id === State.user.id;
      const statusTag = u.status === 'disabled' ? '<span class="role-tag normal">已禁用</span>' : '';
      return `
        <div class="user-item" data-user-id="${escapeHtml(u.id)}">
          <div class="avatar"><span>${escapeHtml(initialOf(u.nickname || u.username))}</span></div>
          <div class="user-item-main">
            <div class="user-item-name" ${ellipsisAttr(u.nickname || u.username)}>${escapeHtml(u.nickname || u.username)} ${roleTag} ${statusTag}</div>
            <div class="user-item-sub">@${escapeHtml(u.username)}</div>
          </div>
          <div class="user-item-actions">
            ${u.role === 'normal' ? '<button class="btn btn-text btn-press" data-act="upgrade">升级</button>' : ''}
            ${u.role === 'member' ? '<button class="btn btn-text btn-press" data-act="downgrade">降级</button>' : ''}
            ${u.status === 'active' ? '<button class="btn btn-text btn-press" data-act="disable">禁用</button>' : '<button class="btn btn-text btn-press" data-act="enable">启用</button>'}
            ${!isSelf ? '<button class="btn btn-text btn-press" data-act="reset_password" style="color:#FAAD14;">重置密码</button>' : ''}
            ${!isSelf ? '<button class="btn btn-text btn-press" data-act="delete" style="color:#F5222D;">删除</button>' : ''}
          </div>
        </div>
      `;
    }).join('');

    box.querySelectorAll('.user-item').forEach(item => {
      item.addEventListener('click', async (e) => {
        const btn = e.target.closest('[data-act]');
        if (!btn) return;
        e.stopPropagation();
        const uid = item.getAttribute('data-user-id');
        const act = btn.getAttribute('data-act');
        await userAction(uid, act);
      });
    });
  }

  async function userAction(uid, action) {
    const confirmMsgs = {
      delete: '确定删除该用户？其数据也会被清除，不可恢复。',
      disable: '确定禁用该用户？他将无法登录。',
      reset_password: '确定重置该用户的密码为 123456 ？',
    };
    if (confirmMsgs[action]) {
      const ok = await confirmDialog('确认操作', confirmMsgs[action], '确定', action === 'delete' ? 'btn-danger' : 'btn-primary');
      if (!ok) return;
    }
    try {
      await api('/api/admin/user_action', {
        method: 'POST',
        body: { user_id: uid, action, new_password: null },
      });
      toast('操作成功', 'success');
      await loadAdminUsers();
    } catch (e) {
      toast(e.message || '操作失败', 'error');
    }
  }

  /* ==================== 邀请码 ==================== */
  async function loadInvites() {
    try {
      const list = await api('/api/admin/invites');
      renderInvites(list || []);
    } catch (e) {
      console.warn('loadInvites failed:', e.message);
    }
  }

  function renderInvites(list) {
    const box = el('invites-list');
    if (!list.length) {
      box.innerHTML = '<div class="empty-hint">暂无邀请码</div>';
      return;
    }
    const reversed = list.slice().reverse().slice(0, 10);
    box.innerHTML = reversed.map(i => {
      let expireStr = '';
      if (i.expire_at) expireStr = ' · 到期 ' + formatDate(i.expire_at);
      const statusMap = { unused: '未使用', used: '已使用', revoked: '已作废' };
      return `
        <div class="invite-item">
          <span class="invite-code">${escapeHtml(i.code)}</span>
          <span class="invite-status ${i.status}">${statusMap[i.status] || i.status}</span>
          <button class="btn btn-ghost btn-press invite-copy" data-code="${escapeHtml(i.code)}">复制</button>
          ${i.status === 'unused' ? `<button class="btn btn-ghost btn-press invite-copy" data-revoke="${escapeHtml(i.code)}">作废</button>` : ''}
        </div>
      `;
    }).join('');

    box.querySelectorAll('[data-code]').forEach(btn => {
      btn.addEventListener('click', () => {
        const code = btn.getAttribute('data-code');
        navigator.clipboard.writeText(code).then(() => toast('已复制', 'success'))
          .catch(() => toast('复制失败，请手动选择', 'warn'));
      });
    });
    box.querySelectorAll('[data-revoke]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const code = btn.getAttribute('data-revoke');
        const ok = await confirmDialog('作废邀请码', `确定作废邀请码 ${code} 吗？`, '作废', 'btn-danger');
        if (!ok) return;
        try {
          await api(`/api/admin/invite_revoke/${code}`, { method: 'POST' });
          toast('已作废', 'success');
          await loadInvites();
        } catch (e) { toast(e.message, 'error'); }
      });
    });
  }

  async function createInvite() {
    const role = el('invite-role').value;
    const expire = parseInt(el('invite-expire').value);
    try {
      const res = await api('/api/admin/invite_create', {
        method: 'POST',
        body: { role, expire_hours: expire },
      });
      toast('邀请码已生成：' + res.code, 'success');
      await loadInvites();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  /* ==================== 系统维护 ==================== */
  async function clearAiCache() {
    const ok = await confirmDialog('清理 AI 缓存', '确定清空 AI 解析缓存？下次解析会重新调用 AI。', '清空', 'btn-warn');
    if (!ok) return;
    try {
      const res = await api('/api/admin/clear_cache', { method: 'POST' });
      toast(res.message || '已清空', 'success');
    } catch (e) { toast(e.message, 'error'); }
  }

  async function clearHistoryAll() {
    const ok = await confirmDialog('清空历史', '确定清空所有操作历史和执行日志？不可恢复。', '清空', 'btn-danger');
    if (!ok) return;
    try {
      const res = await api('/api/admin/clear_history', { method: 'POST' });
      toast(res.message || '已清空', 'success');
      await loadHistory();
    } catch (e) { toast(e.message, 'error'); }
  }

  async function viewSystemStats() {
    try {
      const s = await api('/api/admin/system_stats');
      const box = el('stats-modal-content');
      const items = [
        ['AI 缓存条目', s.ai_cache_count],
        ['有历史的用户数', s.history_users],
        ['自动任务日志数', s.auto_log_count],
        ['用户总数', s.user_count],
        ['剧集索引条目', s.series_index_count],
      ];
      box.innerHTML = items.map(([k, v]) =>
        `<div class="stats-mini-item"><span class="stats-mini-key">${escapeHtml(k)}</span><span class="stats-mini-val">${v}</span></div>`
      ).join('');
      openModal('stats-modal');
    } catch (e) { toast(e.message, 'error'); }
  }

  /* ==================== 个人资料 ==================== */
  let avatarBase64 = '';

  function openProfile() {
    if (!State.user) return;
    el('profile-nickname').value = State.user.nickname || State.user.username;
    el('profile-old-pwd').value = '';
    el('profile-new-pwd').value = '';
    avatarBase64 = '';

    const preview = el('profile-avatar-preview');
    if (State.user.avatar) {
      preview.innerHTML = `<img src="${escapeHtml(State.user.avatar)}">`;
    } else {
      preview.innerHTML = `<span>${escapeHtml(initialOf(State.user.nickname || State.user.username))}</span>`;
    }
    openModal('profile-modal');
  }

  async function saveProfile() {
    const nickname = el('profile-nickname').value.trim();
    const oldPwd = el('profile-old-pwd').value;
    const newPwd = el('profile-new-pwd').value;

    if (!nickname) return toast('请输入昵称', 'error');
    if (nickname.length > 20) return toast('昵称过长', 'error');
    if (oldPwd || newPwd) {
      if (!oldPwd || !newPwd) return toast('修改密码需同时填写原密码和新密码', 'warn');
      if (newPwd.length < 6) return toast('新密码至少 6 位', 'warn');
    }

    try {
      showLoading('保存中…');
      const body = { nickname };
      if (avatarBase64) body.avatar = avatarBase64;
      await api('/api/user/update_profile', { method: 'POST', body });

      if (oldPwd && newPwd) {
        await api('/api/user/change_password', {
          method: 'POST',
          body: { old_password: oldPwd, new_password: newPwd },
        });
      }
      hideLoading();
      closeModal('profile-modal');
      toast('资料已更新', 'success');
      await refreshStatus();
    } catch (e) {
      hideLoading();
      toast(e.message || '保存失败', 'error');
    }
  }

  /* ==================== 事件绑定 ==================== */
  function bindMainEvents() {
    // 顶栏
    el('btn-logout').addEventListener('click', async () => {
      const ok = await confirmDialog('退出登录', '确定退出当前账号吗？', '退出');
      if (!ok) return;
      logoutLocal();
      switchView('auth-view');
    });

    el('topbar-avatar').addEventListener('click', openProfile);
    el('btn-settings').addEventListener('click', openSettings);
    el('btn-settings-back').addEventListener('click', () => switchView('main-app'));

    // 网盘
    el('btn-add-source').addEventListener('click', () => openSourceModal(''));
    el('btn-source-save').addEventListener('click', saveSource);
    el('btn-source-test').addEventListener('click', testSource);

    // 目录浏览
    el('btn-browser-up').addEventListener('click', browserUp);
    el('btn-scan').addEventListener('click', scanVideos);

    // 待整理列表
    el('check-all').addEventListener('change', (e) => checkAllVideos(e.target.checked));
    el('btn-invert').addEventListener('click', invertVideos);
    el('btn-clear-list').addEventListener('click', () => {
      if (!State.videoList.length) return;
      confirmDialog('清空列表', '仅清空当前展示列表，不影响网盘文件。', '清空').then(ok => { if (ok) clearVideoList(); });
    });
    el('btn-rename').addEventListener('click', runRename);
    el('btn-history-all').addEventListener('click', openHistoryFull);

    // AI 管理
    el('btn-ai-manage').addEventListener('click', () => {
      if (State.user && State.user.role === 'admin') {
        openModal('api-modal');
        loadApiConfigs();
      } else {
        openModal('permission-modal');
      }
    });
    el('btn-api-add').addEventListener('click', () => openApiForm(''));
    el('btn-api-modal-add').addEventListener('click', () => openApiForm(''));
    el('btn-api-form-save').addEventListener('click', saveApiForm);
    el('btn-api-form-test').addEventListener('click', testApiForm);
    el('api-form-provider').addEventListener('change', updateApiFormProviderUI);

    // 用户 / 邀请 / 系统维护
    el('btn-invite-create').addEventListener('click', createInvite);
    el('btn-clear-cache').addEventListener('click', clearAiCache);
    el('btn-clear-history').addEventListener('click', clearHistoryAll);
    el('btn-view-stats').addEventListener('click', viewSystemStats);

    // 个人资料
    el('btn-profile-save').addEventListener('click', saveProfile);
    el('profile-avatar-input').addEventListener('change', (e) => {
      const f = e.target.files && e.target.files[0];
      if (!f) return;
      if (f.size > 100 * 1024) return toast('头像请压缩到 100KB 以内', 'warn');
      const reader = new FileReader();
      reader.onload = () => {
        avatarBase64 = reader.result;
        el('profile-avatar-preview').innerHTML = `<img src="${avatarBase64}">`;
      };
      reader.readAsDataURL(f);
    });

    // 自动任务
    el('btn-auto-back').addEventListener('click', () => {
      switchView('main-app');
      stopAutoPoll();
    });
    el('btn-auto-new').addEventListener('click', () => {
      if (!State.user || (State.user.role !== 'admin' && State.user.role !== 'member')) {
        openModal('permission-modal');
        return;
      }
      openAutoForm('');
    });
    el('btn-detail-back').addEventListener('click', () => {
      switchAutoPage('auto-list-view');
      loadAutoConfigs();
    });
    el('btn-detail-edit').addEventListener('click', () => openAutoForm(State.currentTaskId));
    el('btn-detail-run').addEventListener('click', () => runAutoNow(State.currentTaskId));
    el('btn-detail-cancel').addEventListener('click', () => cancelAutoTask(State.currentTaskId));
    el('btn-detail-toggle').addEventListener('click', () => toggleAutoTask(State.currentTaskId));
    el('btn-detail-delete').addEventListener('click', () => deleteAutoTask(State.currentTaskId));
    el('btn-detail-logs-all').addEventListener('click', () => openAllLogs(State.currentTaskId));

    // 表单页
    el('btn-form-back').addEventListener('click', () => {
      switchAutoPage('auto-list-view');
      loadAutoConfigs();
    });
    el('btn-form-cancel').addEventListener('click', () => {
      switchAutoPage('auto-list-view');
      loadAutoConfigs();
    });
    el('btn-form-save').addEventListener('click', saveAutoForm);
    document.querySelectorAll('input[name=form-mode]').forEach(r =>
      r.addEventListener('change', updateFormModeUI)
    );
    document.querySelectorAll('[data-picker]').forEach(btn =>
      btn.addEventListener('click', () => openFolderPicker(btn.getAttribute('data-picker')))
    );
    el('btn-picker-confirm').addEventListener('click', confirmFolderPicker);

    // 进入自动任务模块：主界面某个入口（暂时挂在 AI 卡片下方"管理"按钮旁不行——用设置页入口）
    // 我们直接在主界面加一个入口：通过点击顶栏昵称下方触发太隐晦，这里挂在"设置"图标旁边不好
    // 用一个更直接的方式：点顶栏"设置"按钮是设置页；自动任务从侧边或底部进入
    // 简化：在设置页里放个入口按钮（已存在）。此外左下角可以点击，这里再绑定一次：
    document.addEventListener('click', (e) => {
      const t = e.target.closest('[data-open-auto]');
      if (t) openAutoModule();
    });
  }

  async function openAllLogs(taskId) {
    try {
      const data = await api(`/api/auto/logs?task_id=${encodeURIComponent(taskId)}`);
      const logs = (data.logs || []).slice().reverse();
      const box = el('logs-full-list');
      if (!logs.length) {
        box.innerHTML = '<div class="empty-hint">暂无执行记录</div>';
      } else {
        box.innerHTML = logs.map((log, i) => renderLogCard(log, i)).join('');
        box.querySelectorAll('.log-card').forEach((card, i) => {
          card.addEventListener('click', () => openLogDetail(logs[i]));
        });
      }
      openModal('auto-logs-full-modal');
    } catch (e) {
      toast(e.message, 'error');
    }
  }


  /* ==================== 重命名任务模块 ==================== */
  let renamePollTimer = null;
  let currentRenameTaskId = '';
  let currentRenameFilter = 'all';

  function openRenameTasks() {
    switchView('rename-module');
    switchRenamePage('rename-list-view');
    loadRenameList();
    startRenamePoll();
  }

  function switchRenamePage(id) {
    window.FX.$$('.rename-page').forEach(p => p.classList.remove('rename-page-active'));
    const target = document.getElementById(id);
    if (target) target.classList.add('rename-page-active');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function loadRenameList() {
    const box = el('rename-list');
    if (!box) return;
    try {
      const data = await api('/api/rename_tasks');
      const tasks = data.tasks || [];
      if (!tasks.length) {
        box.innerHTML = '<div class="empty-state">' +
          '<div class="empty-state-icon">📋</div>' +
          '<div class="empty-state-text">暂无重命名任务</div>' +
          '<div class="empty-state-hint">在主界面扫描视频并执行重命名后，任务会出现在这里</div>' +
          '</div>';
        return;
      }
      box.innerHTML = tasks.map(t => renderRenameCard(t)).join('');
      box.querySelectorAll('.rename-card').forEach((card, i) => {
        card.addEventListener('click', () => openRenameDetail(tasks[i].task_id));
      });
    } catch (e) {
      box.innerHTML = '<div class="empty-hint">' + escapeHtml(e.message) + '</div>';
    }
  }

  function renderRenameCard(t) {
    const total = t.total || 0;
    const done = (t.success || 0) + (t.failed || 0);
    const percent = total > 0 ? Math.round(done / total * 100) : 0;
    const statusMap = {
      running:     { label: '进行中', cls: 'running' },
      done:        { label: '已完成', cls: 'done' },
      cancelled:   { label: '已中断', cls: 'cancelled' },
      interrupted: { label: '异常中断', cls: 'interrupted' },
      pending:     { label: '等待中', cls: 'pending' },
    };
    const tag = statusMap[t.status] || { label: t.status, cls: 'pending' };
    return '<div class="rename-card card-press">' +
      '<div class="rename-card-head">' +
        '<div class="rename-card-time">' + escapeHtml(t.time_str || '') + '</div>' +
        '<span class="rename-card-tag ' + tag.cls + '">' + tag.label + '</span>' +
      '</div>' +
      '<div class="rename-card-meta">' +
        '<span class="rename-card-stat">成功 <span class="rename-card-stat-num ok">' + (t.success || 0) + '</span></span>' +
        '<span class="rename-card-stat">失败 <span class="rename-card-stat-num bad">' + (t.failed || 0) + '</span></span>' +
        '<span class="rename-card-stat">总数 <span class="rename-card-stat-num">' + total + '</span></span>' +
        (t.source_name ? '<span style="color:#B4B8BF;">· ' + escapeHtml(t.source_name) + '</span>' : '') +
      '</div>' +
      (t.status === 'running' ? '<div class="rename-card-bar"><div class="rename-card-bar-fill" style="width:' + percent + '%"></div></div>' : '') +
      '</div>';
  }

  async function openRenameDetail(taskId) {
    currentRenameTaskId = taskId;
    currentRenameFilter = 'all';
    document.querySelectorAll('[data-filter]').forEach(b => b.style.color = '');
    const allBtn = document.querySelector('[data-filter="all"]');
    if (allBtn) allBtn.style.color = 'var(--c-primary)';
    switchView('rename-module');
    switchRenamePage('rename-detail-view');
    await refreshRenameDetail();
    startRenameDetailPoll();
  }

  async function refreshRenameDetail() {
    if (!currentRenameTaskId) return;
    try {
      const task = await api('/api/rename_tasks/' + currentRenameTaskId);
      renderRenameDetail(task);
    } catch (e) {
      // 静默
    }
  }

  function renderRenameDetail(task) {
    const total = task.total || 0;
    const success = task.success || 0;
    const failed = task.failed || 0;
    const done = success + failed;
    const percent = total > 0 ? Math.round(done / total * 100) : 0;
    const running = task.status === 'running';

    el('rename-detail-sub').textContent = task.time_str || '';
    el('rename-progress-label').textContent = '已完成 ' + done + ' / ' + total;
    el('rename-progress-fill').style.width = percent + '%';
    let curText = '';
    if (running && task.current_file) curText = '正在处理：' + task.current_file;
    else if (task.status === 'done') curText = '任务已完成';
    else if (task.status === 'cancelled') curText = '已中断';
    else if (task.status === 'interrupted') curText = '异常中断';
    el('rename-progress-current').textContent = curText;
    el('rename-stat-success').textContent = success;
    el('rename-stat-failed').textContent = failed;
    el('rename-stat-total').textContent = total;
    const cancelBtn = el('btn-rename-detail-cancel');
    if (cancelBtn) cancelBtn.style.display = running ? '' : 'none';
    renderRenameFiles(task.files || []);
  }

  function renderRenameFiles(files) {
    const box = el('rename-files');
    if (!box) return;
    const filtered = currentRenameFilter === 'all' ? files :
                     currentRenameFilter === 'success' ? files.filter(f => f.status === 'success') :
                     files.filter(f => f.status === 'fail');
    if (!filtered.length) {
      box.innerHTML = '<div class="empty-hint">' +
        (currentRenameFilter === 'all' ? '暂无文件' : '无匹配文件') + '</div>';
      return;
    }
    const badgeMap = { success: '成功', fail: '失败', pending: '待处理', processing: '处理中' };
    box.innerHTML = filtered.map(f => {
      let extra = '';
      if (f.status === 'success' && f.result_path) {
        const newName = f.result_path.split('/').pop();
        extra = '<div class="rename-file-new" title="' + escapeHtml(f.result_path) + '">' + escapeHtml(newName) + '</div>';
      } else if (f.status === 'fail' && f.error) {
        extra = '<div class="rename-file-error">' + escapeHtml(f.error) + '</div>';
      }
      return '<div class="rename-file-row">' +
        '<div class="rename-file-head">' +
          '<span class="rename-file-badge ' + f.status + '">' + (badgeMap[f.status] || f.status) + '</span>' +
          '<span class="rename-file-old" title="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) + '</span>' +
        '</div>' +
        extra +
        '</div>';
    }).join('');
  }

  function startRenamePoll() {
    if (renamePollTimer) return;
    renamePollTimer = setInterval(() => {
      if (el('rename-module').classList.contains('view-active') &&
          el('rename-list-view').classList.contains('rename-page-active')) {
        loadRenameList();
      }
    }, 5000);
  }

  function startRenameDetailPoll() {
    if (renamePollTimer) {
      clearInterval(renamePollTimer);
      renamePollTimer = null;
    }
    renamePollTimer = setInterval(() => {
      if (el('rename-module').classList.contains('view-active') &&
          el('rename-detail-view').classList.contains('rename-page-active')) {
        refreshRenameDetail();
      }
    }, 3000);
  }

  async function cancelRenameTask() {
    if (!currentRenameTaskId) return;
    const ok = await confirmDialog('中断任务', '确定要中断当前任务吗？已处理的文件不会回滚。', '中断', 'btn-warn');
    if (!ok) return;
    try {
      await api('/api/rename_tasks/' + currentRenameTaskId + '/cancel', { method: 'POST' });
      toast('中断信号已发送', 'success');
      setTimeout(refreshRenameDetail, 500);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function bindRenameEvents() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('[data-open-rename-tasks]')) {
        openRenameTasks();
      }
    });
    const backBtn = el('btn-rename-back');
    if (backBtn) backBtn.addEventListener('click', () => {
      switchView('main-app');
      if (renamePollTimer) { clearInterval(renamePollTimer); renamePollTimer = null; }
    });
    const detailBackBtn = el('btn-rename-detail-back');
    if (detailBackBtn) detailBackBtn.addEventListener('click', () => {
      switchRenamePage('rename-list-view');
      loadRenameList();
      startRenamePoll();
    });
    const refreshBtn = el('btn-rename-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadRenameList);
    const cancelBtn = el('btn-rename-detail-cancel');
    if (cancelBtn) cancelBtn.addEventListener('click', cancelRenameTask);
    document.querySelectorAll('[data-filter]').forEach(btn => {
      btn.addEventListener('click', () => {
        currentRenameFilter = btn.getAttribute('data-filter');
        document.querySelectorAll('[data-filter]').forEach(b => b.style.color = '');
        btn.style.color = 'var(--c-primary)';
        if (currentRenameTaskId) refreshRenameDetail();
      });
    });
  }


  /* ==================== 网盘管理模块（多账号版） ==================== */

  function openDrivesPage() {
    switchView('drives-module');
    switchDrivePage('drives-list-view');
    loadDrivesList();
  }

  function switchDrivePage(id) {
    document.querySelectorAll('.drives-page').forEach(p => p.classList.remove('drives-page-active'));
    const t = document.getElementById(id);
    if (t) t.classList.add('drives-page-active');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function loadDrivesList() {
    try {
      const res = await api('/api/pan123/accounts');
      const accounts = (res && res.accounts) || [];
      const sub = el('drive-pan123-sub');
      if (accounts.length > 0) {
        sub.textContent = '已配置 ' + accounts.length + ' 个账号';
        sub.className = 'drive-item-sub ok';
      } else {
        sub.textContent = '未授权 · 点击配置';
        sub.className = 'drive-item-sub';
      }
    } catch (e) {
      el('drive-pan123-sub').textContent = '状态异常';
    }

    try {
      const list = await api('/api/webdav/list');
      const count = Array.isArray(list) ? list.length : 0;
      const sub = el('drive-webdav-sub');
      if (count > 0) {
        sub.textContent = '已配置 ' + count + ' 个网盘';
        sub.className = 'drive-item-sub ok';
      } else {
        sub.textContent = '未配置 · 点击添加';
        sub.className = 'drive-item-sub';
      }
    } catch (e) {
      el('drive-webdav-sub').textContent = '状态异常';
    }
  }

  async function openPan123Page() {
    switchDrivePage('drive-pan123-view');
    await loadPan123Accounts();
  }

  async function loadPan123Accounts() {
    const box = el('pan123-accounts-list');
    if (!box) return;
    box.innerHTML = '<div class="empty-hint">加载中…</div>';
    try {
      const res = await api('/api/pan123/accounts');
      const accounts = (res && res.accounts) || [];
      if (!accounts.length) {
        box.innerHTML = '<div class="empty-hint">还没有 123 账号，点击下方按钮添加</div>';
        return;
      }
      box.innerHTML = accounts.map(a => {
        const nick = (a.lastUserInfo && a.lastUserInfo.nickname) || '未知账号';
        const used = (a.lastUserInfo && a.lastUserInfo.spaceUsed) || 0;
        const total = (a.lastUserInfo && a.lastUserInfo.spacePermanent) || 0;
        return '<div class="pan123-account-item card-press" data-account-id="' + escapeHtml(a.id) + '">' +
          '<div class="pan123-account-main">' +
            '<div class="pan123-account-name">' + escapeHtml(a.name || nick) + '</div>' +
            '<div class="pan123-account-sub">已用 ' + formatSize(used) + ' / ' + formatSize(total) + '</div>' +
          '</div>' +
          '<div class="pan123-account-actions">' +
            '<button class="btn btn-text btn-press" data-act="rename">改名</button>' +
            '<button class="btn btn-text btn-press" data-act="delete" style="color:var(--c-danger);">删除</button>' +
          '</div>' +
        '</div>';
      }).join('');

      box.querySelectorAll('.pan123-account-item').forEach(item => {
        item.addEventListener('click', async (e) => {
          const btn = e.target.closest('[data-act]');
          if (!btn) return;
          e.stopPropagation();
          const aid = item.getAttribute('data-account-id');
          const act = btn.getAttribute('data-act');
          if (act === 'rename') renamePan123Account(aid);
          else if (act === 'delete') deletePan123Account(aid);
        });
      });
    } catch (e) {
      box.innerHTML = '<div class="empty-hint">加载失败：' + escapeHtml(e.message) + '</div>';
    }
  }

  function formatSize(bytes) {
    if (!bytes || bytes <= 0) return '0 B';
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, n = bytes;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(2) + ' ' + units[i];
  }

  async function startPan123Auth() {
    const btn = el('btn-open-pan123-auth');
    if (!btn) return;
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = '获取授权链接中…';
    try {
      const res = await api('/api/pan123/auth-url');
      if (res && res.authorizeUrl) {
        window.open(res.authorizeUrl, '_blank');
        toast('已打开授权页面，请登录 123 并授权', 'success');
      } else {
        toast('未获取到授权地址', 'error');
      }
    } catch (e) {
      toast('获取授权地址失败：' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }

  async function submitPan123Tokens() {
    const nameInput = el('pan123-account-name');
    const name = (nameInput ? nameInput.value.trim() : '') || '123 云盘';
    const at = el('pan123-access-token').value.trim();
    const rt = el('pan123-refresh-token').value.trim();
    if (!at || !rt) return toast('请填写两个 Token', 'warn');

    const btn = el('btn-pan123-submit');
    btn.disabled = true;
    const oldText = btn.textContent;
    btn.textContent = '验证中…';
    try {
      const res = await api('/api/pan123/accounts', {
        method: 'POST',
        body: { name, access_token: at, refresh_token: rt },
      });
      if (res && res.status === 'ok') {
        toast('添加成功', 'success');
        closeModal('pan123-auth-modal');
        if (nameInput) nameInput.value = '';
        el('pan123-access-token').value = '';
        el('pan123-refresh-token').value = '';
        loadPan123Accounts();
      } else {
        toast('添加失败', 'error');
      }
    } catch (e) {
      toast('添加失败：' + e.message, 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = oldText;
    }
  }

  async function renamePan123Account(accountId) {
    let accounts = [];
    try {
      const r = await api('/api/pan123/accounts');
      accounts = r.accounts || [];
    } catch (e) { return; }
    const a = accounts.find(x => x.id === accountId);
    if (!a) return;
    const newName = prompt('输入新名称：', a.name || '');
    if (!newName || newName === a.name) return;
    try {
      await api('/api/pan123/accounts/' + accountId + '/rename', {
        method: 'POST',
        body: { name: newName },
      });
      toast('已改名', 'success');
      loadPan123Accounts();
    } catch (e) {
      toast('改名失败：' + e.message, 'error');
    }
  }

  async function deletePan123Account(accountId) {
    const ok = await confirmDialog('删除账号', '确定删除这个 123 账号吗？', '删除', 'btn-danger');
    if (!ok) return;
    try {
      await api('/api/pan123/accounts/' + accountId, { method: 'DELETE' });
      toast('已删除', 'success');
      loadPan123Accounts();
    } catch (e) {
      toast('删除失败：' + e.message, 'error');
    }
  }

  function bindDriveEvents() {
    document.addEventListener('click', (e) => {
      if (e.target.closest('[data-open-drives]')) openDrivesPage();
    });

    const backBtn = el('btn-drives-back');
    if (backBtn) backBtn.addEventListener('click', () => switchView('main-app'));

    document.querySelectorAll('[data-drive]').forEach(item => {
      item.addEventListener('click', () => {
        const d = item.getAttribute('data-drive');
        if (d === 'pan123') openPan123Page();
        else if (d === 'webdav') {
          switchView('main-app');
          setTimeout(() => {
            const panel = document.querySelector('.side-col .panel');
            if (panel) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
          }, 200);
        }
      });
    });

    const p123Back = el('btn-drive-pan123-back');
    if (p123Back) p123Back.addEventListener('click', () => {
      switchDrivePage('drives-list-view');
      loadDrivesList();
    });

    const startBtn = el('btn-pan123-start-auth');
    if (startBtn) startBtn.addEventListener('click', () => openModal('pan123-auth-modal'));

    const openAuthBtn = el('btn-open-pan123-auth');
    if (openAuthBtn) openAuthBtn.addEventListener('click', startPan123Auth);

    const submitBtn = el('btn-pan123-submit');
    if (submitBtn) submitBtn.addEventListener('click', submitPan123Tokens);
  }

  /* ==================== 初始化入口 ==================== */
  function injectFooter() {
    // 主界面底部注入署名（不占用 HTML 结构）
    const mainWrap = document.querySelector('#main-app .main-wrap');
    if (mainWrap && !mainWrap.querySelector('.main-footer')) {
      const f = document.createElement('div');
      f.className = 'main-footer';
      f.innerHTML = '<div>Made with ❤ by 陈橙呈</div><div class="main-footer-sub">仅供学习交流，禁止商用</div>';
      mainWrap.appendChild(f);
    }
    // 登录页底部
    const authWrap = document.querySelector('.auth-wrap');
    if (authWrap && !authWrap.querySelector('.auth-footer-extra')) {
      const f = document.createElement('div');
      f.className = 'auth-footer-extra';
      f.style.cssText = 'text-align:center;font-size:11px;color:#8F959E;margin-top:16px;line-height:1.7;';
      f.innerHTML = 'Made with ❤ by 陈橙呈 · 仅供学习交流，禁止商用';
      authWrap.appendChild(f);
    }
  }

  async function bootstrap() {
    bindAuthPage();
    bindMainEvents();
    bindRenameEvents();
    bindDriveEvents();

    // 注入署名（不占 HTML 结构）
    injectFooter();

    // 如果有 token，尝试直接进入主应用
    if (State.token) {
      try {
        await enterApp();
        return;
      } catch (e) {
        // token 失效
        logoutLocal();
      }
    }
    switchView('auth-view');
  }

  // DOM 加载完成后启动
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }

})();