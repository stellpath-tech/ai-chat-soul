(() => {
  const $ = id => document.getElementById(id);
  const pageSize = 12;
  let passcode = '', categories = [], pushType = 'greeting', deliveryScene = '';
  let offset = 0, total = 0, items = [], selected = null, dirty = false, busy = false;
  let listController = null, listRequest = 0;
  let retryUploads = null;
  const ctaHints = {
    greeting: '卡片按钮：和满仓聊聊 → 对应聊天消息', weather: '卡片按钮：知道啦 / 查看天气',
    diary: '卡片按钮：看看今天的日记 → 当日日记', recall: '卡片按钮：回来坐坐 → 首页',
  };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const category = (type = pushType) => categories.find(item => item.type === type);
  const scenes = (type = pushType) => category(type)?.groups.flatMap(group => group.scenes) || [];
  const scene = (value = deliveryScene, type = pushType) => scenes(type).find(item => item.value === value);
  const api = (path, options) => AdminConsole.request(passcode, path, options);

  function message(text, failure = false) {
    $('pageMessage').textContent = text;
    $('pageMessage').className = 'message' + (failure ? ' failure' : '') + (text ? '' : ' hidden');
  }
  function showError(error) {
    if (error.name === 'AbortError') return;
    if (error.status === 401) {
      AdminConsole.clearPasscode(); passcode = '';
      $('appView').classList.add('hidden'); $('loginView').classList.remove('hidden');
      $('loginError').textContent = error.message;
    } else { message(error.message, true); }
  }
  function canLeave() {
    if (busy) return false;
    return !dirty || confirm('有尚未保存的修改，确定放弃吗？');
  }
  function setBusy(value) {
    busy = value;
    $('editorFields').disabled = value;
    $('uploadButton').disabled = value;
    $('imageFile').disabled = value;
    $('newButton').disabled = value || Boolean(scene()?.legacy);
    document.querySelectorAll('[data-remove-image]').forEach(button => { button.disabled = value; });
  }
  function renderNavigation() {
    $('typeTabs').innerHTML = categories.map(item => `<button type="button" class="type-tab" data-type="${escape(item.type)}" aria-current="${item.type === pushType}">${escape(item.label)}<span>${item.total}</span></button>`).join('');
    $('sceneHeading').textContent = category().sceneLabel;
    $('sceneNav').innerHTML = `<button type="button" class="scene-button" data-scene="" aria-current="${!deliveryScene}">全部${escape(category().label)}<small>${category().total}</small></button>` + category().groups.map(group => `
      <section class="scene-group"><h3>${escape(group.label)}</h3>${group.scenes.map(item => `
        <button type="button" class="scene-button" data-scene="${escape(item.value)}" aria-current="${item.value === deliveryScene}" title="${escape(item.description)}"><span>${escape(item.label)}</span><small>${item.total}</small></button>
      `).join('')}</section>`).join('');
    $('categoryLabel').textContent = category().label;
    $('sceneTitle').textContent = scene()?.label || '全部' + category().label;
    $('sceneDescription').textContent = scene()?.description || category().description;
    const chosen = scene() ? [scene()] : scenes();
    const count = key => chosen.reduce((sum, item) => sum + (item[key] || 0), 0);
    $('metricLabel').textContent = category().label + (scene() ? ' · ' + scene().label : '');
    $('totalCount').textContent = count('total');
    $('statusSummary').textContent = `启用 ${count('enabledCount')} · 停用 ${count('total') - count('enabledCount')} · 待配图 ${count('missingImages')}`;
    $('newButton').textContent = scene() && pushType === 'greeting' ? '在此时间段新增' : '新增文案';
    $('newButton').disabled = busy || Boolean(scene()?.legacy);
  }

  async function loadContents(preferredId = null) {
    const request = ++listRequest;
    listController?.abort(); listController = new AbortController();
    const params = new URLSearchParams({pushType, limit: pageSize, offset});
    if (deliveryScene) params.set('deliveryScene', deliveryScene);
    if ($('enabledFilter').value) params.set('enabled', $('enabledFilter').value);
    if ($('keywordFilter').value.trim()) params.set('keyword', $('keywordFilter').value.trim());
    $('contentRows').innerHTML = '<p class="empty-state">正在加载文案…</p>';
    $('prevButton').disabled = $('nextButton').disabled = true;
    $('retryButton').classList.add('hidden');
    try {
      const data = await api('/api/admin/push-contents?' + params, {signal: listController.signal});
      if (request !== listRequest) return;
      items = data.items; total = data.total;
      if (!items.length && offset > 0) { offset = Math.max(0, Math.ceil(total / pageSize) - 1) * pageSize; return loadContents(preferredId); }
      renderContents();
      if (preferredId) {
        const item = items.find(item => item.id === preferredId);
        if (item) openEditor(item);
        else if (selected?.id === preferredId) openEditor(selected);
      }
      if ($('pageMessage').classList.contains('failure')) message('');
      return true;
    } catch (error) {
      if (request !== listRequest || error.name === 'AbortError') return;
      items = []; $('contentRows').innerHTML = '<p class="empty-state">文案暂时无法加载，请重试。</p>';
      $('pageInfo').textContent = '';
      $('retryButton').classList.remove('hidden'); showError(error);
      return false;
    }
  }
  function renderContents() {
    $('pageInfo').textContent = total ? `第 ${offset / pageSize + 1} 页 · ${offset + 1}–${offset + items.length} / ${total}` : '共 0 条';
    $('prevButton').disabled = offset === 0; $('nextButton').disabled = offset + pageSize >= total;
    $('contentRows').innerHTML = items.length ? items.map(item => `
      <button type="button" class="content-row" data-content-id="${item.id}" aria-current="${selected?.id === item.id}">
        <span class="content-copy">
          ${!deliveryScene ? `<span class="scene-tag">${escape(scene(item.deliveryScene)?.label || '待归类')}</span>` : ''}
          <strong>${escape(item.title)}</strong><span class="content-body">${escape(item.body)}</span>
          <span class="content-meta"><span class="pill ${item.enabled ? 'fixed' : 'none'}">${item.enabled ? '启用' : '停用'}</span><span>${item.images.length ? item.images.length + ' 张插图' : '待配图'}</span><span>${escape(item.contentNo)}</span></span>
        </span>
        ${item.images[0]?.imageUrl ? `<img class="content-thumb" src="${escape(item.images[0].imageUrl)}" alt="" loading="lazy">` : ''}
      </button>`).join('') : '<div class="empty-state">当前分类暂无符合条件的文案。<br>可以调整筛选，或点击右上角新增文案。</div>';
  }
  async function refresh(preferredId = null) {
    const data = await api('/api/admin/push-contents/catalog');
    categories = data.categories;
    if (!category()) pushType = categories[0].type;
    if (deliveryScene && !scene()) deliveryScene = '';
    renderNavigation(); return loadContents(preferredId);
  }
  function closeEditor() {
    selected = null; dirty = false;
    $('editorView').classList.add('hidden'); $('emptyEditor').classList.remove('hidden');
    if (items.length) renderContents();
  }
  function sceneOptions(type, value) {
    const options = category(type).groups.map(group => {
      const valid = group.scenes.filter(item => !item.legacy);
      return valid.length ? `<optgroup label="${escape(group.label)}">${valid.map(item => `<option value="${escape(item.value)}">${escape(item.label)} · ${escape(item.description)}</option>`).join('')}</optgroup>` : '';
    }).join('');
    $('deliveryScene').innerHTML = '<option value="">请选择' + category(type).sceneLabel + '</option>' + options;
    $('deliveryScene').value = value || (type === 'diary' ? 'DIARY_READY' : '');
    $('deliverySceneLabel').textContent = category(type).sceneLabel;
    $('deliveryScene').disabled = type === 'diary';
    updateFormHint();
  }
  function updateFormHint() {
    const type = $('pushType').value;
    const chosen = scene($('deliveryScene').value, type);
    $('formSceneHint').textContent = chosen?.description || '选择分类后，文案会进入相应的发送文案池。';
    $('editorContext').textContent = category(type).label + (chosen ? ' / ' + chosen.label : '');
    $('ctaHint').textContent = ctaHints[type];
  }
  function openEditor(item = null) {
    selected = item; dirty = false; $('contentForm').reset();
    retryUploads = null;
    $('formError').textContent = ''; $('uploadStatus').textContent = ''; $('imageFile').value = '';
    const type = item?.pushType || pushType;
    $('pushType').innerHTML = categories.map(entry => `<option value="${entry.type}">${escape(entry.label)}</option>`).join('');
    $('pushType').value = type;
    sceneOptions(type, item?.deliveryScene || deliveryScene);
    $('title').value = item?.title || ''; $('body').value = item?.body || '';
    $('contentNo').value = item?.contentNo || ''; $('enabled').checked = item ? item.enabled : true;
    $('formTitle').textContent = item ? '编辑文案' : '新增文案';
    $('editState').textContent = item ? (item.enabled ? '启用' : '停用') : '待保存';
    $('disableButton').classList.toggle('hidden', !item?.enabled);
    $('editorView').classList.remove('hidden'); $('emptyEditor').classList.add('hidden');
    $('imageSection').classList.toggle('hidden', !item); $('saveFirstHint').classList.toggle('hidden', Boolean(item));
    renderImages(item?.images || []); renderContents();
    if (window.innerWidth <= 1200) $('editorPanel').scrollIntoView({behavior: 'smooth', block: 'start'});
  }
  function renderImages(images) {
    $('imageCount').textContent = `· ${images.length} 张`;
    $('images').innerHTML = images.map(image => `<div class="image-item">
      <button type="button" class="image-thumb" data-preview-image="${image.imageId}" aria-label="预览插图 ${image.imageId}"><img src="${escape(image.imageUrl)}" alt="文案插图" loading="lazy"></button>
      <button type="button" class="button danger" data-remove-image="${image.imageId}">移除</button></div>`).join('');
  }
  async function saveContent(event) {
    event.preventDefault(); if (busy) return;
    const body = {pushType: $('pushType').value, deliveryScene: $('deliveryScene').value,
      title: $('title').value.trim(), body: $('body').value.trim(), enabled: $('enabled').checked};
    if (selected) body.contentNo = selected.contentNo;
    if (!body.deliveryScene || !body.title || !body.body) { $('formError').textContent = '请选择分类并填写标题、正文。'; return; }
    setBusy(true); $('formError').textContent = ''; $('saveButton').textContent = '保存中…';
    try {
      const result = await api(selected ? '/api/admin/push-contents/' + selected.id : '/api/admin/push-contents', {method: selected ? 'PUT' : 'POST', body: JSON.stringify(body)});
      const savedId = selected?.id || result.id;
      if (selected) selected = {...selected, ...body};
      dirty = false; pushType = body.pushType; deliveryScene = body.deliveryScene; offset = 0;
      $('enabledFilter').value = ''; $('keywordFilter').value = '';
      if (await refresh(savedId)) message('文案已保存，可继续添加或维护插图。');
    } catch (error) { $('formError').textContent = error.message; if (error.status === 401) showError(error); }
    finally { setBusy(false); $('saveButton').textContent = '保存文案'; }
  }
  async function disableContent() {
    if (!selected || !canLeave() || !confirm('停用后，这条文案将不再参与新的推送。确认停用？')) return;
    const id = selected.id; setBusy(true);
    try {
      await api('/api/admin/push-contents/' + id, {method: 'DELETE'});
      closeEditor(); await refresh(id); message('文案已停用。编辑时勾选“启用这条文案”即可恢复。');
    } catch (error) { showError(error); } finally { setBusy(false); }
  }
  async function uploadImages() {
    if (!selected || busy) return;
    const files = retryUploads || Array.from($('imageFile').files);
    if (!files.length) { $('uploadStatus').textContent = '请先选择图片。'; return; }
    if (files.some(file => file.size > 10 * 1024 * 1024)) { $('uploadStatus').textContent = '每张图片不能超过 10 MB。'; return; }
    setBusy(true); const id = selected.id; let completed = 0;
    try {
      for (const file of files) {
        $('uploadStatus').textContent = `正在上传 ${completed + 1} / ${files.length}`;
        const form = new FormData(); form.append('file', file);
        const result = await api('/api/admin/push-contents/' + id + '/images', {method: 'POST', body: form});
        selected.images.push(result); completed++; renderImages(selected.images); setBusy(true);
      }
      retryUploads = null; $('imageFile').value = ''; $('uploadStatus').textContent = `已上传 ${completed} 张图片。`;
    } catch (error) {
      // Keep only failed / unattempted files selected, so retry cannot duplicate successful images.
      retryUploads = files.slice(completed);
      $('uploadStatus').textContent = `已上传 ${completed} 张；${error.message} 可以重试剩余图片。`;
      if (error.status === 401) showError(error);
    } finally { setBusy(false); renderContents(); }
    try { categories = (await api('/api/admin/push-contents/catalog')).categories; renderNavigation(); } catch (error) { showError(error); }
  }
  async function removeImage(imageId) {
    if (!selected || busy || !confirm('移除后，新的推送将不再选择这张插图。确认移除？')) return;
    setBusy(true);
    try {
      await api('/api/admin/push-contents/' + selected.id + '/images/' + imageId, {method: 'DELETE'});
      selected.images = selected.images.filter(image => image.imageId !== imageId);
      renderImages(selected.images); renderContents();
      categories = (await api('/api/admin/push-contents/catalog')).categories; renderNavigation();
    } catch (error) { showError(error); } finally { setBusy(false); }
  }
  async function login(candidate) {
    $('loginButton').disabled = true; $('loginError').textContent = '';
    try {
      await AdminConsole.verify(candidate); passcode = candidate.trim();
      $('loginView').classList.add('hidden'); $('appView').classList.remove('hidden');
      await refresh();
    } catch (error) { $('loginError').textContent = error.message; showError(error); $('retryButton').classList.remove('hidden'); }
    finally { $('loginButton').disabled = false; }
  }

  $('loginForm').addEventListener('submit', event => { event.preventDefault(); login($('passcodeInput').value); });
  $('logoutButton').addEventListener('click', () => { if (!canLeave()) return; AdminConsole.clearPasscode(); dirty = false; location.reload(); });
  $('typeTabs').addEventListener('click', async event => {
    const button = event.target.closest('[data-type]'); if (!button || !canLeave()) return;
    pushType = button.dataset.type; deliveryScene = pushType === 'diary' ? 'DIARY_READY' : '';
    offset = 0; $('keywordFilter').value = ''; $('enabledFilter').value = ''; closeEditor();
    renderNavigation(); message(''); await loadContents();
  });
  $('sceneNav').addEventListener('click', async event => {
    const button = event.target.closest('[data-scene]'); if (!button || !canLeave()) return;
    deliveryScene = button.dataset.scene; offset = 0; closeEditor(); renderNavigation(); message(''); await loadContents();
  });
  $('contentRows').addEventListener('click', event => {
    const button = event.target.closest('[data-content-id]'); if (!button || !canLeave()) return;
    openEditor(items.find(item => item.id === Number(button.dataset.contentId)));
  });
  $('newButton').addEventListener('click', () => { if (canLeave()) { openEditor(); $('title').focus(); } });
  $('cancelButton').addEventListener('click', () => { if (canLeave()) closeEditor(); });
  $('contentForm').addEventListener('input', () => { dirty = true; });
  $('pushType').addEventListener('change', () => sceneOptions($('pushType').value, ''));
  $('deliveryScene').addEventListener('change', updateFormHint);
  $('contentForm').addEventListener('submit', saveContent);
  $('disableButton').addEventListener('click', disableContent);
  $('uploadButton').addEventListener('click', uploadImages);
  $('imageFile').addEventListener('change', () => { retryUploads = null; $('uploadStatus').textContent = ''; });
  $('images').addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-image]');
    if (remove) return removeImage(Number(remove.dataset.removeImage));
    const preview = event.target.closest('[data-preview-image]');
    if (preview) { $('previewImage').src = selected.images.find(image => image.imageId === Number(preview.dataset.previewImage)).imageUrl; $('imagePreview').showModal(); }
  });
  $('closePreview').addEventListener('click', () => $('imagePreview').close());
  function filter() { if (!canLeave()) return; offset = 0; closeEditor(); message(''); loadContents(); }
  $('searchButton').addEventListener('click', filter);
  $('enabledFilter').addEventListener('change', filter);
  $('keywordFilter').addEventListener('keydown', event => { if (event.key === 'Enter') filter(); });
  $('keywordFilter').addEventListener('search', filter);
  $('prevButton').addEventListener('click', () => { if (canLeave()) { offset = Math.max(0, offset - pageSize); closeEditor(); loadContents(); } });
  $('nextButton').addEventListener('click', () => { if (canLeave()) { offset += pageSize; closeEditor(); loadContents(); } });
  $('retryButton').addEventListener('click', () => refresh().catch(showError));
  window.addEventListener('beforeunload', event => { if (dirty || busy) { event.preventDefault(); event.returnValue = ''; } });
  const stored = AdminConsole.storedPasscode(); if (stored) login(stored);
})();
