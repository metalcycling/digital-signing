const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
class Element {
  constructor(){this.children=[];this.events={};this.scrollTop=0;}
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  addEventListener(name,callback){this.events[name]=callback;}
}
async function workspace(status,url=null){
  const nodes=new Map();const get=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
  const calls=[],clipboard=[],notices=[];
  const contract={id:'agreement',name:'Test',created_at:'2026-11-23',status,latest_version:2,final_drive_url:url};
  const context=vm.createContext({$:get,document:{createElement:()=>new Element()},selectedContract:null,
    selectContract(){},openDelete(){},clearSelection(){},notify:message=>notices.push(message),
    navigator:{clipboard:{writeText:async text=>clipboard.push(text)}},window:{prompt(){}},
    api:async (url,options)=>{calls.push({url,options});return url==='/api/workspace'?{mode:'google',contracts:[contract]}:{url:'https://drive.google.com/file/d/pdf/view'};}
  });
  const source=fs.readFileSync('digital_signing/static/app.js','utf8');
  vm.runInContext('let mode;'+source.slice(source.indexOf('async function loadContracts()'),source.indexOf('async function loadTemplates()')),context);
  await vm.runInContext('loadContracts()',context);
  const actions=get('contracts').children[0].children[1];
  return {copy:actions.children.find(c=>c.textContent==='Copy link'),calls,clipboard,notices};
}
test('copy link stays disabled until fully executed',async()=>{
  for(const status of ['draft','sent','awaiting_vendor'])assert.equal((await workspace(status)).copy.disabled,true);
});
test('completed agreements copy the saved Drive PDF link',async()=>{
  const v=await workspace('completed','https://drive.google.com/file/d/saved/view');
  assert.equal(v.copy.disabled,false);
  await v.copy.events.click();
  assert.deepEqual(v.clipboard,['https://drive.google.com/file/d/saved/view']);
  assert.equal(v.calls.length,1);
});
test('older completed agreements save once before copying and reuse the link',async()=>{
  const v=await workspace('completed');
  await v.copy.events.click();await v.copy.events.click();
  assert.equal(v.calls.length,2);
  assert.equal(v.calls[1].url,'/api/contracts/agreement/submissions/2/drive');
  assert.equal(v.calls[1].options.method,'POST');
  assert.deepEqual(v.clipboard,['https://drive.google.com/file/d/pdf/view','https://drive.google.com/file/d/pdf/view']);
});
