/* Application state and shared DOM helpers. */
const app = document.querySelector('#app');
const modalRoot = document.querySelector('#modal-root');
const selectionRoot = document.querySelector('#selection-root');
let modalReturnFocus = null;
let paperOpenToken = 0;
let selectionRequestToken = 0;
let chatRequestToken = 0;
let providerModelRequestToken = 0;
const translationRequests = new Set();
const state = {
  page: 'library', papers: [], settings: null, error: '', query: '', dryRun: false,
  detail: null, modal: null, uploadTab: 'file', uploadBusy: false, settingsDraft: null,
  providerModels: {},
  openPapers: [], activeDocumentId: null, homePage: 'library', modelPicker: null,
  readerTab: 'toc', readerView: 'reflow', sidebarOpen: window.innerWidth > 680,
  showTranslation: false, showHighlights: true, fontSize: 17, fontOpen: false,
  selected: null, chatSelected: '', chatBusy: false, activePage: 1, readingTimer: null, reflowScroll: 0, originalScroll: 0,
};
const moduleInfo = {
  summary: { name: '论文总结', hint: '中文标题与概要', icon: 'sparkles' },
  translation: { name: '翻译', hint: '全文、目录、图注与划词', icon: 'translate' },
  chat: { name: 'AI 问答', hint: '论文问答与选段解释', icon: 'chat' },
  reflow: { name: 'AI 重排', hint: '阅读顺序、段落与参考文献校对', icon: 'layout' },
};
const paths = {
  book: '<path d="M12 6c-2-2-6-2-9-1v14c3-1 7-1 9 1 2-2 6-2 9-1V5c-3-1-7-1-9 1Z"/><path d="M12 6v14"/>',
  home: '<path d="m3 10 9-7 9 7M5 9v12h14V9M9 21v-8h6v8"/>',
  settings: '<path d="m9 3-.7 2.4-2 .9L4 5.8 2.5 8.4l1.7 1.8-.1 2.3-1.6 1.8L4 17l2.4-.5 2 1L9 20h3l.7-2.5 2-1 2.3.5 1.5-2.7-1.6-1.8-.1-2.3 1.7-1.8L17 5.8l-2.3.5-2-.9L12 3Z" transform="translate(1.5 .5)"/><circle cx="12" cy="12" r="3"/>',
  research: '<path d="M5 8h14l2 4-2 8H5l-2-8 2-4ZM7 8V4h10v4M8 12h8"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4 4"/>',
  arrow: '<path d="M4 12h16m-6-6 6 6-6 6"/>', back: '<path d="M20 12H4m6-6-6 6 6 6"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>', upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 14v6h16v-6"/>',
  file: '<path d="M14 3H5v18h14V8l-5-5Z"/><path d="M14 3v5h5M8 13h8M8 16h6"/>',
  sparkles: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5L12 3ZM20 2v4M18 4h4"/>',
  translate: '<path d="M3 5h11M8 3v2M5 5c0 5 3 8 7 10M12 5c0 5-3 8-7 10m9 5 4-11 4 11m-6.5-4h5"/>',
  layout: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M12 7h6M12 11h6M12 15h4"/>',
  bulb: '<path d="M9 18h6M9 21h6M8 15a7 7 0 1 1 8 0l-1 3H9l-1-3Z"/><path d="m12 6-2 4h4l-2 4"/>',
  text: '<path d="M3 6h12M9 6v14M5 20h8m3-8 6 8m0-8-6 8"/>',
  chat: '<path d="M21 11.5a8.5 8.5 0 0 1-9 8.5 11 11 0 0 1-4-.8L3 21l1.7-5a9 9 0 0 1-.7-4.5A8.5 8.5 0 1 1 21 11.5Z"/><path d="M8 10h8M8 14h5"/>',
  list: '<path d="M8 5h13M8 12h13M8 19h13M3 5h.1M3 12h.1M3 19h.1"/>',
  image: '<rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="8" cy="9" r="1.5"/><path d="m3 17 5-5 4 4 4-6 5 7"/>',
  highlight: '<path d="m14 3 7 7-10 10H4v-7L14 3ZM4 13l7 7M2 22h12"/>',
  notes: '<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h8M8 11h8M8 15h5"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
  more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  trash: '<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  server: '<rect x="3" y="3" width="18" height="7" rx="2"/><rect x="3" y="14" width="18" height="7" rx="2"/><path d="M7 6.5h.1M7 17.5h.1M12 6.5h5M12 17.5h5"/>',
  check: '<path d="m5 12 4 4L19 6"/>', info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.1"/>',
  edit: '<path d="m16 3 5 5-12 12H4v-5L16 3ZM13 6l5 5"/>', send: '<path d="m3 3 18 9-18 9 4-9-4-9ZM7 12h14"/>',
  copy: '<rect x="8" y="8" width="13" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  sidebar: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M9 4v16"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 13 2M5 16a8 8 0 0 0 13 2"/>',
};
function icon(name, extra = '') { return `<svg class="icon ${extra}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.file}</svg>`; }
function esc(value) { return String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
function attr(value) { return esc(value); }
function dateLabel(value) { const d = new Date(value); return Number.isNaN(d.valueOf()) ? '' : d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }); }
function authorLabel(paper) { return Array.isArray(paper.authors) ? paper.authors.join(', ') : paper.authors || '作者信息待识别'; }
function paperUrl(id, suffix = '') { return `/api/papers/${encodeURIComponent(id)}${suffix}`; }
async function api(url, options = {}) {
  const headers = { ...(options.body && typeof options.body === 'string' ? { 'Content-Type': 'application/json' } : {}), ...(options.headers || {}) };
  const response = await fetch(url, { ...options, headers });
  const type = response.headers.get('content-type') || '';
  const data = type.includes('json') ? await response.json() : { error: await response.text() };
  if (!response.ok) { const error = new Error(data.error || `请求失败 (${response.status})`); error.status = response.status; throw error; }
  return data;
}
function toast(message, kind = '') {
  const el = document.createElement('div'); el.className = `toast ${kind}`; el.textContent = message;
  document.querySelector('#toast-root').append(el); setTimeout(() => el.remove(), 4200);
}
function statusInfo(paper) {
  if (paper.status === 'error') return ['error', '解析未完成'];
  if (paper.status === 'processing') return ['processing', paper.stage || '正在解析'];
  if (paper.ai_status === 'processing' || paper.ai_status === 'pending') return ['processing', 'AI 正在整理'];
  if (paper.ai_status === 'error' || paper.ai_status === 'partial') return ['error', 'AI 处理待重试'];
  if (!paper.summary) return ['', '待配置 AI'];
  return ['ready', '已就绪'];
}
function statusBadge(paper) { const [kind, text] = statusInfo(paper); return `<span class="status-badge ${kind}">${esc(text)}</span>`; }
function illustration() { return `<svg class="empty-illustration" viewBox="0 0 160 140" fill="none" aria-hidden="true"><ellipse cx="82" cy="126" rx="57" ry="8" fill="#EDE8F7"/><rect x="31" y="29" width="76" height="94" rx="7" transform="rotate(-10 31 29)" fill="#E7DFF7" stroke="#D8CAED"/><rect x="50" y="13" width="75" height="104" rx="6" transform="rotate(7 50 13)" fill="#F8F5FE" stroke="#D9CDED"/><path d="M72 39h32M69 48h39M67 57h36" stroke="#D4C4E8" stroke-width="2" stroke-linecap="round"/><rect x="64" y="70" width="31" height="25" rx="3" transform="rotate(7 64 70)" fill="#EEE6FA"/><path d="m70 89 7-7 5 5 5-8 5 13" stroke="#C1A7DE" stroke-width="1.5" stroke-linecap="round"/><path d="m126 43 3-7 3 7 7 3-7 3-3 7-3-7-7-3 7-3Z" fill="#C5B1E8"/><circle cx="29" cy="76" r="3" fill="#D7C9EE"/><path d="M31 16v8m-4-4h8" stroke="#D6C6EC" stroke-width="1.5" stroke-linecap="round"/></svg>`; }

