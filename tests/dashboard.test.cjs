const {readFileSync} = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const code = readFileSync(path.join(__dirname, '../static/i18n.js'), 'utf8') + '\n' + readFileSync(path.join(__dirname, '../static/dashboard.js'), 'utf8');
const BASE = Date.parse('2026-10-06T12:00:00Z');
const stamp = seconds => new Date(BASE + seconds * 1000).toISOString();
const point = (seconds, seq) => ({deviceId:'device', sequenceNumber:seq, enqueuedTime:stamp(seconds),
    values:[1,2,3,1], temperature:25, pressure:1000, origin:'history'});
const record = (seconds, seq) => ({deviceId:'device', sequenceNumber:seq, enqueuedTime:stamp(seconds),
    payload:{temperature:25, pressure:1000, samples:[{x:0,y:2,z:3},{x:2,y:2,z:3}]}});
const flush = () => new Promise(resolve => setImmediate(resolve));

function harness() {
    let clock=0, chart, tick;
    const nodes = new Map(), requests=[], lifecycle={};
    class EventSource {
        static CLOSED=2;
        static instances=[];
        constructor() { this.listeners={}; this.readyState=1; EventSource.instances.push(this); }
        addEventListener(name, fn) { this.listeners[name]=fn; }
        emit(name, data, id='') { this.listeners[name]({data:JSON.stringify(data), lastEventId:id}); }
        close() { this.readyState=2; }
    }
    const node = id => {
        if (!nodes.has(id)) nodes.set(id, {textContent:'', disabled:false, listeners:{},
            classList:{toggle() {}}, getContext() {},
            addEventListener(name, fn) { this.listeners[name]=fn; }});
        return nodes.get(id);
    };
    vm.runInNewContext(code, {console, Date, Math, JSON, AbortController, EventSource,
        performance:{now:() => clock}, document:{getElementById:node},
        window:{addEventListener:(name,fn) => lifecycle[name]=fn},
        Chart:class {constructor(ctx,config) {Object.assign(this,config); chart=this;} update() {}},
        setInterval(fn) {tick=fn;}, setTimeout() {return 1;}, clearTimeout() {},
        fetch(url, options) {return new Promise(resolve => requests.push({url, options, resolve}));}
    });
    const stream=EventSource.instances[0];
    stream.onopen();
    return {node, stream, requests, lifecycle, EventSource,
        chart:() => chart,
        status:(age=0) => stream.emit('status', {serverTime:stamp(clock/1000), dataStatus:age===null?'NO DATA':'RECEIVING DATA',
            consumerStatus:'connected', ageSeconds:age, staleSeconds:30}),
        advance(ms) {clock+=ms; tick();},
        async response(index, points, ok=true, serverTime=stamp(0)) {
            requests[index].resolve({ok,status:ok?200:503,json:async()=>({points,serverTime,tablePointCount:points.length})});
            await flush();
        }
    };
}

(async () => {
    let passed=0;
    async function test(name, fn) {await fn(); passed++; console.log('PASS '+name);}
    await test('history restores four lines without asserting live reception', async () => {
        const h=harness(); h.status(null);
        await h.response(0,[point(-10,1)]);
        assert.equal(h.chart().data.datasets.length,4);
        assert.equal(h.chart().data.datasets[0].data.length,1);
        assert.equal(h.node('status').textContent,'NO DATA');
        assert.equal(h.chart().options.scales.x.max-h.chart().options.scales.x.min,30*60000);
    });
    await test('late history merges with live and overlap appears once', async () => {
        const h=harness(); h.status();
        h.stream.emit('telemetry',{record:record(-5,2)},'a:2');
        await h.response(0,[point(-10,1),point(-5,2)]);
        const data=h.chart().data.datasets;
        assert.equal(data[0].data.length,2);
        assert.equal(data[0].data[1].y,1);
        assert.equal(data[3].data[1].y,1);
        h.stream.emit('telemetry',{record:record(-5,2)},'a:2');
        assert.equal(data[0].data.length,2);
    });
    await test('out-of-order range responses cannot replace new selection', async () => {
        const h=harness(); h.status();
        h.node('historyRange').listeners.change({target:{value:'5'}});
        assert.equal(h.requests[0].options.signal.aborted,true);
        await h.response(1,[point(-10,2)]);
        await h.response(0,[point(-1000,1)]);
        assert.equal(h.chart().data.datasets[0].data.length,1);
        assert.equal(h.chart().options.scales.x.max-h.chart().options.scales.x.min,5*60000);
    });
    await test('storage failure preserves live graph with visible failure', async () => {
        const h=harness(); h.status();
        h.stream.emit('telemetry',{record:record(-1,1)},'a:1');
        await h.response(0,[],false);
        assert.equal(h.chart().data.datasets[0].data.length,1);
        assert.match(h.node('historyNotice').textContent,/불러오지 못/);
    });
    await test('display timer expires status without history polling', async () => {
        const h=harness(); h.status(); await h.response(0,[]);
        h.advance(31000);
        assert.equal(h.node('status').textContent,'데이터 안들어와유');
        assert.equal(h.requests.length,1);
        h.stream.onerror();
        assert.match(h.node('status').textContent,/확인 불가/);
    });
    await test('manual reload and reconnection explicitly refresh history', async () => {
        const h=harness(); h.status(); await h.response(0,[]);
        h.node('reloadHistory').listeners.click();
        assert.equal(h.requests.length,2);
        h.stream.onerror(); h.stream.onopen();
        assert.equal(h.requests.length,3);
        h.stream.emit('reset',{});
        assert.equal(h.requests.length,4);
    });
    await test('browser clock mismatch before status does not discard live point', async () => {
        const h=harness();
        h.stream.emit('telemetry',{record:record(-1,1)},'a:1');
        h.status(); await h.response(0,[]);
        assert.equal(h.chart().data.datasets[0].data.length,1);
    });
    await test('old history response cannot rewind SSE clock', async () => {
        const h=harness(); h.status(); h.advance(5000); h.status();
        await h.response(0,[point(-10,1)],true,stamp(-60));
        assert.equal(h.chart().options.scales.x.max,BASE+5000);
    });
    await test('page hide stops connection; browser back reconnects', async () => {
        const h=harness(); h.lifecycle.pagehide();
        assert.equal(h.stream.readyState,2);
        assert.equal(h.requests[0].options.signal.aborted,true);
        h.lifecycle.pageshow({persisted:true});
        assert.equal(h.EventSource.instances.length,2);
        assert.equal(h.requests.length,2);
    });
    console.log(`${passed} dashboard scenarios passed`);
})().catch(error => {console.error(error); process.exitCode=1;});
