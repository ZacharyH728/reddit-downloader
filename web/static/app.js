/* Creator downloader UI. No build step: plain ES modules, no framework.
 *
 * Two implementation choices matter for large creators:
 *  - Tile DOM nodes are created once and cached by item id, so "have"/selection/
 *    failure updates toggle a class instead of re-rendering a 1000-item grid.
 *  - Off-screen <video> elements get their src cleared, or a phone runs out of
 *    memory scrolling through a few hundred clips.
 */

const $ = (sel) => document.querySelector(sel);

const state = {
  route: { name: 'search' },
  health: null,
  platform: localStorage.getItem('rd_platform') || 'both',
  query: '',
  searching: false,
  results: null,
  library: [],
  creator: null,
  items: [],
  next: null,
  loadingMore: false,
  exhausted: false,
  sort: 'new',
  kind: 'all',
  onlyMissing: false,
  selected: new Set(),
  queued: new Set(),
  failed: new Set(),
  lastAnchor: null,
  showNsfw: localStorage.getItem('rd_nsfw') === 'show',
  jobs: [],
  activeJobs: 0,
  drawerOpen: false,
  pollTimer: null,
  lightbox: null,
};

const tileCache = new Map();
let currentVideo = null;
let videoObserver = null;

/* Search sections render in this order. Trimmed at boot to whatever
 * /api/health says is actually configured. */
let PLATFORMS = ['reddit', 'redgifs', 'twitter'];
const PLATFORM_LABELS = { reddit: 'Reddit', redgifs: 'RedGifs', twitter: 'X' };
/* How a creator's own handle is written on each platform. */
const HANDLE_PREFIX = { reddit: 'u/', twitter: '@', redgifs: '' };

/* Type-filter names. `KIND_FILTERS` in core/creators.py decides what each one
 * actually matches; these are only the words for it. */
const KIND_PLURALS = { video: 'videos', image: 'images', gallery: 'galleries' };

/* "Download everything" respects the filters on screen, so the button has to say
 * so — otherwise a grid filtered to images looks like it will fetch the lot. */
function downloadAllLabel() {
  if (state.kind === 'all') {
    return state.onlyMissing ? 'Download all missing' : 'Download everything';
  }
  const noun = KIND_PLURALS[state.kind] || state.kind;
  return state.onlyMissing ? `Download missing ${noun}` : `Download all ${noun}`;
}

/* Highlight the active platform button. Called at bind time and again after
 * boot, once /api/health has said which platforms actually exist. */
function syncPlatformToggle() {
  for (const b of $('#platform-toggle').children) {
    b.classList.toggle('on', b.dataset.platform === state.platform);
  }
}

/* ---------------------------------------------------------------- api ---- */

class ApiError extends Error {
  constructor(code, message, status, retryAfter) {
    super(message || code);
    this.code = code;
    this.status = status;
    this.retryAfter = retryAfter;
  }
}

async function api(path, options = {}) {
  const headers = Object.assign({}, options.headers);
  if (options.body) headers['Content-Type'] = 'application/json';

  const response = await fetch(path, Object.assign({}, options, { headers }));
  let payload = null;
  try { payload = await response.json(); } catch (e) { /* empty body */ }

  if (!response.ok) {
    const err = (payload && payload.error) || {};
    throw new ApiError(err.code || 'http_error', err.message, response.status,
                       err.retry_after);
  }
  return payload;
}

/* Media is hotlinked from the CDN by default: it is faster, and RedGifs' CDN
 * needs no headers at all. The proxy is the fallback for URLs the browser can't
 * fetch itself (notably signed preview.redd.it links). */
function mediaUrl(url, { force = false } = {}) {
  if (!url) return '';
  if (force || (state.health && state.health.media_proxy_always)) {
    return `/api/proxy?url=${encodeURIComponent(url)}`;
  }
  return url;
}

function retryViaProxy(el, url) {
  if (!url || el.dataset.proxied === '1') return false;
  el.dataset.proxied = '1';
  el.src = mediaUrl(url, { force: true });
  return true;
}

/* ------------------------------------------------------------- helpers ---- */

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child);
  }
  return node;
}

function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