/* Library and navigation. */
function header(reader = false) {
  return `<header class="app-header"><nav class="workspace-tabbar" role="tablist" aria-label="打开的文档"><button class="workspace-home ${reader ? '' : 'active'}" role="tab" aria-selected="${!reader}" tabindex="${reader ? '-1' : '0'}" data-action="home" title="主页">${icon('home')}<span>主页</span></button><div class="document-tabs">${state.openPapers.map(session => {
    const paper = session.id === state.activeDocumentId && reader ? currentPaper() : session.detail.paper;
    const active = reader && session.id === state.activeDocumentId, title = paper.title_zh || paper.title || '论文透镜';
    return `<div class="document-tab ${active ? 'active' : ''}"><button class="document-tab-link" role="tab" aria-selected="${active}" tabindex="${active ? '0' : '-1'}" data-action="switch-document" data-id="${attr(session.id)}" title="${attr(title)}">${icon('file')}<span>${esc(title)}</span>${session.reader.chatBusy ? '<i class="tab-activity" title="AI 正在回答"></i>' : ''}</button><button class="document-tab-close" data-action="close-document" data-id="${attr(session.id)}" aria-label="关闭文档 ${attr(title)}" title="关闭文档">${icon('close')}</button></div>`;
  }).join('')}</div></nav><button class="icon-button workspace-settings" data-action="settings" title="模型与参数设置" aria-label="模型与参数设置">${icon('settings')}</button></header>`;
}
function refreshWorkspaceTabs() {
  const old = app.querySelector('.app-header');
  if (old) { const container = document.createElement('div'); container.innerHTML = header(state.page === 'reader'); old.replaceWith(container.firstElementChild); }
}
const readerSessionKeys = ['readerTab', 'readerView', 'sidebarOpen', 'showTranslation', 'showHighlights', 'fontSize', 'fontOpen', 'chatSelected', 'chatBusy', 'activePage', 'reflowScroll', 'originalScroll'];
function openSession(id = state.activeDocumentId) { return state.openPapers.find(session => session.id === id); }
function rememberChat(session = openSession()) {
  const input = document.querySelector('#chat-input');
  if (session && input) session.chat = { draft: input.value, focused: document.activeElement === input, start: input.selectionStart, end: input.selectionEnd, direction: input.selectionDirection,
    scroll: document.querySelector('#chat-history')?.scrollTop || 0 };
}
function rememberReader() {
  if (state.page !== 'reader' || !state.activeDocumentId || currentPaper()?.id !== state.activeDocumentId) return;
  const session = openSession(); if (!session) return;
  persistReading(); clearTimeout(state.readingTimer);
  session.detail = state.detail;
  session.reader = Object.fromEntries(readerSessionKeys.map(key => [key, state[key]]));
  const canvas = document.querySelector('#reader-canvas');
  if (canvas?.dataset.paperId === session.id) session.anchor = captureReadingAnchor(canvas);
  rememberChat(session);
  session.sideScroll = document.querySelector('#side-content')?.scrollTop || 0;
}
function leaveReader() {
  rememberReader(); ++paperOpenToken; ++selectionRequestToken;
  clearTimeout(state.readingTimer); state.selected = null; hideSelection();
}
function goHome(page = state.homePage) {
  leaveReader(); state.activeDocumentId = null; state.page = page; state.homePage = page; state.fontOpen = false;
  state.modal = null; renderModal(); render();
}
function newReaderSession(detail) {
  return { id: detail.paper.id, detail, reader: { readerTab: 'toc', readerView: 'reflow', sidebarOpen: window.innerWidth > 680,
    showTranslation: false, showHighlights: true, fontSize: Number(detail.reading.font_size) || 17, fontOpen: false,
    chatSelected: '', chatBusy: false, activePage: Number(detail.reading.page) || 1, reflowScroll: Number(detail.reading.scroll) || 0, originalScroll: 0 }, chat: null, anchor: null, sideScroll: 0 };
}
function activateSession(session, detail = session.detail) {
  state.activeDocumentId = session.id; state.detail = detail; session.detail = detail;
  Object.assign(state, session.reader); state.page = 'reader'; state.selected = null; hideSelection();
  state.modal = null; renderModal(); render();
}
async function switchDocument(id) {
  const session = openSession(id); if (!session) return;
  if (state.page === 'reader' && state.activeDocumentId === id) return;
  leaveReader(); activateSession(session);
  const token = ++paperOpenToken;
  try {
    const detail = normalizeDetail(await api(paperUrl(id)));
    if (token !== paperOpenToken || state.page !== 'reader' || state.activeDocumentId !== id) return;
    if (state.chatBusy) return;
    const previous = state.detail; state.detail = { ...detail, reading: previous.reading }; session.detail = state.detail;
    updateReader(previous); refreshWorkspaceTabs();
  } catch (error) { if (token === paperOpenToken) toast(error.message, 'error'); }
}
function closeDocument(id) {
  const index = state.openPapers.findIndex(session => session.id === id); if (index < 0) return;
  const active = state.page === 'reader' && state.activeDocumentId === id;
  if (active) leaveReader();
  state.openPapers.splice(index, 1);
  if (active) {
    const next = state.openPapers[Math.min(index, state.openPapers.length - 1)];
    if (next) activateSession(next); else goHome();
  } else refreshWorkspaceTabs();
}
function render() {
  document.title = state.page === 'reader' && state.detail ? `${state.detail.paper.title_zh || state.detail.paper.title} · 论文透镜` : '主页 · 论文阅读';
  if (state.page === 'reader' && state.detail) return renderReader();
  app.className = '';
  app.innerHTML = `${header()}<div class="app-shell"><aside class="main-sidebar"><p class="sidebar-section-label">工作空间</p><nav aria-label="主导航"><button class="nav-item ${state.page === 'research' ? 'active' : ''}" data-action="navigate" data-page="research">${icon('research')}<span>研究</span>${state.page === 'research' ? '<i class="nav-dot"></i>' : ''}</button><button class="nav-item ${state.page === 'library' ? 'active' : ''}" data-action="navigate" data-page="library">${icon('book')}<span>文献库</span>${state.page === 'library' ? '<i class="nav-dot"></i>' : ''}</button></nav><div class="sidebar-footer"><div class="mini-logo">${icon('sparkles')}让理解更进一步</div><span>你的私人学术阅读空间</span></div></aside><main class="library-main" id="main-content">${state.page === 'research' ? `<div class="page-heading"><div><span class="eyebrow">RESEARCH</span><h1>研究</h1></div></div><div class="research-placeholder" aria-label="研究空白页">${icon('research')}</div>` : libraryHtml()}</main></div>`;
  bindImageFallbacks();
}
function filteredLibraryPapers() {
  const query = state.query.trim().toLocaleLowerCase();
  return state.papers.filter(p => [p.title, p.title_zh, authorLabel(p), p.abstract, p.summary].some(v => String(v || '').toLocaleLowerCase().includes(query)));
}
function libraryResultsMode(filtered) { return state.error ? 'error' : !state.papers.length ? 'empty' : filtered.length ? 'grid' : 'no-results'; }
function libraryResultsHtml(filtered) {
  return state.error ? `<div class="error-state">${icon('info')}<p>${esc(state.error)}</p><button class="button secondary" data-action="reload">重新连接</button></div>` : state.papers.length === 0 ? `<div class="empty-library">${illustration()}<span class="eyebrow" style="font-size:8px;margin-bottom:12px">A NEW CHAPTER STARTS HERE</span><h2>从第一篇论文开始</h2><p>添加 PDF 论文，自动整理内容与阅读结构。<br>在原文与中文理解之间，找到自己的阅读节奏。</p><button class="button primary" data-action="upload">${icon('plus')}添加文章</button><span class="empty-hint">拖入 PDF，或选择本机文件</span></div><div class="feature-strip"><span>${icon('layout')}自动重排</span><span>${icon('translate')}双语阅读</span><span>${icon('chat')}随时提问</span></div>` : filtered.length ? `<div class="paper-grid">${filtered.map(cardHtml).join('')}</div><p class="library-end">— 已展示全部文献 —</p>` : `<div class="empty-library" style="min-height:360px"><div class="upload-icon">${icon('search')}</div><h2>没有找到相关文献</h2><p>换一个标题、作者或关键词试试。</p><button class="button secondary" data-action="clear-search">清除搜索</button></div>`;
}
function libraryHtml() {
  const filtered = filteredLibraryPapers(), query = state.query.trim();
  return `<div class="page-heading"><div><span class="eyebrow">YOUR KNOWLEDGE SPACE</span><h1>文献库</h1><p>收集灵感，让每一次阅读都有收获。</p></div><button class="button primary" data-action="upload">${icon('plus')}添加文章</button></div>${state.dryRun ? `<div class="notice warning library-dryrun-notice">${icon('info')}<span>当前为 dry-run 预览模式，上传与设置操作不会写入文件。</span></div>` : ''}<div class="search-row"><label class="search-box">${icon('search')}<input type="search" id="library-search" placeholder="搜索标题、作者或论文内容…" value="${attr(state.query)}" aria-label="搜索文献"></label><span class="library-count">${state.papers.length} 篇文献${query ? ` · 找到 ${filtered.length} 篇` : ''}</span></div><div class="library-results" data-mode="${libraryResultsMode(filtered)}">${libraryResultsHtml(filtered)}</div>`;
}
function updateLibraryCard(card, paper) {
  const name = paper.title_zh || paper.title || '正在识别论文标题';
  const text = (selector, value) => { const el = card.querySelector(selector); if (el && el.textContent !== value) el.textContent = value; };
  const label = `查看 ${name}`; if (card.getAttribute('aria-label') !== label) card.setAttribute('aria-label', label);
  text('.paper-info h3', name); text('.paper-author', authorLabel(paper));
  text('.paper-tag', `PDF · ${Number(paper.page_count) || '—'} PAGES`);
  text('.paper-meta>span:last-child', dateLabel(paper.created_at));
  let original = card.querySelector('.paper-original');
  if (paper.title_zh) {
    if (!original) { original = document.createElement('p'); original.className = 'paper-original'; card.querySelector('.paper-info h3').after(original); }
    if (original.textContent !== paper.title) original.textContent = paper.title || '';
  } else original?.remove();
  const [kind, status] = statusInfo(paper), badge = card.querySelector('.status-badge');
  if (badge.className !== `status-badge ${kind}`) badge.className = `status-badge ${kind}`;
  if (badge.textContent !== status) badge.textContent = status;
  let progress = card.querySelector('.card-progress');
  if (paper.status === 'processing') {
    if (!progress) { progress = document.createElement('div'); progress.className = 'card-progress'; progress.append(document.createElement('span')); card.append(progress); }
    const width = `${Math.max(2, Math.min(100, Number(paper.progress) || 0))}%`;
    if (progress.firstElementChild.style.width !== width) progress.firstElementChild.style.width = width;
  } else progress?.remove();
  // A thumbnail unavailable during parsing gets one fresh attempt when ready.
  // Successful covers stay mounted throughout every progress/status update.
  const placeholder = card.querySelector('.pdf-placeholder');
  if (placeholder && card.dataset.paperStatus === 'processing' && paper.status === 'ready') {
    const image = document.createElement('img'); image.src = paperUrl(paper.id, '/thumbnail'); image.loading = 'lazy'; image.dataset.fallback = 'cover'; placeholder.replaceWith(image);
  }
  const image = card.querySelector('.paper-cover img'), alt = `${paper.title || '论文'}首页`;
  if (image && image.alt !== alt) image.alt = alt;
  if (card.dataset.paperStatus !== paper.status) card.dataset.paperStatus = paper.status;
}
function updateLibrary() {
  if (state.page !== 'library') return;
  const main = document.querySelector('#main-content'), results = main?.querySelector('.library-results');
  if (!results) return render();
  const scroll = main.scrollTop, filtered = filteredLibraryPapers(), mode = libraryResultsMode(filtered);
  const input = main.querySelector('#library-search'); if (input.value !== state.query) input.value = state.query;
  const count = `${state.papers.length} 篇文献${state.query.trim() ? ` · 找到 ${filtered.length} 篇` : ''}`;
  if (main.querySelector('.library-count').textContent !== count) main.querySelector('.library-count').textContent = count;
  const dryrunNotice = main.querySelector('.library-dryrun-notice');
  if (!state.dryRun) dryrunNotice?.remove();
  else if (!dryrunNotice) main.querySelector('.search-row').insertAdjacentHTML('beforebegin', `<div class="notice warning library-dryrun-notice">${icon('info')}<span>当前为 dry-run 预览模式，上传与设置操作不会写入文件。</span></div>`);
  if (mode === 'grid') {
    if (results.dataset.mode !== mode) results.innerHTML = '<div class="paper-grid"></div><p class="library-end">— 已展示全部文献 —</p>';
    const grid = results.querySelector('.paper-grid'), existing = new Map([...grid.children].map(card => [card.dataset.id, card]));
    filtered.forEach((paper, index) => {
      let card = existing.get(paper.id);
      if (!card) { const template = document.createElement('template'); template.innerHTML = cardHtml(paper); card = template.content.firstElementChild; }
      updateLibraryCard(card, paper);
      if (grid.children[index] !== card) grid.insertBefore(card, grid.children[index] || null);
      existing.delete(paper.id);
    });
    existing.forEach(card => card.remove());
  } else {
    const html = libraryResultsHtml(filtered); if (results.innerHTML !== html) results.innerHTML = html;
  }
  if (results.dataset.mode !== mode) results.dataset.mode = mode;
  bindImageFallbacks(results); main.scrollTop = scroll;
}
function cardHtml(paper) {
  const name = paper.title_zh || paper.title || '正在识别论文标题';
  return `<article class="paper-card" data-action="paper" data-id="${attr(paper.id)}" data-paper-status="${attr(paper.status)}" tabindex="0" role="button" aria-label="查看 ${attr(name)}"><div class="paper-cover"><img src="${paperUrl(paper.id, '/thumbnail')}" alt="${attr(paper.title || '论文')}首页" loading="lazy" data-fallback="cover"><span class="paper-tag">PDF · ${Number(paper.page_count) || '—'} PAGES</span><button class="icon-button card-more" data-action="delete-paper" data-id="${attr(paper.id)}" aria-label="删除文献" title="删除文献">${icon('trash')}</button></div><div class="paper-info"><h3>${esc(name)}</h3>${paper.title_zh ? `<p class="paper-original">${esc(paper.title)}</p>` : ''}<p class="paper-author">${esc(authorLabel(paper))}</p><div class="paper-meta">${statusBadge(paper)}<span>${dateLabel(paper.created_at)}</span></div></div>${paper.status === 'processing' ? `<div class="card-progress"><span style="width:${Math.max(2, Math.min(100, Number(paper.progress) || 0))}%"></span></div>` : ''}</article>`;
}
function bindImageFallbacks(root = document) {
  root.querySelectorAll('img[data-fallback="cover"]').forEach(img => { img.onerror = () => { const fallback = document.createElement('div'); fallback.className = 'pdf-placeholder'; fallback.innerHTML = `PDF<span class="pdf-lines"></span>`; img.replaceWith(fallback); }; });
  root.querySelectorAll('img[data-hide-on-error]').forEach(img => { img.onerror = () => { img.style.display = 'none'; }; });
}
async function reloadPapers(quiet = false) {
  try { const data = await api('/api/papers'); state.papers = data.papers || []; state.dryRun = !!data.dry_run; state.error = ''; if (state.page === 'library') updateLibrary(); else if (!quiet) render(); }
  catch (error) { if (!quiet) { state.error = error.message || '暂时无法连接本地服务'; if (state.page === 'library') updateLibrary(); else render(); } }
}
async function openPaper(id, reading = false) {
  if (reading && openSession(id)) return switchDocument(id);
  if (reading) leaveReader();
  const token = ++paperOpenToken;
  try {
    const detail = await api(paperUrl(id));
    if (token !== paperOpenToken) return;
    ++selectionRequestToken; state.selected = null; hideSelection();
    const normalized = normalizeDetail(detail);
    if (reading) {
      const session = newReaderSession(normalized); state.openPapers.push(session); activateSession(session);
      requestAnimationFrame(() => { if (token === paperOpenToken && isCurrentPaper(id) && state.page === 'reader') restoreReading(); });
    } else { state.detail = normalized; state.modal = 'detail'; renderModal(); }
  } catch (error) { if (token === paperOpenToken) toast(error.message, 'error'); }
}
function normalizeDetail(detail) {
  return { ...detail, document: { pages: [], toc: [], figures: [], warnings: [], ...(detail.document || {}) }, highlights: detail.highlights || [], chat: detail.chat || [], reading: detail.reading || {} };
}
function currentPaper() { return state.detail?.paper; }
function isCurrentPaper(id) { return currentPaper()?.id === id; }
function closeModal() { if (state.modal === 'selection') ++selectionRequestToken; state.modal = null; renderModal(); }
function renderModal() {
  closeModelPicker();
  if (!state.modal) {
    const wasOpen = !!modalRoot.firstElementChild;
    modalRoot.innerHTML = ''; document.body.style.overflow = '';
    if (wasOpen && modalReturnFocus?.isConnected) modalReturnFocus.focus({ preventScroll: true });
    return;
  }
  const opening = !modalRoot.firstElementChild;
  const priorFocus = modalRoot.contains(document.activeElement) ? document.activeElement : null;
  const priorAction = priorFocus?.dataset.action;
  const priorId = priorFocus?.dataset.id;
  if (opening) modalReturnFocus = document.activeElement;
  document.body.style.overflow = 'hidden';
  let title = '', subtitle = '', body = '', foot = '', classes = '';
  if (state.modal === 'upload') {
    title = '添加文章'; subtitle = '把值得读的论文，留在这里。';
    body = uploadHtml();
    foot = `<span class="detail-stat">PDF 文件 · 自动解析与整理</span><button class="button ghost" data-action="close-modal">取消</button>${state.uploadTab === 'path' ? `<button class="button primary" data-action="import-path" ${state.uploadBusy ? 'disabled' : ''}>${state.uploadBusy ? '<span class="spinner"></span>' : icon('plus')}导入文章</button>` : ''}`;
  } else if (state.modal === 'settings') {
    title = '模型与参数设置'; subtitle = '连接你的 AI 服务，为不同阅读任务分配模型。'; classes = 'settings-modal'; body = settingsHtml();
    foot = `<span class="detail-stat">${icon('shield')} Key 仅用于本地服务调用</span><button class="button ghost" data-action="close-modal">取消</button><button class="button primary" data-action="save-settings">保存设置</button>`;
  } else if (state.modal === 'detail') {
    const paper = currentPaper(); if (!paper) return closeModal();
    title = '文献概览'; classes = 'detail-modal'; body = detailHtml();
    foot = `<span class="detail-stat">${Number(paper.page_count) || '—'} 页 · ${dateLabel(paper.created_at)} 添加</span><button class="button primary" data-action="read-paper" data-id="${attr(paper.id)}">进入文献阅读${icon('arrow')}</button>`;
  } else if (state.modal === 'delete') {
    title = '删除文献'; body = `<p class="confirm-copy">确定删除 <span class="confirm-title">${esc(state.deletePaper?.title_zh || state.deletePaper?.title || '这篇文献')}</span>？<br>该文献的高亮、笔记与对话记录也会一起删除。</p>`;
    foot = `<button class="button ghost" data-action="close-modal">取消</button><button class="button danger" data-action="confirm-delete">删除文献</button>`;
  } else if (state.modal === 'selection') {
    title = state.selectionAction === 'explain' ? 'AI 解释' : '划词翻译';
    body = `<blockquote class="selection-quote">${esc(state.selected?.text)}</blockquote><div id="selection-response" class="selection-response">${state.selectionResult ? esc(state.selectionResult) : '<span class="processing-inline"><span class="spinner"></span>正在阅读这段内容…</span>'}</div>`;
    foot = `<button class="button ghost" data-action="close-modal">关闭</button><button class="button secondary" data-action="selection-ask">${icon('chat')}继续问 AI</button>`;
  } else if (state.modal === 'note') {
    title = '编辑高亮笔记';
    const note = state.detail.highlights.find(n => n.id === state.editNoteId);
    body = `<blockquote class="selection-quote">${esc(note?.text)}</blockquote><div class="field"><label for="note-text">你的想法</label><textarea id="note-text" rows="4" placeholder="记录灵感、疑问或自己的理解…">${esc(note?.note)}</textarea></div>`;
    foot = `<button class="button ghost" data-action="close-modal">取消</button><button class="button primary" data-action="save-note">保存笔记</button>`;
  } else if (state.modal === 'image') {
    title = state.figureTitle || '论文图表'; classes = 'image-modal';
    body = `<figure class="image-preview"><div class="image-stage"><img class="image-view" src="${attr(state.figureSrc)}" alt="${attr(title)}"${state.figureCaption ? ' aria-describedby="image-caption"' : ''}></div>${state.figureCaption ? `<figcaption class="image-view-caption" id="image-caption"><p>${esc(state.figureCaption)}</p>${state.figureCaptionZh ? `<p lang="zh-CN">${esc(state.figureCaptionZh)}</p>` : ''}</figcaption>` : ''}</figure>`;
  }
  modalRoot.innerHTML = `<div class="modal-backdrop" data-backdrop="true"><section class="modal ${classes}" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div class="modal-head"><div><h2 id="modal-title" tabindex="-1">${esc(title)}</h2>${subtitle ? `<p>${esc(subtitle)}</p>` : ''}</div><button class="icon-button" data-action="close-modal" title="关闭" aria-label="关闭">${icon('close')}</button></div><div class="modal-body">${body}</div>${foot ? `<div class="modal-foot">${foot}</div>` : ''}</section></div>`;
  bindUpload(); bindImageFallbacks();
  modalRoot.querySelectorAll('.field').forEach((field, index) => {
    const label = field.querySelector('label'), input = field.querySelector('input,textarea,select');
    if (label && input) { if (!input.id) input.id = `modal-field-${index}`; label.htmlFor = input.id; }
  });
  if (opening || priorFocus) requestAnimationFrame(() => {
    const matching = priorAction ? [...modalRoot.querySelectorAll('[data-action]')].find(el => el.dataset.action === priorAction && el.dataset.id === priorId) : null;
    (matching || document.querySelector('#modal-title'))?.focus({ preventScroll: true });
  });
}

