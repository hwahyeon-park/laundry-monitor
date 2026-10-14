"""실행 중인 로컬 웹의 과거 조회를 한 번씩 검사. Azure 설정을 변경하지 않음."""
import json
import sys
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

base = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://localhost:5001'
failures = []
for minutes in (5, 30, 60):
    try:
        with urlopen(f'{base}/api/history?minutes={minutes}', timeout=45) as response:
            data = json.load(response)
        points = data['points']
        times = [datetime.fromisoformat(p['enqueuedTime'].replace('Z', '+00:00')) for p in points]
        keys = [(p['deviceId'], str(p['sequenceNumber']), t) for p, t in zip(points, times)]
        assert times == sorted(times), '시간순 정렬 오류'
        assert len(set(keys)) == len(keys), '중복 이벤트'
        assert all(len(p['values']) == 4 for p in points), '그래프 값 개수 오류'
        assert data['error'] is None, data['error']
        print(f"최근 {minutes}분: Table {data['tablePointCount']}개, 병합 후 {len(points)}개, 제외 {data['skippedRows']}개")
        if points:
            latest = points[-1]
            print('  마지막:', latest['enqueuedTime'], 'XYZ/RMS:', latest['values'])
        if not data['tablePointCount']:
            print('  저장 행 없음: 이 기간에 보드가 꺼져 있었는지, writer 저장 검증 결과를 확인하세요.')
    except HTTPError as error:
        failures.append(f'{minutes}분 HTTP {error.code}: {error.read().decode()[:400]}')
    except (URLError, TimeoutError, KeyError, ValueError, AssertionError) as error:
        failures.append(f'{minutes}분: {error}')
try:
    with urlopen(base+'/api/status', timeout=10) as response:
        status = json.load(response)
    print('실시간 상태:', status.get('dataStatus'), '/ 수신기:', status.get('consumerStatus'))
except (URLError, TimeoutError, ValueError) as error:
    failures.append('상태 조회: '+str(error))
if failures:
    print('\n'.join(failures), file=sys.stderr)
    raise SystemExit(1)
print('조회 검사 완료. Table 개수가 0이면 실제 저장 데이터의 복원까지 검증된 것은 아닙니다.')
