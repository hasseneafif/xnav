'use strict';

// ═══ State ════════════════════════════════════════════════════════════════════
const VIEW = { OVERVIEW: 'overview', EXPANDED: 'expanded' };
let viewMode         = VIEW.OVERVIEW;
let expandedCluster  = null;
let graphData        = null;
let clusterIndex     = {};
let crossEdges       = [];
let clusterMetaNodes = [];
let clusterMetaLinks = [];
let topSim           = null;
let subSim           = null;
let nodeById         = {};
let hasEmbeddings    = false;
let isTransitioning  = false;
let zoomBehavior     = null;
let searchQuery      = '';
let searchMatchIds   = new Set();
let selectedNodeId   = null;
let adjMap           = new Map();  // nodeId → Set<neighborId>, rebuilt per cluster expand
let blastCache       = new Map();  // nodeId → Set, cleared on graph reload
let searchIndex      = [];         // [{id, text}], built once per graph load
let _searchTimer     = null;

// ═══ DOM ══════════════════════════════════════════════════════════════════════
const svg         = d3.select('#graph-svg');
const overlay     = document.getElementById('overlay');
const errorBox    = document.getElementById('error-box');
const sidebar     = document.getElementById('sidebar');
const sidebarBody = document.getElementById('sidebar-body');
const tooltip     = document.getElementById('tooltip');
const statsbar    = document.getElementById('statsbar');
const topStatus   = document.getElementById('topbar-status');
const backBtn     = document.getElementById('back-btn');
const searchInput = document.getElementById('search-input');
const searchBadge = document.getElementById('search-badge');
const legendPanel = document.getElementById('legend');
const legendBtn   = document.getElementById('legend-btn');

let zoomLayer, clusterLinkLayer, clusterLayer, nodeLinkLayer, nodeLayer;

// ═══ Controls ════════════════════════════════════════════════════════════════
document.getElementById('zoom-in-btn').addEventListener('click',  () => zoomBy(1.4));
document.getElementById('zoom-out-btn').addEventListener('click', () => zoomBy(0.71));
document.getElementById('zoom-fit-btn').addEventListener('click', zoomFit);
legendBtn.addEventListener('click', () => {
  legendPanel.classList.toggle('hidden');
  legendBtn.classList.toggle('active');
});
const openSidebarBtn = document.getElementById('open-sidebar-btn');
function hideSidebar() { sidebar.classList.add('hidden'); openSidebarBtn.classList.add('visible'); }
function showSidebar() { sidebar.classList.remove('hidden'); openSidebarBtn.classList.remove('visible'); }
document.getElementById('sidebar-close').addEventListener('click', () => { hideSidebar(); clearNodeSelection(); });
openSidebarBtn.addEventListener('click', () => showSidebar());
backBtn.addEventListener('click', () => exitExpanded());

// ═══ Keyboard shortcuts ═══════════════════════════════════════════════════════
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (!legendPanel.classList.contains('hidden')) {
      legendPanel.classList.add('hidden'); legendBtn.classList.remove('active');
    } else if (viewMode === VIEW.EXPANDED) {
      exitExpanded();
    } else {
      hideSidebar();
    }
    return;
  }
  if (e.key === '/' && document.activeElement !== searchInput) {
    e.preventDefault(); searchInput.focus(); searchInput.select();
  }
});

// ═══ Search ════════════════════════════════════════════════════════════════════
searchInput.addEventListener('input', () => {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(() => {
    searchQuery = searchInput.value.trim().toLowerCase();
    if (!searchQuery) {
      searchMatchIds.clear();
      searchBadge.textContent = '⌕';
      searchBadge.className = '';
    } else {
      searchMatchIds = new Set(
        searchIndex.filter(e => e.text.includes(searchQuery)).map(e => e.id)
      );
      if (searchMatchIds.size > 0) {
        searchBadge.textContent = searchMatchIds.size;
        searchBadge.className = 'has-match';
      } else {
        searchBadge.textContent = '0';
        searchBadge.className = 'no-match';
      }
    }
    applySearch();
  }, 120);
});
searchInput.addEventListener('keydown', e => { if (e.key === 'Escape') searchInput.blur(); });

function applySearch() {
  if (viewMode === VIEW.EXPANDED && nodeLayer) {
    if (!searchQuery) {
      nodeLayer.selectAll('g.node').classed('search-dim', false).classed('search-match', false);
    } else {
      nodeLayer.selectAll('g.node')
        .classed('search-dim',   d => !searchMatchIds.has(d.id))
        .classed('search-match', d =>  searchMatchIds.has(d.id));
    }
  } else if (viewMode === VIEW.OVERVIEW && clusterLayer) {
    if (!searchQuery) {
      clusterLayer.selectAll('g.cluster-blob').style('opacity', null);
    } else {
      clusterLayer.selectAll('g.cluster-blob').each(function(d) {
        const hit = d.nodes && d.nodes.some(n => searchMatchIds.has(n.id));
        d3.select(this).style('opacity', hit ? 1 : 0.15);
      });
    }
  }
}

// ═══ Zoom ══════════════════════════════════════════════════════════════════════
function zoomBy(factor) {
  if (zoomBehavior) svg.transition().duration(300).call(zoomBehavior.scaleBy, factor);
}
function zoomFit() {
  if (zoomBehavior) svg.transition().duration(400).call(zoomBehavior.transform, d3.zoomIdentity);
}
function onZoomLevel(k) {
  if (!nodeLayer) return;
  const opacity = k < 0.4 ? 0 : k < 0.85 ? (k - 0.4) / 0.45 : 1;
  nodeLayer.selectAll('text.node-label').style('opacity', opacity);
}

// ═══ Color palette ════════════════════════════════════════════════════════════
const CLUSTER_PALETTE = [
  '#a855f7',  // purple
  '#86efac',  // light green
  '#60a5fa',  // blue
  '#fb923c',  // orange
  '#fde047',  // yellow
  '#f9a8d4',  // baby pink
  '#2dd4bf',  // teal
  '#f87171',  // red
  '#fbbf24',  // amber
  '#c084fc',  // lavender
  '#38bdf8',  // sky
  '#4ade80',  // green
];
let _paletteIdx = 0;
const _colorMemo = {};
function clusterColor(name) {
  if (!name || name === 'External' || name === 'external') return '#6b7280';
  return clusterIndex[name]?.color || _colorMemo[name] || '#6b7280';
}

// ═══ Boot — poll for analysis ══════════════════════════════════════════════════
const POLL_INTERVAL   = 1200;
const STEP_THRESHOLDS = [0, 2, 6, 14, 22];