/* Import, preview, and model configuration. */
function uploadHtml() {
  return `<div class="tabs"><button class="tab ${state.uploadTab === 'file' ? 'active' : ''}" data-action="upload-tab" data-tab="file">上传 PDF</button><button class="tab ${state.uploadTab === 'path' ? 'active' : ''}" data-action="upload-tab" data-tab="path">本机文件地址</button></div>${state.uploadTab === 'file' ? `<div class="upload-drop" id="upload-drop" tabindex="0" role="button" aria-label="选择 PDF 文件"><div class="upload-icon">${icon('upload')}</div><h3>${state.uploadBusy ? '正在添加论文…' : '将 PDF 拖放到这里'}</h3><p>保留原始文件，并在后台自动整理阅读内容</p><span class="button secondary">${state.uploadBusy ? '<span class="spinner"></span>' : icon('file')}选择 PDF 文件</span><input id="pdf-input" type="file" accept="application/pdf,.pdf" multiple hidden ${state.uploadBusy ? 'disabled' : ''}></div>` : `<div class="field"><label for="pdf-path">PDF 的完整文件地址</label><input id="pdf-path" placeholder="/Users/你的用户名/Documents/paper.pdf" autocomplete="off"><small>填写运行本服务的电脑上的绝对路径；支持中文路径与带空格的文件名。</small></div>`}<div class="upload-disclaimer">${icon('shield')}<span>原始 PDF 与阅读记录保存在本机。AI 功能会将所需论文文本发送至你配置的模型供应商。</span></div><div id="upload-result" class="upload-result"></div>`;
}
function bindUpload() {
  const drop = document.querySelector('#upload-drop'), input = document.querySelector('#pdf-input');
  if (!drop || !input) return;
  drop.onclick = () => { if (!state.uploadBusy) input.click(); };
  drop.onkeydown = event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); input.click(); } };
  input.onclick = event => event.stopPropagation();
  input.onchange = () => uploadFiles([...input.files]);
  for (const name of ['dragenter', 'dragover']) drop.addEventListener(name, event => { event.preventDefault(); drop.classList.add('dragging'); });
  for (const name of ['dragleave', 'drop']) drop.addEventListener(name, event => { event.preventDefault(); drop.classList.remove('dragging'); });
  drop.addEventListener('drop', event => { if (!state.uploadBusy) uploadFiles([...event.dataTransfer.files]); });
}
async function uploadFiles(files) {
  const pdfs = files.filter(file => file.name.toLowerCase().endsWith('.pdf') || file.type === 'application/pdf');
  if (!pdfs.length) return toast('请选择 PDF 文件', 'error');
  state.uploadBusy = true; renderModal(); let count = 0, lastError = '';
  for (const file of pdfs) {
    const feedback = document.querySelector('#upload-result'); if (feedback) feedback.textContent = `正在添加 ${file.name}…`;
    try { await api(`/api/papers?filename=${encodeURIComponent(file.name)}`, { method: 'POST', headers: { 'Content-Type': 'application/pdf' }, body: file }); count++; }
    catch (error) { lastError = error.message; toast(`${file.name}：${error.message}`, 'error'); }
  }
  state.uploadBusy = false;
  if (count) { closeModal(); await reloadPapers(); toast(`已添加 ${count} 篇论文，正在后台整理`, 'success'); }
  else { renderModal(); const feedback = document.querySelector('#upload-result'); if (feedback) feedback.textContent = lastError; }
}
async function importPath(button) {
  const path = document.querySelector('#pdf-path')?.value.trim(); if (!path) return toast('请输入 PDF 的完整文件地址', 'error');
  button.disabled = true; state.uploadBusy = true;
  try { await api('/api/papers/import-path', { method: 'POST', body: JSON.stringify({ path }) }); closeModal(); await reloadPapers(); toast('论文已添加，正在后台整理', 'success'); }
  catch (error) { toast(error.message, 'error'); button.disabled = false; }
  finally { state.uploadBusy = false; }
}
function detailHtml() {
  const p = currentPaper();
  return `<div class="detail-banner"><div class="detail-thumb"><img src="${paperUrl(p.id, '/thumbnail')}" alt="论文首页" data-hide-on-error></div><div><h3>${esc(p.title_zh || p.title || '正在识别标题')}</h3>${p.title_zh ? `<p class="original-title">${esc(p.title)}</p>` : ''}<p class="paper-author">${esc(authorLabel(p))}</p></div></div>${p.status === 'processing' ? `<div class="notice">${icon('layout')}<span>正在解析论文，目录、图表与阅读内容会陆续就绪。<div class="progress-track"><span style="width:${Math.max(2, Math.min(100, Number(p.progress) || 0))}%"></span></div><span class="progress-copy">${esc(p.stage || '正在处理')} · ${Number(p.progress) || 0}%</span></span></div>` : ''}${p.status === 'error' ? `<div class="notice error">${icon('info')}<span>${esc(p.error || '解析出现问题，请尝试重新添加。')}</span></div>` : ''}<p class="detail-section-label">${icon('sparkles')}中文概要</p>${p.summary ? `<p class="detail-summary">${esc(p.summary)}</p>` : `<div class="summary-placeholder">${p.ai_status === 'processing' || p.ai_status === 'pending' ? '<span class="processing-inline"><span class="spinner"></span>AI 正在生成中文标题与概要…</span>' : p.ai_error ? `${esc(p.ai_error)}<br><button class="button ghost" data-action="reprocess">${icon('refresh')}重新整理</button>` : `配置 AI 模型后，即可自动生成中文标题与概要。<br><button class="button ghost" data-action="settings">${icon('settings')}设置模型</button>`}</div>`}${p.abstract && !p.summary ? `<details style="margin-top:20px"><summary class="small muted" style="cursor:pointer">查看原文摘要</summary><p class="detail-abstract" style="margin-top:12px">${esc(p.abstract)}</p></details>` : ''}`;
}
async function openSettings() {
  try {
    state.settings = await api('/api/settings'); state.settingsDraft = structuredClone(state.settings);
    state.settingsDraft.providers ||= []; state.settingsDraft.modules ||= {};
    if (!state.settingsDraft.providers.length) state.settingsDraft.providers.push({ id: `provider-${Date.now()}`, name: '', base_url: '', api_key: '', default_model: '', has_key: false });
    state.modal = 'settings'; renderModal();
    state.settingsDraft.providers.filter(provider => provider.base_url && (provider.api_key || provider.has_key)).forEach(provider => fetchProviderModels(provider.id));
  } catch (error) { toast(error.message, 'error'); }
}
function modelInputHtml(id, setting, value, provider, placeholder) {
  return `<div class="model-combobox"><input id="${attr(id)}" data-setting="${setting}" data-model-input data-model-provider="${attr(provider?.id || '')}" value="${attr(value)}" placeholder="${attr(placeholder)}" role="combobox" aria-autocomplete="list" aria-expanded="false" aria-controls="model-options-popup" spellcheck="false" autocomplete="off"><button type="button" class="model-dropdown-toggle" data-action="toggle-model-options" aria-label="显示模型选项" aria-expanded="false" title="选择模型"><svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button></div>`;
}
function settingsHtml() {
  const draft = state.settingsDraft;
  return `<div class="settings-columns"><section><div class="provider-heading"><div><h3>模型供应商</h3><p>支持兼容 OpenAI API 的模型服务</p></div><button class="icon-button" data-action="add-provider" title="添加供应商" aria-label="添加供应商">${icon('plus')}</button></div>${draft.providers.map((provider, index) => {
    const cached = providerModelsFor(provider);
    return `<div class="provider-card" data-provider-id="${attr(provider.id)}"><div class="provider-title">${icon('server')}<span>供应商 ${String(index + 1).padStart(2, '0')}</span><button class="icon-button" data-action="remove-provider" data-id="${attr(provider.id)}" title="移除供应商" aria-label="移除供应商">${icon('trash')}</button></div><div class="provider-fields"><div class="field"><label>供应商名称</label><input data-setting="name" value="${attr(provider.name)}" placeholder="例如：OpenAI"></div><div class="field"><label>访问 URL</label><input data-setting="base_url" value="${attr(provider.base_url)}" placeholder="https://api.openai.com/v1" spellcheck="false"></div><div class="field key-field"><label>API Key</label><input data-setting="api_key" type="password" value="${attr(provider.api_key)}" placeholder="${provider.has_key ? '已保存 · 留空以保留原 Key' : '输入 API Key'}" autocomplete="new-password" spellcheck="false"></div><div class="field key-field"><label>默认模型</label><div class="provider-model-control">${modelInputHtml(`model-default-${encodeURIComponent(provider.id)}`, 'default_model', provider.default_model, provider, '选择或输入模型 ID')}<button class="button secondary" data-action="fetch-provider-models" data-id="${attr(provider.id)}" ${cached?.status === 'loading' ? 'disabled' : ''}>${cached?.status === 'loading' ? '<span class="spinner"></span>获取中' : `${icon('refresh')}获取模型列表`}</button></div><small>模块未单独指定模型时，使用此默认模型。</small><p class="provider-model-status ${cached?.status === 'error' ? 'error' : ''}" data-model-status aria-live="polite">${esc(providerModelStatus(cached))}</p></div></div><div class="provider-test"><span>${provider.has_key ? '已保存访问凭据' : '首次使用请填写访问凭据'}</span><button data-action="test-provider" data-id="${attr(provider.id)}">${icon('refresh')}测试连接</button></div></div>`;
  }).join('')}${!draft.providers.length ? `<div class="summary-placeholder">还没有模型供应商。点击 + 添加。</div>` : ''}<p class="settings-note">填写访问 URL 与 Key 后可自动获取模型列表；供应商不支持列表接口时，仍可手动输入模型 ID。连接测试优先使用供应商默认模型。</p></section><section class="module-section"><h3>按任务分配模型</h3><p>选择供应商；模型留空时继承该供应商的默认模型。</p>${Object.entries(moduleInfo).map(([key, info]) => {
    const config = draft.modules[key] || {}, provider = draft.providers.find(item => item.id === config.provider_id);
    return `<div class="module-row" data-module="${key}"><div class="module-title">${icon(info.icon)}${info.name}<span class="small muted" style="font-size:9px;margin-left:auto">${info.hint}</span></div><div class="field"><label>模型供应商</label><select data-setting="provider_id"><option value="">暂不配置</option>${draft.providers.map(item => `<option value="${attr(item.id)}" ${config.provider_id === item.id ? 'selected' : ''}>${esc(item.name || '未命名供应商')}</option>`).join('')}</select></div><div class="field"><label>模型 ID（可选）</label>${modelInputHtml(`model-module-${key}`, 'model', config.model, provider, moduleModelPlaceholder(provider))}</div></div>`;
  }).join('')}</section></div>`;
}

