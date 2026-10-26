/* Only public recipient IDs/names arrive here, after server authentication. */
window.LaundryNotifications = (() => {
    'use strict';
    const t = window.LaundryText;
    function mount() {
        const root = document.getElementById('notificationPanel');
        if (!root) return;
        const el = id => document.getElementById(id);
        let csrf = '', cycle = null, busy = false, expiresAt = 0;
        const countdown = el('notificationCountdown');
        const login = el('notificationLogin');
        const picker = el('notificationPicker');
        const message = el('notificationMessage');
        const names = el('notificationNames');
        function lock() {
            expiresAt = 0;
            countdown.textContent = '';
            countdown.hidden = true;
            picker.hidden = true;
            login.hidden = false;
            names.replaceChildren();
            cycle = null;
            el('notificationCurrent').textContent = '';
            if (el('cycleDownload')) el('cycleDownload').hidden = true;
        }
        function updateCountdown() {
            if (!expiresAt) return;
            const seconds = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
            if (!seconds) {
                lock();
                message.textContent = t('설정 화면이 잠겼어요. 저장한 알림은 유지돼요.');
                return;
            }
            countdown.hidden = false;
            countdown.textContent = t`${seconds}초 후 잠금`;
        }
        function setBusy(value) {
            busy = value;
            root.querySelectorAll('button').forEach(button => { button.disabled = value; });
            el('notificationPassword').disabled = value;
            const finished = ['FINISHED', 'CANCELLED'].includes(cycle?.detection_phase);
            const settled = ['accepted', 'partial', 'unknown', 'skipped', 'disabled', 'not_configured', 'missed', 'cancelled'].includes(cycle?.status);
            const editable = cycle && (settled || (cycle.status === 'pending' && !finished));
            names.disabled = value || !editable;
            el('notificationSave').disabled = value || !editable;
            el('notificationSave').textContent = cycle?.alerts_enabled ? t('수신자 변경') : t('알림 켜기');
            if (el('cycleCancel')) {
                el('cycleCancel').disabled = value || !cycle || !cycle.alerts_enabled;
            }
        }
        async function api(path, body) {
            const requestStarted = Date.now();
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 20000);
            try {
                const response = await fetch(`/api/notifications/${path}`, {
                    method: body === undefined ? 'GET' : 'POST',
                    credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
                    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
                    body: body === undefined ? undefined : JSON.stringify(body)
                });
                const data = await response.json();
                if (!response.ok) {
                    if ([401, 403].includes(response.status)) { lock(); csrf = ''; }
                    throw new Error(t(data.error) || t('알림 설정을 처리하지 못했습니다.'));
                }
                if (typeof data.expires_in === 'number') data.expiresAt = requestStarted + data.expires_in * 1000;
                return data;
            } catch (error) {
                if (error.name === 'AbortError') throw new Error(t('응답이 지연됩니다. 상태 새로고침으로 저장 여부를 확인하세요.'));
                throw error;
            } finally { clearTimeout(timer); }
        }
        function renderCycle(value) {
            cycle = value;
            const active = value.recipient_names.length > 0;
            el('notificationCurrent').textContent = active && value.recipient_names.length ? t`알림 받을 사람: ${value.recipient_names.join(', ')}` : '';
            el('notificationHint').textContent = value.alerts_enabled ? t('다음 빨래도 같은 수신자에게 자동으로 알려드려요.') : t('알림이 꺼져 있어요. 수신자를 확인하고 알림을 켜세요.');
        }
        async function refresh() {
            const data = await api('session');
            csrf = data.csrf_token;
            if (!data.authenticated) { lock(); return; }
            expiresAt = data.expiresAt;
            if (!expiresAt || expiresAt <= Date.now()) { lock(); return; }
            login.hidden = true;
            picker.hidden = false;
            if (el('cycleDownload')) el('cycleDownload').hidden = false;
            names.replaceChildren();
            const legend = document.createElement('legend');
            legend.textContent = t('알림 받을 사람');
            names.append(legend);
            for (const person of data.recipients) {
                const label = document.createElement('label');
                label.className = 'notification-option';
                const checkbox = document.createElement('input');
                checkbox.type = 'checkbox'; checkbox.name = 'notificationRecipient';
                checkbox.value = person.id;
                checkbox.checked = data.cycle.recipient_ids.includes(person.id);
                const name = document.createElement('span');
                name.textContent = person.name;
                label.append(checkbox, name); names.append(label);
            }
            renderCycle(data.cycle);
            updateCountdown();
        }
        async function run(action) {
            if (busy) return;
            setBusy(true); message.textContent = '';
            try { await action(); }
            catch (error) { message.textContent = t(error.message) || t('연결을 확인하고 다시 시도하세요.'); }
            finally { setBusy(false); }
        }
        login.addEventListener('submit', event => {
            event.preventDefault();
            run(async () => {
                const input = el('notificationPassword');
                const password = input.value; input.value = '';
                // Recover bootstrap CSRF after a cookie expires or settings are enabled.
                if (!csrf) await refresh();
                const data = await api('login', {password});
                csrf = data.csrf_token;
                await refresh();
                names.querySelector('input')?.focus();
            });
        });
        picker.addEventListener('submit', event => {
            event.preventDefault();
            run(async () => {
                const selected = Array.from(names.querySelectorAll('input:checked'), input => input.value);
                if (!selected.length || !cycle) throw new Error(t('한 명 이상 선택하세요.'));
                const data = await api('recipient', {
                    recipient_ids: selected, cycle_id: cycle.cycle_id, revision: cycle.revision
                });
                if (expiresAt > Date.now()) renderCycle(data.cycle);
                message.textContent = t('알림 설정을 저장했어요.');
            });
        });
        el('notificationRefresh').addEventListener('click', () => run(refresh));
        el('notificationLogout').addEventListener('click', () => run(async () => {
            await api('logout', {}); lock(); csrf = ''; await refresh();
        }));
        el('cycleCancel')?.addEventListener('click', () => run(async () => {
            const data = await api('cancel', {cycle_id:cycle.cycle_id});
            if (expiresAt > Date.now()) renderCycle(data.cycle);
            message.textContent = t('알림을 껐어요. 다시 켤 때까지 문자를 보내지 않아요.');
        }));
        el('cycleDownload')?.addEventListener('click', () => run(async () => {
            const data = await api('cycles');
            const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)], {type:'application/json'}));
            const link = document.createElement('a'); link.href=url; link.download='laundry-cycles.json';
            link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
        }));
        // Absolute deadline avoids extending authentication in background tabs.
        setInterval(updateCountdown, 250);
        document.addEventListener('visibilitychange', updateCountdown);
        window.addEventListener('pageshow', updateCountdown);
        run(refresh);
    }
    return {mount};
})();
