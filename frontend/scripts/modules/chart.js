import { escapeHtml, formatNumber } from './utils.js';

const SVG = 'http://www.w3.org/2000/svg';
const HEIGHT = 240;
const MARGIN = { top: 12, right: 8, bottom: 28, left: 48 };
const BAR_MAX = 24;
const BAR_GAP = 2;
const RADIUS = 4;

const observers = new WeakMap();

function niceStep(max, ticks) {
    const raw = max / ticks;
    const magnitude = 10 ** Math.floor(Math.log10(raw));
    const residual = raw / magnitude;
    const step = residual > 5 ? 10 : residual > 2 ? 5 : residual > 1 ? 2 : 1;
    return step * magnitude;
}

function el(name, attrs = {}) {
    const node = document.createElementNS(SVG, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    return node;
}

// Säule mit 4px-Rundung oben, gerade an der Grundlinie
function columnPath(x, y, width, height) {
    const r = Math.min(RADIUS, width / 2, height);
    return `M${x},${y + height}V${y + r}Q${x},${y} ${x + r},${y}H${x + width - r}` +
           `Q${x + width},${y} ${x + width},${y + r}V${y + height}Z`;
}

export function renderLegend(container, series) {
    if (!container) return;
    container.innerHTML = series.map(s =>
        `<span class="legend-item"><span class="legend-swatch" style="background:${s.color}"></span>${escapeHtml(s.name)}</span>`
    ).join('');
}

/**
 * Gruppiertes Säulendiagramm.
 *
 * @param {HTMLElement} container
 * @param {{labels: string[], titles?: string[], series: {name: string, color: string, values: (number|null)[]}[],
 *          format: (v: number) => string (Standard-Tooltip), tooltipRows?: (i: number) => [string, string][]}} spec
 */
export function renderColumns(container, spec) {
    if (!container) return;
    container._spec = spec;
    if (!observers.has(container)) {
        const observer = new ResizeObserver(() => draw(container));
        observer.observe(container);
        observers.set(container, observer);
    }
    draw(container);
}

function draw(container) {
    const spec = container._spec;
    const width = container.clientWidth;
    if (!spec || width === 0) return;

    const { labels, series, format } = spec;
    const count = labels.length;
    const plotWidth = width - MARGIN.left - MARGIN.right;
    const plotHeight = HEIGHT - MARGIN.top - MARGIN.bottom;

    const allValues = series.flatMap(s => s.values).filter(v => v !== null && v !== undefined);
    const hasData = allValues.some(v => v > 0);
    const step = niceStep(Math.max(...allValues, 0) || 1, 4);
    const yMax = Math.max(step * Math.ceil((Math.max(...allValues, 0) || 1) / step), step);
    const y = value => MARGIN.top + plotHeight - (value / yMax) * plotHeight;
    const tickDecimals = step >= 1 ? 0 : Math.ceil(-Math.log10(step));

    const svg = el('svg', { width, height: HEIGHT, viewBox: `0 0 ${width} ${HEIGHT}`, role: 'img' });

    for (let tick = 0; tick <= yMax + step / 2; tick += step) {
        const ty = y(tick);
        svg.appendChild(el('line', { x1: MARGIN.left, x2: width - MARGIN.right, y1: ty, y2: ty,
                                     class: tick === 0 ? 'chart-baseline' : 'chart-grid' }));
        const label = el('text', { x: MARGIN.left - 8, y: ty + 4, 'text-anchor': 'end', class: 'chart-tick' });
        label.textContent = formatNumber(tick, tickDecimals);
        svg.appendChild(label);
    }

    const band = plotWidth / count;
    const barWidth = Math.max(Math.min(BAR_MAX, (band * 0.8 - BAR_GAP * (series.length - 1)) / series.length), 1);
    const groupWidth = barWidth * series.length + BAR_GAP * (series.length - 1);
    const labelEvery = Math.max(1, Math.ceil(count / Math.max(plotWidth / 44, 1)));

    labels.forEach((text, i) => {
        const x0 = MARGIN.left + band * i + (band - groupWidth) / 2;
        series.forEach((s, k) => {
            const value = s.values[i];
            if (value === null || value === undefined || value <= 0) return;
            const top = y(value);
            svg.appendChild(el('path', {
                d: columnPath(x0 + k * (barWidth + BAR_GAP), top, barWidth, MARGIN.top + plotHeight - top),
                fill: s.color
            }));
        });

        if (i % labelEvery === 0) {
            const label = el('text', { x: MARGIN.left + band * (i + 0.5), y: HEIGHT - 8, 'text-anchor': 'middle',
                                       class: 'chart-tick' });
            label.textContent = text;
            svg.appendChild(label);
        }
    });

    if (!hasData) {
        const empty = el('text', { x: MARGIN.left + plotWidth / 2, y: MARGIN.top + plotHeight / 2,
                                   'text-anchor': 'middle', class: 'chart-empty' });
        empty.textContent = 'Keine Messwerte in diesem Zeitraum';
        svg.appendChild(empty);
    }

    const highlight = el('rect', { y: MARGIN.top, height: plotHeight, width: band, class: 'chart-hover', visibility: 'hidden' });
    svg.insertBefore(highlight, svg.firstChild);

    const tooltip = document.createElement('div');
    tooltip.className = 'chart-tooltip';
    tooltip.hidden = true;

    const hit = el('rect', { x: MARGIN.left, y: 0, width: plotWidth, height: HEIGHT, fill: 'transparent' });
    const show = event => {
        const bounds = svg.getBoundingClientRect();
        const i = Math.min(count - 1, Math.max(0, Math.floor((event.clientX - bounds.left - MARGIN.left) / band)));
        highlight.setAttribute('x', MARGIN.left + band * i);
        highlight.setAttribute('visibility', 'visible');

        const rows = spec.tooltipRows ? spec.tooltipRows(i) : series.map(s => [s.name, format(s.values[i] ?? 0)]);
        const swatches = Object.fromEntries(series.map(s => [s.name, s.color]));
        tooltip.innerHTML = `<div class="chart-tooltip-title">${escapeHtml(spec.titles?.[i] ?? labels[i])}</div>` +
            rows.map(([name, value]) => `<div class="chart-tooltip-row">` +
                `<span>${swatches[name] ? `<span class="legend-swatch" style="background:${swatches[name]}"></span>` : ''}` +
                `${escapeHtml(name)}</span><strong>${escapeHtml(value)}</strong></div>`).join('');
        tooltip.hidden = false;

        const left = MARGIN.left + band * (i + 0.5);
        const flip = left > width / 2;
        tooltip.style.left = flip ? 'auto' : `${left + 12}px`;
        tooltip.style.right = flip ? `${width - left + 12}px` : 'auto';
    };
    hit.addEventListener('pointermove', show);
    hit.addEventListener('pointerdown', show);
    hit.addEventListener('pointerleave', () => {
        tooltip.hidden = true;
        highlight.setAttribute('visibility', 'hidden');
    });
    svg.appendChild(hit);

    container.replaceChildren(svg, tooltip);
}
