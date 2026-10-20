() => {
    const out = [];
    const seen = new Set();
    const visible = el => {
        const rect = el.getBoundingClientRect();
        const style = getComputedStyle(el);
        const shown = !el.hidden && !el.closest('[hidden],[aria-hidden="true"]') &&
            style.display !== 'none' && style.visibility !== 'hidden' &&
            rect.width > 0 && rect.height > 0;
        if (!shown) return false;
        if (rect.top < innerHeight && rect.bottom > 0 && rect.left < innerWidth && rect.right > 0) {
            const x = (Math.max(0, rect.left) + Math.min(innerWidth - 1, rect.right)) / 2;
            const y = (Math.max(0, rect.top) + Math.min(innerHeight - 1, rect.bottom)) / 2;
            const hit = document.elementFromPoint(x, y);
            if (hit && !el.contains(hit) && !hit.contains(el)) return false;
        }
        return true;
    };
    const abs = value => {
        try { return value ? new URL(value, document.baseURI).href : ''; }
        catch { return ''; }
    };
    const label = el => (el.getAttribute('aria-label') ||
        Array.from(el.labels || []).map(item => item.innerText).join(' ') ||
        el.getAttribute('placeholder') || el.innerText || el.getAttribute('title') ||
        el.getAttribute('name') || el.id || '').replace(/\s+/g, ' ').trim().slice(0, 160);
    const formInfo = el => {
        const form = el.form || el.closest('form');
        return {
            form_method: form ? (el.getAttribute('formmethod') || form.method || 'GET').toUpperCase() : '',
            form_action: form ? abs(el.getAttribute('formaction') || form.getAttribute('action') || location.href) : '',
            authentication: !!form?.querySelector('input[type="password"]'),
        };
    };
    const mark = (kind, el, extra = {}) => {
        if (seen.has(el) || el.disabled || el.readOnly || (kind !== 'file' && !visible(el))) return;
        if (el.closest('a[href]') && kind !== 'link') return;
        if (!el.dataset.orbisInteraction) {
            window.__orbisElementSerial = (window.__orbisElementSerial || 0) + 1;
            el.dataset.orbisInteraction = `orbis-${window.__orbisElementSerial}`;
        }
        const parts = [];
        for (let node = el; node && parts.length < 5; node = node.parentElement) {
            if (node.id) { parts.unshift(`#${node.id}`); break; }
            const siblings = node.parentElement ? Array.from(node.parentElement.children).filter(n => n.tagName === node.tagName) : [node];
            parts.unshift(`${node.tagName}:${siblings.indexOf(node)}`);
        }
        seen.add(el);
        out.push({
            kind, selector: `[data-orbis-interaction="${el.dataset.orbisInteraction}"]`,
            key: parts.join('/'),
            navigation: !!el.closest('nav,header,footer,[role="navigation"]') && !el.closest('main,[role="main"],article,dialog,[role="dialog"]'),
            heading: !!el.closest('h1,h2,h3,h4,h5,h6'),
            in_dialog: !!el.closest('dialog,[role="dialog"]'),
            context: Array.from((el.form || el.closest('form,dialog,[role="dialog"]') || el).querySelectorAll('input:not([type="hidden"]),textarea,select'))
                .map(field => [field.name, field.type, field.getAttribute('placeholder') || '']),
            tag: el.tagName.toLowerCase(), label: label(el),
            text: [label(el), el.getAttribute('name'), el.id, el.getAttribute('class')].filter(Boolean).join(' ').slice(0, 500),
            name: el.getAttribute('name') || '', role: el.getAttribute('role') || '',
            selected: el.getAttribute('aria-selected') || '',
            ...formInfo(el), ...extra,
        });
    };
    for (const el of document.querySelectorAll('input,textarea,[contenteditable="true"],select')) {
        const type = (el.getAttribute('type') || 'text').toLowerCase();
        if (['hidden', 'password', 'submit', 'button', 'reset'].includes(type)) continue;
        const details = {type, filled: !!(el.value || (el.isContentEditable && el.innerText)),
            required: el.required, min: el.min || '', max_length: el.maxLength || -1};
        if (el.tagName === 'SELECT') {
            mark('select', el, {...details, options: Array.from(el.options)
                .filter(o => !o.disabled && o.value).map(o => ({value: o.value, label: o.label})).slice(0, 100)});
        } else if (type === 'file') {
            mark('file', el, {...details, filled: el.files.length > 0, accept: el.accept, multiple: el.multiple});
        } else if (['checkbox', 'radio'].includes(type)) {
            mark('check', el, {...details, filled: el.checked});
        } else {
            mark('input', el, details);
        }
    }
    for (const el of document.querySelectorAll('button,input[type="submit"],input[type="button"],[role="button"],[role="tab"],summary,[aria-controls],[aria-expanded],[data-toggle],[data-bs-toggle]')) {
        if (el.matches('input:not([type="submit"]):not([type="button"])')) continue;
        mark('button', el, {
            button_type: (el.getAttribute('type') || (el.tagName === 'BUTTON' ? 'submit' : '')).toLowerCase(),
            nav_url: abs(el.getAttribute('data-href') || el.getAttribute('data-url') || el.getAttribute('routerlink')),
            expanded: el.getAttribute('aria-expanded') || '',
            toggle: el.getAttribute('data-toggle') || el.getAttribute('data-bs-toggle') || '',
            controls: el.getAttribute('aria-controls') || '',
        });
    }
    for (const el of document.querySelectorAll('a[href]')) {
        const href = el.getAttribute('href');
        mark('link', el, {nav_url: abs(href), download: el.hasAttribute('download'),
            local_toggle: href === '#' || href === '',
            method: (el.getAttribute('data-method') || 'GET').toUpperCase()});
    }
    const scrolling = document.scrollingElement;
    if (scrolling && scrolling.scrollHeight > scrolling.clientHeight + scrolling.scrollTop + 8) {
        out.push({kind: 'scroll', selector: '', key: `scroll:${Math.round(scrolling.scrollHeight)}`,
            label: 'Scroll to reveal more content'});
    }
    return out;
}
