const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = readFileSync(path.join(__dirname, '../static/i18n.js'), 'utf8') + '\n' + readFileSync(path.join(__dirname, '../static/notifications.js'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness(withCycle=false) {
    const requests = [], nodes = new Map();
    let now = 100000, tick;
    class Node {
        constructor(tag='div') { this.tag=tag; this.children=[]; this.listeners={}; this.value=''; this.hidden=false; }
        append(...nodes) { this.children.push(...nodes); }
        replaceChildren(...nodes) { this.children=nodes; }
        addEventListener(event, callback) { this.listeners[event]=callback; }
        focus() { this.focused=true; }
        all() { return this.children.flatMap(n => [n,...n.all()]); }
        querySelectorAll(selector) { return this.all().filter(n => n.tag===selector || (selector==='input:checked' && n.tag==='input' && n.checked)); }
        querySelector(selector) { return this.all().find(n => n.tag==='input' && (selector!=='input:checked' || n.checked)); }
        fire(event) { this.listeners[event]({preventDefault() {}}); }
    }
    for (const id of ['notificationPanel','notificationLogin','notificationPicker','notificationMessage','notificationNames',
        'notificationCountdown','notificationPassword','notificationCurrent','notificationHint','notificationSave','notificationRefresh','notificationLogout']) {
        nodes.set(id, new Node(id.match(/Save|Refresh|Logout/) ? 'button' : 'div'));
    }
    if (withCycle) for (const id of ['cycleStart','cyclePrepare','cycleObservedEnd','cycleDownload','cycleCancel','cycleMode']) nodes.set(id,new Node('button'));
    const node=id=>nodes.get(id);
    node('notificationPanel').append(...[...nodes].filter(([id])=>id!=='notificationPanel').map(([,n])=>n));
    const window={addEventListener(){}};
    vm.runInNewContext(code, {window, document:{getElementById:node,createElement:tag=>new Node(tag),addEventListener(){}},
        Date:{now:()=>now}, setInterval(fn){tick=fn;}, AbortController, setTimeout:()=>1, clearTimeout(){},
        fetch(url, options) {return new Promise(resolve=>requests.push({url,options,resolve}));}});
    window.LaundryNotifications.mount();
    return {node, requests, advance(ms){now+=ms;tick();},
        async response(index,data,status=200) {requests[index].resolve({ok:status<400,status,json:async()=>data});await flush();}};
}
const cycle={cycle_id:'one',revision:0,recipient_ids:[],recipient_names:[],status:'pending'};
const publicData={authenticated:true,expires_in:60,csrf_token:'signed-csrf',cycle,recipients:[
    {id:'wally',name:'Wally'},{id:'a',name:'<b>Flatmate A</b>'},{id:'b',name:'Flatmate B'}]};
(async()=>{
    const h=harness();
    await h.response(0,{authenticated:false,csrf_token:'initial'});
    assert.equal(h.node('notificationPicker').hidden,true);
    assert.equal(h.node('notificationNames').children.length,0);
    h.node('notificationPassword').value='0123';
    h.node('notificationLogin').fire('submit');
    assert.equal(h.requests[1].url,'/api/notifications/login');
    assert.equal(h.node('notificationPassword').value,'');
    await h.response(1,{authenticated:true,csrf_token:'signed-csrf'});
    await h.response(2,publicData);
    assert.equal(h.node('notificationCountdown').textContent,'자동 잠금까지 60초');
    h.advance(1000);
    assert.equal(h.node('notificationCountdown').textContent,'자동 잠금까지 59초');
    const checkboxes=h.node('notificationNames').querySelectorAll('input');
    assert.equal(checkboxes.length,3);
    assert.ok(checkboxes.every(r=>r.name==='notificationRecipient'&&r.type==='checkbox'&&!r.required));
    const labels=h.node('notificationNames').querySelectorAll('span');
    assert.equal(labels[1].textContent,'<b>Flatmate A</b>');
    assert.equal(labels[1].children.length,0);
    h.node('notificationPicker').fire('submit');
    await flush();
    assert.equal(h.requests.length,3);
    assert.match(h.node('notificationMessage').textContent,/한 명 이상/);
    checkboxes[0].checked=true;
    checkboxes[2].checked=true;
    h.node('notificationPicker').fire('submit');
    assert.equal(h.requests[3].url,'/api/notifications/recipient');
    assert.deepEqual(JSON.parse(h.requests[3].options.body),{recipient_ids:['wally','b'],cycle_id:'one',revision:0});
    assert.equal(h.requests[3].options.headers['X-CSRF-Token'],'signed-csrf');
    await h.response(3,{cycle:{...cycle,revision:1,recipient_ids:['wally','b'],recipient_names:['Wally','Flatmate B']}});
    assert.equal(h.node('notificationCurrent').textContent,'Notification recipients: Wally, Flatmate B');
    assert.match(h.node('notificationMessage').textContent,/SMS는 발송하지/);
    h.node('notificationPicker').fire('submit');
    await h.response(4,{error:'비밀번호를 다시 입력하세요.'},401);
    assert.equal(h.node('notificationPicker').hidden,true);
    assert.equal(h.node('notificationNames').children.length,0);
    assert.ok(h.requests.every(r=>!r.url.includes('sms')));
    const exp=harness();
    await exp.response(0,publicData);
    exp.advance(60000);
    assert.equal(exp.node('notificationPicker').hidden,true);
    assert.equal(exp.node('notificationLogin').hidden,false);
    assert.equal(exp.node('notificationNames').children.length,0);
    assert.equal(exp.node('notificationCountdown').hidden,true);
    assert.match(exp.node('notificationMessage').textContent,/저장한 수신자는 유지/);
    assert.equal(exp.requests.length,1);
    const c=harness(true);
    const selectedCycle={...cycle,recipient_ids:['wally'],recipient_names:['Wally'],revision:1,detection_phase:'NOT_STARTED'};
    await c.response(0,{...publicData,cycle:selectedCycle});
    assert.equal(c.node('cycleStart').disabled,false);
    c.node('cycleMode').value='dry'; c.node('cycleStart').fire('click');
    assert.deepEqual(JSON.parse(c.requests[1].options.body),{cycle_id:'one',revision:1,mode:'dry'});
    await c.response(1,{cycle:{...selectedCycle,revision:2,detection_phase:'ARMED'}});
    assert.equal(c.node('cycleStart').disabled,true);
    c.node('cycleObservedEnd').fire('click');
    assert.equal(c.requests[2].url,'/api/notifications/observed-end');
    await c.response(2,{cycle:{...selectedCycle,revision:2,detection_phase:'ARMED',observed_end_at:'2026-10-07T11:00:00Z'}});
    assert.equal(c.node('cycleObservedEnd').disabled,true);
    c.node('cycleCancel').fire('click');
    await c.response(3,{cycle:{...selectedCycle,status:'cancelled',revision:3,detection_phase:'CANCELLED'}});
    assert.equal(c.node('cyclePrepare').disabled,false);
    console.log('PASS notification login, three checkboxes, safe text rendering, save-only payload, expiry');
})().catch(error=>{console.error(error);process.exitCode=1;});
