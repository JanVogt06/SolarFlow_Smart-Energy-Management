import { renderColumns, renderLegend } from './chart.js';
import { formatEnergy, formatEuro, formatNumber, formatPercent, formatPower, setText, refreshIcons } from './utils.js';

export const COLORS = { pv: '#c98500', load: '#3987e5', benefit: '#199e70' };

export class DashboardController {
    constructor() {
        this.gridCard = document.getElementById('grid-card');
        this.batteryCard = document.getElementById('battery-card');
        this.batteryIcon = 'battery';
        renderLegend(document.getElementById('today-legend'), [
            { name: 'Erzeugung', color: COLORS.pv }, { name: 'Verbrauch', color: COLORS.load }
        ]);
    }

    update(current) {
        if (!current) return;

        setText('pv-power', formatPower(current.pv_power));
        setText('load-power', formatPower(current.load_power));

        const feeding = current.grid_power < 0;
        this.gridCard.classList.toggle('feeding', feeding);
        this.gridCard.classList.toggle('consuming', !feeding);
        setText('grid-label', feeding ? 'Einspeisung' : 'Netzbezug');
        setText('grid-power', formatPower(Math.abs(current.grid_power)));

        this.updateBattery(current);

        const autarky = document.getElementById('autarky-rate');
        setText(autarky, formatPercent(current.autarky_rate));
        autarky.classList.remove('low', 'medium', 'high');
        autarky.classList.add(current.autarky_rate >= 75 ? 'high' : current.autarky_rate >= 50 ? 'medium' : 'low');

        setText('surplus-power', formatPower(current.feed_in_power));
    }

    updateBattery(current) {
        this.batteryCard.style.display = current.has_battery ? '' : 'none';
        if (!current.has_battery) return;

        const soc = current.battery_soc;
        const power = Math.abs(current.battery_power);
        setText('battery-soc', `${formatNumber(soc)} %`);
        setText('battery-label', power < 10 ? 'Batterie' :
            current.battery_power < 0 ? `lädt ${formatPower(power)}` : `entlädt ${formatPower(power)}`);
        this.batteryCard.classList.toggle('low-battery', soc < 20);
        this.batteryCard.classList.toggle('full-battery', soc > 95);

        const icon = soc <= 20 ? 'battery-low' : soc >= 80 ? 'battery-full' : 'battery-medium';
        if (icon !== this.batteryIcon) {
            this.batteryIcon = icon;
            this.batteryCard.querySelector('.flow-icon').innerHTML = `<i data-lucide="${icon}"></i>`;
            refreshIcons();
        }
    }

    updateDevices(devicesData) {
        const running = devicesData.devices.filter(d => d.state === 'on');
        setText('devices-running', running.length
            ? `${running.length} Gerät${running.length > 1 ? 'e' : ''} aktiv (${formatPower(devicesData.total_consumption)})`
            : 'keine Geräte aktiv');
    }

    updateToday(stats) {
        setText('daily-energy', formatEnergy(stats.energy.pv));
        setText('feed-in-energy', formatEnergy(stats.energy.feed_in));
        setText('benefit-today', formatEuro(stats.costs.benefit));
        setText('autarky-today', formatPercent(stats.autarky));

        renderColumns(document.getElementById('today-chart'), {
            labels: stats.series.map(s => s.label),
            titles: stats.series.map(s => `${s.label}:00 – ${s.label}:59 Uhr`),
            series: [
                { name: 'Erzeugung', color: COLORS.pv, values: stats.series.map(s => s.pv) },
                { name: 'Verbrauch', color: COLORS.load, values: stats.series.map(s => s.load) }
            ],
            format: v => formatNumber(v, 1),
            tooltipRows: i => {
                const s = stats.series[i];
                return s.pv === null ? [['', 'noch keine Werte']] : [
                    ['Erzeugung', formatEnergy(s.pv)], ['Verbrauch', formatEnergy(s.load)],
                    ['Einspeisung', formatEnergy(s.feed_in)], ['Netzbezug', formatEnergy(s.grid)]
                ];
            }
        });
    }

    updateTotal(stats) {
        setText('benefit-total', formatEuro(stats.costs.benefit));
    }
}