function providerModelSignature(provider) { return JSON.stringify([provider.base_url?.trim(), provider.api_key?.trim(), !!provider.has_key]); }
function providerModelsFor(provider) { const cached = state.providerModels[provider.id]; return cached?.signature === providerModelSignature(provider) ? cached : null; }
function moduleModelPlaceholder(provider) { return provider?.default_model ? `留空使用默认模型：${provider.default_model}` : provider ? '留空继承默认模型，或输入模型 ID' : '请先选择供应商'; }
function providerModelStatus(cached) {
  if (cached?.status === 'loading') return '正在获取供应商提供的模型列表…';
  if (cached?.status === 'error') return `${cached.error}；可继续手动输入模型 ID。`;
  if (cached?.status === 'ready') return `已获取 ${cached.models.length} 个模型，可搜索选择或手动输入。`;
  return '填写 URL 与 Key 后获取可用模型，也可以直接手动输入。';
}
function updateProviderModelControls(id) {
  if (state.modal !== 'settings' || !state.settingsDraft) return;
  const provider = state.settingsDraft.providers.find(item => item.id === id); if (!provider) return;
  const cached = providerModelsFor(provider);
  const card = [...modalRoot.querySelectorAll('[data-provider-id]')].find(item => item.dataset.providerId === id);
  if (card) {
    const status = card.querySelector('[data-model-status]'), button = card.querySelector('[data-action="fetch-provider-models"]');
    if (status) { status.textContent = providerModelStatus(cached); status.classList.toggle('error', cached?.status === 'error'); }
    if (button) { button.disabled = cached?.status === 'loading'; button.innerHTML = cached?.status === 'loading' ? '<span class="spinner"></span>获取中' : `${icon('refresh')}获取模型列表`; }
  }
  modalRoot.querySelectorAll('[data-module]').forEach(row => {
    const select = row.querySelector('select'), input = row.querySelector('[data-setting="model"]');
    const selected = state.settingsDraft.providers.find(item => item.id === select.value);
    input.placeholder = moduleModelPlaceholder(selected);
    input.dataset.modelProvider = selected?.id || '';
    for (const option of select.options) { const source = state.settingsDraft.providers.find(item => item.id === option.value); if (source) option.textContent = source.name || '未命名供应商'; }
  });
  if (state.modelPicker) renderModelPicker();
}
function modelOptions(providerId, query = '') {
  const provider = state.settingsDraft?.providers.find(item => item.id === providerId);
  const models = provider ? providerModelsFor(provider)?.models || [] : [];
  const needle = query.trim().toLocaleLowerCase();
  return models.filter(model => !needle || model.toLocaleLowerCase().includes(needle)).slice(0, 150);
}
function closeModelPicker() {
  const input = state.modelPicker && document.getElementById(state.modelPicker.inputId);
  if (input) { input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); input.closest('.model-combobox')?.querySelector('button')?.setAttribute('aria-expanded', 'false'); }
  state.modelPicker = null; document.querySelector('#model-options-popup')?.remove();
}
function openModelPicker(input, showAll = false) {
  if (!input || state.modal !== 'settings') return;
  if (state.modelPicker?.inputId !== input.id) closeModelPicker();
  state.modelPicker = { inputId: input.id, query: showAll ? '' : input.value, activeIndex: -1 };
  input.setAttribute('aria-expanded', 'true'); input.closest('.model-combobox').querySelector('button').setAttribute('aria-expanded', 'true');
  renderModelPicker();
  if (input.dataset.modelProvider) fetchProviderModels(input.dataset.modelProvider);
}
function renderModelPicker() {
  const picker = state.modelPicker; if (!picker) return;
  const input = document.getElementById(picker.inputId); if (!input?.isConnected) return closeModelPicker();
  const provider = state.settingsDraft?.providers.find(item => item.id === input.dataset.modelProvider);
  const cached = provider && providerModelsFor(provider), options = modelOptions(input.dataset.modelProvider, picker.query);
  picker.options = options; picker.activeIndex = Math.min(picker.activeIndex, options.length - 1);
  let popup = document.querySelector('#model-options-popup');
  if (!popup) {
    const dialog = input.closest('[role="dialog"]'); if (!dialog) return closeModelPicker();
    popup = document.createElement('div'); popup.id = 'model-options-popup'; popup.className = 'model-options-popup'; popup.setAttribute('role', 'listbox'); popup.setAttribute('aria-label', '可用模型'); dialog.append(popup);
  }
  const message = !provider ? '先选择一个模型供应商，也可以手动输入。' : cached?.status === 'loading' ? '正在获取模型列表…' : cached?.status === 'error' ? '暂时无法获取列表，可以手动输入模型 ID。' : cached?.models?.length ? '没有匹配的模型，可以继续手动输入。' : '暂无模型列表，可以获取列表或手动输入。';
  popup.innerHTML = `<div class="model-options-head" aria-hidden="true">${icon('search')}<span>${picker.query ? '搜索模型' : '选择模型'} · 支持手动输入</span><span>${options.length}${options.length === 150 ? '+' : ''}</span></div><div class="model-options-list">${options.length ? options.map((model, index) => `<button type="button" tabindex="-1" id="model-option-${index}" class="model-option ${index === picker.activeIndex ? 'active' : ''}" role="option" aria-selected="${model === input.value}" data-action="choose-model" data-value="${attr(model)}"><span>${esc(model)}</span>${model === input.value ? icon('check') : ''}</button>`).join('') : `<p class="model-options-empty">${esc(message)}</p>`}</div>`;
  if (picker.activeIndex >= 0) input.setAttribute('aria-activedescendant', `model-option-${picker.activeIndex}`); else input.removeAttribute('aria-activedescendant');
  positionModelPicker();
}
function positionModelPicker() {
  const input = state.modelPicker && document.getElementById(state.modelPicker.inputId), popup = document.querySelector('#model-options-popup');
  if (!input || !popup) return;
  const box = input.closest('.model-combobox').getBoundingClientRect();
  const below = window.innerHeight - box.bottom - 12, above = box.top - 12;
  const upwards = below < 180 && above > below, height = Math.min(290, Math.max(100, upwards ? above : below));
  popup.style.width = `${Math.min(Math.max(box.width, 240), window.innerWidth - 24)}px`;
  popup.style.left = `${Math.max(12, Math.min(box.left, window.innerWidth - Math.max(box.width, 240) - 12))}px`;
  popup.style.maxHeight = `${height}px`;
  popup.style.top = upwards ? '' : `${box.bottom + 5}px`;
  popup.style.bottom = upwards ? `${window.innerHeight - box.top + 5}px` : '';
}
function chooseModel(value) {
  const input = state.modelPicker && document.getElementById(state.modelPicker.inputId); if (!input) return;
  input.value = value; const id = input.dataset.modelProvider;
  closeModelPicker(); collectSettings(); if (id) updateProviderModelControls(id);
  input.focus({ preventScroll: true });
}
async function fetchProviderModels(id, force = false) {
  const draft = state.modal === 'settings' ? collectSettings() : state.settingsDraft;
  const provider = draft?.providers.find(item => item.id === id); if (!provider) return;
  if (!provider.base_url) { if (force) toast('请先填写访问 URL', 'error'); return; }
  if (!provider.api_key && !provider.has_key) { if (force) toast('请先填写 API Key', 'error'); return; }
  const signature = providerModelSignature(provider), previous = providerModelsFor(provider);
  if (previous?.status === 'loading' || (!force && previous)) return;
  const entry = { signature, models: previous?.models || [], status: 'loading', error: '', token: ++providerModelRequestToken };
  state.providerModels[id] = entry; updateProviderModelControls(id);
  try {
    const result = await api('/api/settings/models', { method: 'POST', body: JSON.stringify({ provider_id: id, ...provider }) });
    if (state.providerModels[id] !== entry) return;
    const current = state.settingsDraft?.providers.find(item => item.id === id);
    if (!current || providerModelSignature(current) !== signature) return;
    entry.models = [...new Set((result.models || []).filter(model => typeof model === 'string' && model.trim()).map(model => model.trim()))].sort();
    entry.status = entry.models.length ? 'ready' : 'error'; entry.error = entry.models.length ? '' : '供应商未返回可用模型';
  } catch (error) { if (state.providerModels[id] !== entry) return; entry.status = 'error'; entry.error = error.message || '获取模型列表失败'; }
  finally { if (state.providerModels[id] === entry) updateProviderModelControls(id); }
}
function collectSettings() {
  if (!state.settingsDraft) return null;
  const previous = state.settingsDraft;
  const providers = [...modalRoot.querySelectorAll('[data-provider-id]')].map(el => ({
    ...(previous.providers.find(p => p.id === el.dataset.providerId) || {}), id: el.dataset.providerId,
    name: el.querySelector('[data-setting="name"]').value.trim(), base_url: el.querySelector('[data-setting="base_url"]').value.trim(), api_key: el.querySelector('[data-setting="api_key"]').value.trim(), default_model: el.querySelector('[data-setting="default_model"]').value.trim(),
  }));
  const modules = {};
  modalRoot.querySelectorAll('[data-module]').forEach(el => { modules[el.dataset.module] = { provider_id: el.querySelector('select').value, model: el.querySelector('input').value.trim() }; });
  state.settingsDraft = { providers, modules }; return state.settingsDraft;
}
async function saveSettings(button) {
  const draft = collectSettings();
  for (const p of draft.providers) { if (!p.name || !p.base_url) return toast('请填写每个供应商的名称与访问 URL', 'error'); }
  button.disabled = true;
  try {
    const result = await api('/api/settings', { method: 'PUT', body: JSON.stringify(draft) });
    state.settings = result.settings || result; closeModal(); toast('设置已保存，后台将整理待处理的论文', 'success'); await reloadPapers(true);
  } catch (error) { toast(error.message, 'error'); button.disabled = false; }
}
async function testProvider(id, button) {
  const draft = collectSettings(), provider = draft.providers.find(p => p.id === id);
  const model = provider.default_model || Object.values(draft.modules).find(m => m.provider_id === id && m.model)?.model;
  if (!provider.base_url) return toast('请先填写访问 URL', 'error');
  if (!model) return toast('请先设置供应商默认模型，或为该供应商分配模型 ID', 'error');
  button.disabled = true; const old = button.innerHTML; button.innerHTML = '<span class="spinner"></span>测试中';
  try { const result = await api('/api/settings/test', { method: 'POST', body: JSON.stringify({ provider_id: id, ...provider, model }) }); toast(result.message || (result.ok ? '连接成功' : '连接失败'), result.ok ? 'success' : 'error'); }
  catch (error) { toast(error.message, 'error'); }
  finally { if (button.isConnected) { button.disabled = false; button.innerHTML = old; } }
}

