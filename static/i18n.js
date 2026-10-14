// The selected page owns its language catalogue. Backend values remain unchanged.
window.LaundryText = (() => {
    const node = document.getElementById('laundryTranslations');
    const messages = node?.textContent ? JSON.parse(node.textContent) : {};
    return (source, ...values) => {
        if (!Array.isArray(source)) return Object.hasOwn(messages, source) ? messages[source] : source;
        const key = source.reduce((text, part, i) => text + (i ? `{${i - 1}}` : '') + part, '');
        const template = Object.hasOwn(messages, key) ? messages[key] : key;
        return template.replace(/\{(\d+)\}/g, (match, i) => i < values.length ? String(values[i]) : match);
    };
})();
