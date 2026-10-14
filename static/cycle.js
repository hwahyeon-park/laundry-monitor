(() => {
    'use strict';
    const t = window.LaundryText;
    const out = document.getElementById('cycleStatus');
    if (!out) return;
    const phases = {NOT_STARTED:t('코스 시작을 기록해 주세요'), ARMED:t('동작 확인 중'), RUNNING:t('작동 중'), QUIET_CANDIDATE:t('조용한 구간 확인 중'), CANCELLED:t('수동 종료'), FINISHED:t('완료로 판정')};
    const sms = {cancelled:t('수동 종료 · 발송 안 함'), missed:t('복구 지연 · 발송 안 함'), pending:t('대기'), disabled:t('발송 꺼짐'), not_configured:t('설정 필요'), claimed:t('발송 요청 중/확인 필요'), accepted:t('서비스 접수 완료'), partial:t('일부 요청 확인 필요'), unknown:t('결과 확인 필요'), skipped:t('발송 생략')};
    const time = value => value ? new Date(value).toLocaleTimeString('en-GB', {timeZone:'Europe/London'}) : '—';
    async function update() {
        try {
            const response = await fetch('/api/cycle', {cache:'no-store', signal:AbortSignal.timeout(10000)});
            const data = await response.json();
            if (!response.ok) throw new Error(t(data.error));
            out.textContent = t`${phases[data.phase] || data.phase}${data.phase !== 'NOT_STARTED' && !['FINISHED','CANCELLED'].includes(data.phase) && !data.fresh ? t(' · 최신 센서 데이터 없음') : ''}\n시작 기록 ${time(data.started_at)} · 자동 완료 ${time(data.detected_end_at)} · 실제 종료 기록 ${time(data.observed_end_at)}\nSMS: ${sms[data.sms_status] || data.sms_status}${!data.sms_ready ? t(' · 실제 발송 설정 필요') : ''}${data.error ? '\n'+t(data.error) : ''}`;
        } catch (error) {out.textContent = t(error.message) || t('코스 상태를 확인하지 못했습니다.');}
        setTimeout(update, 5000);
    }
    update();
})();
