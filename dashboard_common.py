"""Shared plumbing for the dashboard HTML pages (export, overlay, report index).

export_dashboard.py and overlay_dashboards.py emit near-identical pages: the
same dark-theme CSS, drag-resizable panels, a row/grid scaffold, and
fixed-point y-axis formatting.  The constants and helpers here hold the single
copy of those pieces so the generated pages cannot drift apart.

Each consumer loads this module with importlib from its own directory, which
works both from the repository root and when the files are staged side by
side into a ConfigMap.
"""

import gzip
import re
from pathlib import Path

PLOTLY_CDN_TAG = '<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>'

_PLOTLY_SCRIPT = re.compile(r"<script\b.*?</script\s*>", re.DOTALL | re.IGNORECASE)


def read_plotly_bundle(path) -> str:
    """Return the decompressed bundle source, safe to inline in a page."""
    bundle = Path(path)
    try:
        source = gzip.decompress(bundle.read_bytes()).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise SystemExit(f"Missing or unreadable Plotly bundle: {bundle}") from exc
    # A literal "</script" inside the bundle would terminate the host tag.
    return source.replace("</script", "<\\/script")


def plotly_tag(bundle_path=None) -> str:
    """Build the Plotly <script> tag: inline the local bundle when given."""
    if not bundle_path:
        return PLOTLY_CDN_TAG
    return "<script>" + read_plotly_bundle(bundle_path) + "</script>"


def strip_plotly(page: str) -> str:
    """Prepare a dashboard page for embedding in the report index.

    The page's Plotly <script> tag is removed so the page can borrow the
    index's single bundled copy, and the minimal parent-Plotly shim baked
    into pages from before stylesheet mirroring is swapped for the current
    one (see PARENT_PLOTLY_SHIM).

    Only the first script block is considered, and it is only removed when it
    is the Plotly tag (the vendored bundle references cdn.plot.ly), so pages
    that never embedded Plotly pass through unchanged.
    """
    match = _PLOTLY_SCRIPT.search(page)
    if match and "cdn.plot.ly" in match.group(0):
        page = page[:match.start()] + page[match.end():]
    return _MINIMAL_PARENT_SHIM.sub(lambda _match: PARENT_PLOTLY_SHIM, page)


# Dark-theme rules shared by the exported dashboard and the overlay page.
# Each page appends its own extras (toolbar, filter buttons) after this.
BASE_CSS = """  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #111217; color: #d8d9da; font-family: Inter, -apple-system, sans-serif; padding: 16px; }
  h1 { font-size: 20px; font-weight: 500; margin-bottom: 4px; }
  .subtitle { font-size: 13px; color: #8e8e8e; margin-bottom: 20px; }
  .row-header { font-size: 15px; font-weight: 500; color: #d8d9da; padding: 10px 0 6px 4px;
                 border-bottom: 1px solid #2a2a2e; margin: 16px 0 8px 0; cursor: pointer; user-select: none; }
  .row-header:hover { color: #fff; }
  .row-header .arrow { display: inline-block; width: 16px; transition: transform .15s; }
  .row-header.collapsed .arrow { transform: rotate(-90deg); }
  .grid { display: flex; flex-wrap: wrap; gap: 8px; align-items: flex-start; }
  .panel { position: relative; isolation: isolate; z-index: 0; flex: 0 0 auto; width: calc(50% - 4px); max-width: 100%;
            min-width: min(420px, 100%); min-height: 300px; height: 380px; background: #181b1f;
            border: 1px solid #2a2a2e; border-radius: 4px; padding: 0; overflow: hidden; resize: both; }
  .panel.wide { width: 100% !important; }
  @media (max-width: 1000px) { .panel { width: 100%; } }
  .panel-title { display: flex; align-items: center; justify-content: space-between; gap: 8px; height: 36px;
                  font-size: 13px; font-weight: 500; padding: 5px 8px 5px 12px; color: #d8d9da; cursor: grab; user-select: none; }
  .panel-actions { display: flex; gap: 4px; flex: 0 0 auto; }
  .panel-actions button { border: 1px solid #3a3a3e; border-radius: 4px; background: #1e2127; color: #aaa; padding: 2px 7px; cursor: pointer; font-size: 11px; }
  .panel-actions button:hover { color: #fff; border-color: #666; }
  .panel.dragging { opacity: .45; }
  .panel .plot { position: relative; z-index: 0; width: 100%; height: calc(100% - 36px); min-width: 0; min-height: 260px; }
  .empty { color: #555; font-size: 12px; padding: 60px 12px; text-align: center; }
  .hidden { display: none; }
"""