/* Reading surface, structure, figures and annotations. */
function captureReadingAnchor(canvas) {
  if (!canvas) return null;
  const top = canvas.getBoundingClientRect().top;
  const candidates = canvas.querySelectorAll('.block-text,.document-figure,.document-title,.document-title-zh,.document-page-label');
  const element = [...candidates].find(node => !node.hidden && node.getBoundingClientRect().bottom > top + 12);
  if (!element) return null;
  const owner = element.closest('[data-block-id]') || element.closest('.document-page');
  const selector = element.classList.contains('block-translation') ? '.block-translation' : element.classList.contains('block-text') ? '.block-text:not(.block-translation)' : element.classList.contains('document-figure') ? '.document-figure' : null;
  const page = element.closest('.document-page');
  return { element, ownerId: owner?.id, figureId: element.dataset.figureId, selector, offset: element.getBoundingClientRect().top - top,
    pageId: page?.id, pageOffset: page ? page.getBoundingClientRect().top - top : 0 };
}
function restoreReadingAnchor(canvas, anchor, fallback = canvas.scrollTop) {
  let element = anchor?.element;
  if (!element?.isConnected || element.hidden) {
    const owner = anchor?.ownerId && document.getElementById(anchor.ownerId);
    element = anchor?.figureId ? [...canvas.querySelectorAll('[data-figure-id]')].find(node => node.dataset.figureId === anchor.figureId) : anchor?.selector ? owner?.querySelector(anchor.selector) : owner;
    if (element?.hidden) element = owner?.querySelector('.block-text:not(.block-translation)');
  }
  let offset = anchor?.offset;
  if (!element && anchor?.pageId) { element = document.getElementById(anchor.pageId); offset = anchor.pageOffset; }
  const target = element ? canvas.scrollTop + element.getBoundingClientRect().top - canvas.getBoundingClientRect().top - offset : fallback;
  canvas.scrollTo({ top: Math.max(0, target), behavior: 'instant' });
}
function readerLayout(detail) {
  const doc = detail.document;
  return JSON.stringify({ title: detail.paper.title, authors: authorLabel(detail.paper), pages: doc.pages, figures: doc.figures, paragraph_groups: doc.paragraph_groups || [] }, (key, value) => ['text_zh', 'caption_zh'].includes(key) ? undefined : value);
}
function translationText(element, text, visible = true) {
  if (!element) return;
  const value = text || '';
  if (element.textContent !== value) element.textContent = value;
  element.hidden = !visible || !value;
}
function figureTranslation(figure) {
  if (figure.caption_zh) return figure.caption_zh;
  const caption = String(figure.caption || '').replace(/\s+/g, ' ').trim();
  const page = state.detail.document.pages.find(item => Number(item.number) === Number(figure.page));
  return page?.blocks?.find(block => block.id === figure.caption_block_id || (caption && String(block.text || '').replace(/\s+/g, ' ').trim() === caption))?.text_zh || '';
}
function paragraphGroupMap(doc) {
  const known = new Set((doc.pages || []).flatMap(page => (page.blocks || []).map(block => block.id))), groups = new Map();
  for (const group of doc.paragraph_groups || []) {
    const ids = group.block_ids;
    if (!Array.isArray(ids) || ids.length < 2 || !String(group.text || '').trim() || new Set(ids).size !== ids.length || ids.some(id => !known.has(id) || groups.has(id))) continue;
    ids.forEach((id, index) => groups.set(id, { group, last: index === ids.length - 1 }));
  }
  return groups;
}
function blockTranslation(block, groups = paragraphGroupMap(state.detail.document)) {
  if (block.translation_scope === 'figure_content') return '';
  const paragraph = groups.get(block.id);
  // A logical paragraph can span physical pages or an intervening figure.
  // Show its complete translation once, after its final source fragment.
  return paragraph ? paragraph.last ? paragraph.group.text_zh || '' : '' : block.text_zh || '';
}
function translationCoverage(detail = state.detail) {
  const paper = detail.paper, doc = detail.document;
  const total = Number(paper.translation_total), completed = Number(paper.translation_completed), remaining = Number(paper.translation_remaining);
  if ([total, completed, remaining].every(value => Number.isFinite(value) && value >= 0)) {
    return { total, completed, remaining, complete: total > 0 && remaining === 0, progress: total > 0 ? Math.round(completed / total * 100) : 0 };
  }
  const normalized = value => String(value || '').replace(/\s+/g, ' ').trim();
  const blocks = (doc.pages || []).flatMap(page => (page.blocks || []).map(block => ({ ...block, page: page.number })));
  const linked = new Set(), targets = [];
  for (const figure of doc.figures || []) {
    const caption = normalized(figure.caption); if (!caption) continue;
    const block = blocks.find(item => item.id === figure.caption_block_id) || blocks.find(item => Number(item.page) === Number(figure.page) && normalized(item.text) === caption);
    if (block) linked.add(block.id);
    targets.push(!!normalized(figure.caption_zh || block?.text_zh));
  }
  const figureTypes = new Set(['figure', 'image', 'table', 'table_cell', 'figure_label', 'chart']);
  const paragraphs = paragraphGroupMap(doc), counted = new Set();
  for (const [id, { group }] of paragraphs) {
    linked.add(id);
    if (!counted.has(group)) { targets.push(!!normalized(group.text_zh)); counted.add(group); }
  }
  for (const block of blocks) if (normalized(block.text) && !linked.has(block.id) && block.translation_scope !== 'figure_content' && !figureTypes.has(block.type)) targets.push(!!normalized(block.text_zh));
  const done = targets.filter(Boolean).length;
  return { total: targets.length, completed: done, remaining: targets.length - done, complete: targets.length > 0 && done === targets.length, progress: targets.length ? Math.round(done / targets.length * 100) : 0 };
}
function translationActivity(paper = currentPaper()) { return translationRequests.has(paper.id) || ['queued', 'pending', 'processing'].includes(paper.translation_status); }
function translationControl() {
  const paper = currentPaper(), coverage = translationCoverage(), requesting = translationRequests.has(paper.id), busy = translationActivity(paper);
  const label = requesting ? '正在启动…' : busy ? `${paper.translation_status === 'queued' || paper.translation_status === 'pending' ? '等待翻译' : '翻译中'} ${coverage.progress}%` : coverage.complete ? state.showTranslation ? '隐藏译文' : '全文翻译' : coverage.completed || paper.translation_status === 'error' ? '继续翻译' : '全文翻译';
  const title = requesting ? '正在提交全文翻译任务' : busy ? `正在后台翻译，已完成 ${coverage.completed} / ${coverage.total} 段${state.readerView === 'pdf' || !state.showTranslation ? '，点击显示已生成的译文' : '，译文会逐步显示'}` : state.readerView === 'pdf' ? '切换至 AI 重排并显示双语译文' : coverage.complete ? '显示或隐藏全文译文' : '翻译尚未完成的正文与图注';
  return { label, title, busy, disabled: busy && state.readerView === 'reflow' && state.showTranslation, html: `${busy ? '<span class="spinner"></span>' : icon('translate')}<span>${esc(label)}</span>` };
}
function refreshTranslationControl() {
  const button = document.querySelector('[data-action="translate-full"]'); if (!button) return;
  const control = translationControl();
  button.classList.toggle('active', state.showTranslation); button.classList.toggle('translating', control.busy);
  button.disabled = control.disabled; button.title = control.title;
  button.setAttribute('aria-busy', String(control.busy)); button.setAttribute('aria-pressed', String(state.showTranslation));
  if (button.innerHTML !== control.html) button.innerHTML = control.html;
}
function translationNoticeHtml() {
  const paper = currentPaper(), coverage = translationCoverage(), requesting = translationRequests.has(paper.id);
  let message = '', action = '';
  if (requesting) message = '正在启动全文翻译…';
  else if (translationActivity(paper)) message = `${['queued', 'pending'].includes(paper.translation_status) ? '全文翻译已排队，正在等待前序任务完成' : '正在生成全文译文'}，已完成 ${coverage.completed} / ${coverage.total} 段（${coverage.progress}%）。译文会逐步显示。`;
  else if (paper.translation_status === 'needs_configuration') { message = paper.translation_error || '请先配置翻译模型，再继续生成全文译文。'; action = '<button data-action="settings">设置模型</button> <button data-action="retry-translation">继续翻译</button>'; }
  else if (paper.translation_status === 'error') { message = paper.translation_error || '部分正文翻译未完成，已生成的译文已保留。'; action = '<button data-action="retry-translation">重试翻译</button> <button data-action="settings">模型设置</button>'; }
  else if (state.showTranslation && !coverage.complete) { message = coverage.completed ? `已完成 ${coverage.completed} / ${coverage.total} 段，仍有正文或图注待翻译。` : '全文译文尚未生成。'; action = '<button data-action="retry-translation">继续翻译</button>'; }
  return message ? `<div class="document-warning translation-notice" role="status" aria-live="polite">${translationActivity(paper) ? '<span class="spinner"></span>' : ''}<span>${esc(message)} ${action}</span></div>` : '';
}
function documentNoticesHtml() {
  const { paper: p, document: doc } = state.detail;
  return `${p.ai_status === 'processing' || p.ai_status === 'pending' ? `<div class="document-warning"><span class="processing-inline"><span class="spinner"></span>AI 正在优化阅读顺序与段落结构，你可以先开始阅读。</span></div>` : !p.summary && !p.ai_error ? `<div class="document-warning">当前显示已提取的阅读结构。<button data-action="settings">配置模型</button>后，将自动进行 AI 总结与结构优化。</div>` : p.ai_error ? `<div class="document-warning">${esc(p.ai_error)} <button data-action="reprocess">重新整理</button></div>` : ''}${p.layout_error && p.translation_status !== 'error' ? `<div class="document-warning">段落整理未完成：${esc(p.layout_error)}</div>` : ''}${translationNoticeHtml()}${(doc.warnings || []).map(w => `<div class="document-warning">${esc(w)}</div>`).join('')}`;
}
function updateReader(previous = state.detail) {
  const session = openSession(); if (session && isCurrentPaper(session.id)) session.detail = state.detail;
  const canvas = document.querySelector('#reader-canvas');
  if (!canvas) return renderReader();
  const anchor = captureReadingAnchor(canvas), scroll = canvas.scrollTop;
  if (readerLayout(previous) !== readerLayout(state.detail)) {
    canvas.innerHTML = state.readerView === 'pdf' ? originalHtml() : documentHtml();
    bindImageFallbacks();
  } else if (state.readerView === 'reflow') {
    const paragraphs = paragraphGroupMap(state.detail.document);
    for (const page of state.detail.document.pages) for (const block of page.blocks || []) {
      const element = document.getElementById(`block-${block.id}`)?.querySelector('.block-translation');
      translationText(element, blockTranslation(block, paragraphs), state.showTranslation);
    }
    const figures = new Map(state.detail.document.figures.map(figure => [figure.id, figure]));
    canvas.querySelectorAll('[data-figure-id]').forEach(element => {
      const figure = figures.get(element.dataset.figureId);
      translationText(element.querySelector('.figure-translation'), figure ? figureTranslation(figure) : '', state.showTranslation);
    });
    translationText(canvas.querySelector('.document-title-zh'), currentPaper().title_zh);
    const notices = canvas.querySelector('.document-notices'), html = documentNoticesHtml();
    if (notices && notices.innerHTML !== html) notices.innerHTML = html;
  }
  if (JSON.stringify(previous.highlights) !== JSON.stringify(state.detail.highlights)) {
    for (const page of state.detail.document.pages) for (const block of page.blocks || []) {
      const element = document.getElementById(`block-${block.id}`)?.querySelector('.block-text:not(.block-translation)');
      if (element) { const html = highlightedText(block.text || '', block.id, page.number); if (element.innerHTML !== html) element.innerHTML = html; }
    }
  }
  refreshTranslationControl();
  restoreReadingAnchor(canvas, anchor, scroll);
  const oldSidebar = state.readerTab === 'toc' ? previous.document.toc : state.readerTab === 'figures' ? previous.document.figures : state.readerTab === 'notes' ? previous.highlights : state.readerTab === 'points' ? [previous.document.reading_points, previous.paper.ai_status] : previous.chat;
  const newSidebar = state.readerTab === 'toc' ? state.detail.document.toc : state.readerTab === 'figures' ? state.detail.document.figures : state.readerTab === 'notes' ? state.detail.highlights : state.readerTab === 'points' ? [state.detail.document.reading_points, currentPaper().ai_status] : state.detail.chat;
  if (JSON.stringify(oldSidebar) !== JSON.stringify(newSidebar)) refreshSidebar(true);
}
function renderReader() {
  const oldCanvas = document.querySelector('#reader-canvas');
  const session = openSession(), savedChat = session?.chat;
  if (session) session.detail = state.detail;
  const samePaper = oldCanvas?.dataset.paperId === currentPaper().id;
  const sameView = samePaper && oldCanvas.classList.contains('pdf-mode') === (state.readerView === 'pdf');
  const anchor = sameView ? captureReadingAnchor(oldCanvas) : !samePaper ? session?.anchor : null;
  if (oldCanvas?.dataset.paperId === currentPaper().id) { if (oldCanvas.classList.contains('pdf-mode')) state.originalScroll = oldCanvas.scrollTop; else state.reflowScroll = oldCanvas.scrollTop; }
  const previousScroll = state.readerView === 'pdf' ? state.originalScroll : state.reflowScroll;
  const chatInput = samePaper ? document.querySelector('#chat-input') : null;
  const chatDraft = chatInput ? chatInput.value : savedChat?.draft || '';
  const chatFocused = chatInput ? document.activeElement === chatInput : !!savedChat?.focused;
  const chatCaret = chatInput?.selectionStart ?? savedChat?.start ?? 0;
  const chatEnd = chatInput?.selectionEnd ?? savedChat?.end ?? 0;
  const chatDirection = chatInput?.selectionDirection || savedChat?.direction || 'none';
  const oldHistory = samePaper ? document.querySelector('#chat-history') : null, chatScroll = oldHistory?.scrollTop ?? savedChat?.scroll ?? 0;
  const oldSide = samePaper ? document.querySelector('#side-content') : null;
  const oldSideScroll = oldSide?.scrollTop ?? session?.sideScroll ?? 0;
  document.title = `${currentPaper().title_zh || currentPaper().title} · 论文透镜`;
  app.className = 'reader-mode';
  app.innerHTML = `${header(true)}<div class="reader-toolbar"><button class="reader-label" data-action="toggle-sidebar" title="展开或收起阅读助手">${icon('sidebar')}<span>阅读助手</span></button><div class="reader-tools"><button class="tool-button ${state.showTranslation ? 'active' : ''}" data-action="translate-full" title="${state.readerView === 'pdf' ? '切换至 AI 重排并显示双语译文' : '显示或隐藏全文译文'}">${icon('translate')}全文翻译</button><button class="tool-button ${state.showHighlights && state.readerView === 'reflow' ? 'active' : ''}" data-action="toggle-highlights" ${state.readerView === 'pdf' ? 'disabled title="高亮与划词请在 AI 重排中使用"' : ''}>${icon('bulb')}重点高亮</button><button class="tool-button ${state.fontOpen ? 'active' : ''}" data-action="font-menu" ${state.readerView === 'pdf' ? 'disabled title="字号调整请在 AI 重排中使用"' : ''}>${icon('text')}文字</button></div><div class="view-switch" aria-label="阅读视图"><button class="${state.readerView === 'reflow' ? 'active' : ''}" data-action="reader-view" data-view="reflow">AI 重排</button><button class="${state.readerView === 'pdf' ? 'active' : ''}" data-action="reader-view" data-view="pdf">原版 PDF</button></div>${state.fontOpen && state.readerView === 'reflow' ? fontHtml() : ''}</div><div class="reader-workspace"><aside class="reader-sidebar ${state.sidebarOpen ? '' : 'closed'}"><nav class="reader-tabs" aria-label="阅读助手">${[['chat','chat','AI 问答'],['toc','list','目录'],['figures','image','图表'],['notes','highlight','高亮笔记'],['points','bulb','阅读重点']].map(([tab, glyph, name]) => `<button class="reader-tab ${state.readerTab === tab ? 'active' : ''}" data-action="reader-tab" data-tab="${tab}">${icon(glyph)}<span>${name}</span></button>`).join('')}</nav><div class="side-content" id="side-content">${sidebarHtml()}</div></aside><main class="reader-canvas ${state.readerView === 'pdf' ? 'pdf-mode' : ''}" id="reader-canvas">${state.readerView === 'pdf' ? originalHtml() : documentHtml()}</main></div><footer class="reader-bottom"><span class="reader-bottom-left">${icon('shield')}阅读记录自动保存在本机</span><span id="reader-position">第 ${state.activePage} / ${Number(currentPaper().page_count) || 1} 页 <i class="reading-progress"><span style="width:${Math.round(state.activePage / (Number(currentPaper().page_count) || 1) * 100)}%"></span></i></span></footer>`;
  const canvas = document.querySelector('#reader-canvas'); canvas.dataset.paperId = currentPaper().id;
  restoreReadingAnchor(canvas, anchor, previousScroll);
  canvas.addEventListener('scroll', handleReadingScroll, { passive: true });
  bindImageFallbacks();
  refreshTranslationControl();
  if (state.readerTab === 'chat') {
    const input = document.querySelector('#chat-input');
    if (input) { input.value = chatDraft; if (chatFocused) { input.focus({ preventScroll: true }); input.setSelectionRange(chatCaret, chatEnd, chatDirection); } }
    if (oldHistory || savedChat) document.querySelector('#chat-history').scrollTop = chatScroll;
    else requestAnimationFrame(scrollChat);
  } else document.querySelector('#side-content').scrollTop = oldSideScroll;
}
function fontHtml() { return `<div class="font-popover"><h4>阅读字号</h4><div class="font-control"><button data-action="font-adjust" data-delta="-1" aria-label="减小字号">−</button><input type="range" id="font-size" min="14" max="25" value="${state.fontSize}" aria-label="阅读字号"><button data-action="font-adjust" data-delta="1" aria-label="增大字号">+</button><span id="font-size-label">${state.fontSize}</span></div><p>调整重排正文的字体大小，<br>为自己留一点舒适的阅读空间。</p></div>`; }
function originalHtml() {
  const paper = currentPaper(), document = state.detail.document;
  const pages = document.pages.length ? document.pages : Array.from({ length: Math.max(0, Number(paper.page_count) || 0) }, (_, index) => ({ number: index + 1, width: 612, height: 792 }));
  return `<div class="original-document"><div class="original-view-head"><span>原始版面 · 划词与双语功能请使用 AI 重排</span><a href="${paperUrl(paper.id, '/pdf')}" target="_blank" rel="noopener">打开原始 PDF ↗</a></div>${pages.length ? pages.map(page => {
    const width = Math.max(1, Number(page.width) || 612), height = Math.max(1, Number(page.height) || 792), number = Number(page.number) || 1;
    return `<section class="document-page original-page" id="page-${number}" data-page="${number}"><div class="original-page-label">第 ${number} 页</div><div class="original-page-sheet" style="aspect-ratio:${width}/${height}"><img src="${paperUrl(paper.id, `/pages/${number}`)}" alt="论文原版第 ${number} 页" loading="lazy" decoding="async" width="${Math.round(width)}" height="${Math.round(height)}"></div></section>`;
  }).join('') : '<div class="side-empty"><span class="spinner"></span><p>正在准备原版页面…</p></div>'}</div>`;
}
function documentHtml() {
  const detail = state.detail, p = detail.paper, doc = detail.document;
  if (!doc.pages.length) return `<div class="reading-document"><div class="empty-library" style="min-height:330px;border:0">${illustration()}<h2>${p.status === 'error' ? '暂时无法打开阅读内容' : '正在准备阅读内容'}</h2><p>${esc(p.error || p.stage || 'PDF 正在后台解析，完成后会自动出现在这里。')}</p>${p.status === 'error' ? `<button class="button secondary" data-action="reader-view" data-view="pdf">查看原版 PDF</button>` : '<span class="spinner"></span>'}</div></div>`;
  return `<article class="reading-document" id="reading-document" style="--reader-size:${state.fontSize}px"><div class="document-kicker">PAPER READING · ${Number(p.page_count) || doc.pages.length} PAGES</div><h1 class="document-title">${esc(p.title)}</h1><h2 class="document-title-zh" ${p.title_zh ? '' : 'hidden'}>${esc(p.title_zh)}</h2><div class="document-authors">${esc(authorLabel(p))}</div><div class="document-notices">${documentNoticesHtml()}</div>${doc.pages.map(pageHtml).join('')}<div class="document-page-label" style="justify-content:center;margin-top:45px">END OF PAPER</div></article>`;
}
function pageHtml(page) {
  const figures = state.detail.document.figures.filter(figure => Number(figure.page) === Number(page.number));
  const used = new Set();
  const normalized = text => String(text || '').replace(/\s+/g, ' ').trim();
  let content = '';
  for (const block of page.blocks || []) {
    const figure = figures.find(item => (
      item.id === block.figure_id || item.caption_block_id === block.id || (['figure', 'image'].includes(block.type) && item.id === block.id) ||
      (item.caption && normalized(item.caption) === normalized(block.text))
    ));
    if (figure) {
      if (used.has(figure.id)) continue;
      used.add(figure.id);
      content += `<div id="block-${attr(block.id)}" class="document-block" data-block-id="${attr(block.id)}" data-page="${Number(page.number)}">${figureHtml({ ...figure, caption_zh: figure.caption_zh || block.text_zh || '' })}</div>`;
    } else content += blockHtml(block, page);
  }
  content += figures.filter(figure => !used.has(figure.id)).map(figureHtml).join('');
  return `<section class="document-page" id="page-${Number(page.number)}" data-page="${Number(page.number)}"><div class="document-page-label">PAGE ${String(page.number).padStart(2, '0')}</div>${content}</section>`;
}
function blockHtml(block, page) {
  const type = /^[a-z_]+$/i.test(block.type || '') ? block.type : 'paragraph';
  if (type === 'figure' || type === 'image') { const figure = state.detail.document.figures.find(f => f.id === (block.figure_id || block.id)); if (figure) return figureHtml(figure); }
  if (block.translation_scope === 'figure_content' && state.detail.document.figures.some(figure => {
    if (Number(figure.page) !== Number(page.number)) return false;
    if (figure.id === block.figure_id) return true;
    const region = figure.bbox, rect = block.bbox;
    return region?.length === 4 && rect?.length === 4 && rect[0] >= region[0] - 1 && rect[1] >= region[1] - 1 && rect[2] <= region[2] + 1 && rect[3] <= region[3] + 1;
  })) return '';
  const text = block.text || '';
  if (!text.trim()) return '';
  // Displayed formulas are drawn from the PDF; their extracted pieces are not readable text.
  if (block.display_math) {
    // Match the formula's PDF type size to the reading size; the crop adds 3pt on each side.
    const box = block.bbox || [], scale = (Number(box[2]) - Number(box[0]) + 6) / (Number(block.font_size) || 10);
    const width = Number.isFinite(scale) && scale > 0 ? ` style="width:calc(var(--reader-size) * ${scale.toFixed(2)})"` : '';
    return `<div class="document-block block-formula-image" id="block-${attr(block.id)}" data-block-id="${attr(block.id)}" data-page="${Number(page.number)}"><img src="${paperUrl(currentPaper().id, `/formulas/${encodeURIComponent(block.id)}`)}" alt="${attr(text.replace(/\s+/g, ' '))}" loading="lazy"${width}></div>`;
  }
  const paperTitle = currentPaper().title || '';
  if (Number(page.number) === 1 && text.trim() === paperTitle.trim()) return '';
  if (Number(page.number) === 1 && text.trim() === authorLabel(currentPaper()).trim()) return '';
  const heading = ['heading', 'title', 'section_header', 'section_heading'].includes(type);
  const translated = blockTranslation(block);
  return `<div class="document-block block-${heading ? 'heading' : type} ${state.showHighlights ? '' : 'highlight-off'}" id="block-${attr(block.id)}" data-block-id="${attr(block.id)}" data-page="${Number(page.number)}"><${heading ? 'h2' : 'p'} class="block-text">${highlightedText(text, block.id, page.number)}</${heading ? 'h2' : 'p'}><${heading ? 'h2' : 'p'} class="block-text block-translation" lang="zh-CN" ${state.showTranslation && translated ? '' : 'hidden'}>${esc(translated)}</${heading ? 'h2' : 'p'}></div>`;
}
function highlightedText(text, blockId, page) {
  const notes = state.detail.highlights.filter(h => h.text && (h.block_id ? h.block_id === blockId || (h.text.includes('\n') && Number(h.page) === Number(page)) : Number(h.page) === Number(page)));
  const ranges = [];
  for (const note of notes) {
    const fragments = [note.text, ...note.text.split(/\n+/).map(fragment => fragment.trim()).filter(Boolean)];
    for (const fragment of new Set(fragments)) { const index = text.indexOf(fragment); if (index >= 0) ranges.push([index, index + fragment.length]); }
  }
  ranges.sort((a, b) => a[0] - b[0]); let output = '', position = 0;
  for (const [start, end] of ranges) { if (start < position) continue; output += esc(text.slice(position, start)) + `<mark>${esc(text.slice(start, end))}</mark>`; position = end; }
  return output + esc(text.slice(position));
}
function figureHtml(figure) {
  const source = paperUrl(currentPaper().id, `/figures/${encodeURIComponent(figure.id)}`);
  const bbox = figure.bbox || [], width = Number(bbox[2]) - Number(bbox[0]), height = Number(bbox[3]) - Number(bbox[1]);
  const dimensions = width > 0 && height > 0 ? `width="${Math.round(width * 2)}" height="${Math.round(height * 2)}" style="aspect-ratio:${width}/${height};height:auto"` : '';
  const translated = figureTranslation(figure);
  return `<figure class="document-figure" data-figure-id="${attr(figure.id)}"><img src="${source}" alt="${attr(figure.caption || '论文图表')}" loading="lazy" ${dimensions} data-action="view-image" data-src="${source}" data-title="${attr(figure.caption || '论文图表')}" data-hide-on-error>${figure.caption ? `<figcaption><span class="figure-original">${esc(figure.caption)}</span><span class="figure-translation" lang="zh-CN" ${state.showTranslation && translated ? '' : 'hidden'}>${esc(translated)}</span></figcaption>` : ''}</figure>`;
}
function figureDialogTitle(caption) {
  const text = String(caption || '').replace(/\s+/g, ' ').trim();
  const label = text.match(/^(?:(?:supplementary\s+)?(?:fig(?:ure)?\.?|table)\s*[A-Za-z]?\d+[a-z]?|[图表]\s*(?:\d+[a-z]?|[一二三四五六七八九十百]+))/i)?.[0];
  if (label) return label;
  const sentence = text.split(/[。！？!?]|\.(?:\s|$)/)[0];
  return sentence ? sentence.length > 64 ? `${sentence.slice(0, 64)}…` : sentence : '论文图表';
}
function sidebarHtml() {
  const doc = state.detail.document;
  if (state.readerTab === 'toc') return `<div class="side-eyebrow"><span>TABLE OF CONTENTS</span><button data-action="jump-top">回到顶部 ↑</button></div>${doc.toc.length ? doc.toc.map(item => `<button class="toc-item" style="--level:${Math.max(0, Math.min(4, (Number(item.level) || 1) - 1))}" data-action="jump" data-page="${Number(item.page) || 1}" data-block="${attr(item.block_id || '')}"><span class="toc-text"><span class="toc-original">${esc(item.title)}</span>${item.title_zh ? `<span class="toc-translation">${esc(item.title_zh)}</span>` : '<span class="toc-translation">中文目录待模型翻译</span>'}</span><span class="toc-page">${Number(item.page) || 1}</span></button>`).join('') : sideEmpty('list', '暂未识别到论文目录。<br>可以直接在正文中继续阅读。')}`;
  if (state.readerTab === 'figures') return `<div class="side-eyebrow"><span>FIGURES & TABLES</span><span>${doc.figures.length} 项</span></div>${doc.figures.length ? doc.figures.map(figure => { const src = paperUrl(currentPaper().id, `/figures/${encodeURIComponent(figure.id)}`); return `<article class="figure-card"><img class="figure-image" src="${src}" alt="${attr(figure.caption || '论文图表')}" loading="lazy" data-action="view-image" data-src="${src}" data-title="${attr(figure.caption || '论文图表')}" data-hide-on-error><div class="figure-caption"><div class="figure-page"><span>${figure.kind === 'table' ? 'TABLE' : 'FIGURE'}</span><button data-action="jump" data-page="${Number(figure.page) || 1}" style="font:inherit;color:inherit;padding:0">第 ${Number(figure.page) || 1} 页 ↗</button></div><p>${esc(figure.caption || '图注待识别')}</p><p class="figure-zh">${esc(figureTranslation(figure) || '中文图注待模型翻译')}</p></div></article>`; }).join('') : sideEmpty('image', '暂未提取到可展示的图表。<br>原始 PDF 中的图表仍可查看。')}`;
  if (state.readerTab === 'notes') return `<div class="side-eyebrow"><span>HIGHLIGHTS & NOTES</span><span>${state.detail.highlights.length} 条</span></div>${state.detail.highlights.length ? state.detail.highlights.map(note => `<article class="note-card"><p class="note-source">${esc(note.text)}</p><p class="note-translation">${esc(note.text_zh || (note.translation_status === 'needs_configuration' ? '配置翻译模型后生成中文译文' : note.translation_status === 'error' ? note.translation_error || '中文译文生成失败' : '中文译文待生成'))}</p>${note.note ? `<div class="note-comment">${esc(note.note)}</div>` : ''}<div class="note-actions"><span data-action="jump" data-page="${Number(note.page) || 1}" data-block="${attr(note.block_id || '')}">第 ${Number(note.page) || 1} 页 ↗</span><button data-action="edit-note" data-id="${attr(note.id)}" title="编辑笔记">${icon('edit')}</button><button data-action="delete-note" data-id="${attr(note.id)}" title="删除高亮">${icon('trash')}</button></div></article>`).join('') : sideEmpty('highlight', '在重排正文中选中一段文字，<br>点击「高亮」留下你的阅读痕迹。')}`;
  if (state.readerTab === 'points') { const points = doc.reading_points || []; return `<div class="side-eyebrow"><span>READING INSIGHTS</span>${icon('sparkles')}</div>${points.length ? points.map((point, index) => `<div class="point-item"><span class="point-number">${String(index + 1).padStart(2, '0')}</span><p>${esc(typeof point === 'string' ? point : point.text || point.content || '')}</p></div>`).join('') : sideEmpty('bulb', currentPaper().ai_status === 'processing' ? 'AI 正在整理论文的阅读重点…' : '配置 AI 模型后，<br>这里将展示论文的核心阅读重点。', true)}`; }
  return chatHtml();
}
function sideEmpty(glyph, text, settings = false) { return `<div class="side-empty">${icon(glyph)}<p>${text}</p>${settings ? '<button data-action="settings">设置模型 →</button>' : ''}</div>`; }
function refreshSidebar(preservePosition = false) {
  const content = document.querySelector('#side-content'); if (!content) return;
  const scroll = content.scrollTop, input = content.querySelector('#chat-input');
  const historyScroll = content.querySelector('#chat-history')?.scrollTop || 0;
  const draft = input?.value || '', focused = input && document.activeElement === input;
  const start = input?.selectionStart, end = input?.selectionEnd, direction = input?.selectionDirection;
  content.innerHTML = sidebarHtml(); bindImageFallbacks();
  const field = content.querySelector('#chat-input');
  if (field && preservePosition) {
    field.value = draft;
    if (focused) { field.focus({ preventScroll: true }); field.setSelectionRange(start, end, direction); }
  }
  if (field && !input && openSession()?.chat) {
    const saved = openSession().chat; field.value = saved.draft;
    field.setSelectionRange(saved.start, saved.end, saved.direction);
  }
  if (preservePosition) { content.scrollTop = scroll; const history = content.querySelector('#chat-history'); if (history) history.scrollTop = historyScroll; }
  else if (state.readerTab === 'chat') scrollChat();
}
function jumpTo(page, blockId = '') {
  state.activePage = Math.max(1, Number(page) || 1);
  const target = blockId && state.readerView === 'reflow' ? document.getElementById(`block-${blockId}`) : null;
  (target || document.getElementById(`page-${state.activePage}`))?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  if (window.innerWidth <= 680 && state.sidebarOpen) { state.sidebarOpen = false; document.querySelector('.reader-sidebar')?.classList.add('closed'); }
  persistReading();
}
function handleReadingScroll() {
  if (state.page !== 'reader') return;
  const canvas = document.querySelector('#reader-canvas'); if (!canvas) return;
  const top = canvas.getBoundingClientRect().top; let page = 1;
  for (const section of canvas.querySelectorAll('.document-page')) { if (section.getBoundingClientRect().top < top + canvas.clientHeight * .35) page = Number(section.dataset.page) || page; else break; }
  state.activePage = page;
  const footer = document.querySelector('#reader-position');
  if (footer) footer.innerHTML = `第 ${page} / ${Number(currentPaper().page_count) || 1} 页 <i class="reading-progress"><span style="width:${Math.min(100, Math.round(page / (Number(currentPaper().page_count) || 1) * 100))}%"></span></i>`;
  clearTimeout(state.readingTimer); state.readingTimer = setTimeout(persistReading, 650);
}
function persistReading() {
  if (!state.detail || state.page !== 'reader') return;
  const canvas = document.querySelector('#reader-canvas');
  const original = canvas?.classList.contains('pdf-mode');
  if (canvas) { if (original) state.originalScroll = canvas.scrollTop; else state.reflowScroll = canvas.scrollTop; }
  const reading = { page: state.activePage, scroll: original ? 0 : state.reflowScroll, font_size: state.fontSize };
  state.detail.reading = reading;
  api(paperUrl(currentPaper().id, '/reading'), { method: 'PUT', body: JSON.stringify(reading) }).catch(() => {});
}
function restoreReading() {
  const canvas = document.querySelector('#reader-canvas'); if (!canvas) return;
  if (state.detail.reading.scroll) canvas.scrollTo({ top: Number(state.detail.reading.scroll), behavior: 'instant' });
  else if (state.detail.reading.page > 1) jumpTo(state.detail.reading.page, state.detail.reading.block_id);
}
function setFont(size) {
  state.fontSize = Math.max(14, Math.min(25, Number(size) || 17));
  document.querySelector('#reading-document')?.style.setProperty('--reader-size', `${state.fontSize}px`);
  const input = document.querySelector('#font-size'); if (input) input.value = state.fontSize;
  const label = document.querySelector('#font-size-label'); if (label) label.textContent = state.fontSize;
  clearTimeout(state.readingTimer); state.readingTimer = setTimeout(persistReading, 650);
}
async function translateFull(button, force = false) {
  const paperId = currentPaper().id;
  const returningToReflow = state.readerView === 'pdf';
  const previousPage = state.activePage;
  if (returningToReflow) { state.readerView = 'reflow'; renderReader(); requestAnimationFrame(() => { if (isCurrentPaper(paperId) && state.page === 'reader' && state.readerView === 'reflow') jumpTo(previousPage); }); }
  const coverage = translationCoverage();
  if (!force && !returningToReflow && state.showTranslation && coverage.complete && !translationActivity()) { state.showTranslation = false; updateReader(); return; }
  state.showTranslation = true;
  updateReader();
  if ((!force && coverage.complete) || translationActivity()) return;
  translationRequests.add(paperId); updateReader();
  try {
    const data = await api(paperUrl(paperId, '/translate'), { method: 'POST', body: '{}' });
    updateTranslationPaper(paperId, data.paper || { translation_status: data.status || 'queued', translation_error: '' });
    if (isCurrentPaper(paperId) && state.page === 'reader') toast(translationCoverage().complete ? '全文译文已就绪' : '全文翻译已在后台开始，译文会逐步显示', 'success');
  }
  catch (error) {
    updateTranslationPaper(paperId, { translation_status: error.status === 503 ? 'needs_configuration' : 'error', translation_error: error.message });
    if (isCurrentPaper(paperId) && state.page === 'reader') toast(error.message, 'error');
  }
  finally { translationRequests.delete(paperId); if (isCurrentPaper(paperId) && state.page === 'reader') updateReader(); }
}
function updateTranslationPaper(paperId, patch) {
  const session = openSession(paperId), active = isCurrentPaper(paperId), detail = active ? state.detail : session?.detail;
  if (!detail) return;
  const next = { ...detail, paper: { ...detail.paper, ...patch } };
  if (session) session.detail = next;
  if (active) { state.detail = next; if (state.page === 'reader') updateReader(detail); }
}
async function reprocess(button) {
  const paperId = currentPaper().id;
  button.disabled = true;
  try { const result = await api(paperUrl(paperId, '/reprocess'), { method: 'POST', body: '{}' }); if (!isCurrentPaper(paperId)) return; if (result.paper) state.detail.paper = result.paper; toast('已重新启动 AI 整理', 'success'); if (state.modal === 'detail') renderModal(); else if (state.page === 'reader') renderReader(); }
  catch (error) { if (isCurrentPaper(paperId)) toast(error.message, 'error'); if (button.isConnected) button.disabled = false; }
}

