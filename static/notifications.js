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
        const statusLabels = {
            cancelled: t('코스를 수동 종료했습니다. SMS는 발송하지 않았습니다.'),
            missed: t('발송 처리 복구가 늦어 문자를 보내지 않았습니다.'),
            disabled: t('코스는 완료됐지만 SMS 발송은 꺼져 있습니다.'),
            not_configured: t('SMS 서버 설정이 필요합니다.'),
            pending: t('수신자만 저장합니다. 지금 SMS를 보내지 않습니다.'),
            claimed: t('SMS 요청 처리 여부를 확인 중입니다.'),
            accepted: t('SMS 서비스가 요청을 접수했습니다. 수신 완료를 뜻하지는 않습니다.'),
            partial: t('일부 SMS의 처리 결과 확인이 필요합니다. 자동으로 재발송하지 않습니다.'),
            unknown: t('SMS 처리 결과 확인이 필요합니다. 자동으로 재발송하지 않습니다.'),
            skipped: t('이 cycle은 수신자 없이 완료되었습니다.')
        };
        function lock() {
            expiresAt = 0;
            countdown.textContent = '';
            countdown.hidden = true;
            picker.hidden = true;
            login.hidden = false;
            names.replaceChildren();
            cycle = null;
            el('notificationCurrent').textContent = t('비밀번호 입력 후 알림 문자 메시지 받을 사람을 정하세요.');
        }
        function updateCountdown() {
            if (!expiresAt) return;
            const seconds = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
            if (!seconds) {
                lock();
                message.textContent = t('잠금 해제 시간이 끝났습니다. 수신자를 다시 선택하려면  비밀번호를 다시 입력하세요.');
                return;
            }
            countdown.hidden = false;
            countdown.textContent = t`자동 잠금까지 ${seconds}초`;
        }
        function setBusy(value) {
            busy = value;
            root.querySelectorAll('button').forEach(button => { button.disabled = value; });
            el('notificationPassword').disabled = value;
            const finished = ['FINISHED', 'CANCELLED'].includes(cycle?.detection_phase);
            names.disabled = value || finished || (cycle && cycle.status !== 'pending');
            el('notificationSave').disabled = value || !cycle || finished || cycle.status !== 'pending';
            if (el('cycleStart')) {
                el('cycleStart').disabled = value || !cycle || !['NOT_STARTED', undefined].includes(cycle.detection_phase) || !cycle.recipient_ids.length;
                el('cyclePrepare').disabled = value || !finished;
                el('cycleCancel').disabled = value || !cycle || finished || cycle.detection_phase === 'NOT_STARTED';
                el('cycleObservedEnd').disabled = value || !cycle || ['NOT_STARTED', undefined].includes(cycle.detection_phase) || !!cycle.observed_end_at;
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
            el('notificationCurrent').textContent = `Notification recipients: ${value.recipient_names.map(name => t(name)).join(', ') || 'Not selected'}`;
            el('notificationHint').textContent = statusLabels[value.status] || t('상태를 확인하세요.');
        }
        async function refresh() {
            const data = await api('session');
            csrf = data.csrf_token;
            if (!data.authenticated) { lock(); return; }
            expiresAt = data.expiresAt;
            if (!expiresAt || expiresAt <= Date.now()) { lock(); return; }
            login.hidden = true;
            picker.hidden = false;
            names.replaceChildren();
            const legend = document.createElement('legend');
            legend.textContent = t('이번 세탁·건조 알림을 받을 사람 (한 명 이상 선택)');
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
                message.textContent = t('수신자를 저장했습니다. SMS는 발송하지 않았습니다.');
            });
        });
        el('notificationRefresh').addEventListener('click', () => run(refresh));
        el('notificationLogout').addEventListener('click', () => run(async () => {
            await api('logout', {}); lock(); csrf = ''; await refresh();
        }));
        el('cycleStart')?.addEventListener('click', () => run(async () => {
            const data = await api('start', {cycle_id:cycle.cycle_id, revision:cycle.revision, mode:el('cycleMode').value});
            if (expiresAt > Date.now()) renderCycle(data.cycle);
            message.textContent = t('시작 시각을 기록했습니다. 서버가 센서 데이터를 계속 계산합니다.');
        }));
        el('cycleObservedEnd')?.addEventListener('click', () => run(async () => {
            const data = await api('observed-end', {cycle_id:cycle.cycle_id});
            if (expiresAt > Date.now()) renderCycle(data.cycle);
            message.textContent = t('실제 종료를 확인한 시각을 기록했습니다.');
        }));
        el('cycleCancel')?.addEventListener('click', () => run(async () => {
            const data = await api('cancel', {cycle_id:cycle.cycle_id});
            if (expiresAt > Date.now()) renderCycle(data.cycle);
            message.textContent = t('코스를 수동 종료했습니다. 이 코스의 SMS는 보내지 않습니다.');
        }));
        el('cyclePrepare')?.addEventListener('click', () => run(async () => {
            await api('prepare', {cycle_id:cycle.cycle_id}); await refresh();
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
