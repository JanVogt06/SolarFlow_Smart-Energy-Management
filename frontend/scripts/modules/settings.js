import { refreshIcons, setText, showNotification } from './utils.js';

// Einstellung der API -> Eingabefeld
const FIELDS = {
    fronius_ip: ['fronius-ip', 'text'],
    update_interval: ['poll-interval', 'number'],
    enable_hue: ['enable-hue', 'checkbox'],
    hue_bridge_ip: ['hue-bridge-ip', 'text'],
    electricity_price: ['electricity-price', 'number'],
    electricity_price_night: ['electricity-price-night', 'number'],
    night_tariff_start: ['night-tariff-start', 'number'],
    night_tariff_end: ['night-tariff-end', 'number'],
    feed_in_tariff: ['feed-in-tariff', 'number'],
    hysteresis_minutes: ['hysteresis-minutes', 'number'],
    manual_override_minutes: ['manual-override-minutes', 'number'],
    min_battery_soc_on: ['battery-soc-on', 'number'],
    min_battery_soc_off: ['battery-soc-off', 'number']
};

export class SettingsController {
    constructor(api, onSaved) {
        this.api = api;
        this.onSaved = onSaved;
        this.button = document.getElementById('save-settings');
        this.button.addEventListener('click', () => this.save());
    }

    async onActivate() {
        try {
            this.fill(await this.api.settings());
            const hue = await this.api.hue();
            setText('hue-setting-status', !hue.enabled ? ''
                : hue.connected ? `Verbunden – ${hue.lights.length} Hue-Geräte gefunden`
                : hue.error || 'Bridge nicht erreichbar');
        } catch (error) {
            showNotification(`Einstellungen nicht geladen: ${error.message}`, 'error');
        }
    }

    fill(settings) {
        for (const [name, [id, type]] of Object.entries(FIELDS)) {
            const input = document.getElementById(id);
            if (!input || settings[name] === undefined) continue;
            if (type === 'checkbox') input.checked = Boolean(settings[name]);
            else input.value = settings[name];
        }
    }

    read() {
        const payload = {};
        for (const [name, [id, type]] of Object.entries(FIELDS)) {
            const input = document.getElementById(id);
            if (type === 'checkbox') payload[name] = input.checked;
            else if (type === 'number' && input.value !== '') payload[name] = Number(input.value);
            else if (type === 'text' && input.value.trim()) payload[name] = input.value.trim();
        }
        return payload;
    }

    async save() {
        this.button.disabled = true;
        this.button.innerHTML = '<i data-lucide="loader"></i> Speichere...';
        refreshIcons();
        try {
            const response = await this.api.saveSettings(this.read());
            this.fill(response.settings);
            showNotification(response.message, 'success');
            this.onSaved?.(response.settings);
            setTimeout(() => this.onActivate(), 1500);
        } catch (error) {
            showNotification(error.message, 'error');
        } finally {
            this.button.disabled = false;
            this.button.innerHTML = '<i data-lucide="save"></i> Speichern';
            refreshIcons();
        }
    }
}