function duration(seconds) {
  if (!seconds && seconds !== 0) return '';
  const total = Math.round(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function bytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`;
}

function count(n) {
  if (n === null || n === undefined) return '';
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

function toast(message, kind = '') {
  const node = el('div', { class: `toast ${kind}`, text: message });
  $('#toasts').append(node);
  setTimeout(() => node.remove(), 5200);
}

function banner(message, kind = '') {
  const area = $('#banner-area');
  clear(area);
  if (message) area.append(el('div', { class: `banner ${kind}`, text: message }));
}

/* -------------------------------------------------------------- router ---- */

function parseHash() {
  const hash = location.hash.replace(/^#\/?/, '');
  const parts = hash.split('/').filter(Boolean).map(decodeURIComponent);
  if (parts[0] === 'c' && parts[1] && parts[2]) {
    return { name: 'creator', platform: parts[1], creator: parts[2] };
  }
  if (parts[0] === 'jobs') return { name: 'jobs' };
  return { name: 'search' };
}

async function route() {
  const next = parseHash();
  state.route = next;

  if (next.name === 'creator') {
    await openCreator(next.platform, next.creator);
  } else if (next.name === 'jobs') {
    state.drawerOpen = true;
    renderJobs();
    renderSearch();
  } else {
    renderSearch();
    if (!state.results) loadLibrary();
  }
}

/* -------------------------------------------------------------- search ---- */

async function doSearch(query) {
  state.query = query;
  state.searching = true;
  state.results = null;
  renderSearch();
  try {
    state.results = await api(
      `/api/search?q=${encodeURIComponent(query)}&platform=${state.platform}`);
  } catch (err) {
    banner(err.message || 'Search failed.', 'error');
    state.results = null;
  } finally {
    state.searching = false;
    renderSearch();
  }
}

async function loadLibrary() {
  try {
    const data = await api('/api/library');
    state.library = data.creators || [];
    if (state.route.name === 'search' && !state.results) renderSearch();
  } catch (err) { /* not important enough to surface */ }
}

function resultRow(item) {
  const avatar = item.avatar
    ? el('img', { src: mediaUrl(item.avatar), alt: '', loading: 'lazy',
                  referrerpolicy: 'no-referrer',
                  onerror: (e) => { if (!retryViaProxy(e.target, item.avatar)) {
                    e.target.replaceWith(el('div', { class: 'avatar-fallback', text: '?' })); } } })
    : el('div', { class: 'avatar-fallback', text: (item.display || '?')[0].toUpperCase() });

  const tags = el('div', { class: 'tags' },
    el('span', { class: 'chip plat', text: item.platform }),
    item.verified ? el('span', { class: 'chip', text: 'verified' }) : null,
    item.suspended ? el('span', { class: 'chip warn', text: 'suspended' }) : null,
    item.count ? el('span', { class: 'chip', text: `${count(item.count)} posts` }) : null,
    item.have_files ? el('span', { class: 'chip have', text: `${item.have_files} files` }) : null);

  return el('button', {
    class: 'result', type: 'button',
    onclick: () => { location.hash = `#/c/${item.platform}/${encodeURIComponent(item.name)}`; },
  }, avatar,
     el('div', { class: 'who' },
        el('b', { text: item.display || item.name }),
        el('span', { text: (HANDLE_PREFIX[item.platform] || '') + item.name })),
     tags);
}

function renderSearch() {
  const view = $('#view');
  clear(view);
  $('#search-input').value = state.query;

  if (state.searching) {
    view.append(el('div', { class: 'spinner', text: 'Searching…' }));
    return;
  }

  if (state.results) {
    let any = false;
    for (const platform of PLATFORMS) {
      const section = state.results[platform];
      if (!section) continue;
      const results = section.results || [];
      any = any || results.length > 0;
      view.append(el('div', { class: 'section-title' },
        el('span', { text: PLATFORM_LABELS[platform] || platform }),
        // X has no fuzzy index, so say so once here rather than letting an
        // empty section read as "this creator doesn't exist".
        section.exact_only && !results.length && !section.error
          ? el('span', { class: 'hint', text: 'exact handle only' }) : null,
        section.error ? el('span', { class: 'err', text: section.error }) : null));
      if (results.length) {
        view.append(el('div', { class: 'results' }, ...results.map(resultRow)));
      } else if (!section.error) {
        view.append(el('div', { class: 'empty', text: 'No creators matched.' }));
      }
    }
    if (!any) {
      view.append(el('div', { class: 'empty' },
        el('h2', { text: 'Nothing found' }),
        el('p', { text: 'Try the exact handle - Reddit and RedGifs index names '
                        + 'loosely, and X only matches exactly.' })));
    }
    return;
  }

  if (state.library.length) {
    view.append(el('div', { class: 'section-title' }, el('span', { text: 'Your library' })));
    view.append(el('div', { class: 'results' }, ...state.library.map((c) => resultRow({
      platform: c.platform, name: c.creator, display: c.creator,
      count: c.items, have_files: c.files,
    }))));
    return;
  }

  view.append(el('div', { class: 'empty' },
    el('h2', { text: 'Search for a creator' }),
    el('p', { text: 'Look someone up on Reddit or RedGifs, preview what they have, then download all of it or just the parts you want.' })));
}

/* ------------------------------------------------------------- creator ---- */