async function pollForGraph() {
  let attempts    = 0;
  let currentStep = 0;
  const steps = document.querySelectorAll('#overlay-steps .step');
  setStep(steps, 0, 'active');

  while (true) {
    await sleep(attempts === 0 ? 400 : POLL_INTERVAL);
    attempts++;

    let nextStep = 0;
    for (let i = 0; i < STEP_THRESHOLDS.length; i++) { if (attempts >= STEP_THRESHOLDS[i]) nextStep = i; }
    if (nextStep > currentStep) {
      setStep(steps, currentStep, 'done');
      currentStep = nextStep;
      setStep(steps, currentStep, 'active');
    }

    try {
      const res = await fetch('/api/graph');
      if (res.status === 503) { document.getElementById('overlay-sub').textContent = `Analyzing… (${attempts}s)`; continue; }
      if (!res.ok) {
        const body = await res.json().catch(() => ({ detail: res.statusText }));
        showError(body.detail || 'Unknown error'); return;
      }
      steps.forEach((s, i) => setStep(steps, i, 'done'));
      onGraphReady(await res.json()); return;
    } catch (e) {
      document.getElementById('overlay-sub').textContent = `Retrying… (${e.message})`;
    }
  }
}

function setStep(steps, idx, state) {
  if (!steps[idx]) return;
  const icons = { active: '●', done: '✓', pending: '○' };
  steps[idx].className = 'step ' + state;
  steps[idx].querySelector('.step-icon').textContent = icons[state] || '○';
}

function onGraphReady(data) {
  graphData = data;
  overlay.classList.add('hidden');
  const stats    = data.stats || {};
  const repoPath = stats.repo_path || '';
  const repoName = stats.repo_name || repoPath.split(/[/\\]/).filter(Boolean).pop() || 'project';
  document.getElementById('project-name').textContent = repoName;
  document.getElementById('project-path').textContent = repoPath;
  topStatus.textContent = 'READY'; topStatus.className = 'ready';
  searchInput.disabled = false;
  renderStats(stats);
  buildClusterIndex(data);
  renderOverview();
}

function showError(msg) {
  overlay.classList.add('hidden'); errorBox.classList.remove('hidden');
  document.getElementById('error-msg').textContent = msg;
  topStatus.textContent = 'ERROR'; topStatus.className = 'error';
}

// ═══ Stats bar ════════════════════════════════════════════════════════════════
function renderStats(s) {
  const gpu = s.gpu_available
    ? `<span class="stat-gpu">AMD ${esc(s.device_name)}</span>`
    : `<span>${esc(s.device_name || 'cpu')}</span>`;
  statsbar.innerHTML = [
    `<span><span class="stat-hi">${s.total_files ?? '?'}</span> files</span>`,
    `<span><span class="stat-hi">${s.total_units ?? '?'}</span> units</span>`,
    `<span><span class="stat-hi">${s.total_dependencies ?? '?'}</span> deps</span>`,
    `<span>parsed <span class="stat-hi">${(s.parse_time_ms ?? 0).toFixed(0)}ms</span></span>`,
    s.embed_time_ms > 0
      ? `<span>embedded <span class="stat-hi">${s.embed_time_ms.toFixed(0)}ms</span> on ${gpu}</span>`
      : gpu,
  ].join('');
}

// ═══ Build cluster index ══════════════════════════════════════════════════════
function buildClusterIndex(data) {
  clusterIndex = {}; nodeById = {};
  data.nodes.forEach(n => { nodeById[n.id] = n; });
  hasEmbeddings = data.nodes.some(n => n.has_embedding);

  // Flat search index built once — avoids O(n) field access on every keystroke
  blastCache.clear();
  searchIndex = data.nodes.map(n => ({
    id: n.id,
    text: (n.name + ' ' + (n.file_path || '')).toLowerCase(),
  }));

  let clusters = data.clusters;
  if (!clusters || !clusters.length) {
    const grouped = {};
    data.nodes.forEach(n => { const c = n.cluster || n.group || 'Root'; grouped[c] = (grouped[c] || 0) + (n.kind === 'module' ? 1 : 0); });
    clusters = Object.entries(grouped).map(([name, size]) => ({
      name, size, unit_count: data.nodes.filter(n => (n.cluster || n.group) === name).length, files: [],
    }));
  }

  _paletteIdx = 0;
  for (const c of clusters) {
    const nodes = data.nodes.filter(n => (n.cluster || n.group) === c.name);
    const color = (c.name === 'External' || c.name === 'external')
      ? '#6b7280'
      : CLUSTER_PALETTE[_paletteIdx++ % CLUSTER_PALETTE.length];
    clusterIndex[c.name] = { ...c, color, nodes, baseR: clusterRadius(c) };
  }

  crossEdges = (data.cluster_edges || []).filter(e => e.source !== e.target);
  if (!data.cluster_edges) {
    const counts = {};
    data.edges.forEach(e => {
      const sc = nodeById[e.source]?.cluster || nodeById[e.source]?.group;
      const tc = nodeById[e.target]?.cluster || nodeById[e.target]?.group;
      if (sc && tc && sc !== tc) counts[sc + '|||' + tc] = (counts[sc + '|||' + tc] || 0) + 1;
    });
    crossEdges = Object.entries(counts).map(([k, count]) => { const [s, t] = k.split('|||'); return { source: s, target: t, count }; });
  }
}

function clusterRadius(c) {
  const n = Math.max(c.unit_count || 0, c.size || 1);
  return 30 + Math.sqrt(n) * 5.5;
}

// ═══ Blob dot generation ══════════════════════════════════════════════════════
// Navigation-arrow path: tip pointing up, V-notch at bottom facing cluster center.
function navArrow(s) {
  return `M0,${-s} L${s*0.58},${s*0.5} L0,${s*0.14} L${-s*0.58},${s*0.5}Z`;
}

function generateBlobDots(R, unitCount) {
  const N = Math.min(140, 5 + Math.round((unitCount || 1) * 1.5));
  const sigma = R / 2.1;
  const dots = []; let attempts = 0;
  while (dots.length < N && attempts < N * 4) {
    attempts++;
    const u1 = Math.random() || 1e-12, u2 = Math.random();
    const mag = sigma * Math.sqrt(-2 * Math.log(u1));
    const dx = mag * Math.cos(2 * Math.PI * u2), dy = mag * Math.sin(2 * Math.PI * u2);
    if (dx*dx + dy*dy > R*R) continue;
    const dist = Math.hypot(dx, dy);
    const rel  = Math.min(dist / R, 1);
    const alpha = 0.08 + 0.58 * Math.pow(rel, 0.65);
    const rot   = Math.atan2(dy, dx) * 180 / Math.PI + 90;
    const size  = 2.2 + Math.random() * 2.8;
    dots.push({ dx, dy, size, alpha, rot });
  }
  return dots;
}