# Shared JS: panel chrome (drag-reorder, resize, wide, legend toggle), the
# panel header markup, the y-axis tick-precision ladder, and the row/grid
# scaffold.  `root` and renderPanel(grid, id, panel) come from the host page;
# function declarations hoist, so the scaffold may run before renderPanel's
# definition in the source.
PANEL_JS = """
let draggedPanel = null;
function makePanelInteractive(panel, plot) {
  const title = panel.querySelector('.panel-title');
  title.draggable = true;
  title.addEventListener('dragstart', () => { draggedPanel = panel; panel.classList.add('dragging'); });
  title.addEventListener('dragend', () => { draggedPanel = null; panel.classList.remove('dragging'); });
  panel.addEventListener('dragover', event => {
    event.preventDefault();
    if (!draggedPanel || draggedPanel === panel || draggedPanel.parentElement !== panel.parentElement) return;
    panel.parentElement.insertBefore(draggedPanel, event.clientY < panel.getBoundingClientRect().top + panel.offsetHeight / 2 ? panel : panel.nextSibling);
  });
  title.addEventListener('dblclick', event => { if (!event.target.closest('button')) panel.classList.toggle('wide'); });
  panel.querySelector('[data-action="shorter"]').addEventListener('click', () => { panel.style.height = Math.max(300, panel.offsetHeight - 120) + 'px'; });
  panel.querySelector('[data-action="taller"]').addEventListener('click', () => { panel.style.height = Math.min(1200, panel.offsetHeight + 160) + 'px'; });
  panel.querySelector('[data-action="wide"]').addEventListener('click', () => { panel.classList.toggle('wide'); });
  panel.querySelector('[data-action="legend"]').addEventListener('click', () => { if (plot.layout) Plotly.relayout(plot, { showlegend: !plot.layout.showlegend }); });
  new ResizeObserver(() => { if (window.Plotly && plot.data) Plotly.Plots.resize(plot); }).observe(panel);
}

function panelHeaderHtml(title) {
  return '<div class="panel-title"><span>' + title + '</span><span class="panel-actions">' +
    '<button data-action="shorter" title="Shorter">−</button><button data-action="taller" title="Taller">+</button>' +
    '<button data-action="wide" title="Toggle full width">↔</button><button data-action="legend" title="Toggle legend">Legend</button></span></div>';
}

// Fixed-point formatting is intentional: monitoring values must never flip to
// exponent notation.  Keep only precision that remains meaningful.
function yTickDecimals(largestY, smallestY) {
  const spanY = largestY - smallestY;
  const magnitude = Math.max(Math.abs(largestY), Math.abs(smallestY));
  const magnitudeDecimals = magnitude >= 100 ? 0 : magnitude >= 1 ? 2 : magnitude >= 0.01 ? 4 : 6;
  const rangeDecimals = spanY > 0 ? Math.max(0, Math.ceil(-Math.log10(spanY / 6)) + 1) : magnitudeDecimals;
  return Math.min(8, Math.max(magnitudeDecimals, rangeDecimals));
}

function scaffoldRows(rows, panels, renderPanel) {
  let currentGrid = null;
  if (rows.length === 0) {
    currentGrid = document.createElement('div');
    currentGrid.className = 'grid';
    root.appendChild(currentGrid);
    for (const [pid, panel] of Object.entries(panels)) renderPanel(currentGrid, pid, panel);
  } else {
    for (const item of rows) {
      if (item.type === 'row') {
        const h = document.createElement('div');
        h.className = 'row-header';
        h.innerHTML = '<span class="arrow">▾</span> ' + item.title;
        currentGrid = document.createElement('div');
        currentGrid.className = 'grid';
        h.addEventListener('click', () => {
          h.classList.toggle('collapsed');
          currentGrid.classList.toggle('hidden');
        });
        root.appendChild(h);
        root.appendChild(currentGrid);
      } else if (item.type === 'panel' && panels[item.id]) {
        if (!currentGrid) {
          currentGrid = document.createElement('div');
          currentGrid.className = 'grid';
          root.appendChild(currentGrid);
        }
        renderPanel(currentGrid, item.id, panels[item.id]);
      }
    }
  }
}
"""


# A page stripped by strip_plotly() borrows Plotly from the window that
# embedded it (the report index carries the single bundled copy).  Plotly
# registers its stylesheets on the document that evaluated the bundle — the
# embedding page — so a borrowed namespace would otherwise draw the modebar
# unstyled inside an iframe: an unpositioned toolbar whose icons fall back
# to a black fill and vanish on the dark panels.  The shim therefore mirrors
# every plotly.js-style-* rule block into this document after each draw.
PARENT_PLOTLY_SHIM = """if (!window.Plotly && window.parent && window.parent.Plotly) {
  const parentPlotly = window.parent.Plotly;
  const mirrorPlotlyStyles = () => {
    try {
      window.parent.document.querySelectorAll('style[id^="plotly.js-style-"]').forEach(block => {
        if (!block.sheet) return;
        const css = Array.from(block.sheet.cssRules, rule => rule.cssText).join('\\n');
        let mirror = document.getElementById(block.id);
        if (!mirror) {
          mirror = document.createElement('style');
          mirror.id = block.id;
          document.head.appendChild(mirror);
        }
        if (mirror.textContent !== css) mirror.textContent = css;
      });
    } catch (err) {
      // An unreachable parent document only costs the modebar styling.
    }
  };
  const mirrorAfterDraw = (draw) => (...args) => {
    const pending = draw.apply(parentPlotly, args);
    if (pending && typeof pending.then === 'function') {
      pending.then(mirrorPlotlyStyles, mirrorPlotlyStyles);
    }
    mirrorPlotlyStyles();
    return pending;
  };
  const wrapped = {};
  for (const name of ['newPlot', 'react']) {
    if (typeof parentPlotly[name] === 'function') wrapped[name] = mirrorAfterDraw(parentPlotly[name]);
  }
  // Copy the namespace instead of mutating the parent's: its own draws stay
  // untouched while this document's draws refresh the mirrored styles.
  window.Plotly = Object.assign({}, parentPlotly, wrapped);
  mirrorPlotlyStyles();
}
"""

# The first shim only borrowed the parent's namespace verbatim; pages baked
# with it get the stylesheet-mirroring version swapped in when embedded.
_MINIMAL_PARENT_SHIM = re.compile(
    r"if \(!window\.Plotly && window\.parent && window\.parent\.Plotly\) \{\s*"
    r"window\.Plotly = window\.parent\.Plotly;\s*\}"
)
