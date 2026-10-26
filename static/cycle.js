(() => {
    'use strict';
    const t = window.LaundryText;
    const out = document.getElementById('cycleStatus');
    if (!out) return;
    const details = document.getElementById('cycleDetails');
    const warning = document.getElementById('cycleWarning');
    const phases = {OFF:t('알림 꺼짐'),NOT_STARTED:t('설정 전'), ARMED:t('동작 대기'), RUNNING:t('작동 중'), QUIET_CANDIDATE:t('완료 확인 중'), CANCELLED:t('알림 취소됨'), FINISHED:t('완료 감지됨')};
    const sms = {cancelled:t('수동 종료 · 발송 안 함'), missed:t('복구 지연 · 발송 안 함'), pending:t('대기'), disabled:t('발송 꺼짐'), not_configured:t('설정 필요'), claimed:t('발송 요청 중/확인 필요'), accepted:t('서비스 접수 완료'), partial:t('일부 요청 확인 필요'), unknown:t('결과 확인 필요'), skipped:t('발송 생략')};
    const time = value => value ? new Date(value).toLocaleTimeString('en-GB', {timeZone:'Europe/London'}) : '—';
    async function update() {
        try {
            const response = await fetch('/api/cycle', {cache:'no-store', signal:AbortSignal.timeout(10000)});
            const data = await response.json();
            if (!response.ok) throw new Error(t(data.error));
            out.textContent = data.phase === 'FINISHED' && data.auto_repeat ? t('완료 알림 처리됨 · 다음 빨래 대기') : phases[data.phase] || data.phase;
            const alerts = [];
            if (data.phase !== 'OFF' && data.phase !== 'NOT_STARTED' && !['FINISHED', 'CANCELLED'].includes(data.phase) && !data.fresh)
                alerts.push(t('센서 데이터를 기다리고 있어요.'));
            if (data.phase !== 'OFF' && !data.sms_ready) alerts.push(t('문자 발송 설정이 필요해요.'));
            if (data.phase !== 'OFF' && ['partial', 'unknown', 'claimed', 'missed', 'disabled', 'not_configured'].includes(data.sms_status))
                alerts.push(t('문자 발송 상태를 상세 정보에서 확인하세요.'));
            if (data.error) alerts.push(t(data.error));
            if (warning) { warning.textContent = alerts.join(' '); warning.hidden = !alerts.length; }
            if (details) details.textContent = t`알림 설정 ${time(data.started_at)} · 완료 감지 ${time(data.detected_end_at)}\n문자: ${sms[data.sms_status] || data.sms_status}`;
        } catch (error) {
            out.textContent = t('상태 확인 필요');
            if (warning) { warning.textContent = t(error.message) || t('코스 상태를 확인하지 못했습니다.'); warning.hidden = false; }
        }
        setTimeout(update, 5000);
    }
    update();
})();
