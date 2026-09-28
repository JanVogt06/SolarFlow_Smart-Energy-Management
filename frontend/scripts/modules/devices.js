import {
    escapeHtml, formatCountdown, formatDuration, formatPower, refreshIcons, setText, showNotification
} from './utils.js';

const STATUS_TEXT = { on: 'EIN', off: 'AUS', blocked: 'GESPERRT', unreachable: 'NICHT ERREICHBAR' };

export class DevicesController {
    constructor(api) {
        this.api = api;
        this.devices = [];
        this.hue = null;
        this.editing = null;

        this.grid = document.getElementById('devices-grid');
        this.modal = document.getElementById('device-modal');
        this.form = document.getElementById('device-form');
        this.timeRanges = document.getElementById('time-ranges-container');
        this.hueSelect = document.getElementById('device-hue-device');
        this.nameInput = document.getElementById('device-name');

        document.getElementById('add-device-btn').addEventListener('click', () => this.openModal());
        document.getElementById('close-device-modal').addEventListener('click', () => this.closeModal());
        document.getElementById('cancel-device').addEventListener('click', () => this.closeModal());
        this.modal.querySelector('.modal-overlay').addEventListener('click', () => this.closeModal());
        document.getElementById('save-device').addEventListener('click', () => this.saveDevice());
        document.getElementById('add-time-range').addEventListener('click', () => this.addTimeRange());
        this.hueSelect.addEventListener('change', () => {
            if (this.hueSelect.value) this.nameInput.value = this.hueSelect.value;
        });

        // Delegiert, weil die Karten bei jedem Update neu gezeichnet werden
        this.grid.addEventListener('click', event => {
            const button = event.target.closest('[data-action]');
            if (button) this.handleAction(button.dataset.action, button.dataset.device);
        });
    }

    async handleAction(action, name) {
        const device = this.devices.find(d => d.name === name);
        if (!device) return;
        if (action === 'edit') return this.openModal(device);
        if (action === 'delete' && !confirm(`Gerät "${name}" wirklich entfernen?`)) return;

        const actions = {
            toggle: () => this.api.switchDevice(name, device.state !== 'on'),
            auto: () => this.api.releaseManual(name),
            delete: () => this.api.deleteDevice(name)
        };
        try {
            showNotification((await actions[action]()).message, 'success');
            await this.refresh();
        } catch (error) {
            showNotification(error.message, 'error');
        }
    }

    async refresh() {
        try {
            this.update(await this.api.devices());
        } catch (error) {
            console.warn('Geräte konnten nicht geladen werden:', error);
        }
    }

    async onActivate() {
        await this.refresh();
        this.loadEvents();
    }

    update(data) {
        if (!data) return;
        this.devices = data.devices;
        this.hue = data.hue;
        this.renderHueStatus();
        this.render();

        const active = this.devices.filter(d => d.state === 'on');
        setText('active-devices', String(active.length));
        setText('total-consumption', formatPower(data.total_consumption));
    }

    renderHueStatus() {
        const status = document.getElementById('hue-status');
        const hue = this.hue;
        const [text, css] = !hue.enabled ? ['Hue-Steuerung aus', 'off']
            : hue.connected ? [`Hue verbunden (${hue.bridge_ip})`, 'ok']
            : [hue.error || 'Hue Bridge nicht erreichbar', 'error'];
        setText(status, text);
        status.className = `hue-status ${css}`;
    }