/* Selection actions and contextual AI conversations. */
function captureSelection() {
  if (state.page !== 'reader' || state.modal || state.readerView !== 'reflow') return;
  const selection = window.getSelection();
  const text = selection?.toString().trim();
  if (!text || !selection.rangeCount) { hideSelection(); return; }
  const range = selection.getRangeAt(0);
  const origin = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer : range.startContainer.parentElement;
  const end = range.endContainer.nodeType === Node.ELEMENT_NODE ? range.endContainer : range.endContainer.parentElement;
  const block = origin.closest('.document-block');
  if (!origin.closest('#reading-document') || !end.closest('#reading-document')) { hideSelection(); return; }
  const rect = range.getBoundingClientRect();
  if (!rect.width && !rect.height) return;
  const parts = [];
  document.querySelectorAll('#reading-document .block-text').forEach(element => {
    if (!range.intersectsNode(element)) return;
    const part = document.createRange(); part.selectNodeContents(element);
    if (element.contains(range.startContainer)) part.setStart(range.startContainer, range.startOffset);
    if (element.contains(range.endContainer)) part.setEnd(range.endContainer, range.endOffset);
    const partText = part.toString().trim(), owner = element.closest('.document-block');
    if (partText) parts.push({ text: partText, block_id: owner.dataset.blockId, page: Number(owner.dataset.page) || state.activePage });
  });
  state.selected = { text, block_id: block?.dataset.blockId || '', page: Number(block?.dataset.page || origin.closest('[data-page]')?.dataset.page) || state.activePage, parts };
  const top = Math.max(112, rect.top - 52), width = Math.min(370, window.innerWidth - 20);
  const left = Math.max(10, Math.min(window.innerWidth - width - 10, rect.left + rect.width / 2 - width / 2));
  selectionRoot.innerHTML = `<div class="selection-toolbar" style="top:${top}px;left:${left}px"><button data-action="selection-explain">${icon('bulb')}AI 解释</button><button data-action="selection-ask">${icon('chat')}问 AI</button><button data-action="selection-translate">翻译</button><button data-action="selection-highlight">${icon('highlight')}高亮</button><button data-action="selection-copy" aria-label="复制文字" title="复制文字">${icon('copy')}</button></div>`;
}
function hideSelection() { selectionRoot.innerHTML = ''; }
async function selectionAI(action) {
  if (!state.selected?.text) return;
  const paperId = currentPaper().id, requestToken = ++selectionRequestToken;
  state.selectionAction = action; state.selectionResult = ''; state.modal = 'selection'; hideSelection(); renderModal();
  const selectedText = state.selected.text;
  try {
    const result = await api(paperUrl(paperId, '/selection'), { method: 'POST', body: JSON.stringify({ text: selectedText, action }) });
    if (!isCurrentPaper(paperId) || requestToken !== selectionRequestToken) return;
    state.selectionResult = result.text; if (state.modal === 'selection') renderModal();
  }
  catch (error) { if (isCurrentPaper(paperId) && requestToken === selectionRequestToken) { state.selectionResult = error.message; if (state.modal === 'selection') renderModal(); } }
}
async function addHighlight() {
  if (!state.selected?.text) return;
  const paperId = currentPaper().id, selectionReference = state.selected;
  const selected = { ...selectionReference }; hideSelection();
  try {
    const segments = selected.parts?.length > 1 ? selected.parts : [selected];
    for (const segment of segments) {
      const result = await api(paperUrl(paperId, '/highlights'), { method: 'POST', body: JSON.stringify({ text: segment.text, block_id: segment.block_id, page: segment.page, color: 'yellow' }) });
      if (isCurrentPaper(paperId) && !state.detail.highlights.some(note => note.id === result.highlight.id)) state.detail.highlights.push(result.highlight);
    }
    if (!isCurrentPaper(paperId)) return;
    if (state.page === 'reader' && state.selected === selectionReference) { window.getSelection()?.removeAllRanges(); state.showHighlights = true; state.readerTab = 'notes'; state.sidebarOpen = true; renderReader(); }
    toast('已添加高亮', 'success');
  } catch (error) { if (isCurrentPaper(paperId)) toast(error.message, 'error'); }
}
function askSelection() {
  if (!state.selected?.text) return;
  state.chatSelected = state.selected.text; closeModal(); hideSelection(); state.readerTab = 'chat'; state.sidebarOpen = true; renderReader();
  document.querySelector('#chat-input')?.focus();
}
function chatHtml() {
  return `<div class="chat-panel"><div class="chat-history" id="chat-history">${!state.detail.chat.length ? `<div class="chat-welcome"><div class="chat-orb">${icon('sparkles')}</div><h3>一起读懂这篇论文</h3><p>从一个问题开始。<br>你也可以在正文中划词，针对某段内容提问。</p></div><button class="chat-suggestion" data-action="chat-suggestion" data-question="这篇论文主要解决了什么问题？">这篇论文主要解决了什么问题？ ↗</button><button class="chat-suggestion" data-action="chat-suggestion" data-question="请解释这篇论文的核心方法和创新点。">核心方法与创新点是什么？ ↗</button><button class="chat-suggestion" data-action="chat-suggestion" data-question="这篇论文有哪些局限，以及值得进一步探索的方向？">有哪些局限与后续研究方向？ ↗</button>` : state.detail.chat.map(message => `<div class="chat-message ${message.role === 'user' ? 'user' : 'assistant'}">${esc(message.content)}</div>`).join('')}${state.chatBusy ? '<div class="processing-inline"><span class="spinner"></span>正在结合论文思考…</div>' : ''}</div><div class="chat-compose">${state.chatSelected ? `<div class="chat-selection"><span>${esc(state.chatSelected)}</span><button data-action="clear-chat-selection" title="取消引用">${icon('close')}</button></div>` : ''}<div class="chat-input-box"><textarea id="chat-input" placeholder="询问这篇论文…" rows="2" ${state.chatBusy ? 'disabled' : ''}></textarea><button class="chat-send" data-action="chat-send" aria-label="发送问题" ${state.chatBusy ? 'disabled' : ''}>${icon('send')}</button></div><p class="chat-footnote">AI 回答仅供参考，请结合原文判断</p></div></div>`;
}
function scrollChat() { const history = document.querySelector('#chat-history'); if (history) history.scrollTop = history.scrollHeight; }
async function sendChat(suggestion = '') {
  if (state.chatBusy) return;
  const input = document.querySelector('#chat-input'), message = suggestion || input?.value.trim();
  if (!message) return;
  const paperId = currentPaper().id, session = openSession(), detail = state.detail, requestToken = ++chatRequestToken;
  if (!session) return;
  session.chatRequestToken = requestToken;
  const selected = state.chatSelected; const userMessage = { role: 'user', content: message };
  detail.chat.push(userMessage); state.chatBusy = true; state.chatSelected = '';
  session.detail = detail; session.reader.chatBusy = true; session.reader.chatSelected = ''; session.chat = { draft: '', focused: true, start: 0, end: 0, direction: 'none', scroll: 0 };
  refreshSidebar(); refreshWorkspaceTabs();
  const active = () => state.page === 'reader' && state.activeDocumentId === paperId && openSession(paperId) === session;
  try {
    const result = await api(paperUrl(paperId, '/chat'), { method: 'POST', body: JSON.stringify({ message, ...(selected ? { selected_text: selected } : {}) }) });
    if (session.chatRequestToken === requestToken) detail.chat.push(result.message);
  }
  catch (error) { if (session.chatRequestToken === requestToken) { detail.chat = detail.chat.filter(item => item !== userMessage); session.chat.draft = message; if (active()) toast(error.message, 'error'); } }
  finally {
    if (session.chatRequestToken === requestToken) {
      session.reader.chatBusy = false;
      if (active()) {
        state.chatBusy = false; state.detail = detail;
        if (state.readerTab === 'chat') { refreshSidebar(); const field = document.querySelector('#chat-input'); if (field && !detail.chat.includes(userMessage)) field.value = message; }
      }
      refreshWorkspaceTabs();
    }
  }
}
async function saveNote(button) {
  const paperId = currentPaper().id, noteId = state.editNoteId;
  const note = document.querySelector('#note-text')?.value || ''; button.disabled = true;
  try {
    const result = await api(paperUrl(paperId, `/highlights/${encodeURIComponent(noteId)}`), { method: 'PATCH', body: JSON.stringify({ note }) });
    if (!isCurrentPaper(paperId)) return;
    state.detail.highlights = state.detail.highlights.map(item => item.id === noteId ? result.highlight : item);
    if (state.modal === 'note' && state.editNoteId === noteId) closeModal();
    if (state.page === 'reader') refreshSidebar(); toast('笔记已保存', 'success');
  }
  catch (error) { if (isCurrentPaper(paperId)) toast(error.message, 'error'); if (button.isConnected) button.disabled = false; }
}
async function deleteNote(id) {
  const paperId = currentPaper().id;
  try { await api(paperUrl(paperId, `/highlights/${encodeURIComponent(id)}`), { method: 'DELETE' }); if (!isCurrentPaper(paperId)) return; state.detail.highlights = state.detail.highlights.filter(item => item.id !== id); if (state.page === 'reader') renderReader(); toast('高亮已删除'); }
  catch (error) { if (isCurrentPaper(paperId)) toast(error.message, 'error'); }
}

