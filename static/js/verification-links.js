(function () {
  'use strict';

  const $ = id => document.getElementById(id);
  const has = id => Boolean($(id));
  let page = 1, totalPages = 0, items = [], importing = false;
  const selected = new Set();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char]));

  const notify = message => {
    const element = $('toast');
    if (!element) return;
    element.textContent = message;
    element.classList.add('show');
    setTimeout(() => element.classList.remove('show'), 2400);
  };

  async function api(url, options) {
    const response = await fetch(url, Object.assign({ credentials: 'same-origin' }, options || {}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) throw Error(data.error_code || 'request_failed');
    return data;
  }

  function setComboboxExpanded(input, menu, expanded) {
    input.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    menu.hidden = !expanded;
  }

  function renderComboboxOptions(input, menu, options, emptyText) {
    const matches = Array.isArray(options) ? options.slice(0, 10) : [];
    menu.innerHTML = matches.length
      ? matches.map(option => `<button type="button" class="combobox-option" role="option" data-mailbox-value="${esc(option)}">${esc(option)}</button>`).join('')
      : `<span class="combobox-empty">${esc(emptyText || '没有匹配的邮箱')}</span>`;
    menu.querySelectorAll('[data-mailbox-value]').forEach(option => option.onclick = () => {
      input.value = option.dataset.mailboxValue || '';
      input.dispatchEvent(new Event('change', { bubbles: true }));
      setComboboxExpanded(input, menu, false);
    });
    setComboboxExpanded(input, menu, true);
  }

  async function searchMailboxOptions(input, menu) {
    const root = input.closest('[data-mailbox-combobox]');
    if (!root) return;
    const requestId = String(Date.now()) + Math.random();
    root.dataset.mailboxRequestId = requestId;
    renderComboboxOptions(input, menu, [], '加载中...');
    const params = new URLSearchParams({ q: input.value.trim(), limit: 10 });
    try {
      const data = await api('/api/verification-links/main-mailboxes?' + params);
      if (root.dataset.mailboxRequestId !== requestId) return;
      renderComboboxOptions(input, menu, data.items || []);
    } catch (_) {
      if (root.dataset.mailboxRequestId !== requestId) return;
      renderComboboxOptions(input, menu, [], '主邮箱列表加载失败');
    }
  }

  function setupMailboxCombobox(root) {
    const input = root.querySelector('[role="combobox"]');
    const menu = root.querySelector('.combobox-options');
    const clear = root.querySelector('.combobox-clear');
    if (!input || !menu || !clear || root.dataset.comboboxReady) return;
    root.dataset.comboboxReady = '1';
    clear.hidden = !input.value;
    input.addEventListener('focus', () => searchMailboxOptions(input, menu));
    input.addEventListener('input', () => {
      clear.hidden = !input.value;
      clearTimeout(root._mailboxSearchTimer);
      root._mailboxSearchTimer = setTimeout(() => searchMailboxOptions(input, menu), 220);
    });
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') setComboboxExpanded(input, menu, false);
      if (event.key === 'Enter' && !menu.hidden) {
        const first = menu.querySelector('[data-mailbox-value]');
        if (first) { event.preventDefault(); first.click(); }
      }
    });
    clear.addEventListener('click', () => {
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      input.focus();
    });
  }

  function setupMailboxComboboxes() {
    document.querySelectorAll('[data-mailbox-combobox]').forEach(setupMailboxCombobox);
    document.addEventListener('click', event => {
      if (event.target.closest('[data-mailbox-combobox]')) return;
      document.querySelectorAll('[data-mailbox-combobox]').forEach(root => {
        const input = root.querySelector('[role="combobox"]');
        const menu = root.querySelector('.combobox-options');
        if (input && menu) setComboboxExpanded(input, menu, false);
      });
    });
  }

  async function loadMainMailboxes() {
    setupMailboxComboboxes();
  }

  async function load() {
    if (!has('rows')) return;
    const params = new URLSearchParams({
      page,
      page_size: 20,
      query: has('query') ? $('query').value : '',
      status: has('status') ? $('status').value : 'all',
    });
    if (has('mainEmailFilter') && $('mainEmailFilter').value.trim()) {
      params.set('main_email', $('mainEmailFilter').value.trim());
    }
    try {
      const data = await api('/api/verification-links?' + params);
      items = data.items || [];
      totalPages = data.pagination.pages || 0;
      if (has('summary')) $('summary').textContent = `共 ${data.pagination.total} 条`;
      if (has('activeCount')) $('activeCount').textContent = data.pagination.total || 0;
      if (has('pageInfo')) $('pageInfo').textContent = totalPages ? `第 ${page}/${totalPages} 页` : '无数据';
      $('rows').innerHTML = items.length ? items.map(row => `<tr><td><input type="checkbox" data-id="${row.id}" ${selected.has(row.id) ? 'checked' : ''}></td><td>${esc(row.main_email_display)}</td><td>${esc(row.recipient_email_display)}</td><td><span class="status ${row.status}">${row.status === 'active' ? '有效' : '已过期'}</span></td><td>${row.primary_access_count || 0}</td><td>${esc(row.created_at)}</td><td>${esc(row.expires_at || '永久')}</td><td class="actions"><button class="button" data-copy="${esc(row.share_url)}">复制链接</button><button class="button" data-expire="${row.id}">改期限</button></td></tr>`).join('') : '<tr><td colspan="8" class="empty">暂无链接</td></tr>';
      document.querySelectorAll('[data-id]').forEach(element => element.onchange = () => element.checked ? selected.add(+element.dataset.id) : selected.delete(+element.dataset.id));
      document.querySelectorAll('[data-copy]').forEach(element => element.onclick = () => navigator.clipboard.writeText(element.dataset.copy).then(() => notify('链接已复制')));
      document.querySelectorAll('[data-expire]').forEach(element => element.onclick = () => changeExpiry(+element.dataset.expire));
    } catch (error) {
      notify('加载失败：' + error.message);
    }
  }

  async function changeExpiry(id) {
    const value = prompt('输入 ISO 失效时间，留空表示永久');
    if (value === null) return;
    try {
      await api('/api/verification-links/' + id, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ expires_at:value || null }) });
      notify('已更新');
      load();
    } catch (error) {
      notify('更新失败：' + error.message);
    }
  }

  async function downloadPayload(payload) {
    const response = await fetch('/api/verification-links/export', { method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
    if (!response.ok) throw Error('export_failed');
    await downloadResponse(response, 'verification-links.txt');
  }

  async function downloadResponse(response, fallbackName) {
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    const disposition = response.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename\*?=(?:UTF-8''|"?)([^";]+)/i);
    link.download = match ? decodeURIComponent(match[1]) : fallbackName;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }

  async function downloadGroups(groups) {
    const exportGroups = (groups || []).map(group => ({ main_email: group.main_email, account_id: group.account_id, record_ids: group.record_ids }));
    await downloadPayload({ groups: exportGroups });
  }

  async function exportSelected() {
    if (!selected.size) return notify('请先选择记录');
    try {
      await downloadPayload({ ids: [...selected] });
      notify('已导出选中记录');
    } catch (error) {
      notify('导出失败：' + error.message);
    }
  }

  function selectedFilesText(files) {
    const selectedFiles = [...(files || [])];
    if (!selectedFiles.length) return '未选择文件';
    if (selectedFiles.length === 1) return `已选择：${selectedFiles[0].name}`;
    const names = selectedFiles.slice(0, 3).map(file => file.name).join('、');
    const suffix = selectedFiles.length > 3 ? ` 等 ${selectedFiles.length} 个文件` : `，共 ${selectedFiles.length} 个文件`;
    return `已选择：${names}${suffix}`;
  }

  function updateFileSelections() {
    if (!has('files')) return;
    const files = $('files').files;
    const mode = window.verificationImportMode || (files.length > 1 ? 'batch' : 'single');
    const text = selectedFilesText(files);
    if (has('singleFileSelection')) {
      const active = mode === 'single' && files.length > 0;
      $('singleFileSelection').textContent = active ? text : '未选择文件';
      $('singleFileSelection').classList.toggle('selected', active);
    }
    if (has('batchFileSelection')) {
      const active = mode === 'batch' && files.length > 0;
      $('batchFileSelection').textContent = active ? text : '未选择文件';
      $('batchFileSelection').classList.toggle('selected', active);
    }
  }

  function pickImportFiles(mode) {
    if (!has('files')) return;
    window.verificationImportMode = mode;
    $('files').multiple = mode === 'batch';
    $('files').click();
  }

  async function deleteMainMailboxLinks() {
    if (!has('mainEmailFilter')) return;
    const mainEmail = $('mainEmailFilter').value.trim();
    if (!mainEmail) return notify('请先选择主邮箱');
    const message = `确认删除主邮箱 ${mainEmail} 及其全部已导入收件人邮箱？`;
    if (!confirm(message)) return;
    try {
      const data = await api('/api/verification-links/main-mailbox', {
        method: 'DELETE',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ main_email: mainEmail }),
      });
      selected.clear();
      page = 1;
      await load();
      notify(`已删除 ${data.deleted_count || 0} 条`);
    } catch (error) {
      notify('删除主邮箱失败：' + error.message);
    }
  }

  async function startImport(options = {}) {
    if (importing || !has('files')) return;
    const files = options.files || $('files').files;
    if (!files.length) return notify('请选择 TXT 文件');
    const mode = window.verificationImportMode || (files.length === 1 ? 'single' : 'batch');
    if (mode === 'single' && files.length !== 1) return notify('单个导入只能选择 1 个 TXT 文件');
    importing = true;
    const importButtons = ['import', 'batchImport', 'textImport'].map(id => $(id)).filter(Boolean);
    importButtons.forEach(button => { button.disabled = true; });
    const form = new FormData();
    [...files].forEach(file => form.append('files', file));
    form.append('mode', mode);
    form.append('auto_export', '1');
    const mainEmail = options.mainEmail !== undefined ? options.mainEmail : '';
    if (mainEmail) form.append('mainemail', mainEmail);
    if (has('expiry')) form.append('expiry', $('expiry').value);
    if (has('expiresAt') && $('expiresAt').value) form.append('expires_at', new Date($('expiresAt').value).toISOString());
    try {
      const publicBaseUrl = has('importBaseUrl') ? $('importBaseUrl').value.trim() : '';
      if (publicBaseUrl) await api('/api/verification-links/settings', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ base_url: publicBaseUrl }) });
      const response = await fetch('/api/verification-links/import', { method:'POST', body:form, credentials:'same-origin' });
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw Error(data.error_code || 'import_failed');
      }
      await downloadResponse(response, mode === 'batch' ? 'verification-links.zip' : 'api-verification-links.txt');
      notify('导入完成，文件已自动下载');
      $('files').value = '';
      updateFileSelections();
      if (options.clearText && has('textRecipients')) $('textRecipients').value = '';
      selected.clear();
      page = 1;
      load();
    } catch (error) {
      notify('导入失败：' + error.message);
    } finally {
      importing = false;
      importButtons.forEach(button => { button.disabled = false; });
    }
  }

  async function startTextImport() {
    const value = has('textRecipients') ? $('textRecipients').value.trim() : '';
    if (!value) return notify('请先输入收件人邮箱');
    const button = $('textImport');
    if (button) button.disabled = true;
    window.verificationImportMode = 'single';
    try {
      await startImport({
        files: [new File([value], 'recipients.txt', { type: 'text/plain' })],
        mainEmail: has('textMainemail') ? $('textMainemail').value : '',
        clearText: true,
      });
    } finally {
      if (button) button.disabled = false;
    }
  }

  function setupManagementPage() {
    if (!has('rows')) return;
    if (has('refresh')) $('refresh').onclick = () => { page = 1; load(); };
    if (has('query')) $('query').onkeydown = event => { if (event.key === 'Enter') { page = 1; load(); } };
    if (has('mainEmailFilter')) {
      $('mainEmailFilter').addEventListener('change', () => { page = 1; load(); });
      $('mainEmailFilter').addEventListener('keydown', event => { if (event.key === 'Enter') { page = 1; load(); } });
    }
    if (has('status')) $('status').onchange = () => { page = 1; load(); };
    if (has('prev')) $('prev').onclick = () => { if (page > 1) { page--; load(); } };
    if (has('next')) $('next').onclick = () => { if (page < totalPages) { page++; load(); } };
    if (has('selectAll')) $('selectAll').onchange = event => document.querySelectorAll('[data-id]').forEach(element => { element.checked = event.target.checked; event.target.checked ? selected.add(+element.dataset.id) : selected.delete(+element.dataset.id); });
    if (has('deleteMainEmail')) $('deleteMainEmail').onclick = deleteMainMailboxLinks;
    if (has('delete')) $('delete').onclick = async () => { if (!selected.size) return notify('请先选择记录'); if (!confirm('确认删除选中链接？')) return; try { await api('/api/verification-links/batch-delete', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:[...selected]}) }); selected.clear(); notify('已删除'); load(); } catch (error) { notify('删除失败：' + error.message); } };
    if (has('export')) $('export').onclick = exportSelected;
    load();
  }

  function setupImportPage() {
    if (has('expiry')) $('expiry').onchange = event => { if (has('expiresAt')) $('expiresAt').hidden = event.target.value !== 'custom'; };
    if (has('files')) $('files').onchange = updateFileSelections;
    if (has('singleFilePick')) $('singleFilePick').onclick = () => pickImportFiles('single');
    if (has('batchFilePick')) $('batchFilePick').onclick = () => pickImportFiles('batch');
    if (has('import')) $('import').onclick = () => { window.verificationImportMode = 'single'; updateFileSelections(); startImport(); };
    if (has('batchImport')) $('batchImport').onclick = () => { window.verificationImportMode = 'batch'; updateFileSelections(); startImport(); };
    if (has('textImport')) $('textImport').onclick = startTextImport;
    if (has('saveSettings')) $('saveSettings').onclick = async () => { try { const data = await api('/api/verification-links/settings', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({base_url:has('baseUrl') ? $('baseUrl').value : ''}) }); if (has('effective')) $('effective').textContent = '当前：' + data.effective_base_url; notify('设置已保存'); } catch (error) { notify('保存失败：' + error.message); } };
    if (has('baseUrl') || has('importBaseUrl')) {
      (async () => {
        try {
          const data = await api('/api/verification-links/settings');
          if (has('baseUrl')) $('baseUrl').value = data.configured_base_url || '';
          if (has('importBaseUrl')) $('importBaseUrl').value = data.configured_base_url || '';
          if (has('effective')) $('effective').textContent = '当前：' + data.effective_base_url;
        } catch (_) {}
      })();
    }
  }

  loadMainMailboxes();
  setupImportPage();
  setupManagementPage();
})();