function generateBleedDots(targetCluster, sourceClusterName, count) {
  const target = clusterIndex[targetCluster], source = clusterIndex[sourceClusterName];
  if (!target || !source || target.x == null) return [];
  const theta = Math.atan2(source.y - target.y, source.x - target.x);
  const R = target.baseR, N = Math.min(count, 20), color = clusterColor(sourceClusterName);
  const dots = [];
  for (let i = 0; i < N; i++) {
    const spread = (Math.random() - 0.5) * (Math.PI / 2.5);
    const a      = theta + spread;
    const rOff   = R + (Math.random() * 5 - 2);
    const rot    = a * 180 / Math.PI + 90 + 180;
    const size   = 2.5 + Math.random() * 2.5;
    const alpha  = 0.5 + Math.random() * 0.4;
    dots.push({ dx: Math.cos(a)*rOff, dy: Math.sin(a)*rOff, size, alpha, rot, fill: color });
  }
  return dots;
}

// ═══ SVG layer setup ══════════════════════════════════════════════════════════
function setupSvgLayers() {
  svg.selectAll('*').remove();
  const defs = svg.append('defs');

  ['imports','calls','inherits'].forEach(kind => {
    defs.append('marker')
      .attr('id', `arr-${kind}`)
      .attr('viewBox', '0 -4 8 8').attr('refX', 14).attr('refY', 0)
      .attr('markerWidth', 6).attr('markerHeight', 6).attr('orient', 'auto')
      .append('path').attr('d', 'M0,-4L8,0L0,4')
      .attr('fill', kind === 'inherits' ? '#8866ff' : kind === 'calls' ? '#44ff88' : '#444460');
  });

  const addGlow = (id, std) => defs.append('filter').attr('id', id).call(f => {
    f.append('feGaussianBlur').attr('stdDeviation', std).attr('result', 'blur');
    const m = f.append('feMerge');
    m.append('feMergeNode').attr('in', 'blur');
    m.append('feMergeNode').attr('in', 'SourceGraphic');
  });
  addGlow('glow', '2');
  addGlow('glow-strong', '5');

  zoomLayer        = svg.append('g').attr('id', 'zoom-layer');
  clusterLinkLayer = zoomLayer.append('g').attr('id', 'cluster-link-layer');
  clusterLayer     = zoomLayer.append('g').attr('id', 'cluster-layer');
  nodeLinkLayer    = zoomLayer.append('g').attr('id', 'node-link-layer');
  nodeLayer        = zoomLayer.append('g').attr('id', 'node-layer');

  zoomBehavior = d3.zoom().scaleExtent([0.08, 5]).on('zoom', e => {
    zoomLayer.attr('transform', e.transform);
    onZoomLevel(e.transform.k);
  });
  svg.call(zoomBehavior);

  svg.on('click', event => {
    // Any click that wasn't stopped by a node/cluster handler is empty-space
    if (viewMode === VIEW.EXPANDED) {
      if (selectedNodeId) clearNodeSelection();
      else exitExpanded();
    } else { hideSidebar(); clearNodeSelection(); }
  });
}

// ═══ Render overview ══════════════════════════════════════════════════════════

function buildLabelGradients() {
  const defs = svg.select('defs');
  Object.values(clusterIndex).forEach(c => {
    const id = 'lbl-' + c.name.replace(/[^a-zA-Z0-9]/g, '_');
    c._gradId = id;
    const grad = defs.append('linearGradient')
      .attr('id', id).attr('x1', '0').attr('y1', '0').attr('x2', '0').attr('y2', '1')
      .attr('gradientUnits', 'objectBoundingBox');
    grad.append('stop').attr('offset', '0%').attr('stop-color', c.color).attr('stop-opacity', 1);
    grad.append('stop').attr('offset', '100%').attr('stop-color', c.color).attr('stop-opacity', 0.3);
  });
}