async function openCreator(platform, name) {
  state.creator = null;
  state.items = [];
  state.next = null;
  state.exhausted = false;
  state.selected.clear();
  state.queued.clear();
  state.failed.clear();
  state.lastAnchor = null;
  tileCache.clear();
  banner('');

  const view = $('#view');
  clear(view);
  view.append(el('div', { class: 'spinner', text: `Loading ${name}…` }));

  try {
    state.creator = await api(`/api/creators/${platform}/${encodeURIComponent(name)}`);
  } catch (err) {
    clear(view);
    view.append(creatorError(err, platform, name));
    return;
  }
  renderCreator();
  await loadMore();
}

function creatorError(err, platform, name) {
  const messages = {
    creator_not_found: [`No such ${platform} creator`,
                        `Nothing on ${platform} matches "${name}".`],
    creator_suspended: ['Account suspended', 'That account has been suspended.'],
    rate_limited: ['Rate limited', 'Too many requests just now. Try again shortly.'],
    invalid_creator: ['Invalid name', `"${name}" is not a valid ${platform} username.`],
    upstream_error: [`${platform} is unavailable`, 'The source is not responding.'],
  };
  const [heading, detail] = messages[err.code] || ['Could not load creator',
                                                   err.message || 'Unknown error.'];
  return el('div', { class: 'empty' },
    el('h2', { text: heading }),
    el('p', { text: detail }),
    el('p', {}, el('button', {
      type: 'button', text: 'Retry',
      onclick: () => openCreator(platform, name),
    })));
}

function renderCreator() {
  const view = $('#view');
  clear(view);
  const info = state.creator;
  if (!info) return;

  const profile = info.profile || {};
  const avatar = profile.avatar
    ? el('img', { src: mediaUrl(profile.avatar), alt: '', referrerpolicy: 'no-referrer',
                  onerror: (e) => { if (!retryViaProxy(e.target, profile.avatar)) {
                    e.target.replaceWith(el('div', { class: 'avatar-fallback', text: '?' })); } } })
    : el('div', { class: 'avatar-fallback', text: info.creator[0].toUpperCase() });

  view.append(el('div', { class: 'creator-head' },
    avatar,
    el('div', { class: 'who' },
      el('h1', { text: profile.display || info.creator }),
      el('div', { class: 'sub' },
        `${info.platform} · ${info.have.items} downloaded (${bytes(info.have.bytes)}) · ${info.dest}`),
      profile.suspended ? el('div', { class: 'sub' }, el('span', { class: 'chip warn', text: 'suspended' })) : null),
    el('div', { class: 'creator-actions' },
      profile.url ? el('a', { href: profile.url, target: '_blank', rel: 'noreferrer noopener',
                              class: 'chip', text: 'Open profile ↗' }) : null,
      el('button', { type: 'button', class: 'primary', text: downloadAllLabel(),
                     onclick: downloadAll }))));

  view.append(el('div', { class: 'toolbar' },
    el('button', {
      type: 'button', class: `toggle ${state.onlyMissing ? 'on' : ''}`,
      text: 'Only missing',
      onclick: () => { state.onlyMissing = !state.onlyMissing; reloadItems(); },
    }),
    el('button', {
      type: 'button', class: `toggle ${state.kind !== 'all' ? 'on' : ''}`,
      text: { all: 'All types', video: 'Videos', image: 'Images', gallery: 'Galleries' }[state.kind],
      onclick: () => {
        const order = ['all', 'video', 'image', 'gallery'];
        state.kind = order[(order.indexOf(state.kind) + 1) % order.length];
        reloadItems();
      },
    }),
    info.platform === 'reddit' ? el('button', {
      type: 'button', class: 'toggle',
      text: state.sort === 'new' ? 'Newest' : 'Top',
      onclick: () => { state.sort = state.sort === 'new' ? 'top' : 'new'; reloadItems(); },
    }) : null,
    el('button', {
      type: 'button', class: `toggle ${state.showNsfw ? 'on' : ''}`, text: 'Show NSFW',
      onclick: () => {
        state.showNsfw = !state.showNsfw;
        localStorage.setItem('rd_nsfw', state.showNsfw ? 'show' : 'blur');
        for (const [, node] of tileCache) {
          node.classList.toggle('blurred',
            !state.showNsfw && node.dataset.nsfw === '1' && node.dataset.revealed !== '1');
        }
      },
    }),
    el('span', { class: 'grow' }),
    el('span', { class: 'chip', id: 'load-status', text: '' })));

  view.append(el('div', { class: 'grid', id: 'grid' }));
  view.append(el('div', { id: 'grid-foot' }));
  updateLoadStatus();
  ensureVideoObserver();
}