    render() {
        if (!this.devices.length) {
            this.renderedHtml = null;
            this.grid.innerHTML = `<div class="empty-state">
                <i data-lucide="plug"></i>
                <p>Noch keine Geräte. Lege ein Gerät mit demselben Namen wie in der Hue-App an.</p></div>`;
            refreshIcons();
            return;
        }

        const html = this.devices.map(d => {
            const name = escapeHtml(d.name);
            const manual = d.manual_remaining;
            const status = manual ? (d.state === 'on' ? 'MANUELL EIN' : 'MANUELL AUS') : STATUS_TEXT[d.state] || d.state;
            const controllable = d.state !== 'unreachable' && this.hue.enabled && this.hue.connected;
            const note = !controllable ? ['plug-zap', 'device-warning', d.hint]
                : manual ? ['hand', 'device-manual', `Automatik pausiert noch ${formatCountdown(manual)}`]
                : d.hysteresis_remaining ? ['timer', 'device-hysteresis', `Wartet noch ${formatCountdown(d.hysteresis_remaining)}`]
                : d.hint ? ['info', 'device-hint', d.hint] : null;

            return `
            <div class="device-card ${d.state === 'on' ? 'active' : ''} ${d.state === 'unreachable' ? 'unreachable' : ''}">
                <div class="device-header">
                    <div class="device-info">
                        <h3 class="device-name text-gradient">${name}</h3>
                        <p class="device-description">${escapeHtml(d.description)}</p>
                    </div>
                    <span class="device-status ${manual ? 'manual' : d.state}">${status}</span>
                </div>

                <div class="device-metrics">
                    <div class="device-metric"><div class="metric-icon"><i data-lucide="zap"></i></div>
                        <div class="metric-info"><span class="metric-label">Leistung</span>
                        <span class="metric-value">${formatPower(d.power_consumption)}</span></div></div>
                    <div class="device-metric"><div class="metric-icon"><i data-lucide="activity"></i></div>
                        <div class="metric-info"><span class="metric-label">Priorität</span>
                        <span class="metric-value">${d.priority}</span></div></div>
                    <div class="device-metric"><div class="metric-icon"><i data-lucide="clock"></i></div>
                        <div class="metric-info"><span class="metric-label">Heute</span>
                        <span class="metric-value">${formatDuration(d.runtime_today)}</span></div></div>
                </div>

                <div class="device-thresholds">
                    <div class="threshold-bar"><span class="threshold-label">Ein ab</span>
                        <span class="threshold-value">${formatPower(d.switch_on_threshold)}</span></div>
                    <div class="threshold-bar"><span class="threshold-label">Aus unter</span>
                        <span class="threshold-value">${formatPower(d.switch_off_threshold)}</span></div>
                    <div class="threshold-bar"><span class="threshold-label">Zeiten</span>
                        <span class="threshold-value">${d.allowed_time_ranges.length
                            ? d.allowed_time_ranges.map(r => r.join('–')).join(', ') : 'immer'}</span></div>
                </div>

                ${note ? `<div class="${note[1]}"><i data-lucide="${note[0]}"></i><span>${escapeHtml(note[2])}</span></div>` : ''}

                <div class="device-actions">
                    <button class="btn btn-secondary btn-small" data-action="toggle" data-device="${name}"
                            ${controllable ? '' : 'disabled'}>
                        <i data-lucide="power"></i> ${d.state === 'on' ? 'Ausschalten' : 'Einschalten'}
                    </button>
                    ${manual ? `<button class="btn btn-secondary btn-small" data-action="auto" data-device="${name}">
                        <i data-lucide="refresh-cw"></i> Automatik</button>` : ''}
                    <button class="btn-icon" data-action="edit" data-device="${name}" title="Bearbeiten">
                        <i data-lucide="pencil"></i></button>
                    <button class="btn-icon device-delete" data-action="delete" data-device="${name}" title="Entfernen">
                        <i data-lucide="trash-2"></i></button>
                </div>
            </div>`;
        }).join('');

        // Nur bei Änderungen neu zeichnen - sonst verpufft ein Klick während des Updates
        if (html === this.renderedHtml) return;
        this.renderedHtml = html;
        this.grid.innerHTML = html;
        refreshIcons();
    }

