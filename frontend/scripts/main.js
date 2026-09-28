import { ApiClient } from './modules/api.js';
import { DashboardController } from './modules/dashboard.js';
import { DevicesController } from './modules/devices.js';
import { SettingsController } from './modules/settings.js';
import { StatisticsController } from './modules/statistic.js';
import { TabController } from './modules/tabs.js';
import { refreshIcons, setText, showBanner, updateConnectionStatus } from './modules/utils.js';

// Gesamtwerte ändern sich langsam - seltener abfragen als die Live-Werte
const TOTAL_REFRESH_MS = 60000;

class SolarFlowApp {
    constructor() {
        this.api = new ApiClient();
        this.interval = 5000;
        this.timer = null;
        this.lastTotals = 0;

        this.dashboard = new DashboardController();
        this.devices = new DevicesController(this.api);
        this.statistics = new StatisticsController(this.api);
        this.settings = new SettingsController(this.api, settings => this.setInterval(settings.update_interval));
        this.tabs = new TabController((name, previous) => {
            this.controllerFor(previous)?.onDeactivate?.();
            this.controllerFor(name)?.onActivate?.();
        });

        refreshIcons();
        this.start();
        document.addEventListener('visibilitychange', () => document.hidden ? this.stop() : this.start());
    }

    controllerFor(tab) {
        return { devices: this.devices, statistics: this.statistics, settings: this.settings }[tab];
    }

    async start() {
        if (this.timer) return;
        try {
            const [status, settings] = await Promise.all([this.api.status(), this.api.settings()]);
            setText('app-version', `SolarFlow ${status.version}`);
            this.interval = Math.max(settings.update_interval, 2) * 1000;
        } catch {
            // Der erste Update-Zyklus meldet die Störung
        }
        await this.update();
        this.timer = setInterval(() => this.update(), this.interval);
    }

    stop() {
        clearInterval(this.timer);
        this.timer = null;
    }

    setInterval(seconds) {
        this.interval = Math.max(seconds, 2) * 1000;
        this.stop();
        this.start();
    }

    async update() {
        const [current, devices, today] = await Promise.allSettled([
            this.api.current(), this.api.devices(), this.api.stats('day')
        ]);

        const serverDown = [current, devices, today].every(r => r.status === 'rejected' && !r.reason.status);
        updateConnectionStatus(!serverDown);
        if (serverDown) {
            showBanner('Keine Verbindung zum SolarFlow-Server – läuft das Programm bzw. der Container?');
            return;
        }

        setText('last-update', new Date().toLocaleTimeString('de-DE'));
        if (current.status === 'fulfilled') this.dashboard.update(current.value);
        if (devices.status === 'fulfilled') {
            this.dashboard.updateDevices(devices.value);
            this.devices.update(devices.value);
        }
        if (today.status === 'fulfilled') this.dashboard.updateToday(today.value);

        showBanner(this.problem(current, devices));

        if (Date.now() - this.lastTotals > TOTAL_REFRESH_MS) {
            this.lastTotals = Date.now();
            this.api.stats('all').then(total => this.dashboard.updateTotal(total)).catch(() => {});
        }
        this.statistics.refresh();
    }

    problem(current, devices) {
        if (current.status === 'rejected') return `Wechselrichter: ${current.reason.message}`;
        if (current.value.stale) {
            return `Der Wechselrichter antwortet nicht – letzter Messwert vor ${Math.round(current.value.age_seconds / 60)} min`;
        }
        const hue = devices.status === 'fulfilled' ? devices.value.hue : null;
        if (hue?.enabled && !hue.connected && hue.error) return `Hue: ${hue.error} – Geräte werden nicht geschaltet`;
        return null;
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.app = new SolarFlowApp();
});