function renderOverview() {
  setupSvgLayers();
  buildLabelGradients();
  viewMode = VIEW.OVERVIEW; expandedCluster = null;
  backBtn.classList.remove('visible');

  const W = svg.node().clientWidth  || 900;
  const H = svg.node().clientHeight || 600;

  clusterMetaNodes = Object.values(clusterIndex).map(c => ({
    id: c.name, size: c.size,
    weight: 1 + Math.log(1 + Math.min(Math.max(c.unit_count || 0, c.size || 1), 25)),
  }));

  const linkAgg = {};
  for (const e of crossEdges) {
    const key = [e.source, e.target].sort().join('|||');
    linkAgg[key] = (linkAgg[key] || 0) + e.count;
  }
  clusterMetaLinks = Object.entries(linkAgg)
    .map(([k, count]) => { const [a, b] = k.split('|||'); return { source: a, target: b, count }; })
    .filter(l => clusterIndex[l.source] && clusterIndex[l.target]);

  const blobSel = clusterLayer.selectAll('g.cluster-blob')
    .data(Object.values(clusterIndex), d => d.name)
    .join(enter => {
      const g = enter.append('g').attr('class', 'cluster-blob').attr('data-cluster', d => d.name);
      const inner = g.append('g').attr('class', 'blob-inner');
      inner.append('circle').attr('class', 'hit-target').attr('r', d => d.baseR).attr('fill', 'transparent');
      inner.append('circle').attr('class', 'halo')
        .attr('r', d => d.baseR).attr('fill', d => d.color).attr('opacity', 0.07).attr('filter', 'url(#glow-strong)');
      inner.append('g').attr('class', 'blob-dots').each(function(d) {
        const rawDots = generateBlobDots(d.baseR, d.unit_count || d.nodes.length || 1);
        d.blobDotData = rawDots.map(dot => {
          const el = document.createElementNS('http://www.w3.org/2000/svg', 'path');
          el.setAttribute('d', navArrow(dot.size));
          el.setAttribute('fill', d.color);
          el.setAttribute('opacity', dot.alpha);
          el.setAttribute('transform', `translate(${dot.dx},${dot.dy}) rotate(${dot.rot})`);
          this.appendChild(el);
          return { el, cx: dot.dx, cy: dot.dy, rot: dot.rot,
                   vx: (Math.random() - 0.5) * 0.3, vy: (Math.random() - 0.5) * 0.3 };
        });
      });
      inner.append('g').attr('class', 'bleed-layer');
      inner.append('text').attr('class', 'cluster-label')
        .attr('y', 4).attr('font-size', d => Math.max(13, Math.min(24, d.baseR * 0.32)))
        .attr('fill', d => `url(#${d._gradId})`).text(d => d.name);
      inner.append('text').attr('class', 'cluster-count')
        .attr('y', d => Math.max(11, Math.min(20, d.baseR * 0.28)) + 15)
        .attr('font-size', 10).text(d => `${d.size || 0} files · ${d.unit_count || 0} units`);
      return g;
    });

  blobSel
    .on('click', (event, d) => {
      event.stopPropagation();
      // Click on empty space inside the active expanded cluster → deselect node
      if (viewMode === VIEW.EXPANDED && expandedCluster === d.name) {
        if (selectedNodeId) clearNodeSelection();
        return;
      }
      onClusterClick(d);
    })
    .on('mouseover', (event, d) => {
      document.getElementById('tt-name').textContent = d.name;
      document.getElementById('tt-kind').textContent = `cluster · ${d.size || 0} files`;
      document.getElementById('tt-path').textContent  = `${d.unit_count || 0} units`;
      tooltip.style.display = 'block';
      tooltip.style.left = (event.clientX + 14) + 'px'; tooltip.style.top = (event.clientY + 14) + 'px';
    })
    .on('mousemove', event => { tooltip.style.left = (event.clientX + 14) + 'px'; tooltip.style.top = (event.clientY + 14) + 'px'; })
    .on('mouseout',  () => { tooltip.style.display = 'none'; });

  const linkSel = clusterLinkLayer.selectAll('path.cluster-link')
    .data(clusterMetaLinks, d => `${d.source}|||${d.target}`)
    .join('path').attr('class', 'cluster-link')
    .attr('stroke', '#5a5a78').attr('stroke-width', d => 1 + Math.log(1 + d.count) * 1.4).attr('opacity', 0.12);

  topSim = d3.forceSimulation(clusterMetaNodes)
    .force('link',    d3.forceLink(clusterMetaLinks).id(d => d.id).distance(40).strength(1.0))
    .force('charge',  d3.forceManyBody().strength(d => -50 * d.weight))
    .force('x',       d3.forceX(W/2).strength(0.12))
    .force('y',       d3.forceY(H/2).strength(0.12))
    .force('collide', d3.forceCollide().radius(d => clusterIndex[d.id].baseR + 8))
    .on('tick', () => {
      clusterMetaNodes.forEach(n => {
        const c = clusterIndex[n.id];
        if (!c) return;
        const r = c.baseR + 8;
        n.x = Math.max(r, Math.min(W - r, n.x ?? W/2));
        n.y = Math.max(r, Math.min(H - r, n.y ?? H/2));
        c.x = n.x; c.y = n.y;
      });
      blobSel.attr('transform', d => `translate(${d.x ?? 0},${d.y ?? 0})`);
      linkSel.attr('d', d => clusterArc(d));
    });

  topSim.on('end.bleed', () => startLiveAnimation());
  setTimeout(startLiveAnimation, 600);
  setTimeout(() => applySearch(), 120);
}

function clusterArc(d) {
  const a = clusterIndex[typeof d.source === 'object' ? d.source.id : d.source];
  const b = clusterIndex[typeof d.target === 'object' ? d.target.id : d.target];
  if (!a || !b || a.x == null) return '';
  const dx = b.x - a.x, dy = b.y - a.y;
  const dr = Math.sqrt(dx*dx + dy*dy) * 1.6;
  return `M${a.x},${a.y}A${dr},${dr} 0 0,1 ${b.x},${b.y}`;
}

// ═══ Live animation ═══════════════════════════════════════════════════════════
let _liveRafId = null, _particleLayer = null;
const _particles = [];

function _bezierAt(t, p0x, p0y, p1x, p1y, p2x, p2y) {
  const u = 1 - t;
  return { x: u*u*p0x + 2*u*t*p1x + t*t*p2x, y: u*u*p0y + 2*u*t*p1y + t*t*p2y };
}

function startLiveAnimation() {
  stopLiveAnimation();
  _particleLayer = zoomLayer.insert('g', '#cluster-layer').attr('id', 'particle-flow-layer');

  // Particles flow from dependency cluster → importer cluster, colored like the dependency
  crossEdges.forEach(e => {
    const color = clusterIndex[e.target]?.color || '#fff';
    for (let i = 0; i < 2; i++) {
      const el = _particleLayer.append('path').attr('d', navArrow(3.5)).attr('fill', color).attr('opacity', 0).node();
      _particles.push({ el, edge: e, t: i / 2, speed: 0.0012 + Math.random() * 0.0008 });
    }
  });

  let last = 0;
  function tick(now) {
    if (viewMode !== VIEW.OVERVIEW) { stopLiveAnimation(); return; }
    const dt = Math.min(now - last, 50) / 16.67; last = now;

    // Blob dots: random drift inside each cluster with tip pointing toward movement
    Object.values(clusterIndex).forEach(cluster => {
      if (!cluster.blobDotData) return;
      const R = cluster.baseR * 0.85;
      cluster.blobDotData.slice(0, 15).forEach(s => {
        s.vx += (Math.random() - 0.5) * 0.025;
        s.vy += (Math.random() - 0.5) * 0.025;
        const spd = Math.hypot(s.vx, s.vy);
        if (spd > 0.18) { s.vx = s.vx / spd * 0.18; s.vy = s.vy / spd * 0.18; }
        const dist = Math.hypot(s.cx, s.cy);
        if (dist > R) { s.vx -= (s.cx / dist) * 0.05; s.vy -= (s.cy / dist) * 0.05; }
        s.cx += s.vx * dt;
        s.cy += s.vy * dt;
        if (spd > 0.05) {
          const target = Math.atan2(s.vy, s.vx) * 180 / Math.PI + 90;
          s.rot += (target - s.rot) * 0.1;
        }
        s.el.setAttribute('transform', `translate(${s.cx},${s.cy}) rotate(${s.rot})`);
      });
    });

    // Dependency particles: dep cluster → importer cluster
    _particles.forEach(p => {
      p.t += p.speed * dt;
      if (p.t > 1) p.t -= 1;
      const a = clusterIndex[p.edge.target], b = clusterIndex[p.edge.source];
      if (!a || !b || a.x == null) { p.el.setAttribute('opacity', '0'); return; }
      const dx = b.x - a.x, dy = b.y - a.y;
      const bz = { p0x: a.x, p0y: a.y, p1x: (a.x+b.x)/2 - dy*0.3, p1y: (a.y+b.y)/2 + dx*0.3, p2x: b.x, p2y: b.y };
      const pt  = _bezierAt(p.t, bz.p0x, bz.p0y, bz.p1x, bz.p1y, bz.p2x, bz.p2y);
      const pt2 = _bezierAt(Math.min(p.t + 0.02, 1), bz.p0x, bz.p0y, bz.p1x, bz.p1y, bz.p2x, bz.p2y);
      const angle = Math.atan2(pt2.y - pt.y, pt2.x - pt.x) * 180 / Math.PI;
      const fade  = p.t < 0.08 ? p.t / 0.08 : p.t > 0.92 ? (1 - p.t) / 0.08 : 1;
      p.el.setAttribute('transform', `translate(${pt.x},${pt.y}) rotate(${angle + 90})`);
      p.el.setAttribute('opacity', String(fade * 0.9));
    });

    _liveRafId = requestAnimationFrame(tick);
  }
  _liveRafId = requestAnimationFrame(tick);
}

