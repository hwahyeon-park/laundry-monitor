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
assert.equal(t('RECEIVING DATA'), en ? 'RECEIVING DATA' : '데이터 수신 중');
// Interpolated recipient text stays literal and is never evaluated or treated as HTML.
assert.equal(t`자동 잠금까지 ${'<script>${evil}</script>'}초`, en ? 'Locks in <script>${evil}</script>s' : '자동 잠금까지 <script>${evil}</script>초');
for (const id of ['cycleMode','cycleStart','cycleObservedEnd','cyclePrepare']) assert.ok(!html.includes(`id="${id}"`));
for (const script of ['i18n','cycle','notifications','dashboard']) assert.ok(html.includes(`/static/${script}.js`));
if (en) assert.ok(!/[가-힣]/.test(html.replace(/<script id="laundryTranslations"[\s\S]*?<\/script>/, '')));
console.log(`PASS ${en ? 'English' : 'Korean'} labels, dynamic text, API errors and interpolation`);

(async () => {
    const nodes = Object.fromEntries(['cycleStatus','cycleDetails','cycleWarning'].map(id=>[id,{textContent:'',hidden:false}]));
    const node = nodes.cycleDetails;
    let data = {phase:'NOT_STARTED',sms_status:'pending',sms_ready:true};
    const run = async () => {
        vm.runInNewContext(fs.readFileSync(path.join(root,'static/cycle.js'),'utf8'), {
            window:context.window, document:{getElementById:id=>nodes[id]}, Date, AbortSignal,
            fetch:async()=>({ok:true,json:async()=>data}),setTimeout(){}
        });
        await new Promise(resolve=>setImmediate(resolve));
        assert.ok(!node.textContent.includes('t('),node.textContent);
        if(en) for (const n of Object.values(nodes)) assert.ok(!/[가-힣]/.test(n.textContent),n.textContent);
    };
    await run();
    assert.ok(node.textContent.includes(en ? 'Enabled —' : '알림 설정 —'));
    data = {phase:'RUNNING',sms_status:'pending',sms_ready:false,fresh:false};
    await run();
    assert.equal(nodes.cycleStatus.textContent,en ? 'Running' : '작동 중');
    assert.equal(nodes.cycleWarning.hidden,false);
    assert.ok(nodes.cycleWarning.textContent.includes(en ? 'Waiting for sensor data' : '센서 데이터를 기다리고'));
    assert.ok(nodes.cycleWarning.textContent.includes(en ? 'SMS configuration is required' : '문자 발송 설정이 필요'));
    data = {phase:'RUNNING',sms_status:'pending',sms_ready:true,fresh:true};
    await run();
    assert.equal(nodes.cycleWarning.hidden,true);
    data = {phase:'OFF',sms_status:'disabled',sms_ready:false,fresh:false};
    await run();
    assert.equal(nodes.cycleWarning.hidden,true);
    assert.equal(nodes.cycleStatus.textContent,en ? 'Notifications off' : '알림 꺼짐');
    data = {phase:'FINISHED',sms_status:'accepted',sms_ready:true,auto_repeat:true};
    await run();
    assert.ok(nodes.cycleStatus.textContent.includes(en ? 'next load' : '다음 빨래'));
    console.log('PASS cycle status renders both optional warnings without untranslated text');
})();