function updateLoadStatus() {
  const node = $('#load-status');
  if (!node || !state.creator) return;
  const total = state.creator.platform === 'redgifs' && state.itemTotal
    ? ` of ~${state.itemTotal}` : '';
  node.textContent = `${state.items.length}${total} loaded`;

  const foot = $('#grid-foot');
  if (!foot) return;
  clear(foot);
  if (state.loadingMore) {
    foot.append(el('div', { class: 'spinner', text: 'Loading…' }));
  } else if (state.next) {
    foot.append(el('div', { class: 'spinner' },
      el('button', { type: 'button', text: 'Load more', onclick: loadMore })));
  } else if (state.items.length === 0) {
    foot.append(el('div', { class: 'empty' },
      el('h2', { text: state.onlyMissing ? 'Nothing missing' : 'Nothing downloadable' }),
      el('p', { text: state.onlyMissing
        ? 'Everything loaded from this creator is already downloaded.'
        : 'This creator has no posts this tool can download (text posts and external links are skipped).' })));
  } else {
    const notes = [`End of listing · ${state.items.length} items`];
    if (state.creator.platform === 'reddit' && state.items.length >= 900) {
      notes.push('Reddit only exposes roughly the most recent 1000 posts.');
    }
    if (state.creator.platform === 'twitter') {
      notes.push('X throttles deep paging; scroll again later if this stopped short.');
    }
    foot.append(el('div', { class: 'spinner', text: notes.join(' — ') }));
  }
}

async function reloadItems() {
  state.items = [];
  state.next = null;
  state.exhausted = false;
  tileCache.clear();
  renderCreator();
  await loadMore();
}

async function loadMore() {
  if (state.loadingMore || state.exhausted || !state.creator) return;
  state.loadingMore = true;
  updateLoadStatus();

  const info = state.creator;
  const params = new URLSearchParams({
    limit: '30', sort: state.sort, kind: state.kind,
    only: state.onlyMissing ? 'missing' : 'all',
  });
  if (state.next) params.set('cursor', state.next);

  try {
    const data = await api(
      `/api/creators/${info.platform}/${encodeURIComponent(info.creator)}/items?${params}`);
    if (data.total) state.itemTotal = data.total;
    const seen = new Set(state.items.map((i) => i.id));
    const fresh = (data.items || []).filter((i) => !seen.has(i.id));
    state.items.push(...fresh);
    state.next = data.next || null;
    if (!state.next) state.exhausted = true;
    appendTiles(fresh);
  } catch (err) {
    if (err.code === 'rate_limited') {
      banner(`Rate limited${err.retryAfter ? ` - retry in ${err.retryAfter}s` : ''}.`, 'error');
    } else if (err.code === 'twitter_auth') {
      // Distinct from a transient error: nothing on X will work again until the
      // cookie is replaced, so say that rather than "try again".
      banner('X rejected the saved session. Refresh TWITTER_AUTH_TOKEN and '
             + 'restart the server.', 'error');
    } else {
      banner(err.message || 'Could not load more items.', 'error');
    }
    state.exhausted = true;
  } finally {
    state.loadingMore = false;
    updateLoadStatus();
  }
}

/* --------------------------------------------------------------- tiles ---- */

function appendTiles(items) {
  const grid = $('#grid');
  if (!grid) return;
  for (const item of items) grid.append(buildTile(item));
}

function buildTile(item) {
  const cached = tileCache.get(item.id);
  if (cached) return cached;

  const thumbUrl = item.thumb || item.preview;
  const media = thumbUrl
    ? el('img', {
        class: 'thumb', alt: '', loading: 'lazy', decoding: 'async',
        referrerpolicy: 'no-referrer', src: mediaUrl(thumbUrl),
        onerror: (e) => {
          if (!retryViaProxy(e.target, thumbUrl)) {
            e.target.remove();
            tile.append(el('div', { class: 'noimg', text: 'no preview' }));
          }
        },
      })
    : el('div', { class: 'noimg', text: item.kind });

  const marks = el('div', { class: 'marks' },
    item.count > 1 ? el('span', { class: 'mark', text: `1/${item.count}` }) : null,
    item.duration ? el('span', { class: 'mark', text: duration(item.duration) }) : null,
    item.have ? el('span', { class: 'mark have', text: '✓ have' }) : null,
    (!item.have && item.in_saved) ? el('span', { class: 'mark saved', text: 'in saved' }) : null,
    item.gone ? el('span', { class: 'mark bad', text: 'deleted' }) : null);

  const check = el('button', {
    type: 'button', class: 'check', 'aria-label': 'Select',
    onclick: (e) => { e.stopPropagation(); toggleSelect(item.id, e.shiftKey); },
  }, '✓');

  const tile = el('div', {
    class: 'tile', dataset: { id: item.id, nsfw: item.nsfw ? '1' : '0' },
  }, media,
     el('button', {
       type: 'button', class: 'open', 'aria-label': item.title || 'Preview',
       onclick: (e) => {
         // Cmd/Ctrl-click selects instead of previewing.
         if (e.metaKey || e.ctrlKey) { toggleSelect(item.id, e.shiftKey); return; }
         openLightbox(state.items.findIndex((i) => i.id === item.id));
       },
     }),
     check, marks);

  if (item.nsfw && !state.showNsfw) {
    tile.classList.add('blurred');
    tile.append(el('button', {
      type: 'button', class: 'reveal', text: 'show',
      onclick: (e) => {
        e.stopPropagation();
        tile.dataset.revealed = '1';
        tile.classList.remove('blurred');
        e.target.remove();
      },
    }));
  }
  if (item.avg_color) tile.style.background = item.avg_color;

  tileCache.set(item.id, tile);
  return tile;
}

