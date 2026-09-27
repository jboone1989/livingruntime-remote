const vm = require('node:vm');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const widgets = JSON.parse(fs.readFileSync(0,'utf8'));
const flush = async () => {for(let i=0;i<120;i++) await Promise.resolve();};
function boot(html, kind, mode='ok', saved=null, ledger={state:'pending'}) {
  const events={}, nodes={status:{},detail:{}}, timers=new Map();
  let nextTimer=0,now=0;
  const state={messages:0, reads:0, fallback:0, saved, nodes, timers, ledger};
  const parent={postMessage(message){
    if(message.id===undefined)return;
    if(mode==='drop-context' && message.method==='ui/update-model-context')return;
    if(mode==='drop-wait' && message.params?.name?.startsWith('wait_'))return;
    const reply={jsonrpc:'2.0',id:message.id,result:{}};
    if(message.method==='tools/call'){
      const name=message.params.name;
      if(name.startsWith('wait_')){
        state.reads++;
        if(mode==='drop-context' && state.reads===1) reply.result={structuredContent:{timedOut:true,job:{status:'RUNNING'}}};
        else reply.result={structuredContent:{terminal:true,job:{job_id:'job',status:'SUCCEEDED',terminal:true},state:{status:'SUCCEEDED'},completionDelivery:{event_id:'event',state:ledger.state}}};
      } else if(name==='claim_job_completion'){
        const explicit=message.params.arguments.retry_uncertain;
        if(ledger.state==='pending' || (ledger.state==='uncertain'&&explicit)){
          ledger.state='sending'; reply.result={structuredContent:{state:'sending',claim_token:'token'}};
        }else reply.result={structuredContent:{state:ledger.state}};
      } else if(name==='settle_job_completion'){
        if(mode==='drop-settle')return;
        ledger.state=message.params.arguments.outcome;
        reply.result={structuredContent:{state:ledger.state}};
      }
    }
    if(message.method==='ui/message'){
      state.messages++;
      if(mode==='drop-message')return;
      if(mode==='reject-message')reply.result={isError:true};
    }
    queueMicrotask(()=>{for(const fn of events.message||[])fn({source:parent,data:reply});});
  }};
  const window={parent,addEventListener:(name,fn)=>(events[name]||=[]).push(fn),openai:{
    widgetState:saved,toolOutput:kind==='long'?{runtimeJobId:'job'}:{jobId:'job'},
    setWidgetState(value){state.saved=value;window.openai.widgetState=value;},
    async sendFollowUpMessage(){state.fallback++;throw Error('fallback rejected');}
  }};
  const context={window,document:{getElementById:id=>nodes[id],createElement:()=>({}),querySelector:()=>({appendChild:node=>{nodes[node.id]=node;}})},
    setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,at:now+ms});return id;},clearTimeout:id=>timers.delete(id)};
  vm.runInNewContext(html.split('<script>')[1].split('</script>')[0],context);
  state.advance=async ms=>{now+=ms;const due=[...timers.entries()].filter(([id,t])=>t.at<=now);for(const [id,t]of due){if(timers.delete(id))t.fn();}await flush();};
  state.hide=()=>{for(const fn of events.pagehide||[])fn({});};
  return state;
}
(async()=>{
  for(const [kind,html]of Object.entries(widgets)){
    const normal=boot(html,kind);await flush();
    assert.equal(normal.nodes.status.textContent,'COMPLETED');assert.equal(normal.messages,1);assert.equal(normal.ledger.state,'accepted');
    const restored=boot(html,kind,'ok',normal.saved,normal.ledger);await flush();assert.equal(restored.messages,0);
    const lost=boot(html,kind,'drop-message');await flush();await lost.advance(10001);
    assert.equal(lost.nodes.status.textContent,'DELIVERY_UNCERTAIN');assert.equal(lost.fallback,0);assert.equal(lost.ledger.state,'uncertain');
    const reloadUnknown=boot(html,kind,'ok',lost.saved,lost.ledger);await flush();assert.equal(reloadUnknown.messages,0);
    reloadUnknown.nodes.recover.onclick();await flush();assert.equal(reloadUnknown.messages,1);assert.equal(reloadUnknown.ledger.state,'accepted');
    const rejected=boot(html,kind,'reject-message');await flush();assert.equal(rejected.ledger.state,'pending');assert.notEqual(rejected.saved.watcherState,'COMPLETED');
    const reloadRejected=boot(html,kind,'ok',rejected.saved,rejected.ledger);await flush();assert.equal(reloadRejected.messages,1);
    const ackLost=boot(html,kind,'drop-settle');await flush();await ackLost.advance(25001);
    assert.equal(ackLost.saved.notificationAccepted,true);
    const reloadAck=boot(html,kind,'ok',ackLost.saved,ackLost.ledger);await flush();assert.equal(reloadAck.messages,0);assert.equal(reloadAck.ledger.state,'accepted');
    const progress=boot(html,kind,'drop-context');await flush();assert.equal(progress.reads,2);assert.equal(progress.nodes.status.textContent,'COMPLETED');
    const dead=boot(html,kind,'drop-wait');await flush();await dead.advance(75001);assert.equal(dead.nodes.status.textContent,'DISCONNECTED');assert.ok(dead.nodes.recover);
    const hidden=boot(html,kind,'drop-wait');await flush();hidden.hide();await flush();assert.equal(hidden.timers.size,0);
  }
  console.log('PASS: both deployed watcher scripts handle acceptance, reload, rejection, uncertainty, ACK loss, context loss, wait timeout and teardown');
})().catch(error=>{console.error(error);process.exitCode=1;});