    async loadEvents() {
        const list = document.getElementById('event-list');
        try {
            const events = await this.api.events(30);
            list.innerHTML = events.length ? events.map(e => {
                const when = new Date(e.timestamp.replace(' ', 'T'));
                return `<li class="event ${escapeHtml(e.new_state)}">
                    <time>${when.toLocaleString('de-DE', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })}</time>
                    <strong>${escapeHtml(e.device_name)}</strong>
                    <span>${escapeHtml(e.action)}</span>
                    <span class="muted">${escapeHtml(e.reason || '')}</span>
                </li>`;
            }).join('') : '<li class="muted">Noch keine Schaltvorgänge</li>';
        } catch (error) {
            list.innerHTML = `<li class="muted">${escapeHtml(error.message)}</li>`;
        }
    }

    // --- Formular -----------------------------------------------------------

    async openModal(device = null) {
        this.editing = device;
        this.form.reset();
        this.timeRanges.innerHTML = '';
        setText('device-modal-title', device ? `${device.name} bearbeiten` : 'Neues Gerät');

        if (device) {
            const values = {
                'device-name': device.name, 'device-description': device.description,
                'device-power': device.power_consumption, 'device-priority': device.priority,
                'device-switch-on': device.switch_on_threshold, 'device-switch-off': device.switch_off_threshold,
                'device-min-runtime': device.min_runtime, 'device-max-runtime': device.max_runtime_per_day
            };
            for (const [id, value] of Object.entries(values)) document.getElementById(id).value = value;
            device.allowed_time_ranges.forEach(([start, end]) => this.addTimeRange(start, end));
        }

        this.modal.classList.add('active');
        refreshIcons();
        await this.loadHueLights();
    }

    closeModal() {
        this.modal.classList.remove('active');
    }

    async loadHueLights() {
        const picker = document.getElementById('hue-picker');
        try {
            const hue = await this.api.hue();
            const taken = new Set(this.devices.map(d => d.name));
            const lights = hue.lights.filter(name => !taken.has(name) || name === this.editing?.name);
            picker.hidden = !lights.length;
            this.hueSelect.innerHTML = '<option value="">-- Namen selbst eingeben --</option>' +
                lights.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
            this.hueSelect.value = lights.includes(this.nameInput.value) ? this.nameInput.value : '';
        } catch {
            picker.hidden = true;
        }
    }

    addTimeRange(start = '06:00', end = '22:00') {
        const row = document.createElement('div');
        row.className = 'time-range-row';
        row.innerHTML = `
            <input type="time" class="time-start" value="${start}" required>
            <span class="time-separator">bis</span>
            <input type="time" class="time-end" value="${end}" required>
            <button type="button" class="remove-time-range" title="Entfernen"><i data-lucide="x"></i></button>`;
        row.querySelector('.remove-time-range').addEventListener('click', () => row.remove());
        this.timeRanges.appendChild(row);
        refreshIcons();
    }

    async saveDevice() {
        if (!this.form.checkValidity()) {
            this.form.reportValidity();
            return;
        }

        const data = new FormData(this.form);
        const number = key => Number(data.get(key)) || 0;
        const device = {
            name: String(data.get('name')).trim(),
            description: String(data.get('description') || ''),
            power_consumption: number('power_consumption'),
            priority: number('priority'),
            switch_on_threshold: number('switch_on_threshold'),
            switch_off_threshold: number('switch_off_threshold'),
            min_runtime: number('min_runtime'),
            max_runtime_per_day: number('max_runtime_per_day'),
            allowed_time_ranges: [...this.timeRanges.querySelectorAll('.time-range-row')]
                .map(row => [row.querySelector('.time-start').value, row.querySelector('.time-end').value])
                .filter(([start, end]) => start && end)
        };

        try {
            const response = this.editing
                ? await this.api.updateDevice(this.editing.name, device)
                : await this.api.createDevice(device);
            showNotification(response.message, 'success');
            this.closeModal();
            await this.refresh();
        } catch (error) {
            showNotification(error.message, 'error');
        }
    }
}
