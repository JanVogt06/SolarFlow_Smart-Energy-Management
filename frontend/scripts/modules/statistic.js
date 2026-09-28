import { renderColumns, renderLegend } from './chart.js';
import { COLORS } from './dashboard.js';
import {
    escapeHtml, formatEnergy, formatEuro, formatNumber, formatPercent, formatPower, setText, showNotification
} from './utils.js';

const BUCKET_TEXT = { hour: 'je Stunde', day: 'je Tag', month: 'je Monat', year: 'je Jahr' };

export class StatisticsController {
    constructor(api) {
        this.api = api;
        this.period = 'day';
        this.ref = null;
        this.active = false;
        this.loading = false;

        this.periodButtons = document.querySelectorAll('#period-select button');
        this.prev = document.getElementById('period-prev');
        this.next = document.getElementById('period-next');

        this.periodButtons.forEach(button => button.addEventListener('click', () => {
            this.period = button.dataset.period;
            this.ref = null;
            this.load();
        }));
        this.prev.addEventListener('click', () => this.navigate(this.prev.dataset.ref));
        this.next.addEventListener('click', () => this.navigate(this.next.dataset.ref));

        renderLegend(document.getElementById('energy-legend'), [
            { name: 'Erzeugung', color: COLORS.pv }, { name: 'Verbrauch', color: COLORS.load }
        ]);
    }

    navigate(ref) {
        if (!ref) return;
        this.ref = ref;
        this.load();
    }

    onActivate() {
        this.active = true;
        this.load();
    }

    onDeactivate() {
        this.active = false;
    }

    // Aktuelle Zeiträume laufend nachführen, vergangene bleiben stehen
    refresh() {
        if (this.active && !this.ref) this.load(true);
    }

    async load(quiet = false) {
        if (this.loading) return;
        this.loading = true;
        try {
            this.render(await this.api.stats(this.period, this.ref));
        } catch (error) {
            if (!quiet) showNotification(error.message, 'error');
        } finally {
            this.loading = false;
        }
    }

    render(stats) {
        const { period, energy, costs } = stats;

        this.periodButtons.forEach(b => b.classList.toggle('active', b.dataset.period === period.kind));
        setText('period-label', period.label);
        this.prev.dataset.ref = period.previous || '';
        this.next.dataset.ref = period.next || '';
        this.prev.disabled = !period.previous;
        this.next.disabled = !period.next;

        setText('stat-pv', formatEnergy(energy.pv));
        setText('stat-load', formatEnergy(energy.load));
        setText('stat-self', formatEnergy(energy.self_consumption));
        setText('stat-grid', formatEnergy(energy.grid));
        setText('stat-feed-in', formatEnergy(energy.feed_in));
        setText('stat-autarky', formatPercent(stats.autarky));
        setText('stat-self-rate', formatPercent(stats.self_consumption_rate));
        setText('stat-pv-max', stats.pv_max === null ? '–' : formatPower(stats.pv_max));

        setText('cost-without', formatEuro(costs.cost_without_solar));
        setText('cost-grid', formatEuro(costs.grid_cost));
        setText('cost-saved', formatEuro(costs.saved));
        setText('cost-revenue', formatEuro(costs.feed_in_revenue));
        setText('cost-benefit', formatEuro(costs.benefit));

        setText('energy-chart-subtitle', `in kWh ${BUCKET_TEXT[period.bucket]}`);
        const labels = stats.series.map(s => s.label);
        const titles = stats.series.map(s => this.bucketTitle(s, period.bucket));

        renderColumns(document.getElementById('energy-chart'), {
            labels, titles,
            series: [
                { name: 'Erzeugung', color: COLORS.pv, values: stats.series.map(s => s.pv) },
                { name: 'Verbrauch', color: COLORS.load, values: stats.series.map(s => s.load) }
            ],
            format: v => formatNumber(v, 1),
            tooltipRows: i => {
                const s = stats.series[i];
                if (s.pv === null) return [['', 'liegt in der Zukunft']];
                return [['Erzeugung', formatEnergy(s.pv)], ['Verbrauch', formatEnergy(s.load)],
                        ['Einspeisung', formatEnergy(s.feed_in)], ['Netzbezug', formatEnergy(s.grid)],
                        ['Nutzen', formatEuro(s.benefit)]];
            }
        });

        renderColumns(document.getElementById('benefit-chart'), {
            labels, titles,
            series: [{ name: 'Nutzen', color: COLORS.benefit, values: stats.series.map(s => s.benefit) }],
            format: v => formatNumber(v, 2),
            tooltipRows: i => [['Nutzen', formatEuro(stats.series[i].benefit)]]
        });

        this.renderDevices(stats.devices);
        this.renderTable(stats.series, titles);
    }

    bucketTitle(bucket, kind) {
        const start = new Date(bucket.start);
        if (kind === 'hour') return `${bucket.label}:00 – ${bucket.label}:59 Uhr`;
        if (kind === 'day') return start.toLocaleDateString('de-DE', { weekday: 'long', day: 'numeric', month: 'long' });
        if (kind === 'month') return start.toLocaleDateString('de-DE', { month: 'long', year: 'numeric' });
        return String(start.getFullYear());
    }

    renderDevices(devices) {
        const body = document.querySelector('#device-usage tbody');
        body.innerHTML = devices.length ? devices.map(d => `
            <tr>
                <td>${escapeHtml(d.name)}</td>
                <td>${formatNumber(d.runtime_hours, 1)} h</td>
                <td>${formatEnergy(d.energy)}</td>
                <td>${d.switches}</td>
            </tr>`).join('')
            : '<tr><td colspan="4" class="muted">Keine Geräte gelaufen</td></tr>';
    }

    renderTable(series, titles) {
        const body = document.querySelector('#series-table tbody');
        body.innerHTML = series.filter(s => s.pv !== null).map(s => `
            <tr>
                <td>${escapeHtml(titles[series.indexOf(s)])}</td>
                <td>${formatEnergy(s.pv)}</td>
                <td>${formatEnergy(s.load)}</td>
                <td>${formatEnergy(s.feed_in)}</td>
                <td>${formatEnergy(s.grid)}</td>
                <td>${formatEuro(s.benefit)}</td>
            </tr>`).join('');
    }
}