function stopLiveAnimation() {
  if (_liveRafId) { cancelAnimationFrame(_liveRafId); _liveRafId = null; }
  _particles.length = 0;
  if (_particleLayer) { _particleLayer.remove(); _particleLayer = null; }
}

// ═══ Cluster interaction ══════════════════════════════════════════════════════
function onClusterClick(cluster) {
  if (isTransitioning) return;
  if (viewMode === VIEW.EXPANDED && expandedCluster === cluster.name) return;
  showClusterSidebar(cluster);
  if (viewMode === VIEW.EXPANDED) exitExpanded(() => enterExpanded(cluster.name));
  else enterExpanded(cluster.name);
}

function showClusterSidebar(cluster) {
  showSidebar();
  sidebarBody.classList.remove('node-mode'); sidebarBody.classList.add('cluster-mode');
  const nameEl = document.getElementById('sidebar-name');
  nameEl.textContent = cluster.name; nameEl.style.color = cluster.color;
  document.getElementById('sidebar-meta').textContent =
    `cluster · ${cluster.size || 0} files · ${cluster.unit_count || 0} units`;

  const ul = document.getElementById('top-files-list');
  ul.innerHTML = '';
  const topFiles = (cluster.files || []).slice(0, 8);
  if (!topFiles.length) { ul.innerHTML = '<li style="color:var(--dim);font-size:12px;padding:5px 6px">—</li>'; }
  else {
    topFiles.forEach(fp => {
      const li = document.createElement('li');
      li.className = 'dep-item';
      li.innerHTML = `<span class="dep-arrow">→</span><span class="dep-name">${esc(fp)}</span>`;
      li.title = fp; ul.appendChild(li);
    });
  }
  renderClusterEdgeList('imports-from-list', crossEdges.filter(e => e.target === cluster.name), 'source');
  renderClusterEdgeList('imports-to-list',   crossEdges.filter(e => e.source === cluster.name), 'target');
}

function renderClusterEdgeList(containerId, edges, otherKey) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  const sorted = edges.slice().sort((a, b) => b.count - a.count);
  if (!sorted.length) { container.innerHTML = '<div style="color:var(--dim);font-size:12px;padding:5px 6px">—</div>'; return; }
  sorted.forEach(e => {
    const other = e[otherKey], color = clusterColor(other);
    const div = document.createElement('div'); div.className = 'cluster-edge-item';
    div.innerHTML = `<span class="cluster-edge-name"><span class="cluster-edge-dot" style="background:${color}"></span>${otherKey === 'source' ? '←' : '→'} ${esc(other)}</span><span class="cluster-edge-count">${e.count}</span>`;
    div.addEventListener('click', evt => { evt.stopPropagation(); const c = clusterIndex[other]; if (c) onClusterClick(c); });
    container.appendChild(div);
  });
}

// ═══ Expand / exit ════════════════════════════════════════════════════════════
const EXPAND_PHASE_A = 350;
const EXPAND_PHASE_B = 400;

function enterExpanded(clusterName) {
  if (isTransitioning) return;
  const target = clusterIndex[clusterName];
  if (!target) return;
  stopLiveAnimation();
  isTransitioning = true; viewMode = VIEW.EXPANDED; expandedCluster = clusterName;
  if (topSim) topSim.stop();

  const W = svg.node().clientWidth || 900, H = svg.node().clientHeight || 600;
  const R_target = 0.42 * Math.min(W, H);

  const inactive = Object.values(clusterIndex).filter(c => c.name !== clusterName);
  inactive.sort((a, b) => a.name.localeCompare(b.name));
  const ringR = Math.min(W, H) * 0.43;
  inactive.forEach((c, i) => {
    const angle = (i / Math.max(1, inactive.length)) * 2 * Math.PI - Math.PI / 2;
    c._ringX = W/2 + Math.cos(angle) * ringR; c._ringY = H/2 + Math.sin(angle) * ringR;
  });

  clusterLayer.selectAll('g.cluster-blob').each(function(c) {
    const isActive = c.name === clusterName;
    const inner = d3.select(this).select('.blob-inner');
    const scale = isActive ? (R_target / c.baseR) : 0.25;
    const tx = isActive ? W/2 : (c._ringX ?? W/2);
    const ty = isActive ? H/2 : (c._ringY ?? H/2);
    d3.select(this).transition().duration(EXPAND_PHASE_A).ease(d3.easeCubicInOut)
      .attr('transform', `translate(${tx},${ty})`).style('opacity', isActive ? 1 : 0.14);
    inner.transition().duration(EXPAND_PHASE_A).ease(d3.easeCubicInOut).attr('transform', `scale(${scale})`);
    if (isActive) {
      ['blob-dots','bleed-layer','cluster-label','cluster-count'].forEach(cls =>
        d3.select(this).select('.' + cls).transition().duration(EXPAND_PHASE_A)
          .style('opacity', cls === 'blob-dots' ? 0.04 : cls === 'bleed-layer' ? 0 : 0.1));
    }
  });
  clusterLinkLayer.selectAll('path.cluster-link').transition().duration(EXPAND_PHASE_A).style('opacity', 0);

  setTimeout(() => {
    renderSubGraph(target, W, H, R_target);
    backBtn.classList.add('visible');
    isTransitioning = false;
  }, EXPAND_PHASE_A);
}