function refreshTileFlags() {
  for (const [id, node] of tileCache) {
    node.classList.toggle('selected', state.selected.has(id));
    node.classList.toggle('queued', state.queued.has(id));
    node.classList.toggle('failed', state.failed.has(id));
  }
}

/* ----------------------------------------------------------- selection ---- */

function toggleSelect(id, extend) {
  const ids = state.items.map((i) => i.id);
  if (extend && state.lastAnchor && state.lastAnchor !== id) {
    const from = ids.indexOf(state.lastAnchor);
    const to = ids.indexOf(id);
    if (from >= 0 && to >= 0) {
      const [lo, hi] = from < to ? [from, to] : [to, from];
      const selecting = !state.selected.has(id);
      for (let i = lo; i <= hi; i += 1) {
        if (selecting) state.selected.add(ids[i]);
        else state.selected.delete(ids[i]);
      }
    }
  } else if (state.selected.has(id)) {
    state.selected.delete(id);
  } else {
    state.selected.add(id);
  }
  state.lastAnchor = id;
  refreshTileFlags();
  renderActionBar();
}

function renderActionBar() {
  const bar = $('#actionbar');
  const n = state.selected.size;
  bar.hidden = n === 0 || state.route.name !== 'creator';
  $('#selection-count').textContent = `${n} selected`;
  $('#download-selected').textContent = `Download ${n} selected`;
}

async function downloadSelected() {
  if (!state.creator || !state.selected.size) return;
  const ids = [...state.selected];
  try {
    const data = await api('/api/downloads', {
      method: 'POST',
      body: JSON.stringify({
        platform: state.creator.platform,
        creator: state.creator.creator,
        mode: 'selected',
        ids,
      }),
    });
    for (const id of ids) state.queued.add(id);
    state.selected.clear();
    refreshTileFlags();
    renderActionBar();
    toast(`Downloading ${ids.length} item${ids.length === 1 ? '' : 's'}…`, 'ok');
    trackJob(data.job);
  } catch (err) {
    toast(err.message || 'Could not start the download.', 'err');
  }
}

async function downloadAll() {
  if (!state.creator) return;
  const info = state.creator;

  // These describe how much of the creator's history gets *scanned*. The
  // filters below then decide what of it is downloaded, so the two are quoted
  // separately rather than folded into one (wrong) number.
  let scanned = 'everything available';
  if (info.platform === 'redgifs' && state.itemTotal) {
    scanned = `${state.itemTotal} items`;
  } else if (info.platform === 'reddit') {
    scanned = 'up to about 1000 items (Reddit’s listing cap)';
  } else if (info.platform === 'twitter') {
    // X gives no total, so the job bar runs indeterminate and the only honest
    // number to quote is the cap the server will stop at.
    scanned = info.listing_cap
      ? `up to ${info.listing_cap} tweets (X paging cap)`
      : 'every tweet X will page through';
  }

  const filtered = state.kind !== 'all' || state.onlyMissing;
  const nouns = state.kind !== 'all' ? (KIND_PLURALS[state.kind] || state.kind) : 'items';
  const what = state.onlyMissing ? `${nouns} you don’t already have` : nouns;

  const prompt = filtered
    ? `Scan ${scanned} from ${info.creator} and download only the ${what} `
      + `into ${info.dest}?`
    : `Download ${scanned} from ${info.creator} into ${info.dest}?`;
  if (!confirm(prompt)) return;

  try {
    const data = await api('/api/downloads', {
      method: 'POST',
      body: JSON.stringify({
        platform: info.platform,
        creator: info.creator,
        mode: 'all',
        kind: state.kind,
        only: state.onlyMissing ? 'missing' : 'all',
      }),
    });
    toast(filtered ? `Started downloading ${what}…`
                   : 'Started downloading everything…', 'ok');
    trackJob(data.job);
  } catch (err) {
    toast(err.message || 'Could not start the download.', 'err');
  }
}

/* ---------------------------------------------------------------- jobs ---- */

