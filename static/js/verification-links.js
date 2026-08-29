(function () {
  'use strict';
  const $ = id => document.getElementById(id);
  let page = 1, totalPages = 0, items = [], importing = false;
  const selected = new Set();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[char]));
  const notify = message => { const element = $('toast'); element.textContent = message; element.classList.add('show'); setTimeout(() => element.classList.remove('show'), 2400); };
  async function api(url, options) {
    const response = await fetch(url, Object.assign({ credentials: 'same-origin' }, options || {}));
    const data = await response.json().catch(() => ({}));
    if (!response.ok || data.success === false) throw Error(data.error_code || 'request_failed');
    return data;
  }
  let mailboxOptions = [];
  function setComboboxExpanded(input, menu, expanded) {
    input.setAttribute('aria-expanded', expanded ? 'true' : 'false');
    menu.hidden = !expanded;
  }
  function renderComboboxOptions(input, menu) {
    const query = input.value.trim().toLowerCase();
    const matches = mailboxOptions.filter(option => !query || option.toLowerCase().includes(query));
    menu.innerHTML = matches.length
      ? matches.map(option => `<button type="button" class="combobox-option" role="option" data-mailbox-value="${esc(option)}">${esc(option)}</button>`).join('')
      : '<span class="combobox-empty">没有匹配的邮箱</span>';
    menu.querySelectorAll('[data-mailbox-value]').forEach(option => option.onclick = () => {
      input.value = option.dataset.mailboxValue || '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      setComboboxExpanded(input, menu, false);
    });
    setComboboxExpanded(input, menu, true);
  }
  function setupMailboxCombobox(root) {
    const input = root.querySelector('[role="combobox"]');
    const menu = root.querySelector('.combobox-options');
    const clear = root.querySelector('.combobox-clear');
    if (!input || !menu || !clear) return;
    input.addEventListener('focus', () => renderComboboxOptions(input, menu));
    input.addEventListener('input', () => {
      clear.hidden = !input.value;
      renderComboboxOptions(input, menu);
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
    try {
      const data = await api('/api/accounts?limit=10000&include_untagged=true');
      const seen = new Set();
      const options = [];
      (data.accounts || []).forEach(account => {
        const addresses = [account.email, ...(account.aliases || [])];
        addresses.forEach(address => {
          const value = String(address || '').trim();
          const key = value.toLowerCase();
          if (!value || seen.has(key)) return;
          seen.add(key);
          options.push(value);
        });
      });
      mailboxOptions = options;
    } catch (_) {
      notify('主邮箱列表加载失败，请手动刷新页面');
    }
  }
  async function load() {
    const params = new URLSearchParams({ page, page_size: 50, query: $('query').value, status: $('status').value });
    try {
      const data = await api('/api/verification-links?' + params); items = data.items || []; totalPages = data.pagination.pages || 0;
      $('summary').textContent = `共 ${data.pagination.total} 条`; $('pageInfo').textContent = totalPages ? `第 ${page}/${totalPages} 页` : '无数据';
      $('rows').innerHTML = items.length ? items.map(row => `<tr><td><input type="checkbox" data-id="${row.id}" ${selected.has(row.id) ? 'checked' : ''}></td><td>${esc(row.main_email_display)}</td><td>${esc(row.recipient_email_display)}</td><td><span class="status ${row.status}">${row.status === 'active' ? '有效' : '已过期'}</span></td><td>${row.primary_access_count || 0}</td><td>${esc(row.created_at)}</td><td>${esc(row.expires_at || '永久')}</td><td class="actions"><button class="button" data-copy="${esc(row.share_url)}">复制链接</button><button class="button" data-expire="${row.id}">改期限</button></td></tr>`).join('') : '<tr><td colspan="8" class="empty">暂无链接</td></tr>';
      document.querySelectorAll('[data-id]').forEach(element => element.onchange = () => element.checked ? selected.add(+element.dataset.id) : selected.delete(+element.dataset.id));
      document.querySelectorAll('[data-copy]').forEach(element => element.onclick = () => navigator.clipboard.writeText(element.dataset.copy).then(() => notify('链接已复制')));
      document.querySelectorAll('[data-expire]').forEach(element => element.onclick = () => changeExpiry(+element.dataset.expire));
    } catch (error) { notify('加载失败：' + error.message); }
  }
  async function changeExpiry(id) { const value = prompt('输入 ISO 失效时间，留空表示永久'); if (value === null) return; try { await api('/api/verification-links/' + id, { method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ expires_at:value || null }) }); notify('已更新'); load(); } catch (error) { notify('更新失败：' + error.message); } }
  async function downloadGroups(groups) {
    const exportGroups = (groups || []).map(group => ({ main_email: group.main_email, account_id: group.account_id, record_ids: group.record_ids }));
    const response = await fetch('/api/verification-links/export', { method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ groups: exportGroups }) });
    if (!response.ok) throw Error('export_failed');
    const blob = await response.blob(); const link = document.createElement('a'); link.href = URL.createObjectURL(blob);
    const disposition = response.headers.get('Content-Disposition') || ''; const match = disposition.match(/filename\*?=(?:UTF-8''|\"?)([^\";]+)/i);
    link.download = match ? decodeURIComponent(match[1]) : 'verification-links.txt'; link.click(); setTimeout(() => URL.revokeObjectURL(link.href), 1000);
  }
  async function startImport(options = {}) {
    if (importing) return; const files = options.files || $('files').files; if (!files.length) return notify('请选择 TXT 文件'); importing = true; const importButton = $('import'); if (importButton) importButton.disabled = true;
    const form = new FormData(); [...files].forEach(file => form.append('files', file)); form.append('mode', window.verificationImportMode || (files.length === 1 ? 'single' : 'batch'));
    const mainEmail = options.mainEmail !== undefined ? options.mainEmail : $('mainemail').value;
    if (window.verificationImportMode !== 'batch' && mainEmail) form.append('mainemail', mainEmail); form.append('expiry', $('expiry').value); if ($('expiresAt').value) form.append('expires_at', new Date($('expiresAt').value).toISOString());
    try {
      const publicBaseUrl = $('importBaseUrl').value.trim();
      if (publicBaseUrl) await api('/api/verification-links/settings', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ base_url: publicBaseUrl }) });
      const response = await fetch('/api/verification-links/import', { method:'POST', body:form, credentials:'same-origin' }); const data = await response.json().catch(() => ({}));
      if (!response.ok || data.success === false) throw Error(data.error_code || 'import_failed');
      await downloadGroups(data.groups || []); notify('导入完成，文件已自动下载'); $('files').value = ''; if (options.clearText) $('textRecipients').value = ''; selected.clear(); page = 1; load();
    } catch (error) { notify('导入失败：' + error.message); } finally { importing = false; if (importButton) importButton.disabled = false; }
  }
  async function startTextImport() {
    const value = $('textRecipients').value.trim();
    if (!value) return notify('请先输入收件人邮箱');
    const button = $('textImport');
    if (button) button.disabled = true;
    window.verificationImportMode = 'single';
    try {
      await startImport({
        files: [new File([value], 'recipients.txt', { type: 'text/plain' })],
        mainEmail: $('textMainemail').value,
        clearText: true,
      });
    } finally {
      if (button) button.disabled = false;
    }
  }
  $('refresh').onclick = load; $('query').onkeydown = event => { if (event.key === 'Enter') { page = 1; load(); } }; $('status').onchange = () => { page = 1; load(); };
  $('prev').onclick = () => { if (page > 1) { page--; load(); } }; $('next').onclick = () => { if (page < totalPages) { page++; load(); } };
  $('selectAll').onchange = event => document.querySelectorAll('[data-id]').forEach(element => { element.checked = event.target.checked; event.target.checked ? selected.add(+element.dataset.id) : selected.delete(+element.dataset.id); });
  $('delete').onclick = async () => { if (!selected.size) return notify('请先选择记录'); if (!confirm('确认删除选中链接？')) return; try { await api('/api/verification-links/batch-delete', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ids:[...selected]}) }); selected.clear(); notify('已删除'); load(); } catch (error) { notify('删除失败：' + error.message); } };
  $('expiry').onchange = event => $('expiresAt').hidden = event.target.value !== 'custom'; $('files').onchange = () => { if (window.verificationImportMode === 'batch') startImport(); }; $('import').onclick = () => { window.verificationImportMode = 'single'; startImport(); };
  $('batchImport').onclick = () => { window.verificationImportMode = 'batch'; $('files').click(); };
  $('textImport').onclick = startTextImport;
  $('manage').onclick = () => { $('management').classList.toggle('open'); if ($('management').classList.contains('open')) load(); };
  $('saveSettings').onclick = async () => { try { const data = await api('/api/verification-links/settings', { method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({base_url:$('baseUrl').value}) }); $('effective').textContent = '当前：' + data.effective_base_url; notify('设置已保存'); } catch (error) { notify('保存失败：' + error.message); } };
  (async () => { try { const data = await api('/api/verification-links/settings'); $('baseUrl').value = data.configured_base_url || ''; $('importBaseUrl').value = data.configured_base_url || ''; $('effective').textContent = '当前：' + data.effective_base_url; } catch (_) {} })(); loadMainMailboxes(); load();
})();
