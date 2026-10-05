// Exercise the actual page's refresh behavior without a browser or network.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const {test} = require('node:test');

class Element {
  constructor(tag='div') {this.tagName=tag;this.children=[];this.value='';this.classList={add(){},remove(){}};}
  set textContent(value) {this.value=String(value);this.children=[];}
  get textContent() {return this.value + this.children.map(c=>c.textContent).join('');}
  get childElementCount() {return this.children.length;}
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.value='';this.children=children;}
  setAttribute() {}
  addEventListener() {}
}

test('same completed task visibly refreshes when control and next action change',()=>{
  const nodes=new Map();
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},
    createElement(tag){return new Element(tag);},createDocumentFragment(){return new Element('fragment');}};
  let script=[...fs.readFileSync(__dirname+'/dashboard.html','utf8').matchAll(/<script>([\s\S]*?)<\/script>/g)][0][1];
  // Disable polling; expose the real renderers while leaving their code intact.
  script=script.replace('  tick();\n  setInterval(tick, POLL_MS);','  globalThis.renderers={renderSummary,applyRunList};');
  assert.ok(script.includes('globalThis.renderers='));
  const context={document,window:{addEventListener(){}},location:{search:'',protocol:'http:'},URLSearchParams};
  vm.createContext(context);vm.runInContext(script,context);
  const row={id:'task-1',title:'测试',status:'completed',stage_label:'用户已接手',control_label:'用户',next_action:'在客户端继续',
    started:'2026-10-05T01:00:00+08:00',finished:'2026-10-05T01:01:00+08:00'};
  context.renderers.renderSummary(row);context.renderers.applyRunList([row]);
  assert.ok(nodes.get('summary').textContent.includes('用户已接手'));
  const returned={...row,stage_label:'已交回 Codex',control_label:'Codex',next_action:'等待明确任务'};
  context.renderers.renderSummary(returned);context.renderers.applyRunList([returned]);
  assert.ok(nodes.get('summary').textContent.includes('已交回 Codex'));
  assert.ok(nodes.get('summary').textContent.includes('等待明确任务'));
  assert.ok(!nodes.get('summary').textContent.includes('用户已接手'));
  assert.ok(nodes.get('runList').textContent.includes('已交回 Codex'));
  assert.ok(nodes.get('summary').textContent.includes('委派执行状态执行结束'));
  const reviewed={...returned,review:'inconclusive',assignment:{policy:'bounded',attempt_number:1,attempt_limit:2,attempts_remaining:1}};
  context.renderers.renderSummary(reviewed);
  assert.ok(nodes.get('summary').textContent.includes('尚无法确认'));
  assert.ok(nodes.get('summary').textContent.includes('任务链剩余 1 轮'));
  context.renderers.renderSummary({...reviewed,assignment:{...reviewed.assignment,attempts_remaining:0}});
  assert.ok(nodes.get('summary').textContent.includes('任务链剩余 0 轮'));
  assert.ok(!nodes.get('summary').textContent.includes('任务链剩余 1 轮'));
  context.renderers.renderSummary({...reviewed,metering:{coverage:'unavailable',note:'测试',cost:{status:'unavailable',note:'非账单'}}});
  assert.ok(nodes.get('summary').textContent.includes('未知：尚无提供方计数'));
  context.renderers.renderSummary({...reviewed,usage:{inputTokens:1000,outputTokens:200,cacheReadTokens:9000},
    metering:{coverage:'reported',partial_fields:[],note:'仅主会话',cost:{status:'estimated',usd_min:.000297,usd_max:.000594,note:'非账单'},
      quota_contribution:{windows:{five_hour:{percent_min:.002475,percent_max:.00495},weekly:{percent_min:0,percent_max:1},monthly:{percent_min:0,percent_max:1}}}}});
  assert.ok(nodes.get('summary').textContent.includes('$0.000297–$0.000594'));
  assert.ok(nodes.get('summary').textContent.includes('非账户剩余'));
  assert.ok(nodes.get('summary').textContent.includes('未缓存输入 token'));
});