function trackJob(job) {
  state.jobs = [job, ...state.jobs.filter((j) => j.id !== job.id)];
  state.drawerOpen = true;
  renderJobs();
  schedulePoll(600);
}

async function pollJobs() {
  try {
    const data = await api('/api/jobs?limit=25');
    const previouslyActive = new Set(
      state.jobs.filter((j) => j.active).map((j) => j.id));
    state.jobs = data.jobs || [];
    state.activeJobs = data.active || 0;

    // When a job for the creator on screen finishes, recompute "have" badges
    // server-side rather than inventing a per-item progress protocol.
    const justFinished = state.jobs.filter(
      (j) => !j.active && previouslyActive.has(j.id));
    for (const job of justFinished) {
      for (const id of job.failed_ids || []) state.failed.add(id);
      if (state.creator && job.platform === state.creator.platform
          && job.creator === state.creator.creator) {
        state.queued.clear();
        refreshTileFlags();
        await refreshHaveState();
      }
    }
    renderJobs();
  } catch (err) { /* transient; the next tick retries */ }
  schedulePoll();
}

function schedulePoll(delay) {
  clearTimeout(state.pollTimer);
  if (document.hidden) return;
  const active = state.activeJobs > 0 || state.jobs.some((j) => j.active);
  state.pollTimer = setTimeout(pollJobs, delay || (active ? 1000 : 5000));
}

async function refreshHaveState() {
  if (!state.creator) return;
  try {
    const info = await api(
      `/api/creators/${state.creator.platform}/${encodeURIComponent(state.creator.creator)}`);
    state.creator.have = info.have;
  } catch (err) { /* header stat only */ }
  // Re-fetch the pages we have loaded so `have` reflects the finished job.
  const loaded = state.items.length;
  state.items = [];
  state.next = null;
  state.exhausted = false;
  tileCache.clear();
  renderCreator();
  while (state.items.length < loaded && !state.exhausted) {
    await loadMore(); // eslint-disable-line no-await-in-loop
  }
  refreshTileFlags();
}

/* What a job covers, in the drawer. An "all" job carries the filters that were
 * on screen when it started, so "everything" alone would misreport it. `kind`
 * is guarded because a job from a pre-filter server has no such field. */
function jobScope(job) {
  if (job.mode !== 'all') return `${job.total || 0} selected`;
  const parts = [];
  if (job.kind && job.kind !== 'all') parts.push(KIND_PLURALS[job.kind] || job.kind);
  if (job.only === 'missing') parts.push('missing only');
  return parts.length ? parts.join(' · ') : 'everything';
}

function jobRow(job) {
  const total = job.total || 0;
  const pct = total ? Math.min(100, Math.round((job.completed / total) * 100)) : 0;
  const indeterminate = job.active && !total;

  return el('div', { class: 'job' },
    el('div', { class: 'row' },
      el('div', { class: 'grow' },
        el('div', { class: 'name', text: `${job.platform}/${job.creator}` }),
        el('div', { class: 'meta', text: jobScope(job) })),
      el('span', { class: `state ${job.state}`, text: job.state.replace(/_/g, ' ') })),
    el('div', { class: `bar ${indeterminate ? 'indeterminate' : ''}` },
      el('div', { style: `width:${pct}%` })),
    el('div', { class: 'counts' },
      el('span', { text: total ? `${job.completed}/${total}` : `${job.completed} done` }),
      job.downloaded ? el('span', { class: 'ok', text: `${job.downloaded} downloaded` }) : null,
      job.skipped ? el('span', { text: `${job.skipped} already had` }) : null,
      job.failed ? el('span', { class: 'bad', text: `${job.failed} failed` }) : null,
      job.gone ? el('span', { text: `${job.gone} deleted` }) : null),
    job.current ? el('div', { class: 'meta', text: job.current }) : null,
    job.error ? el('div', { class: 'meta', text: job.error }) : null,
    (job.errors && job.errors.length)
      ? el('details', {},
          el('summary', { text: `${job.errors.length} error${job.errors.length === 1 ? '' : 's'}` }),
          el('ul', {}, ...job.errors.slice(0, 25).map(
            (e) => el('li', { text: `${e.title || e.id}: ${e.error}` }))))
      : null,
    el('div', { class: 'row' },
      job.active ? el('button', {
        type: 'button', text: 'Cancel',
        onclick: async () => {
          try { await api(`/api/jobs/${job.id}/cancel`, { method: 'POST' }); pollJobs(); }
          catch (err) { toast(err.message, 'err'); }
        },
      }) : null,
      (!job.active && job.failed_ids && job.failed_ids.length) ? el('button', {
        type: 'button', text: `Retry ${job.failed_ids.length} failed`,
        onclick: async () => {
          try {
            const data = await api(`/api/jobs/${job.id}/retry-failed`, { method: 'POST' });
            trackJob(data.job);
          } catch (err) { toast(err.message, 'err'); }
        },
      }) : null));
}

