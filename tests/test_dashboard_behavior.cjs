// Dashboard interaction regression checks. Transport is an in-memory fixture:
// this file never starts the Flask app or sends an HTTP request.
// Run from any directory: node tests/test_dashboard_behavior.cjs
const fs = require("fs"), path = require("path"), vm = require("vm"), assert = require("assert");
const html = fs.readFileSync(path.join(__dirname, "..", "dashboard.html"), "utf8");
const code = html.split("<!-- shared:js -->")[1].match(/<script>([\s\S]*?)<\/script>/)[1];
class Node {
  constructor(id = "") { this.id=id; this.textContent=""; this.dataset={}; this.style={}; this.children=[]; this.value=""; this.validity={badInput:false}; this.listeners={}; this.className=""; this.attributes={}; this.classList={toggle(){},remove(){},add(){}}; this.scrollTop=0; this.clientHeight=200; this.scrollHeight=200; }
  set innerHTML(v) { this._html=v; this.children=[]; } get innerHTML(){return this._html||"";}
  addEventListener(type,fn) { (this.listeners[type]??=[]).push(fn); }
  fire(type,event={}) { return Promise.all((this.listeners[type]||[]).map(fn=>fn.call(this,event))); }
  setAttribute(k,v){this.attributes[k]=v;} querySelector(){return new Node();}
  appendChild(n){this.children.push(n);} replaceChildren(n){this.children=n.children;}
  focus(){this.focused=true;} closest(){return null;}
}
const nodes = new Map();
function el(id){if(!nodes.has(id))nodes.set(id,new Node(id));return nodes.get(id);}
for(const id of html.matchAll(/id="([^"]+)"/g))el(id[1]);
el("mode").value="demo";
const document=new Node("document");document.hidden=false;document.documentElement=new Node("html");
document.getElementById=el;document.createElement=()=>new Node();document.createDocumentFragment=()=>new Node();
const window=new Node("window");let currentUrl=new URL("http://fixture.local/?api=http%3A%2F%2Ffixture.local");
const location={get href(){return currentUrl.href;},get search(){return currentUrl.search;}};
let timers=[], clock=0, serial=0;
function schedule(fn,delay){const id=++serial;timers.push({id,fn,delay});return id;}
function clear(id){timers=timers.filter(t=>t.id!==id);}
const cfg={brand:"Fixture Desk",agent_count:0,agents:[],engine:"fixture",engine_reason:"offline checks",confidence_threshold:7,telegram_configured:false,risk:{capital:100000},universe:{total:25}};
function verdict(i){return {symbol:"STOCK"+String(i).padStart(2,"0"),name:"Company "+String(i).padStart(2,"0"),price:100+i,day_change_pct:i/10,tracks:{intraday:{verdict:"WATCH",confidence:i%11,why:"Evidence "+i},positional:{verdict:i%2?"WATCH":"BUY",confidence:i%7,why:"Risk considered",gated:i===2}}};}
let state={status:"done",run_id:1,started_at:"fixture",mode:"demo",agents:[],kpis:{universe:25,in_debate:0,buy_signals:0,intraday_signals:0,positional_signals:0},verdicts:Array.from({length:25},(_,i)=>verdict(i)),log:[]};
let configFailures=1,statusFailure=false,posts=0,postResolve,requestCalls=[],postBody;
async function deskRequest(path,opts){
  requestCalls.push(path);
  if(path==="/config"){if(configFailures-->0)throw Error("Offline fixture");return cfg;}
  if(path==="/status"){if(statusFailure)throw Error("Offline fixture");return structuredClone(state);}
  if(path==="/start"){posts++;postBody=JSON.parse(opts.body);return new Promise(resolve=>postResolve=resolve);}
  throw Error("Unexpected request");
}
const context={console,document,window,location,URL,URLSearchParams,history:{replaceState(_,__,url){currentUrl=new URL(url);}},localStorage:{getItem(){return null},setItem(){}},Date:{now:()=>clock},setTimeout:schedule,clearTimeout:clear,el,deskRequest,
  text:(n,v)=>{if(n)n.textContent=String(v)},esc:v=>String(v??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")};
vm.createContext(context);vm.runInContext(code,context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function pollNow(){const timer=timers.shift();assert(timer,"poll timer expected");await timer.fn();await flush();}
(async()=>{
  await flush();
  assert(el("start").disabled);assert(!el("retry-connection").hidden);assert.equal(timers[0].delay,2000);
  await el("retry-connection").fire("click");await flush();
  assert(!el("start").disabled);assert(el("retry-connection").hidden);assert.equal(el("feed").children.length,12);
  assert(el("feed").children[0].innerHTML.includes("STOCK00"));assert.equal(timers[0].delay,10000);
  await el("load-more").fire("click");assert.equal(el("feed").children.length,24);assert(el("feed").children[12].focused);
  el("verdict-search").value="Company 03";await el("verdict-search").fire("input",{});
  assert.equal(el("feed").children.length,1);assert(el("feed").children[0].innerHTML.includes("STOCK03"));
  assert.equal(currentUrl.searchParams.get("q"),"Company 03");assert.equal(currentUrl.searchParams.get("api"),"http://fixture.local");
  await el("verdict-clear").fire("click");assert.equal(el("feed").children.length,12);assert.equal(currentUrl.searchParams.get("q"),null);
  el("verdict-sort").value="confidence";await el("verdict-sort").fire("change");
  assert(el("feed").children[0].innerHTML.includes("STOCK10"));
  el("buysonly").checked=true;await el("buysonly").fire("change");assert(el("feednote").textContent.startsWith("13 of 25"));
  el("verdict-search").value="No such name";await el("verdict-search").fire("input",{});
  assert(el("feed").innerHTML.includes("No matching verdicts"));assert(el("feed").innerHTML.includes("data-clear-filters"));
  await el("verdict-clear").fire("click");
  const before=el("feed").children;
  await pollNow();assert.strictEqual(el("feed").children,before,"unchanged response should not repaint rows");
  state.verdicts[0].price=777;await pollNow();assert(el("feed").children[0].innerHTML.includes("777.00"));
  statusFailure=true;await pollNow();assert(el("start").disabled);assert.equal(el("feed").children.length,12);
  assert(el("banner").textContent.includes("last received research"));
  statusFailure=false;await el("retry-connection").fire("click");await flush();assert(!el("start").disabled);
  el("capital").value="0";await el("start").fire("click");assert.equal(posts,0);assert.equal(el("capital").attributes["aria-invalid"],"true");
  el("capital").value="Infinity";await el("start").fire("click");assert.equal(posts,0);
  el("capital").value="123000";
  const first=el("start").fire("click");const second=el("start").fire("click");await flush();assert.equal(posts,1);
  assert.equal(postBody.capital,123000);assert.equal(postBody.mode,"demo");
  state.status="running";postResolve({ok:true});await first;await second;await flush();
  assert(el("start").disabled);assert.equal(el("startlabel").textContent,"Analysing…");assert.equal(timers[0].delay,1000);
  document.hidden=true;await document.fire("visibilitychange");assert.equal(timers.length,0);
  const calls=requestCalls.length;await el("retry-connection").fire("click");assert.equal(requestCalls.length,calls);
  document.hidden=false;await document.fire("visibilitychange");await flush();assert.equal(timers.length,1);
  console.log("PASS: config retry, 12-row pagination, newest order, search, URL state, sort, buys filter, empty reset, stable rendering, same-length updates, offline retention/recovery, finite capital, duplicate Start prevention, active/idle intervals and hidden-tab pause. All requests used a local in-memory fixture.");
})().catch(error=>{console.error(error);process.exitCode=1;});