function exitExpanded(onDone) {
  if (isTransitioning && !onDone) return;
  if (viewMode !== VIEW.EXPANDED) { onDone && onDone(); return; }
  isTransitioning = true; selectedNodeId = null;

  nodeLayer.selectAll('g.node').transition().duration(250).style('opacity', 0).remove();
  nodeLinkLayer.selectAll('path.link').transition().duration(250).style('opacity', 0).remove();
  if (subSim) { subSim.stop(); subSim = null; }
  adjMap.clear();
  backBtn.classList.remove('visible');

  const W = svg.node().clientWidth || 900, H = svg.node().clientHeight || 600;

  clusterLayer.selectAll('g.cluster-blob').each(function(c) {
    const inner = d3.select(this).select('.blob-inner');
    inner.transition().duration(EXPAND_PHASE_A).ease(d3.easeCubicInOut).attr('transform', 'scale(1)');
    ['blob-dots','bleed-layer','cluster-label','cluster-count'].forEach(cls =>
      d3.select(this).select('.' + cls).transition().duration(EXPAND_PHASE_A).style('opacity', null));
    d3.select(this).transition().duration(EXPAND_PHASE_A).ease(d3.easeCubicInOut)
      .style('opacity', 1).attr('transform', `translate(${c.x ?? W/2},${c.y ?? H/2})`);
  });
  clusterLinkLayer.selectAll('path.cluster-link').transition().duration(EXPAND_PHASE_A).style('opacity', 0.12);

  setTimeout(() => {
    viewMode = VIEW.OVERVIEW; expandedCluster = null;
    if (topSim) topSim.alpha(0.3).restart();
    isTransitioning = false;
    applySearch();
    startLiveAnimation();
    onDone && onDone();
  }, EXPAND_PHASE_A + 50);
}

