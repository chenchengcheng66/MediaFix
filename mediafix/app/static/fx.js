/* ============================================================
   MediaFix · fx.js
   纯动效工具集：Toast / Loading / Modal / 视图切换 / 弹簧行为
   不包含任何业务逻辑，只暴露 window.FX 给 app.js 调用
   ============================================================ */

(function (global) {
  'use strict';

  /* ==================== 常量 ==================== */
  const TOAST_DURATION = 2600;

  /* ==================== 内部辅助 ==================== */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.from((root || document).querySelectorAll(sel)); }

  /* ==================== Toast ==================== */
  let toastTimer = null;
  function toast(message, type) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = String(message || '');
    el.className = 'toast toast-show' + (type ? ' toast-' + type : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      el.classList.remove('toast-show');
    }, TOAST_DURATION);
  }

  /* ==================== Loading ==================== */
  let loadingCount = 0;
  function showLoading(text) {
    loadingCount++;
    const overlay = $('#loading-overlay');
    const label = $('#loading-text');
    if (!overlay) return;
    if (label) label.textContent = text || '处理中…';
    overlay.style.display = 'flex';
  }
  function hideLoading(force) {
    if (force) loadingCount = 0;
    else loadingCount = Math.max(0, loadingCount - 1);
    if (loadingCount === 0) {
      const overlay = $('#loading-overlay');
      if (overlay) overlay.style.display = 'none';
    }
  }

  /* ==================== Modal 控制 ==================== */
  // 弹窗栈：后打开的弹窗盖在前面
  const modalStack = [];
  function refreshModalZ() {
    modalStack.forEach((m, i) => { m.style.zIndex = 1000 + i * 10; });
  }

  function openModal(id) {
    const el = typeof id === 'string' ? document.getElementById(id) : id;
    if (!el) return;
    if (modalStack.indexOf(el) < 0) modalStack.push(el);
    el.classList.add('modal-open');
    refreshModalZ();
    lockBody();
    const firstInput = el.querySelector('input:not([type=file]):not([disabled]), textarea');
    if (firstInput && window.matchMedia('(min-width: 769px)').matches) {
      setTimeout(() => firstInput.focus(), 120);
    }
  }

  function closeModal(id) {
    const el = typeof id === 'string' ? document.getElementById(id) : id;
    if (!el) return;
    el.classList.remove('modal-open');
    el.style.zIndex = '';
    const idx = modalStack.indexOf(el);
    if (idx >= 0) modalStack.splice(idx, 1);
    refreshModalZ();
    unlockBodyIfNoModal();
  }

  function closeAllModals() {
    $$('.modal.modal-open').forEach(m => m.classList.remove('modal-open'));
    unlockBodyIfNoModal(true);
  }

  // 判断还有没有打开的弹窗，没有就解锁背景滚动
  function unlockBodyIfNoModal(force) {
    if (force) {
      document.body.style.overflow = '';
      return;
    }
    if (!document.querySelector('.modal.modal-open')) {
      document.body.style.overflow = '';
    }
  }

  function lockBody() {
    document.body.style.overflow = 'hidden';
  }

  /* ==================== 视图切换 ==================== */
  function switchView(id) {
    $$('.view').forEach(v => v.classList.remove('view-active'));
    const target = document.getElementById(id);
    if (target) target.classList.add('view-active');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  // 自动任务模块内部的层级切换（列表 / 详情 / 表单）
  function switchAutoPage(id) {
    $$('.auto-page').forEach(p => p.classList.remove('auto-page-active'));
    const target = document.getElementById(id);
    if (target) target.classList.add('auto-page-active');
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  /* ==================== 通用二次确认 ==================== */
  // 返回 Promise<boolean>
  let confirmResolver = null;
  function confirmDialog(title, text, okText, okClass) {
    const modal = $('#confirm-modal');
    if (!modal) return Promise.resolve(false);
    $('#confirm-title').textContent = title || '请确认';
    $('#confirm-text').textContent = text || '确定执行该操作吗？';
    const okBtn = $('#confirm-ok');
    okBtn.textContent = okText || '确定';
    okBtn.className = 'btn btn-press ' + (okClass || 'btn-primary');
    openModal('confirm-modal');
    return new Promise(resolve => {
      confirmResolver = resolve;
    });
  }

  function bindConfirmEvents() {
    const cancelBtn = $('#confirm-cancel');
    const okBtn = $('#confirm-ok');
    const modal = $('#confirm-modal');
    if (!modal) return;

    function done(val) {
      closeModal('confirm-modal');
      if (confirmResolver) { confirmResolver(val); confirmResolver = null; }
    }
    cancelBtn.addEventListener('click', () => done(false));
    okBtn.addEventListener('click', () => done(true));
    // 点遮罩关闭 = 取消
    modal.querySelector('.modal-mask').addEventListener('click', () => done(false));
  }

  /* ==================== 弹窗关闭的通用绑定 ==================== */
  // 所有带 data-close 属性的按钮：点击关闭指定弹窗
  // 所有 .modal-mask：点击关闭所在的弹窗
  function bindGlobalModalEvents() {
    document.addEventListener('click', (e) => {
      const closeBtn = e.target.closest('[data-close]');
      if (closeBtn) {
        closeModal(closeBtn.getAttribute('data-close'));
        return;
      }
      if (e.target.classList && e.target.classList.contains('modal-mask')) {
        const modal = e.target.closest('.modal');
        if (modal) closeModal(modal.id);
      }
    });

    // ESC 关闭最上层弹窗
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        const open = $$('.modal.modal-open');
        if (open.length) closeModal(open[open.length - 1].id);
      }
    });
  }

  /* ==================== 折叠区（日志详情用） ==================== */
  function bindCollapse() {
    document.addEventListener('click', (e) => {
      const head = e.target.closest('.log-detail-section-head');
      if (!head) return;
      const section = head.closest('.log-detail-section');
      if (section) section.classList.toggle('collapsed');
    });
  }

  /* ==================== 数字滚动动画 ==================== */
  // 让一个数字元素从当前值平滑动画到目标值
  function animateNumber(el, to, duration) {
    if (!el) return;
    const from = parseInt(el.textContent.replace(/[^\d-]/g, ''), 10) || 0;
    if (from === to) { el.textContent = to; return; }
    const dur = duration || 500;
    const start = performance.now();
    function step(now) {
      const p = Math.min(1, (now - start) / dur);
      // easeOutCubic
      const eased = 1 - Math.pow(1 - p, 3);
      const val = Math.round(from + (to - from) * eased);
      el.textContent = val;
      if (p < 1) requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  /* ==================== 表单 range 数值联动 ==================== */
  function bindRangeHint(rangeId, hintId, suffix) {
    const range = document.getElementById(rangeId);
    const hint = document.getElementById(hintId);
    if (!range || !hint) return;
    const update = () => { hint.textContent = '当前 ' + range.value + (suffix || ''); };
    range.addEventListener('input', update);
    update();
  }

  /* ==================== 键盘遮挡修复 ==================== */
  (function bindKeyboardAvoidance() {
    if (!window.visualViewport) return;
    const vv = window.visualViewport;
    const root = document.documentElement;
    function update() {
      const kb = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      root.style.setProperty('--kb-height', kb + 'px');
    }
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    update();
  })();

  /* ==================== 初始化 ==================== */
  function init() {
    bindGlobalModalEvents();
    bindConfirmEvents();
    bindCollapse();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  /* ==================== 导出 ==================== */
  global.FX = {
    toast,
    showLoading,
    hideLoading,
    openModal,
    closeModal,
    closeAllModals,
    switchView,
    switchAutoPage,
    confirmDialog,
    animateNumber,
    bindRangeHint,
    $,
    $$,
  };

})(window);