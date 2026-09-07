window.AdminConsole = (() => {
  const storageKey = 'ommo-admin-passcode-v1';
  const oldKeys = ['complaint-admin-passcode-v1', 'push-admin-passcode'];
  function storedPasscode() {
    try { return localStorage.getItem(storageKey) || oldKeys.map(key => localStorage.getItem(key)).find(Boolean) || ''; }
    catch { return ''; }
  }
  function savePasscode(value) {
    try {
      localStorage.setItem(storageKey, value);
      oldKeys.forEach(key => localStorage.removeItem(key));
    } catch { /* The current login still works with storage disabled. */ }
  }
  function clearPasscode() {
    try { [storageKey, ...oldKeys].forEach(key => localStorage.removeItem(key)); } catch {}
  }
  async function request(passcode, path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('x-admin-passcode', passcode);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    let response;
    try { response = await fetch(path, {...options, headers}); }
    catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error('网络连接失败，请稍后重试。');
    }
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload?.success) {
      const error = new Error(response.status === 401 ? '口令无效或已过期，请重新获取访问口令。' : payload?.message || '服务暂时不可用，请稍后重试。');
      error.status = response.status;
      throw error;
    }
    return payload.data;
  }
  async function verify(passcode) {
    if (!passcode.trim()) throw new Error('请输入访问口令。');
    await request(passcode.trim(), '/api/admin/complaints/auth', {method: 'POST'});
    savePasscode(passcode.trim());
  }
  return {request, verify, storedPasscode, clearPasscode};
})();