// ═══ Sub-graph render ═════════════════════════════════════════════════════════
function renderSubGraph(cluster, W, H, R_target) {
  if (subSim) { subSim.stop(); subSim = null; }

  const memberIds = new Set(cluster.nodes.map(n => n.id));
  const nodes = cluster.nodes.map(n => ({ ...n }));
  const links = graphData.edges
    .filter(e => memberIds.has(e.source) && memberIds.has(e.target))
    .map(e => ({ ...e }));

  // Build adjacency map from raw IDs before D3 mutates them into node objects
  adjMap = new Map();
  links.forEach(e => {
    if (!adjMap.has(e.source)) adjMap.set(e.source, new Set());
    if (!adjMap.has(e.target)) adjMap.set(e.target, new Set());
    adjMap.get(e.source).add(e.target);
    adjMap.get(e.target).add(e.source);
  });

  nodeLayer.selectAll('*').remove(); nodeLinkLayer.selectAll('*').remove();

  const degMap = {};
  nodes.forEach(n => { degMap[n.id] = 0; });
  links.forEach(l => { if (degMap[l.source] !== undefined) degMap[l.source]++; if (degMap[l.target] !== undefined) degMap[l.target]++; });
  const maxDeg = Math.max(1, ...Object.values(degMap));
  const radius = d => Math.max(5, Math.min(28, 5 + (degMap[d.id] || 0) / maxDeg * 22));

  const fastMode  = nodes.length > 30;
  const ultraFast = nodes.length > 80;
  const glowAttr  = fastMode ? null : 'url(#glow)';

  const linkSel = nodeLinkLayer.selectAll('path.link').data(links).join('path')
    .attr('class', d => `link ${d.kind}`)
    .attr('stroke', d => d.kind === 'inherits' ? '#8866ff' : d.kind === 'calls' ? '#44ff88' : '#3a3a5c')
    .attr('stroke-width', d => d.kind === 'inherits' ? 2 : 1)
    .attr('marker-end', fastMode ? null : (d => `url(#arr-${d.kind})`))
    .style('opacity', 0);

  const nodeSel = nodeLayer.selectAll('g.node').data(nodes, d => d.id).join('g')
    .attr('class', 'node' + (fastMode ? ' fast' : '')).attr('data-id', d => d.id)
    .attr('transform', `translate(${W/2},${H/2})`).style('opacity', 0)
    .call(d3.drag()
      .on('start', (e, d) => { if (!e.active) subSim.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on('drag',  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on('end',   (e, d) => { if (!e.active) subSim.alphaTarget(0); d.fx = null; d.fy = null; }))
    .on('mouseover', onNodeHover)
    .on('mousemove', e => { tooltip.style.left = (e.clientX + 14) + 'px'; tooltip.style.top = (e.clientY + 14) + 'px'; })
    .on('mouseout',  onNodeOut)
    .on('click',     onNodeClick);

  const showLabels = nodes.length <= 60;
  if (ultraFast) {
    nodeSel.each(function(d) {
      const g = d3.select(this), r = radius(d), col = cluster.color;
      g.append('circle').attr('r', r)
        .attr('fill', 'rgba(10,10,20,0.85)').attr('stroke', col).attr('stroke-width', 1.2);
    });
  } else {
  nodeSel.each(function(d) {
    const g = d3.select(this), r = radius(d), col = cluster.color;
    let labelX = r + 6;
    if (d.kind === 'module') {
      const w = r, h = r * 1.3, fold = r * 0.32; labelX = w + 6;
      const path = g.append('path')
        .attr('d', `M${-w},${-h} L${w-fold},${-h} L${w},${-h+fold} L${w},${h} L${-w},${h} Z M${w-fold},${-h} L${w-fold},${-h+fold} L${w},${-h+fold}`)
        .attr('fill', 'rgba(10,10,20,0.85)').attr('stroke', col).attr('stroke-width', 1.5);
      if (glowAttr) path.attr('filter', glowAttr);
      if (!fastMode) {
        [h * -0.25, h * 0.1, h * 0.45].forEach(y =>
          g.append('line').attr('x1', -w*0.52).attr('y1', y).attr('x2', w*0.52).attr('y2', y)
            .attr('stroke', col).attr('stroke-width', 0.8).attr('opacity', 0.4));
      }
    } else if (d.kind === 'class' || d.kind === 'interface') {
      const hr = r * 1.15; labelX = hr + 6;
      const pts = d3.range(6).map(i => { const a = (i * Math.PI/3) - Math.PI/6; return `${Math.cos(a)*hr},${Math.sin(a)*hr}`; }).join(' ');
      const poly = g.append('polygon').attr('points', pts)
        .attr('fill', 'rgba(10,10,20,0.85)').attr('stroke', col).attr('stroke-width', 1.5);
      if (glowAttr) poly.attr('filter', glowAttr);
      if (!fastMode) {
        g.append('text').attr('text-anchor', 'middle').attr('dominant-baseline', 'middle')
          .attr('font-size', Math.max(6, r * 0.5)).style('fill', col).style('opacity', 0.6)
          .style('pointer-events', 'none').attr('font-family', 'var(--mono)').text('{}');
      }
    } else {
      const bw = r * 1.25, bh = r * 0.8; labelX = bw + 6;
      const rect = g.append('rect').attr('x', -bw).attr('y', -bh).attr('width', bw*2).attr('height', bh*2)
        .attr('rx', bh).attr('fill', 'rgba(10,10,20,0.85)').attr('stroke', col).attr('stroke-width', 1.5);
      if (glowAttr) rect.attr('filter', glowAttr);
      if (!fastMode) {
        g.append('text').attr('text-anchor', 'middle').attr('dominant-baseline', 'middle')
          .attr('font-size', Math.max(6, r * 0.5)).style('fill', col).style('opacity', 0.6)
          .style('pointer-events', 'none').attr('font-family', 'var(--mono)').text('fn');
      }
    }
    if (showLabels) {
      g.append('text').attr('class', 'node-label').attr('x', labelX).attr('y', 0)
        .attr('font-size', Math.min(11, 7 + r * 0.15))
        .text(d.name.length > 24 ? d.name.slice(0, 22) + '…' : d.name);
    }
  });
  }

  const nodeCount = nodes.length;
  const linkRenderer = fastMode ? subLine : subArc;
  const tickStride   = ultraFast ? 3 : (nodeCount > 60 ? 2 : 1);
  let _tickFrame = 0;

  subSim = d3.forceSimulation(nodes)
    .force('link',    d3.forceLink(links).id(d => d.id).distance(d => d.kind === 'imports' ? 35 : 55).strength(0.6))
    .force('charge',  d3.forceManyBody().strength(ultraFast ? -22 : fastMode ? -30 : -45).theta(0.98).distanceMax(fastMode ? 180 : 400))
    .force('center',  d3.forceCenter(W/2, H/2))
    .force('collide', d3.forceCollide().radius(d => radius(d) + 4).iterations(1))
    .alphaDecay(ultraFast ? 0.15 : fastMode ? 0.05 : 0.0228)
    .velocityDecay(fastMode ? 0.65 : 0.4)
    .alphaMin(ultraFast ? 0.05 : 0.005)
    .on('tick', () => {
      nodes.forEach(d => {
        d.x = Math.max(32, Math.min(W - 32, d.x ?? W/2));
        d.y = Math.max(32, Math.min(H - 32, d.y ?? H/2));
      });
      if (++_tickFrame % tickStride !== 0) return;
      linkSel.attr('d', linkRenderer);
      nodeSel.attr('transform', d => `translate(${d.x},${d.y})`);
    });

  if (ultraFast) {
    nodeSel.style('opacity', 1);
    linkSel.style('opacity', null);
  } else {
    nodeSel.transition().duration(EXPAND_PHASE_B).delay((_, i) => i * 2).style('opacity', 1);
    linkSel.transition().duration(EXPAND_PHASE_B).style('opacity', null);
  }
  setTimeout(() => applySearch(), EXPAND_PHASE_B + 50);
}

function subArc(d) {
  const sx = d.source.x ?? 0, sy = d.source.y ?? 0;
  const tx = d.target.x ?? 0, ty = d.target.y ?? 0;
  const dr = Math.hypot(tx-sx, ty-sy) * 1.4;
  return `M${sx},${sy}A${dr},${dr} 0 0,1 ${tx},${ty}`;
}

function subLine(d) {
  const sx = d.source.x ?? 0, sy = d.source.y ?? 0;
  const tx = d.target.x ?? 0, ty = d.target.y ?? 0;
  return `M${sx},${sy}L${tx},${ty}`;
}

// ═══ Node interaction ═════════════════════════════════════════════════════════
function _applyNeighborDim(id) {
  const neighbors = adjMap.get(id) ?? new Set();
  d3.selectAll('#node-layer g.node').each(function(n) {
    const isDimmed = n.id !== id && !neighbors.has(n.id);
    d3.select(this)
      .classed('dimmed',      isDimmed && n.kind !== 'module')
      .classed('dimmed-soft', isDimmed && n.kind === 'module');
  });
  // Single pass over links: compute source/target once, set both classes together
  d3.selectAll('#node-link-layer path.link').each(function(l) {
    const s = l.source.id ?? l.source, t = l.target.id ?? l.target;
    const connected = s === id || t === id;
    d3.select(this).classed('dimmed', !connected).classed('highlighted', connected);
  });
}

function onNodeHover(event, d) {
  _applyNeighborDim(d.id);
  document.getElementById('tt-name').textContent = d.name;
  document.getElementById('tt-kind').textContent = d.kind;
  document.getElementById('tt-path').textContent  = d.file_path + (d.line_start ? ':' + d.line_start : '');
  tooltip.style.display = 'block';
}

function onNodeOut() {
  if (selectedNodeId) {
    _applyNeighborDim(selectedNodeId);
  } else {
    d3.selectAll('#node-layer g.node').classed('dimmed', false).classed('dimmed-soft', false);
    d3.selectAll('#node-link-layer path.link').classed('dimmed', false).classed('highlighted', false);
  }
  tooltip.style.display = 'none';
}

function applySelectionDim(nodeId) {
  _applyNeighborDim(nodeId);
}

function clearNodeSelection() {
  selectedNodeId = null;
  if (!nodeLayer) return;
  d3.selectAll('#node-layer g.node').classed('selected', false).classed('blast', false).classed('dimmed', false).classed('dimmed-soft', false);
  d3.selectAll('#node-link-layer path.link').classed('dimmed', false).classed('highlighted', false);
}

// Visual selection + structural sidebar. Resets AI explain box to prompt.
function selectNode(domEl, d) {
  selectedNodeId = d.id;
  d3.selectAll('#node-layer g.node').classed('blast', false).classed('selected', false);
  d3.select(domEl).classed('selected', true);
  const blast = computeBlast(d.id, 2);
  d3.selectAll('#node-layer g.node').classed('blast', n => blast.has(n.id) && n.id !== d.id);
  applySelectionDim(d.id);

  showSidebar();
  sidebarBody.classList.remove('cluster-mode'); sidebarBody.classList.add('node-mode');

  document.getElementById('sidebar-name').textContent = d.name;
  document.getElementById('sidebar-name').style.color = '';
  document.getElementById('sidebar-meta').textContent =
    `${d.kind} · ${d.file_path}${d.line_start ? ':' + d.line_start : ''}`;

  const sourceSection = document.getElementById('source-section');
  const sourcePreview = document.getElementById('source-preview');
  if (d.source_snippet) {
    sourcePreview.textContent = d.source_snippet; sourceSection.style.display = '';
  } else if (d.docstring) {
    sourcePreview.textContent = '"""' + d.docstring + '"""'; sourceSection.style.display = '';
  } else {
    sourceSection.style.display = 'none';
  }

  if (graphData) {
    const depIds    = graphData.edges.filter(e => e.source === d.id).map(e => e.target);
    const usedByIds = graphData.edges.filter(e => e.target === d.id).map(e => e.source);
    renderDepList('deps-list',    depIds.slice(0, 15), true);
    renderDepList('used-by-list', usedByIds.slice(0, 15), true);
  }

  const explainBox = document.getElementById('explain-box');
  explainBox.className = 'loading';
  explainBox.textContent = 'Double-click for AI explanation.';
  document.getElementById('explain-role').textContent = '';
  document.getElementById('risk-section').style.display    = 'none';
  document.getElementById('similar-section').style.display = 'none';
}

// Camera pan + LLM. Structural sidebar content is already set by selectNode.
async function activateNode(d) {
  requestAnimationFrame(() => {
    if (!zoomBehavior) return;
    const clickedEl = nodeLayer.selectAll('g.node').filter(n => n.id === d.id).node();
    if (!clickedEl) return;
    const svgRect  = svg.node().getBoundingClientRect();
    const nodeRect = clickedEl.getBoundingClientRect();
    const nx = nodeRect.left + nodeRect.width  / 2 - svgRect.left;
    const ny = nodeRect.top  + nodeRect.height / 2 - svgRect.top;
    const t  = d3.zoomTransform(svg.node());
    svg.transition().duration(400).call(
      zoomBehavior.transform,
      d3.zoomIdentity.translate(t.x + svgRect.width / 2 - nx, t.y + svgRect.height / 2 - ny).scale(t.k)
    );
  });

  const explainBox  = document.getElementById('explain-box');
  const explainRole = document.getElementById('explain-role');

  if (d.group === 'external' || d.cluster === 'External') {
    explainBox.className = ''; explainBox.textContent = 'External package — not part of this codebase.';
    explainRole.textContent = d.id; return;
  }

  explainBox.className = 'loading'; explainBox.textContent = 'Loading AI explanation…';
  try {
    const r = await fetch('/api/explain', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit_id: d.id }),
    });
    if (r.ok) {
      const ex = await r.json();
      explainBox.className = ''; explainBox.textContent = ex.summary;
      explainRole.textContent = ex.role;
      if (ex.risk_notes) { document.getElementById('risk-box').textContent = ex.risk_notes; document.getElementById('risk-section').style.display = ''; }
    } else {
      explainBox.className = ''; explainBox.textContent = 'LLM unavailable — start Ollama or configure XNAV_LLM_URL.';
    }
  } catch { explainBox.textContent = 'Could not reach explanation service.'; }

  if (hasEmbeddings) {
    try {
      const r = await fetch(`/api/similar/${encodeURIComponent(d.id)}?top_k=5`);
      if (r.ok) {
        const sim = await r.json();
        if (sim.similar?.length) { renderSimilar(sim.similar); document.getElementById('similar-section').style.display = ''; }
      }
    } catch { /* embeddings optional */ }
  }
}

