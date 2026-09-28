import { refreshIcons } from './utils.js';

export class TabController {
    constructor(onChange) {
        this.onChange = onChange;
        this.current = 'overview';
        document.querySelectorAll('.nav-tab').forEach(tab =>
            tab.addEventListener('click', () => this.switchTab(tab.dataset.tab)));
    }

    switchTab(name) {
        if (this.current === name) return;
        const previous = this.current;
        this.current = name;

        document.querySelectorAll('.nav-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.tab === name));
        document.querySelectorAll('.tab-content').forEach(content =>
            content.classList.toggle('active', content.id === `${name}-tab`));

        this.onChange?.(name, previous);
        refreshIcons();
    }
}
