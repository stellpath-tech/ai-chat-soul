// Run against an isolated preview server: NODE_PATH=<jsdom node_modules> node tests/push_content_ui.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {JSDOM} = require('jsdom');
const base = process.env.PUSH_ADMIN_TEST_URL || 'http://127.0.0.1:9876';
const dom = new JSDOM(fs.readFileSync('channel/web/push_contents.html', 'utf8'), {url: base + '/push-contents', runScripts: 'outside-only'});
const w = dom.window;
const $ = id => w.document.getElementById(id);
const submit = id => $(id).dispatchEvent(new w.Event('submit', {bubbles:true, cancelable:true}));
const input = (id, value) => { $(id).value = value; $(id).dispatchEvent(new w.Event('input', {bubbles:true})); };
const change = (id, value) => { input(id, value); $(id).dispatchEvent(new w.Event('change', {bubbles:true})); };
let allowDiscard = false, forceListFailure = false;
w.confirm = () => allowDiscard;
w.HTMLElement.prototype.scrollIntoView = function () {};
w.Headers = Headers; w.AbortController = AbortController;
w.fetch = async (path, options = {}) => {
  if (forceListFailure && String(path).startsWith('/api/admin/push-contents?')) throw Error('offline');
  if (options.body instanceof w.FormData) {
    const body = new FormData();
    for (const [key,value] of options.body) {
      const bytes = await new Promise(resolve => { const reader = new w.FileReader(); reader.onload = () => resolve(reader.result); reader.readAsArrayBuffer(value); });
      body.append(key, new Blob([bytes], {type:value.type}), value.name);
    }
    options = {...options, body};
  }
  return fetch(new URL(path, base), {...options, headers: Object.fromEntries(options.headers.entries())});
};
w.eval(fs.readFileSync('channel/web/static/js/admin-auth.js', 'utf8'));
w.eval(fs.readFileSync('channel/web/static/js/push-contents.js', 'utf8'));
async function until(check) {
  for (let i=0; i<150; i++) { if (check()) return; await new Promise(resolve=>setTimeout(resolve,30)); }
  throw Error('UI did not reach expected state: ' + $('loginError').textContent + ' / ' + $('pageMessage').textContent + ' / ' + $('formError').textContent);
}
const clickScene = value => w.document.querySelector('[data-scene="'+value+'"]').click();
const clickType = value => w.document.querySelector('[data-type="'+value+'"]').click();

(async () => {
  assert.match(w.document.querySelector('.token-link').href, /feishu.cn\/wiki\//);
  input('passcodeInput','wrong'); submit('loginForm');
  await until(() => $('loginError').textContent.includes('口令无效'));
  input('passcodeInput','preview-only'); submit('loginForm');
  await until(() => w.document.querySelector('[data-type="weather"]'));
  assert.equal(w.document.querySelectorAll('#sceneNav [data-scene]').length,18);
  clickScene('GREETING_0730');
  await until(() => $('contentRows').textContent.includes('AM-0730'));
  assert.ok(Array.from(w.document.querySelectorAll('.content-meta')).every(row=>row.textContent.includes('AM-0730-')));
  $('newButton').click();
  assert.equal($('deliveryScene').value,'GREETING_0730');
  assert.equal($('deliveryScene').tagName,'SELECT');
  input('title','早餐测试专用'); input('body','一起吃点热乎的早餐。');
  clickType('weather');
  assert.equal($('title').value,'早餐测试专用'); // Discard was declined.
  assert.equal(w.document.querySelector('[data-type][aria-current="true"]').dataset.type,'greeting');
  submit('contentForm');
  await until(() => $('contentNo').value.startsWith('AM-0730-') && $('formTitle').textContent==='编辑文案');
  const createdNo = $('contentNo').value;
  assert.equal($('title').value,'早餐测试专用');
  assert.ok(!$('imageSection').classList.contains('hidden'));
  const file = new w.File([Buffer.from('89504e470d0a1a0a','hex'),Buffer.from('fixture')], 'test.png', {type:'image/png'});
  Object.defineProperty($('imageFile'),'files',{configurable:true,value:[file,file]});
  $('uploadButton').click();
  await until(() => $('uploadStatus').textContent==='已上传 2 张图片。');
  assert.equal(w.document.querySelectorAll('[data-remove-image]').length,2);
  allowDiscard=true;
  w.document.querySelector('[data-remove-image]').click();
  await until(() => w.document.querySelectorAll('[data-remove-image]').length===1 && !$('uploadButton').disabled);
  $('disableButton').click();
  await until(() => $('editState').textContent==='停用');
  $('enabled').checked=true; submit('contentForm');
  await until(() => $('editState').textContent==='启用');
  clickType('weather'); await until(() => $('categoryLabel').textContent==='天气预警');
  assert.equal(w.document.querySelectorAll('#sceneNav [data-scene]').length,13);
  clickScene('WEATHER_TYPHOON'); await until(() => $('contentRows').textContent.includes('W-TYPHOON'));
  $('newButton').click(); assert.equal($('deliverySceneLabel').textContent,'预警类型'); assert.equal($('deliveryScene').value,'WEATHER_TYPHOON');
  clickType('recall'); await until(() => $('categoryLabel').textContent==='用户召回');
  assert.equal(w.document.querySelectorAll('#sceneNav [data-scene]').length,4);
  clickScene('RECALL_15'); await until(() => $('contentRows').textContent.includes('RECALL-15'));
  $('newButton').click(); assert.equal($('deliveryScene').value,'RECALL_15');
  clickType('diary'); await until(() => $('contentRows').textContent.includes('DIARY-'));
  $('newButton').click(); assert.equal($('deliveryScene').value,'DIARY_READY'); assert.ok($('deliveryScene').disabled);
  $('cancelButton').click();
  input('keywordFilter','没有这样的标题__'); $('searchButton').click();
  await until(() => $('pageInfo').textContent==='共 0 条');
  assert.ok($('prevButton').disabled && $('nextButton').disabled);
  input('keywordFilter',''); forceListFailure=true; $('searchButton').click();
  await until(() => $('pageMessage').textContent.includes('网络连接失败'));
  assert.ok(!$('appView').classList.contains('hidden')); // Not a fake login failure.
  forceListFailure=false; $('retryButton').click();
  await until(() => $('contentRows').textContent.includes('DIARY-'));
  const complaint = new JSDOM(fs.readFileSync('channel/web/complaints.html','utf8'),{url:base+'/complaints',runScripts:'outside-only'});
  complaint.window.eval(fs.readFileSync('channel/web/static/js/admin-auth.js','utf8'));
  complaint.window.localStorage.setItem('complaint-admin-passcode-v1','preview-only');
  assert.equal(complaint.window.AdminConsole.storedPasscode(),'preview-only');
  assert.equal(w.document.querySelector('link[rel=stylesheet]').getAttribute('href'),complaint.window.document.querySelector('link[rel=stylesheet]').getAttribute('href'));
  console.log('PASS: login/help, scene filters, prefilled create, auto ID, dirty guard, multi-image upload/removal, disable/restore, all four categories, empty state, network retry, shared theme/auth. Created ' + createdNo);
  complaint.window.close(); dom.window.close();
})().catch(error=>{console.error(error);dom.window.close();process.exitCode=1;});