function onNodeClick(event, d) {
  event.stopPropagation();
  if (selectedNodeId === d.id) {
    activateNode(d);
  } else {
    selectNode(this, d);
  }
}

// ═══ Node navigation ══════════════════════════════════════════════════════════
function navigateToNode(nodeId) {
  const n = nodeById[nodeId]; if (!n) return;
  if (viewMode === VIEW.EXPANDED && expandedCluster === n.cluster) {
    const el = nodeLayer.selectAll('g.node').filter(d => d.id === nodeId);
    if (!el.empty()) {
      const nd = el.datum(), domEl = el.node();
      selectNode(domEl, nd);
      activateNode(nd);
    }
  } else {
    const c = clusterIndex[n.cluster];
    if (c) { if (viewMode === VIEW.EXPANDED) exitExpanded(() => onClusterClick(c)); else onClusterClick(c); }
  }
}

function renderDepList(listId, ids, navigable) {
  const ul = document.getElementById(listId); ul.innerHTML = '';
  if (!ids.length) { ul.innerHTML = '<li style="color:var(--dim);font-size:12px;padding:5px 6px">—</li>'; return; }
  ids.forEach(id => {
    const n = nodeById[id];
    const name = n ? n.name : id.split('::').pop();
    const badge = n && n.cluster && n.cluster !== expandedCluster ? n.cluster : '';
    const li = document.createElement('li');
    li.className = 'dep-item' + (navigable ? ' nav' : '');
    li.innerHTML = `<span class="dep-arrow">→</span><span class="dep-name">${esc(name)}</span>` +
      (badge ? `<span class="dep-badge">${esc(badge)}</span>` : '');
    li.title = id;
    if (navigable) li.addEventListener('click', e => { e.stopPropagation(); navigateToNode(id); });
    ul.appendChild(li);
  });
}

function renderSimilar(items) {
  const c = document.getElementById('similar-list'); c.innerHTML = '';
  items.forEach(item => {
    const n = nodeById[item.id], name = n ? n.name : item.id.split('::').pop();
    const div = document.createElement('div'); div.className = 'similar-item';
    div.innerHTML = `<span class="similar-name">~ ${esc(name)}</span><span class="similar-score">${(item.score*100).toFixed(0)}%</span>`;
    div.addEventListener('click', () => navigateToNode(item.id));
    c.appendChild(div);
  });
}

function computeBlast(unitId, depth) {
  const cacheKey = `${unitId}:${depth}`;
  if (blastCache.has(cacheKey)) return blastCache.get(cacheKey);
  const result = new Set(); if (!graphData) return result;
  let frontier = new Set([unitId]);
  for (let i = 0; i < depth; i++) {
    const next = new Set();
    graphData.edges.forEach(e => { if (frontier.has(e.target) && !result.has(e.source)) next.add(e.source); });
    next.forEach(id => result.add(id)); frontier = next;
  }
  blastCache.set(cacheKey, result);
  return result;
}

// ═══ Utilities ════════════════════════════════════════════════════════════════
function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }
function esc(s)    { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

window.addEventListener('resize', () => {
  const w = svg.node().clientWidth, h = svg.node().clientHeight;
  if (viewMode === VIEW.OVERVIEW && topSim) topSim.force('center', d3.forceCenter(w/2, h/2)).alpha(0.1).restart();
  if (viewMode === VIEW.EXPANDED  && subSim) subSim.force('center', d3.forceCenter(w/2, h/2)).alpha(0.1).restart();
});

// ═══ Start ════════════════════════════════════════════════════════════════════
pollForGraph();