function renderJobs() {
  const drawer = $('#jobs-drawer');
  drawer.hidden = !state.drawerOpen;
  const badge = $('#jobs-badge');
  const active = state.jobs.filter((j) => j.active).length;
  badge.hidden = active === 0;
  badge.textContent = String(active);

  if (!state.drawerOpen) return;
  const list = $('#jobs-list');
  clear(list);
  if (!state.jobs.length) {
    list.append(el('div', { class: 'empty', text: 'No downloads yet.' }));
    return;
  }
  for (const job of state.jobs) list.append(jobRow(job));
}

/* ----------------------------------------------------------- lightbox ---- */

function ensureVideoObserver() {
  if (videoObserver) return;
  videoObserver = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const video = entry.target;
      if (!entry.isIntersecting && video.src) {
        video.pause();
        video.removeAttribute('src');
        video.load();
      }
    }
  }, { rootMargin: '200px' });
}

function openLightbox(index) {
  if (index < 0 || index >= state.items.length) return;
  state.lightbox = { index, galleryIndex: 0 };
  $('#lightbox').hidden = false;
  renderLightbox();
}

function closeLightbox() {
  state.lightbox = null;
  $('#lightbox').hidden = true;
  if (currentVideo) { currentVideo.pause(); currentVideo = null; }
  clear($('#lb-stage'));
}

function moveLightbox(delta) {
  if (!state.lightbox) return;
  const item = state.items[state.lightbox.index];
  // Within a gallery, arrows step through its pages first.
  if (item && item.count > 1 && item.gallery && item.gallery.length) {
    const next = state.lightbox.galleryIndex + delta;
    if (next >= 0 && next < item.gallery.length) {
      state.lightbox.galleryIndex = next;
      renderLightbox();
      return;
    }
  }
  const target = state.lightbox.index + delta;
  if (target < 0 || target >= state.items.length) {
    if (target >= state.items.length && state.next) loadMore();
    return;
  }
  state.lightbox = { index: target, galleryIndex: 0 };
  renderLightbox();
}

async function renderLightbox() {
  if (!state.lightbox) return;
  const item = state.items[state.lightbox.index];
  if (!item) return;
  const stage = $('#lb-stage');
  clear(stage);
  if (currentVideo) { currentVideo.pause(); currentVideo = null; }

  let source = item.preview;
  let type = item.preview_type;
  let note = '';

  if (item.kind === 'gallery' && item.gallery && item.gallery.length) {
    source = item.gallery[state.lightbox.galleryIndex] || item.gallery[0];
    type = 'image';
  } else if (item.preview_type === 'redgifs' && item.redgifs_id) {
    // A RedGifs post found through a Reddit listing has no CDN URL yet.
    stage.append(el('div', { class: 'spinner', text: 'Resolving…' }));
    try {
      const media = await api(`/api/redgifs/gif/${item.redgifs_id}`);
      source = media.silent || media.sd || media.hd;
      type = 'video';
      if (!media.has_audio) note = 'no audio';
    } catch (err) {
      clear(stage);
      stage.append(el('div', { class: 'empty' },
        el('h2', { text: 'Unavailable' }),
        el('p', { text: err.code === 'gone_upstream'
          ? 'This gif has been deleted from RedGifs.' : (err.message || '') })));
      renderLightboxMeta(item, '');
      return;
    }
    clear(stage);
  } else if (item.kind === 'video' && item.preview_type === 'video') {
    if (item.source === 'twitter') {
      // X serves animated GIFs as silent MP4s. Nothing is missing from the
      // download here, unlike the v.redd.it case below.
      if (item.has_audio === false) note = 'no audio';
    } else {
      // v.redd.it fallback_url is the video-only DASH track.
      note = 'preview is silent - the download includes audio';
    }
  }

  if (!source) {
    stage.append(el('div', { class: 'empty', text: 'No preview available.' }));
  } else if (type === 'video') {
    const video = el('video', {
      src: mediaUrl(source), controls: true, autoplay: true, playsinline: true,
      loop: true, preload: 'metadata',
      onerror: () => { retryViaProxy(video, source); },
    });
    currentVideo = video;
    stage.append(video);
  } else {
    stage.append(el('img', {
      src: mediaUrl(source), alt: item.title || '', referrerpolicy: 'no-referrer',
      onerror: (e) => { retryViaProxy(e.target, source); },
    }));
  }
  renderLightboxMeta(item, note);
}