/* Event routing, focus behavior, and background job updates. */
document.addEventListener('click', async event => {
  if (!event.target.closest('.model-combobox') && !event.target.closest('.model-options-popup')) closeModelPicker();
  if (event.target.matches('[data-model-input]')) openModelPicker(event.target, true);
  const button = event.target.closest('[data-action]');
  if (!button) {
    if (event.target.matches('[data-backdrop]') && !state.uploadBusy) closeModal();
    if (!event.target.closest('#reading-document') && !event.target.closest('#selection-root')) hideSelection();
    return;
  }
  const action = button.dataset.action;
  try {
    switch (action) {
      case 'home': goHome(); break;
      case 'navigate': goHome(button.dataset.page); break;
      case 'switch-document': await switchDocument(button.dataset.id); break;
      case 'close-document': closeDocument(button.dataset.id); break;
      case 'upload': state.modal = 'upload'; state.uploadTab = 'file'; renderModal(); break;
      case 'upload-tab': if (!state.uploadBusy) { state.uploadTab = button.dataset.tab; renderModal(); } break;
      case 'close-modal': if (!state.uploadBusy) closeModal(); break;
      case 'settings': await openSettings(); break;
      case 'add-provider': collectSettings(); state.settingsDraft.providers.push({ id: `provider-${Date.now()}`, name: '', base_url: '', api_key: '', default_model: '', has_key: false }); renderModal(); break;
      case 'remove-provider': collectSettings(); delete state.providerModels[button.dataset.id]; state.settingsDraft.providers = state.settingsDraft.providers.filter(p => p.id !== button.dataset.id); Object.values(state.settingsDraft.modules).forEach(m => { if (m.provider_id === button.dataset.id) m.provider_id = ''; }); renderModal(); break;
      case 'save-settings': await saveSettings(button); break;
      case 'test-provider': await testProvider(button.dataset.id, button); break;
      case 'fetch-provider-models': await fetchProviderModels(button.dataset.id, true); break;
      case 'toggle-model-options': { const input = button.closest('.model-combobox').querySelector('input'); if (state.modelPicker?.inputId === input.id) closeModelPicker(); else { openModelPicker(input, true); input.focus({ preventScroll: true }); } break; }
      case 'choose-model': chooseModel(button.dataset.value); break;
      case 'import-path': await importPath(button); break;
      case 'paper': await openPaper(button.dataset.id); break;
      case 'read-paper': await openPaper(button.dataset.id, true); break;
      case 'delete-paper': state.deletePaper = state.papers.find(p => p.id === button.dataset.id); state.modal = 'delete'; renderModal(); break;
      case 'confirm-delete': button.disabled = true; await api(paperUrl(state.deletePaper.id), { method: 'DELETE' }); closeDocument(state.deletePaper.id); closeModal(); await reloadPapers(); toast('文献已删除'); break;
      case 'clear-search': state.query = ''; updateLibrary(); break;
      case 'reload': await reloadPapers(); break;
      case 'toggle-sidebar': state.sidebarOpen = !state.sidebarOpen; document.querySelector('.reader-sidebar')?.classList.toggle('closed', !state.sidebarOpen); break;
      case 'reader-tab': rememberChat(); state.readerTab = button.dataset.tab; document.querySelectorAll('.reader-tab').forEach(el => el.classList.toggle('active', el === button)); refreshSidebar(); break;
      case 'reader-view': { const page = state.activePage, paperId = currentPaper().id, view = button.dataset.view; persistReading(); state.readerView = view; state.fontOpen = false; hideSelection(); renderReader(); requestAnimationFrame(() => { if (isCurrentPaper(paperId) && state.page === 'reader' && state.readerView === view) jumpTo(page); }); break; }
      case 'toggle-highlights': state.showHighlights = !state.showHighlights; renderReader(); break;
      case 'font-menu': state.fontOpen = !state.fontOpen; renderReader(); break;
      case 'font-adjust': setFont(state.fontSize + Number(button.dataset.delta)); break;
      case 'translate-full': await translateFull(button); break;
      case 'retry-translation': await translateFull(button, true); break;
      case 'reprocess': await reprocess(button); break;
      case 'jump': jumpTo(button.dataset.page, button.dataset.block); break;
      case 'jump-top': document.querySelector('#reader-canvas')?.scrollTo({ top: 0, behavior: 'smooth' }); break;
      case 'view-image': {
        const figureId = button.closest('[data-figure-id]')?.dataset.figureId;
        const figure = state.detail?.document.figures.find(item => item.id === figureId || paperUrl(currentPaper().id, `/figures/${encodeURIComponent(item.id)}`) === button.dataset.src);
        state.figureSrc = button.dataset.src; state.figureCaption = button.dataset.title || ''; state.figureTitle = figureDialogTitle(state.figureCaption);
        state.figureCaptionZh = figure ? figureTranslation(figure) : ''; state.modal = 'image'; renderModal(); break;
      }
      case 'selection-explain': await selectionAI('explain'); break;
      case 'selection-translate': await selectionAI('translate'); break;
      case 'selection-highlight': await addHighlight(); break;
      case 'selection-ask': askSelection(); break;
      case 'selection-copy': await navigator.clipboard.writeText(state.selected?.text || ''); hideSelection(); toast('已复制文字'); break;
      case 'edit-note': state.editNoteId = button.dataset.id; state.modal = 'note'; renderModal(); break;
      case 'save-note': await saveNote(button); break;
      case 'delete-note': await deleteNote(button.dataset.id); break;
      case 'chat-send': await sendChat(); break;
      case 'chat-suggestion': await sendChat(button.dataset.question); break;
      case 'clear-chat-selection': state.chatSelected = ''; refreshSidebar(); break;
    }
  } catch (error) { toast(error.message, 'error'); if (button.isConnected) button.disabled = false; }
});
document.addEventListener('input', event => {
  if (state.modal === 'settings' && event.target.matches('[data-setting]')) {
    const card = event.target.closest('[data-provider-id]'), id = card?.dataset.providerId;
    collectSettings();
    if (id) {
      const provider = state.settingsDraft.providers.find(item => item.id === id);
      if (state.providerModels[id] && state.providerModels[id].signature !== providerModelSignature(provider)) delete state.providerModels[id];
      updateProviderModelControls(id);
    }
    if (event.target.matches('[data-model-input]')) openModelPicker(event.target);
  }
  if (event.target.id === 'library-search') {
    state.query = event.target.value; updateLibrary();
  }
  if (event.target.id === 'font-size') setFont(event.target.value);
});
document.addEventListener('change', event => {
  if (state.modal !== 'settings' || !event.target.matches('[data-setting]')) return;
  const draft = collectSettings();
  const card = event.target.closest('[data-provider-id]');
  if (card && ['base_url', 'api_key'].includes(event.target.dataset.setting)) {
    fetchProviderModels(card.dataset.providerId);
  } else if (event.target.matches('select[data-setting="provider_id"]')) {
    const id = event.target.value;
    const row = event.target.closest('[data-module]'), input = row.querySelector('[data-setting="model"]');
    const provider = draft.providers.find(item => item.id === id);
    input.placeholder = moduleModelPlaceholder(provider);
    input.dataset.modelProvider = id;
    closeModelPicker();
    if (id) fetchProviderModels(id);
  }
});
document.addEventListener('keydown', event => {
  if (event.target.matches('[data-model-input]')) {
    if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
      event.preventDefault();
      if (state.modelPicker?.inputId !== event.target.id) openModelPicker(event.target, true);
      const picker = state.modelPicker, options = picker.options || [];
      if (options.length) { picker.activeIndex = picker.activeIndex < 0 ? (event.key === 'ArrowDown' ? 0 : options.length - 1) : (picker.activeIndex + (event.key === 'ArrowDown' ? 1 : options.length - 1)) % options.length; renderModelPicker(); document.getElementById(`model-option-${picker.activeIndex}`)?.scrollIntoView({ block: 'nearest' }); }
      return;
    }
    if (state.modelPicker && event.key === 'Enter' && state.modelPicker.activeIndex >= 0) { event.preventDefault(); chooseModel(state.modelPicker.options[state.modelPicker.activeIndex]); return; }
    if (state.modelPicker && event.key === 'Escape') { event.preventDefault(); closeModelPicker(); return; }
    if (event.key === 'Tab') closeModelPicker();
  }
  if (event.target.matches('.workspace-tabbar [role="tab"]') && ['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
    event.preventDefault(); const tabs = [...document.querySelectorAll('.workspace-tabbar [role="tab"]')], index = tabs.indexOf(event.target);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length;
    const target = tabs[next], id = target.dataset.id;
    (id ? switchDocument(id) : Promise.resolve(goHome())).then(() => (id ? [...document.querySelectorAll('.document-tab-link')].find(tab => tab.dataset.id === id) : document.querySelector('.workspace-home'))?.focus());
    return;
  }
  if (event.key === 'Escape') { if (state.modal && !state.uploadBusy) closeModal(); else if (state.fontOpen) { state.fontOpen = false; renderReader(); } hideSelection(); }
  if (event.key === 'Enter' && !event.shiftKey && event.target.id === 'chat-input') { event.preventDefault(); sendChat(); }
  if (event.key === 'Enter' && event.target.id === 'pdf-path') { event.preventDefault(); const button = document.querySelector('[data-action="import-path"]'); if (button) importPath(button); }
  if (event.key === 'Enter' && event.target.matches('.paper-card')) { event.preventDefault(); openPaper(event.target.dataset.id); }
  if (event.key === 'Tab' && state.modal) {
    const focusable = [...modalRoot.querySelectorAll('button:not(:disabled),input:not([type="hidden"]):not(:disabled),textarea,select,[tabindex="0"]')].filter(el => el.offsetParent !== null && el.tabIndex >= 0);
    if (!focusable.length) return;
    if (event.shiftKey && (document.activeElement === focusable[0] || !modalRoot.contains(document.activeElement))) { event.preventDefault(); focusable.at(-1).focus(); }
    else if (!event.shiftKey && (document.activeElement === focusable.at(-1) || !modalRoot.contains(document.activeElement))) { event.preventDefault(); focusable[0].focus(); }
  }
});
window.addEventListener('resize', positionModelPicker);
document.addEventListener('scroll', event => { if (state.modelPicker && !event.target.closest?.('.model-options-popup')) closeModelPicker(); }, true);
selectionRoot.addEventListener('mousedown', event => event.preventDefault());
document.addEventListener('mouseup', event => { if (!event.target.closest('#selection-root')) setTimeout(captureSelection, 20); });
document.addEventListener('touchend', () => setTimeout(captureSelection, 200), { passive: true });
document.addEventListener('selectionchange', () => { if (!window.getSelection()?.toString().trim()) hideSelection(); });
window.addEventListener('beforeunload', () => { if (state.detail && state.page === 'reader') { const payload = { page: state.activePage, scroll: state.readerView === 'pdf' ? 0 : document.querySelector('#reader-canvas')?.scrollTop || 0, font_size: state.fontSize }; fetch(paperUrl(currentPaper().id, '/reading'), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload), keepalive: true }).catch(() => {}); } });
let pollBusy = false;
async function poll() {
  if (pollBusy || document.hidden) return; pollBusy = true;
  try {
    const paperList = await api('/api/papers');
    const changed = JSON.stringify(state.papers) !== JSON.stringify(paperList.papers || []) || state.dryRun !== !!paperList.dry_run || !!state.error;
    state.papers = paperList.papers || []; state.dryRun = !!paperList.dry_run; state.error = '';
    if (changed && state.page === 'library') updateLibrary();
    if (state.detail && (state.page === 'reader' || state.modal === 'detail')) {
      const paperId = currentPaper().id, token = paperOpenToken;
      const detail = normalizeDetail(await api(paperUrl(paperId)));
      if (!isCurrentPaper(paperId) || token !== paperOpenToken) return;
      const different = JSON.stringify({ p: detail.paper, d: detail.document, h: detail.highlights, c: detail.chat }) !== JSON.stringify({ p: state.detail.paper, d: state.detail.document, h: state.detail.highlights, c: state.detail.chat });
      if (different && !state.chatBusy && !state.modal?.match(/note|selection|settings|image/) && !window.getSelection()?.toString().trim()) {
        const previous = state.detail, reading = state.detail.reading; state.detail = { ...detail, reading };
        if (state.page === 'reader') updateReader(previous);
        if (state.page === 'reader' && (previous.paper.title !== detail.paper.title || previous.paper.title_zh !== detail.paper.title_zh)) refreshWorkspaceTabs();
        if (state.modal === 'detail') renderModal();
      }
    }
  } catch { /* A later poll reconnects without disturbing the active reading session. */ }
  finally { pollBusy = false; }
}
async function initialize() {
  const results = await Promise.allSettled([api('/api/papers'), api('/api/settings')]);
  if (results[0].status === 'fulfilled') { state.papers = results[0].value.papers || []; state.dryRun = !!results[0].value.dry_run; }
  else state.error = `无法连接本地服务：${results[0].reason.message}`;
  if (results[1].status === 'fulfilled') state.settings = results[1].value;
  render(); setInterval(poll, 4000);
}
initialize();
