const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const root = path.join(__dirname, '..');
const html = fs.readFileSync(process.argv[2] || path.join(root, 'templates/index.html'), 'utf8');
const catalogue = html.match(/<script id="laundryTranslations" type="application\/json">([\s\S]*?)<\/script>/)[1];
const context = {window:{}, document:{getElementById:() => ({textContent:catalogue})}};
vm.runInNewContext(fs.readFileSync(path.join(root, 'static/i18n.js'), 'utf8'), context);
const t = context.window.LaundryText;
const en = html.includes('<html lang="en">');
assert.equal(t('작동 중'), en ? 'Running' : '작동 중');
assert.equal(t`자동 잠금까지 ${59}초`, en ? 'Locks in 59s' : '자동 잠금까지 59초');
assert.equal(t('비밀번호가 맞지 않습니다.'), en ? 'Incorrect access code.' : '비밀번호가 맞지 않습니다.');
assert.equal(t`최근 ${30}분 저장 기록을 불러왔습니다.`, en ? 'Loaded records from the last 30 minutes.' : '최근 30분 저장 기록을 불러왔습니다.');
assert.equal(t('RECEIVING DATA'), 'RECEIVING DATA');
// Interpolated recipient text stays literal and is never evaluated or treated as HTML.
assert.equal(t`자동 잠금까지 ${'<script>${evil}</script>'}초`, en ? 'Locks in <script>${evil}</script>s' : '자동 잠금까지 <script>${evil}</script>초');
for (const script of ['i18n','cycle','notifications','dashboard']) assert.ok(html.includes(`/static/${script}.js`));
if (en) assert.ok(!/[가-힣]/.test(html.replace(/<script id="laundryTranslations"[\s\S]*?<\/script>/, '')));
console.log(`PASS ${en ? 'English' : 'Korean'} labels, dynamic text, API errors and interpolation`);

(async () => {
    const node = {textContent:''};
    let data = {phase:'NOT_STARTED',sms_status:'pending',sms_ready:true};
    const run = async () => {
        vm.runInNewContext(fs.readFileSync(path.join(root,'static/cycle.js'),'utf8'), {
            window:context.window, document:{getElementById:()=>node}, Date, AbortSignal,
            fetch:async()=>({ok:true,json:async()=>data}),setTimeout(){}
        });
        await new Promise(resolve=>setImmediate(resolve));
        assert.ok(!node.textContent.includes('t('),node.textContent);
        if(en) assert.ok(!/[가-힣]/.test(node.textContent),node.textContent);
    };
    await run();
    assert.ok(node.textContent.includes(en ? 'Started —' : '시작 기록 —'));
    data = {phase:'RUNNING',sms_status:'pending',sms_ready:false,fresh:false};
    await run();
    assert.ok(node.textContent.includes(en ? 'no recent sensor data' : '최신 센서 데이터 없음'));
    assert.ok(node.textContent.includes(en ? 'SMS configuration required' : '실제 발송 설정 필요'));
    console.log('PASS cycle status renders both optional warnings without untranslated text');
})();
