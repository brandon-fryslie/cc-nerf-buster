// gh-pages-showcase template — drop-in JS.
// Reads showcase.json (sibling file) and populates the page.
// Sections with no data hide themselves; the page degrades gracefully.

const $ = (sel) => document.querySelector(sel);

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function applyHero(project, hero) {
  if (!project) return;
  document.title = project.name || '—';
  if (project.tagline) {
    document.querySelector('meta[name=description]')?.setAttribute('content', project.tagline);
  }

  $('#eyebrow').textContent = project.eyebrow || '';
  $('#project-name').textContent = project.name || '—';
  $('#tagline').textContent = project.tagline || '';

  const badges = [];
  if (project.language) badges.push({ kind: 'lang', text: project.language });
  if (project.license) badges.push({ kind: 'license', text: project.license });
  (project.badges || []).forEach(b => badges.push({ kind: 'extra', text: b }));
  $('#badges').innerHTML = badges
    .map(b => `<span class="badge"><span class="dot"></span>${escapeHtml(b.text)}</span>`)
    .join('');

  const ctas = project.ctas || [];
  // Auto-add a "View source" CTA if a github_url is provided and ctas is empty
  if (ctas.length === 0 && project.github_url) {
    ctas.push({ label: 'View source', url: project.github_url, primary: true });
  }
  $('#ctas').innerHTML = ctas
    .map(c => `<a class="cta ${c.primary ? 'primary' : ''}" href="${escapeHtml(c.url)}"
        ${/^https?:\/\//.test(c.url) ? 'target="_blank" rel="noopener"' : ''}>
        ${escapeHtml(c.label)}
      </a>`)
    .join('');
}

function applyVisual(visual) {
  if (!visual || !visual.kind) return;
  const frame = $('#visual-frame');
  let html = '';
  switch (visual.kind) {
    case 'image':
      html = `<img src="${escapeHtml(visual.src)}" alt="${escapeHtml(visual.alt || '')}">`;
      break;
    case 'video':
      html = `<video src="${escapeHtml(visual.src)}" autoplay muted loop playsinline></video>`;
      break;
    case 'iframe':
      html = `<iframe src="${escapeHtml(visual.src)}" title="${escapeHtml(visual.alt || 'demo')}" loading="lazy"></iframe>`;
      break;
    case 'code':
      html = `<pre><code>${escapeHtml(visual.code || '')}</code></pre>`;
      break;
    default:
      return;
  }
  frame.innerHTML = html;
  $('#visual-section').hidden = false;
}

function applyHighlights(highlights, heading) {
  if (!Array.isArray(highlights) || !highlights.length) return;
  if (heading) $('#highlights-heading').textContent = heading;
  $('#highlights').innerHTML = highlights
    .map(h => `<div class="highlight-card">
      <h3>${escapeHtml(h.title || '')}</h3>
      <p>${escapeHtml(h.description || '')}</p>
    </div>`)
    .join('');
  $('#highlights-section').hidden = false;
}

function applyQuickstart(quickstart, heading) {
  if (!quickstart || !quickstart.code) return;
  if (heading) $('#quickstart-heading').textContent = heading;
  $('#quickstart-code').textContent = quickstart.code;
  $('#quickstart-section').hidden = false;
}

function applyScreenshots(screenshots, heading) {
  if (!Array.isArray(screenshots) || !screenshots.length) return;
  if (heading) $('#screenshots-heading').textContent = heading;
  $('#screenshots').innerHTML = screenshots
    .map(s => `<figure class="screenshot-figure">
      <img src="${escapeHtml(s.src)}" alt="${escapeHtml(s.caption || s.alt || '')}" loading="lazy">
      ${s.caption ? `<figcaption>${escapeHtml(s.caption)}</figcaption>` : ''}
    </figure>`)
    .join('');
  $('#screenshots-section').hidden = false;
}

function applyLinks(links, heading) {
  if (!Array.isArray(links) || !links.length) return;
  if (heading) $('#links-heading').textContent = heading;
  $('#links').innerHTML = links
    .map(l => `<li>
      <a href="${escapeHtml(l.url)}"
         ${/^https?:\/\//.test(l.url) ? 'target="_blank" rel="noopener"' : ''}>
        ${escapeHtml(l.label || l.url)}
      </a>
    </li>`)
    .join('');
  $('#links-section').hidden = false;
}

function applyFooter(project) {
  const sourceText = project?.github_url
    ? `Source: <a href="${escapeHtml(project.github_url)}" target="_blank" rel="noopener">${escapeHtml(project.github_url.replace(/^https?:\/\//, ''))}</a>`
    : '';
  $('#footer-source').innerHTML = sourceText;
}

async function init() {
  let data;
  try {
    const res = await fetch('showcase.json', { cache: 'no-cache' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    console.error('Failed to load showcase.json:', e);
    $('#project-name').textContent = 'Configuration missing';
    $('#tagline').textContent = 'Drop a showcase.json next to index.html. See the example in this template.';
    return;
  }

  applyHero(data.project, data.hero);
  applyVisual(data.hero);
  applyHighlights(data.highlights, data.headings?.highlights);
  applyQuickstart(data.quickstart, data.headings?.quickstart);
  applyScreenshots(data.screenshots, data.headings?.screenshots);
  applyLinks(data.links, data.headings?.links);
  applyFooter(data.project);
}

init();
