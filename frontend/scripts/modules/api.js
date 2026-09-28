// Das Dashboard wird vom SolarFlow-Server selbst ausgeliefert - gleiche Adresse
export class ApiClient {
    async request(endpoint, { method = 'GET', body, timeout = 8000 } = {}) {
        let response;
        try {
            response = await fetch(endpoint, {
                method,
                headers: body ? { 'Content-Type': 'application/json' } : undefined,
                body: body ? JSON.stringify(body) : undefined,
                signal: AbortSignal.timeout(timeout)
            });
        } catch (error) {
            throw new Error(error.name === 'TimeoutError'
                ? 'Zeitüberschreitung bei der Serveranfrage'
                : 'SolarFlow-Server nicht erreichbar');
        }

        const data = await response.json().catch(() => null);
        if (!response.ok) {
            const error = new Error(this.errorText(data, response));
            error.status = response.status;
            throw error;
        }
        return data;
    }

    errorText(data, response) {
        const detail = data?.detail;
        if (typeof detail === 'string') return detail;
        if (Array.isArray(detail)) return detail.map(d => d.msg.replace(/^Value error, /, '')).join(', ');
        return `HTTP ${response.status}`;
    }

    status() { return this.request('/api/status'); }
    current() { return this.request('/api/current'); }
    stats(period = 'day', ref = null) {
        return this.request(`/api/stats?period=${period}${ref ? `&ref=${ref}` : ''}`);
    }
    devices() { return this.request('/api/devices'); }
    events(limit = 30) { return this.request(`/api/devices/events?limit=${limit}`); }
    hue() { return this.request('/api/hue'); }
    settings() { return this.request('/api/settings'); }
    saveSettings(settings) {
        // Ein Wechsel der Hue-Bridge kann einen Moment dauern
        return this.request('/api/settings', { method: 'PUT', body: settings, timeout: 20000 });
    }
    createDevice(device) { return this.request('/api/devices', { method: 'POST', body: device }); }
    updateDevice(name, device) {
        return this.request(`/api/devices/${encodeURIComponent(name)}`, { method: 'PUT', body: device });
    }
    deleteDevice(name) { return this.request(`/api/devices/${encodeURIComponent(name)}`, { method: 'DELETE' }); }
    switchDevice(name, on) {
        return this.request(`/api/devices/${encodeURIComponent(name)}/switch`, { method: 'POST', body: { on } });
    }
    releaseManual(name) {
        return this.request(`/api/devices/${encodeURIComponent(name)}/manual`, { method: 'DELETE' });
    }
}
