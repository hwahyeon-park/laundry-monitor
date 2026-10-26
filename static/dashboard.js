// Notification setup is independent of chart/telemetry initialization.
window.LaundryNotifications?.mount();
(() => {
    'use strict';
    const t = window.LaundryText;
    let chart;
    let source;
    let status;
    let statusAt = 0;
    let signalAt = performance.now();
    let lastId = null;
    let retryTimer;
    let paused = false;
    let transportReady = false;
    let hadConnection = false;
    let minutes = 30;
    let historyRequest = 0;
    let historyController;
    let serverClock = Date.now();
    let serverClockAt = performance.now();
    let clockKnown = false;
    let lastRenderAt = 0;
    const points = new Map();
    const element = id => document.getElementById(id);
    const consumerLabels = {
        not_started: 'Starting…', not_configured: 'Azure settings required',
        disabled: 'Disabled', connecting: 'Connecting…', connected: 'Connected',
        retrying: 'Reconnecting…', stopped: 'Stopped'
    };
    const currentTime = () => serverClock + performance.now() - serverClockAt;

    function syncClock(value) {
        const time = Date.parse(value);
        if (Number.isFinite(time)) {
            // 늦게 끝난 기록 응답이 SSE로 맞춘 시계를 과거로 돌리지 않도록 한다.
            serverClock = clockKnown ? Math.max(time, currentTime()) : time;
            serverClockAt = performance.now();
            clockKnown = true;
        }
    }

    function updateStatus() {
        const badge = element('status');
        let text = status?.dataStatus || 'NO DATA';
        if (!transportReady) {
            text = t('연결 복구 중 — 수신 상태 확인 불가');
        } else if (!status) {
            text = t('수신 상태 확인 중');
        } else if (status.ageSeconds != null &&
            status.ageSeconds + (performance.now() - statusAt) / 1000 >= status.staleSeconds) {
            text = t('데이터 안들어와유');
        }
        badge.textContent = t(text);
        badge.classList.toggle('active', text === 'RECEIVING DATA');
        badge.classList.toggle('inactive', text !== 'RECEIVING DATA');
    }

    function livePoint(record) {
        const payload = record.payload;
        const samples = payload.samples;
        if (!Array.isArray(samples) || !samples.length) throw new Error('Empty batch');
        if (!samples.every(s => ['x', 'y', 'z'].every(axis => Number.isFinite(s[axis])))) {
            throw new Error('Invalid acceleration');
        }
        const means = ['x', 'y', 'z'].map(axis =>
            samples.reduce((sum, sample) => sum + sample[axis], 0) / samples.length);
        const rms = Math.sqrt(samples.reduce((sum, sample) => sum +
            (sample.x - means[0]) ** 2 + (sample.y - means[1]) ** 2 +
            (sample.z - means[2]) ** 2, 0) / samples.length);
        return {
            deviceId: record.deviceId, sequenceNumber: record.sequenceNumber,
            enqueuedTime: record.enqueuedTime, values: [...means, rms],
            temperature: payload.temperature, pressure: payload.pressure, origin: 'live'
        };
    }

    function addPoint(point) {
        const time = Date.parse(point.enqueuedTime);
        if (!Number.isFinite(time) || typeof point.deviceId !== 'string' ||
            !/^\d+$/.test(String(point.sequenceNumber)) ||
            !Array.isArray(point.values) || point.values.length !== 4 ||
            !point.values.every(Number.isFinite) || point.values[3] < 0 ||
            !Number.isFinite(point.temperature) || !Number.isFinite(point.pressure)) return;
        // 저장 기록과 SSE가 동일 이벤트를 가리키면 한 점으로 합친다.
        const key = [point.deviceId, time, String(point.sequenceNumber)].join('|');
        const previous = points.get(key);
        if (previous?.origin === 'live' && point.origin !== 'live') return;
        points.set(key, { ...point, time });
    }

    function render() {
        const end = currentTime();
        const start = end - minutes * 60000;
        for (const [key, point] of points) {
            if (clockKnown && point.time < start) points.delete(key);
        }
        const sorted = [...points.entries()].sort((a, b) => a[1].time - b[1].time);
        // 응답/메모리 크기는 제한. 정상 5초 주기의 1시간 기록은 약 720점.
        for (const [key] of sorted.slice(0, Math.max(0, sorted.length - 2000))) points.delete(key);
        const visible = sorted.slice(-2000).map(([, point]) => point);
        const latest = visible[visible.length - 1];
        element('lastUpdated').textContent = latest ? new Date(latest.time).toLocaleString('en-GB') : '--';
        element('temperature').textContent = latest ? latest.temperature.toFixed(2) + ' °C' : '--';
        element('pressure').textContent = latest ? latest.pressure.toFixed(2) + ' hPa' : '--';
        const histories = [0, 1, 2, 3].map(index => visible.map(point => ({ x: point.time, y: point.values[index] })));
        if (!chart) {
            const labels = [t('X 축 평균'), t('Y 축 평균'), t('Z 축 평균'), t('진동 세기 [Vibration RMS]')];
            const colors = ['#36a2eb', '#ff6384', '#ff9f40', '#7c3aed'];
            chart = new Chart(element('accelerationChart').getContext('2d'), {
                type: 'line',
                data: { datasets: labels.map((label, index) => ({
                    label, data: histories[index],
                    yAxisID: index === 3 ? 'vibration' : 'acceleration',
                    borderColor: colors[index], backgroundColor: colors[index],
                    borderWidth: index === 3 ? 2.5 : 1.5,
                    pointRadius: 2, tension: 0, spanGaps: 15000
                })) },
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    interaction: { mode: 'index', intersect: false },
                    scales: {
                        x: {
                            type: 'linear', min: start, max: end,
                            title: { display: true, text: t('수신 시각') },
                            ticks: { maxTicksLimit: 8, callback: value => new Date(value).toLocaleTimeString('en-GB', {
                                hour: '2-digit', minute: '2-digit', second: '2-digit'
                            }) }
                        },
                        acceleration: { type: 'linear', position: 'left', beginAtZero: true,
                            title: { display: true, text: t('평균 가속도 (g)') } },
                        vibration: { type: 'linear', position: 'right', beginAtZero: true,
                            grid: { drawOnChartArea: false }, title: { display: true, text: t('진동 세기 (g)') } }
                    },
                    plugins: { legend: { labels: { boxWidth: 12, boxHeight: 12 } }, tooltip: { callbacks: {
                        title: items => items.length ? new Date(items[0].parsed.x).toLocaleString('en-GB') : '',
                        label: item => item.dataset.label + ': ' + item.parsed.y.toFixed(4) + ' g'
                    } } }
                }
            });
        }
        histories.forEach((history, index) => { chart.data.datasets[index].data = history; });
        chart.options.scales.x.min = start;
        chart.options.scales.x.max = end;
        chart.update('none');
        lastRenderAt = performance.now();
    }

    async function loadHistory() {
        if (paused) return;
        const requestId = ++historyRequest;
        historyController?.abort();
        const controller = new AbortController();
        historyController = controller;
        const selectedMinutes = minutes;
        const timeout = setTimeout(() => controller.abort(), 30000);
        element('historyNotice').textContent = t('기록을 불러오고 있어요…');
        element('reloadHistory').disabled = true;
        try {
            const response = await fetch(`/api/history?minutes=${selectedMinutes}`, {
                signal: controller.signal, cache: 'no-store'
            });
            const data = await response.json();
            if (requestId !== historyRequest || paused) return;
            if (!Array.isArray(data.points)) throw new Error('Invalid history response');
            syncClock(data.serverTime);
            data.points.forEach(addPoint);
            // 기존 SSE 점을 지우지 않고 병합: 늦은 응답이 최신 데이터를 덮지 않음.
            render();
            if (!response.ok) {
                element('historyNotice').textContent = response.status === 422
                    ? t('기록이 많아요. 확인할 시간을 조금 줄여 주세요.')
                    : t('기록을 불러오지 못했어요. ‘기록 불러오기’을 눌러 다시 시도해 주세요.');
            } else {
                element('historyNotice').textContent = data.tablePointCount
                    ? t`최근 ${selectedMinutes}분의 기록이에요.`
                    : t('아직 이 시간대의 기록이 없어요. 새 데이터가 들어오면 그래프에 표시돼요.');
                if (data.skippedRows) element('historyNotice').textContent += t` 읽을 수 없는 기록 ${data.skippedRows}개는 제외했습니다.`;
            }
        } catch (error) {
            if (requestId !== historyRequest || paused) return;
            element('historyNotice').textContent = t('기록을 불러오지 못했어요. ‘기록 불러오기’을 눌러 다시 시도해 주세요.');
        } finally {
            clearTimeout(timeout);
            if (requestId === historyRequest) element('reloadHistory').disabled = false;
        }
    }

    function connect() {
        if (paused) return;
        clearTimeout(retryTimer);
        source?.close();
        transportReady = false;
        status = undefined;
        source = new EventSource('/api/stream');
        signalAt = performance.now();
        element('connectionStatus').textContent = t('Connecting…');
        updateStatus();
        source.onopen = () => {
            signalAt = performance.now();
            transportReady = true;
            element('connectionStatus').textContent = t('Connected');
            if (hadConnection) loadHistory();
            hadConnection = true;
            updateStatus();
        };
        source.addEventListener('telemetry', event => {
            signalAt = performance.now();
            if (event.lastEventId && event.lastEventId === lastId) return;
            try {
                addPoint(livePoint(JSON.parse(event.data).record));
                lastId = event.lastEventId;
                render();
            } catch (error) {
                element('replayNotice').textContent = t('이 데이터 묶음을 표시하지 못했습니다.');
            }
        });
        source.addEventListener('status', event => {
            signalAt = performance.now();
            try {
                status = JSON.parse(event.data);
                statusAt = performance.now();
                syncClock(status.serverTime);
                element('consumerStatus').textContent = t(consumerLabels[status.consumerStatus] || 'Unknown');
                updateStatus();
            } catch (error) {
                element('replayNotice').textContent = t('수신 상태를 확인하지 못했습니다.');
            }
        });
        source.addEventListener('reset', () => {
            lastId = null;
            element('replayNotice').textContent = t('연결을 복구했습니다. 누락 구간의 저장 기록을 다시 확인합니다.');
            loadHistory();
        });
        source.onerror = () => {
            transportReady = false;
            status = undefined;
            element('connectionStatus').textContent = t('Reconnecting…');
            updateStatus();
            if (source.readyState === EventSource.CLOSED) {
                retryTimer = setTimeout(connect, 10000 + Math.random() * 3000);
            }
        };
    }

    element('historyRange').addEventListener('change', event => {
        minutes = Number(event.target.value);
        render();
        loadHistory();
    });
    element('reloadHistory').addEventListener('click', loadHistory);
    // 이 타이머는 표시 시각/상태만 갱신. 주기적 기록 조회는 하지 않는다.
    setInterval(() => {
        if (paused) return;
        updateStatus();
        if (performance.now() - lastRenderAt >= 5000) render();
        if (performance.now() - signalAt > 35000) connect();
    }, 1000);
    window.addEventListener('pagehide', () => {
        paused = true;
        clearTimeout(retryTimer);
        historyController?.abort();
        source?.close();
    });
    window.addEventListener('pageshow', event => {
        if (event.persisted) { paused = false; connect(); loadHistory(); }
    });
    // 실시간 연결부터 연 다음 기록 조회. 조회 중 도착한 데이터도 보존한다.
    connect();
    render();
    loadHistory();
})();
