const numberFormats = new Map();

export function formatNumber(value, decimals = 0) {
    if (value === null || value === undefined || Number.isNaN(value)) return '–';
    if (!numberFormats.has(decimals)) {
        numberFormats.set(decimals, new Intl.NumberFormat('de-DE', {
            minimumFractionDigits: decimals, maximumFractionDigits: decimals
        }));
    }
    return numberFormats.get(decimals).format(value);
}

export const formatPower = watts => `${formatNumber(watts)} W`;
export const formatPercent = value => (value === null || value === undefined) ? '–' : `${formatNumber(value)} %`;
export const formatEuro = value => (value === null || value === undefined) ? '–' : `${formatNumber(value, 2)} €`;

export function formatEnergy(kwh) {
    if (kwh === null || kwh === undefined) return '–';
    if (Math.abs(kwh) >= 1000) return `${formatNumber(kwh / 1000, 2)} MWh`;
    return `${formatNumber(kwh, Math.abs(kwh) < 10 ? 2 : 1)} kWh`;
}

export function formatDuration(minutes) {
    if (!minutes) return '0 min';
    const h = Math.floor(minutes / 60);
    const m = Math.round(minutes % 60);
    return h > 0 ? `${h} h ${m} min` : `${m} min`;
}

export function formatCountdown(seconds) {
    if (!seconds || seconds <= 0) return '';
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return m > 0 ? `${m}:${String(s).padStart(2, '0')} min` : `${s} s`;
}

export function escapeHtml(text) {
    return String(text ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    }[c]));
}

export function setText(id, text) {
    const element = typeof id === 'string' ? document.getElementById(id) : id;
    if (element && element.textContent !== text) element.textContent = text;
}

export function refreshIcons() {
    if (window.lucide) window.lucide.createIcons();
}

export function updateConnectionStatus(online) {
    const status = document.getElementById('connection-status');
    if (!status) return;
    status.classList.toggle('online', online);
    status.classList.toggle('offline', !online);
    setText(status.querySelector('.status-text'), online ? 'Verbunden' : 'Getrennt');
}

export function showBanner(message) {
    const banner = document.getElementById('status-banner');
    if (!banner) return;
    banner.hidden = !message;
    if (message) setText('status-banner-text', message);
}

export function showNotification(message, type = 'info') {
    const icons = { success: 'check-circle', error: 'alert-circle', warning: 'alert-triangle', info: 'info' };
    const notification = document.createElement('div');
    notification.className = `notification notification-${type}`;
    notification.innerHTML = `<i data-lucide="${icons[type] || 'info'}"></i><span>${escapeHtml(message)}</span>`;
    document.body.appendChild(notification);
    refreshIcons();

    requestAnimationFrame(() => {
        notification.style.opacity = '1';
        notification.style.transform = 'translateX(0)';
    });
    setTimeout(() => {
        notification.style.opacity = '0';
        notification.style.transform = 'translateX(100%)';
        setTimeout(() => notification.remove(), 300);
    }, 3500);
}