function renderLightboxMeta(item, note) {
  const meta = $('#lb-meta');
  clear(meta);
  const position = item.count > 1
    ? ` · ${state.lightbox.galleryIndex + 1}/${item.count}` : '';
  meta.append(
    el('div', { class: 'grow' },
      el('div', { class: 'title', text: (item.title || item.id) + position }),
      el('div', { text: [
        item.kind,
        item.duration ? duration(item.duration) : '',
        item.have ? 'already downloaded' : '',
        note,
      ].filter(Boolean).join(' · ') })),
    item.permalink ? el('a', { href: item.permalink, target: '_blank',
                               rel: 'noreferrer noopener', text: 'Source ↗' }) : null,
    el('button', {
      type: 'button', class: state.selected.has(item.id) ? 'primary' : '',
      text: state.selected.has(item.id) ? 'Selected' : 'Select',
      onclick: () => { toggleSelect(item.id, false); renderLightboxMeta(item, note); },
    }));
}

/* --------------------------------------------------------------- boot ---- */

function bindChrome() {
  $('#search-form').addEventListener('submit', (e) => {
    e.preventDefault();
    const query = $('#search-input').value.trim();
    if (query.length < 2) { toast('Enter at least two characters.', 'err'); return; }
    if (location.hash !== '#/') location.hash = '#/';
    doSearch(query);
  });

  $('#platform-toggle').addEventListener('click', (e) => {
    const button = e.target.closest('button[data-platform]');
    if (!button) return;
    state.platform = button.dataset.platform;
    localStorage.setItem('rd_platform', state.platform);
    syncPlatformToggle();
    if (state.query) doSearch(state.query);
  });
  syncPlatformToggle();

  $('#jobs-button').addEventListener('click', () => {
    state.drawerOpen = !state.drawerOpen;
    renderJobs();
    if (state.drawerOpen) pollJobs();
  });
  $('#jobs-close').addEventListener('click', () => {
    state.drawerOpen = false;
    renderJobs();
  });

  $('#select-all').addEventListener('click', () => {
    for (const item of state.items) state.selected.add(item.id);
    refreshTileFlags();
    renderActionBar();
  });
  $('#clear-selection').addEventListener('click', () => {
    state.selected.clear();
    refreshTileFlags();
    renderActionBar();
  });
  $('#download-selected').addEventListener('click', downloadSelected);

  $('#lb-close').addEventListener('click', closeLightbox);
  $('#lb-prev').addEventListener('click', () => moveLightbox(-1));
  $('#lb-next').addEventListener('click', () => moveLightbox(1));
  $('#lightbox').addEventListener('click', (e) => {
    if (e.target.id === 'lightbox') closeLightbox();
  });

  document.addEventListener('keydown', (e) => {
    if (state.lightbox) {
      if (e.key === 'Escape') closeLightbox();
      else if (e.key === 'ArrowLeft') moveLightbox(-1);
      else if (e.key === 'ArrowRight') moveLightbox(1);
      else if (e.key === ' ') { e.preventDefault(); toggleSelect(state.items[state.lightbox.index].id, false); }
      return;
    }
    const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
    if (typing) return;
    if (e.key === '/') { e.preventDefault(); $('#search-input').focus(); }
    else if (e.key === 'Escape' && state.selected.size) {
      state.selected.clear(); refreshTileFlags(); renderActionBar();
    } else if (e.key === 'a' && state.route.name === 'creator') {
      $('#select-all').click();
    } else if (e.key === 'd' && state.selected.size) {
      downloadSelected();
    }
  });

  // Infinite scroll.
  window.addEventListener('scroll', () => {
    if (state.route.name !== 'creator' || state.loadingMore || !state.next) return;
    if (window.innerHeight + window.scrollY >= document.body.offsetHeight - 900) {
      loadMore();
    }
  }, { passive: true });

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearTimeout(state.pollTimer);
    else schedulePoll(500);
  });

  window.addEventListener('hashchange', route);
}

async function boot() {
  try {
    state.health = await api('/api/health');
  } catch (err) {
    banner('Could not reach the server.', 'error');
  }

  if (state.health && state.health.platforms) {
    PLATFORMS = state.health.platforms;
  }
  // Hide the toggle for anything the server didn't report as configured, and
  // fall back to "Both" if the hidden one was the remembered choice.
  for (const button of document.querySelectorAll('#platform-toggle button')) {
    const supported = !button.dataset.optional
      || PLATFORMS.includes(button.dataset.platform);
    button.hidden = !supported;
    if (!supported && state.platform === button.dataset.platform) {
      state.platform = 'both';
      localStorage.setItem('rd_platform', 'both');
    }
  }
  syncPlatformToggle();

  const warnings = (state.health && state.health.warnings) || [];
  const notable = warnings.find((w) => w.code === 'reddit_credentials_missing');
  if (notable) banner(notable.message, 'error');

  await route();
  pollJobs();
}

bindChrome();
boot();
